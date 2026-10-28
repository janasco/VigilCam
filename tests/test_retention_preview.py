import os
import time

from fastapi.testclient import TestClient

from app.main import app
from app.services import retention_preview as retention_preview_mod


def test_retention_preview_import():
    import app.services.retention_preview  # noqa: F401
    import app.api.retention_preview  # noqa: F401
    from app.services.retention_preview import (  # noqa: F401
        RetentionPreviewService,
        retention_preview_service,
        preview,
        generate_report,
        build_report,
    )
    from app.api.retention_preview import router  # noqa: F401


def test_retention_preview_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/retention_preview" in paths or "/api/retention/preview" in paths
    assert any("retention" in p and "preview" in p for p in paths) or any(
        "retention" in p and "report" in p for p in paths
    )


def test_retention_preview_service_dry_run(tmp_path):
    cam = tmp_path / "cam1"
    cam.mkdir(parents=True)
    old_file = cam / "old.mp4"
    new_file = cam / "new.mp4"
    old_file.write_bytes(b"x" * 100)
    new_file.write_bytes(b"y" * 100)
    old_mtime = time.time() - (10 * 86400)
    os.utime(old_file, (old_mtime, old_mtime))

    mgr = retention_preview_mod.RetentionPreviewService(storage_root=str(tmp_path))
    result = mgr.preview(max_age_days=1)
    assert result.get("dry_run") is True
    assert result.get("preview") is True
    deleted = result.get("deleted") or []
    assert str(old_file) in deleted
    assert str(new_file) not in deleted
    # preview never removes
    assert old_file.exists()
    assert new_file.exists()
    # report aliases
    assert (result.get("selected") or []) == deleted or set(result.get("selected") or []) == set(deleted)
    assert result.get("freed_bytes") == 100
    assert result.get("total_bytes") == 200


def test_retention_preview_service_size_limit(tmp_path):
    cam = tmp_path / "cam1"
    cam.mkdir(parents=True)
    a = cam / "a.mp4"
    b = cam / "b.mp4"
    a.write_bytes(b"x" * 100)
    b.write_bytes(b"y" * 100)
    mt = time.time() - 100
    os.utime(a, (mt, mt))
    os.utime(b, (mt + 1, mt + 1))

    mgr = retention_preview_mod.RetentionPreviewService(storage_root=str(tmp_path))
    result = mgr.preview(max_age_days=3650, max_total_bytes=100)
    assert result.get("dry_run") is True
    assert result.get("deleted_count") == 1
    assert str(a) in (result.get("selected") or [])
    assert a.exists()


def test_retention_preview_service_per_camera(tmp_path):
    cam_a = tmp_path / "cam_a"
    cam_b = tmp_path / "cam_b"
    cam_a.mkdir()
    cam_b.mkdir()
    old_a = cam_a / "old.mp4"
    old_a.write_bytes(b"x" * 100)
    old_mtime = time.time() - (10 * 86400)
    os.utime(old_a, (old_mtime, old_mtime))
    (cam_b / "new.mp4").write_bytes(b"y" * 50)

    mgr = retention_preview_mod.RetentionPreviewService(storage_root=str(tmp_path))
    result = mgr.generate_report(max_age_days=1)
    per_camera = result.get("per_camera") or result.get("breakdown") or []
    by_name = {c.get("camera_name"): c for c in per_camera}
    assert "cam_a" in by_name
    assert "cam_b" in by_name
    assert str(old_a) in (by_name["cam_a"].get("selected") or by_name["cam_a"].get("to_delete") or [])
    assert by_name["cam_b"].get("selected_count") == 0


def test_retention_preview_service_report_aliases(tmp_path):
    cam = tmp_path / "cam1"
    cam.mkdir(parents=True)
    f = cam / "f.mp4"
    f.write_bytes(b"x" * 10)

    mgr = retention_preview_mod.RetentionPreviewService(storage_root=str(tmp_path))
    for fn_name in ("get_preview", "build_report", "get_report", "report", "preview_report", "run", "dry_run"):
        fn = getattr(mgr, fn_name)
        result = fn(max_age_days=3650, max_total_bytes=10**9)
        assert isinstance(result, dict)
        assert result.get("dry_run") is True


def test_retention_preview_api(tmp_path):
    client = TestClient(app)
    target = tmp_path / "api"
    cam = target / "cam1"
    cam.mkdir(parents=True)
    old = cam / "old.mp4"
    old.write_bytes(b"z" * 50)
    old_mtime = time.time() - (10 * 86400)
    os.utime(old, (old_mtime, old_mtime))

    paths = list(app.openapi()["paths"].keys())
    prefix = None
    for cand in ("/api/retention_preview", "/api/retention/preview", "/api/retention/report"):
        if cand in paths:
            prefix = cand
            break
    assert prefix is not None, paths

    r = client.post(prefix, json={"storage_root": str(target), "max_age_days": 1})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("dry_run") is True
    assert str(old) in (data.get("deleted") or [])
    assert old.exists()

    r2 = client.get(prefix, params={"storage_root": str(target), "max_age_days": 1})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("dry_run") is True
    assert old.exists()
