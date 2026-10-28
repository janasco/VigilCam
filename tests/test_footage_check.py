from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _stamp(dt: datetime, suffix: str = "_main.mp4") -> str:
    return dt.strftime("%Y%m%d_%H%M%S") + suffix


def _seed(root: Path, base: datetime):
    cam = root / "cam1"
    cam.mkdir(parents=True, exist_ok=True)
    (cam / _stamp(base)).write_bytes(b"x" * 16)
    (cam / _stamp(base + timedelta(seconds=600))).write_bytes(b"x" * 16)
    return cam


def test_footage_check_imports():
    import app.services.footage_check as svc  # noqa: F401
    import app.api.footage_check as api  # noqa: F401

    assert hasattr(api, "router")
    assert hasattr(svc, "FootageChecker")
    assert hasattr(svc, "check_footage")
    assert hasattr(svc, "has_footage")
    assert hasattr(svc, "parse_segment_start")
    assert hasattr(svc, "get_status")


def test_point_hit_and_miss(tmp_path):
    from app.services import footage_check as svc

    root = tmp_path / "recordings"
    base = datetime(2024, 1, 1, 12, 0, 0)
    _seed(root, base)

    hit = svc.check_footage(camera="cam1", at=base + timedelta(seconds=60), root=root)
    assert hit["available"] is True
    assert hit["has_footage"] is True
    assert hit["segment_file"] is not None
    assert hit["reason"] == "covered"

    # Second segment starts at +600s; +300s falls in the gap.
    miss = svc.check_footage(camera="cam1", at=base + timedelta(seconds=400), root=root)
    assert miss["available"] is False
    assert miss["reason"] == "gap"
    assert miss["segment_file"] is None

    assert svc.has_footage(camera="cam1", at=base + timedelta(seconds=60), root=root) is True
    assert svc.has_footage(camera="cam1", at=base + timedelta(seconds=400), root=root) is False


def test_unknown_camera_and_bounds(tmp_path):
    from app.services import footage_check as svc

    root = tmp_path / "rec2"
    base = datetime(2024, 1, 1, 12, 0, 0)
    _seed(root, base)

    unknown = svc.check_footage(camera="nope", at=base, root=root)
    assert unknown["available"] is False
    assert unknown["reason"] in ("unknown_camera", "no_segments")

    before = svc.check_footage(camera="cam1", at=base - timedelta(hours=1), root=root)
    assert before["available"] is False
    assert before["reason"] == "before_coverage"

    after = svc.check_footage(camera="cam1", at=base + timedelta(hours=5), root=root)
    assert after["available"] is False
    assert after["reason"] == "after_coverage"

    assert svc.check_footage(camera=None, at=base, root=root)["reason"] == "missing_camera"
    assert svc.check_footage(camera="cam1", at=None, root=root)["reason"] == "missing_timestamp"


def test_explicit_intervals_no_filesystem():
    from app.services import footage_check as svc

    base = datetime(2024, 1, 1, 12, 0, 0)
    per_camera = {
        "cam1": [{"start": base, "end": base + timedelta(seconds=300)}],
    }
    hit = svc.check_footage(camera="cam1", at=base + timedelta(seconds=10),
                            per_camera_segments=per_camera, root="/nonexistent-root-xyz")
    assert hit["available"] is True

    miss = svc.check_footage(camera="cam1", at=base + timedelta(seconds=500),
                             per_camera_segments=per_camera, root="/nonexistent-root-xyz")
    assert miss["available"] is False

    det = svc.FootageChecker(root="/nonexistent-root-xyz")
    assert det.has_footage(camera="cam1", at=base + timedelta(seconds=10),
                           per_camera_segments=per_camera) is True
    assert svc.FootageAvailabilityChecker is svc.FootageChecker
    st = det.get_status(camera="cam1", at=base, per_camera_segments=per_camera)
    assert st["available"] is True
    assert svc.get_status(camera="cam1", at=base,
                           per_camera_segments=per_camera)["available"] is True


def _mounted_client() -> TestClient:
    from app.api.footage_check import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_footage_check_api(tmp_path):
    client = _mounted_client()
    root = tmp_path / "api-rec"
    base = datetime(2024, 5, 1, 8, 0, 0)
    _seed(root, base)

    r = client.get("/api/footage_check/", params={"camera": "cam1", "at": base.isoformat(),
                                                  "path": str(root)})
    assert r.status_code == 200, r.text
    assert r.json()["available"] is True

    r2 = client.post("/api/footage/check", json={"camera": "cam1",
                                                 "at": (base + timedelta(seconds=400)).isoformat(),
                                                 "path": str(root)})
    assert r2.status_code == 200, r2.text
    assert r2.json()["available"] is False

    r3 = client.post("/api/footage-check/check", json={"camera_name": "cam1",
                                                       "timestamp": base.isoformat(),
                                                       "storage_root": str(root)})
    assert r3.status_code == 200, r3.text

    r4 = client.get("/api/cameras/cam1/footage", params={"at": base.isoformat(),
                                                         "path": str(root)})
    assert r4.status_code == 200, r4.text
    assert r4.json()["available"] is True


def test_footage_check_router_prefix():
    from app.api.footage_check import router

    assert any("/api/footage_check" in getattr(r, "path", "") for r in router.routes)
    assert any("/api/footage" in getattr(r, "path", "") for r in router.routes)


def test_footage_check_on_full_app():
    from app.main import app

    client = TestClient(app)
    paths = list(app.openapi()["paths"].keys())
    assert any("footage" in p for p in paths)
    r = client.get("/api/footage_check/status")
    assert r.status_code == 200, r.text
