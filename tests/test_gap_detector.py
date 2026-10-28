from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _stamp(dt: datetime, suffix: str = "_main.mp4") -> str:
    return dt.strftime("%Y%m%d_%H%M%S") + suffix


def test_gap_detector_imports():
    import app.services.gap_detector as svc  # noqa: F401
    import app.api.gap_detector as api  # noqa: F401

    assert hasattr(api, "router")
    assert hasattr(svc, "GapDetector")
    assert hasattr(svc, "detect_gaps")
    assert hasattr(svc, "find_gaps_for_intervals")
    assert hasattr(svc, "merge_intervals")
    assert hasattr(svc, "parse_segment_start")
    assert hasattr(svc, "get_status")


def test_parse_segment_start():
    from app.services import gap_detector as svc

    dt = svc.parse_segment_start("20240101_120000_main.mp4")
    assert dt == datetime(2024, 1, 1, 12, 0, 0)
    assert svc.parse_segment_start("nope.mp4") is None
    # Dated sub-stream variant still parses.
    assert svc.parse_segment_start("20240101_120500_sub.mp4") == datetime(2024, 1, 1, 12, 5, 0)


def test_find_gaps_for_intervals_basic():
    from app.services import gap_detector as svc

    base = datetime(2024, 1, 1, 12, 0, 0)
    intervals = [
        (base, base + timedelta(seconds=300)),
        (base + timedelta(seconds=600), base + timedelta(seconds=900)),
    ]
    gaps = svc.find_gaps_for_intervals(intervals)
    assert len(gaps) == 1
    assert gaps[0]["start"] == base + timedelta(seconds=300)
    assert gaps[0]["end"] == base + timedelta(seconds=600)
    assert gaps[0]["duration_seconds"] == 300.0

    # Contiguous intervals -> no gaps.
    assert svc.find_gaps_for_intervals([
        (base, base + timedelta(seconds=300)),
        (base + timedelta(seconds=300), base + timedelta(seconds=600)),
    ]) == []

    # Overlapping intervals merge.
    assert svc.find_gaps_for_intervals([
        (base, base + timedelta(seconds=400)),
        (base + timedelta(seconds=200), base + timedelta(seconds=600)),
    ]) == []

    # Tiny gap below threshold is ignored.
    tiny = [
        (base, base + timedelta(seconds=300)),
        (base + timedelta(seconds=300, milliseconds=500),
         base + timedelta(seconds=600)),
    ]
    assert svc.find_gaps_for_intervals(tiny, min_gap_seconds=5) == []


def test_detect_gaps_from_explicit_segments():
    from app.services import gap_detector as svc

    per_camera = {
        "cam1": [
            {"start": datetime(2024, 1, 1, 12, 0, 0), "end": datetime(2024, 1, 1, 12, 5, 0)},
            {"start": datetime(2024, 1, 1, 12, 10, 0), "end": datetime(2024, 1, 1, 12, 15, 0)},
        ],
        "cam2": [
            {"start": datetime(2024, 1, 1, 12, 0, 0), "end": datetime(2024, 1, 1, 12, 5, 0)},
            {"start": datetime(2024, 1, 1, 12, 5, 0), "end": datetime(2024, 1, 1, 12, 10, 0)},
        ],
    }
    rep = svc.detect_gaps(per_camera_segments=per_camera)
    assert rep["total_gaps"] == 1
    assert rep["status"] == "gaps"
    assert len(rep["gaps_by_camera"]["cam1"]) == 1
    assert rep["gaps_by_camera"]["cam2"] == []
    assert "cam1" in rep["cameras_with_gaps"]
    assert "cam2" not in rep["cameras_with_gaps"]

    # Camera filter.
    only = svc.detect_gaps(per_camera_segments=per_camera, camera_name="cam2")
    assert only["cameras"] == ["cam2"]
    assert only["total_gaps"] == 0
    assert only["ok"] is True


def test_scan_directory_finds_missing_ranges(tmp_path):
    from app.services import gap_detector as svc

    root = tmp_path / "recordings"
    cam = root / "cam1"
    cam.mkdir(parents=True)
    base = datetime(2024, 1, 1, 12, 0, 0)
    # 12:00 and 12:10 segments with default 300s length -> 5-minute gap.
    (cam / _stamp(base)).write_bytes(b"x" * 16)
    (cam / _stamp(base + timedelta(seconds=600))).write_bytes(b"x" * 16)
    # Second camera with contiguous coverage -> no gaps.
    cam2 = root / "cam2"
    cam2.mkdir()
    (cam2 / _stamp(base)).write_bytes(b"x" * 16)
    (cam2 / _stamp(base + timedelta(seconds=300))).write_bytes(b"x" * 16)

    rep = svc.detect_gaps(root=root)
    assert rep["total_gaps"] == 1
    assert len(rep["gaps_by_camera"]["cam1"]) == 1
    assert rep["gaps_by_camera"]["cam2"] == []
    gap = rep["gaps_by_camera"]["cam1"][0]
    assert gap["duration_seconds"] == 300.0

    # Class + aliases.
    det = svc.GapDetector(root)
    rep2 = det.detect()
    assert rep2["total_gaps"] == 1
    assert det.scan()["total_gaps"] == 1
    assert svc.RecordingGapDetector is svc.GapDetector
    st = det.get_status()
    assert st["total_gaps"] == 1
    assert svc.get_status(root)["total_gaps"] == 1


def _mounted_client() -> TestClient:
    from app.api.gap_detector import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_gap_detector_api_status_and_scan(tmp_path):
    client = _mounted_client()
    r = client.get("/api/gap_detector/")
    assert r.status_code == 200, r.text
    assert "status" in r.json()

    root = tmp_path / "api-rec"
    cam = root / "camA"
    cam.mkdir(parents=True)
    base = datetime(2024, 5, 1, 8, 0, 0)
    (cam / _stamp(base)).write_bytes(b"x" * 16)
    (cam / _stamp(base + timedelta(seconds=600))).write_bytes(b"x" * 16)

    r2 = client.post("/api/gap_detector/scan", json={"path": str(root)})
    assert r2.status_code == 200, r2.text
    data = r2.json()
    assert data["total_gaps"] == 1
    assert data["gaps_by_camera"]["camA"][0]["duration_seconds"] == 300.0

    r3 = client.post("/api/gaps/detect", json={"path": str(root)})
    assert r3.status_code == 200, r3.text

    r4 = client.get("/api/gap_detector/cameras/camA", params={"path": str(root)})
    assert r4.status_code == 200, r4.text


def test_gap_detector_router_prefix():
    from app.api.gap_detector import router

    assert any("/api/gap_detector" in getattr(r, "path", "") for r in router.routes)
    assert any("/api/gaps" in getattr(r, "path", "") for r in router.routes)


def test_gap_detector_on_full_app(tmp_path):
    from app.main import app

    client = TestClient(app)
    paths = list(app.openapi()["paths"].keys())
    assert any("gap" in p for p in paths)
    r = client.get("/api/gap_detector/status")
    assert r.status_code == 200, r.text
