from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services import snapshot_fallback as fb


def test_snapshot_fallback_imports():
    from app.main import app as _app
    from app.api import snapshot_fallback as api_mod

    assert hasattr(api_mod, "router")
    paths = list(_app.openapi()["paths"].keys())
    assert "/api/snapshot-fallback/status" in paths
    assert "/api/snapshot-fallback/capture" in paths
    assert "/api/snapshot_fallback/status" in paths


def _ok_primary_only(source, dest, timeout):
    # Succeed only for the primary URL.
    if "primary-good" in str(source):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"\xff\xd8fake")
        return True
    return False


def _fail_primary_ok_fallback(source, dest, timeout):
    if "fallback-good" in str(source):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"\xff\xd8fake")
        return True
    return False


def _always_fail(source, *a, **k):
    return False


def test_capture_primary_success(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr("app.core.config.SNAPSHOT_DIR", tmp_path)
    fb.clear()
    r = fb.capture_with_fallback(
        camera_id=7101,
        primary_source="rtsp://primary-good/cam",
        fallback_source="rtsp://fallback-good/cam",
        capturer=_ok_primary_only,
    )
    assert r.success is True
    assert r.method == "primary"
    assert r.fallback_used is False
    assert r.source_used == "rtsp://primary-good/cam"
    assert Path(r.path).is_file()
    assert fb.is_fallback_used("camera:7101") is False
    fb.clear()


def test_capture_falls_back_to_secondary(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr("app.core.config.SNAPSHOT_DIR", tmp_path)
    fb.clear()
    r = fb.capture_with_fallback(
        camera_id=7102,
        primary_source="rtsp://bad-primary/cam",
        fallback_source="rtsp://fallback-good/cam",
        capturer=_fail_primary_ok_fallback,
    )
    assert r.success is True
    assert r.method == "fallback"
    assert r.fallback_used is True
    assert r.source_used == "rtsp://fallback-good/cam"
    assert Path(r.path).is_file()
    fb.clear()


def test_capture_placeholder_when_all_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr("app.core.config.SNAPSHOT_DIR", tmp_path)
    fb.clear()
    r = fb.capture_with_fallback(
        camera_id=7103,
        primary_source="rtsp://bad/a",
        fallback_source="rtsp://bad/b",
        capturer=_always_fail,
    )
    assert r.success is True
    assert r.method == "placeholder"
    assert r.fallback_used is True
    assert Path(r.path).is_file()
    assert bytes(Path(r.path).read_bytes()[:2]) == b"\xff\xd8"
    fb.clear()


def test_capture_uses_last_good_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr("app.core.config.SNAPSHOT_DIR", tmp_path)
    fb.clear()
    first = fb.capture_with_fallback(
        camera_id=7104,
        primary_source="rtsp://primary-good/cam",
        capturer=_ok_primary_only,
    )
    assert first.method == "primary"
    cached_path = fb.get_last_good("camera:7104")
    assert cached_path is not None
    # Now both sources fail; should serve cached copy.
    second = fb.capture_with_fallback(
        camera_id=7104,
        primary_source="rtsp://bad/a",
        fallback_source="rtsp://bad/b",
        capturer=_always_fail,
    )
    assert second.method == "cache"
    assert second.fallback_used is True
    assert Path(second.path).is_file()
    fb.clear()


def test_capture_aliases_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr("app.core.config.SNAPSHOT_DIR", tmp_path)
    fb.clear()
    assert callable(fb.capture)
    assert callable(fb.capture_snapshot)
    r = fb.capture(camera_id=7105, primary_source="x", capturer=_always_fail)
    assert r["success"] is True
    assert fb.get_status("camera:7105") is not None
    assert fb.get_status(7105) is not None
    summary = fb.get_summary()
    assert summary["total"] >= 1
    assert "fallback_used" in summary
    fb.clear()


def test_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr("app.core.config.SNAPSHOT_DIR", tmp_path)
    fb.clear()
    # Ensure underlying ffmpeg is not used in endpoint path: no sources -> placeholder.
    client = TestClient(app)

    r = client.get("/api/snapshot-fallback/status")
    assert r.status_code == 200
    assert "total" in r.json()

    r2 = client.get("/api/snapshot-fallback/summary")
    assert r2.status_code == 200

    rc = client.post("/api/snapshot-fallback/capture", json={"camera_id": 7106})
    assert rc.status_code == 200, rc.text
    body = rc.json()
    assert body.get("success") is True
    assert body.get("method") == "placeholder"
    assert Path(body["path"]).is_file()

    rg = client.get("/api/snapshot-fallback/camera/7106")
    assert rg.status_code == 200
    assert rg.json()["camera_id"] == 7106

    rl = client.get("/api/snapshot-fallback/results")
    assert rl.status_code == 200
    assert isinstance(rl.json(), list)

    ra = client.get("/api/snapshot_fallback/status")
    assert ra.status_code == 200

    r404 = client.get("/api/snapshot-fallback/camera/999998")
    assert r404.status_code == 404

    rd = client.delete("/api/snapshot-fallback/results")
    assert rd.status_code == 200
    assert rd.json()["ok"] is True
    fb.clear()
