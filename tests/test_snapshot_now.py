from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services import snapshot_now


def test_imports():
    import app.services.snapshot_now  # noqa: F401
    import app.api.snapshot_now  # noqa: F401
    from app.services.snapshot_now import capture_snapshot_now  # noqa: F401
    from app.api.snapshot_now import router  # noqa: F401
    assert router is not None


def test_capture_creates_file(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_now, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr("app.core.config.SNAPSHOT_DIR", tmp_path)
    result = snapshot_now.capture_snapshot_now(camera_id=1)
    assert result.get("success") is True
    assert result.get("status") == "captured"
    path = Path(result["path"])
    assert path.is_file()
    assert path.stat().st_size > 0
    assert path.suffix.lower() in (".jpg", ".jpeg", ".png")
    assert bytes(path.read_bytes()[:2]) == b"\xff\xd8"


def test_capture_placeholder_without_source(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_now, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr("app.core.config.SNAPSHOT_DIR", tmp_path)
    result = snapshot_now.capture_now(label="test-cam")
    assert result.get("ok") is True
    assert Path(result["snapshot_path"]).is_file()


def test_capture_aliases(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_now, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr("app.core.config.SNAPSHOT_DIR", tmp_path)
    assert callable(snapshot_now.take_snapshot_now)
    assert callable(snapshot_now.capture_single_snapshot)
    r = snapshot_now.take_snapshot_now(camera_id=2)
    assert Path(r["path"]).is_file()


def test_endpoint_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_now, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr("app.core.config.SNAPSHOT_DIR", tmp_path)
    # Ensure service module used by router sees the patched dir via config.
    client = TestClient(app)
    r = client.post("/api/snapshot_now/capture", json={"camera_id": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("success") is True
    assert body.get("snapshot_path") or body.get("path")
    assert Path(body.get("snapshot_path") or body.get("path")).is_file()


def test_endpoint_capture_empty_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_now, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr("app.core.config.SNAPSHOT_DIR", tmp_path)
    client = TestClient(app)
    r = client.post("/api/snapshot_now/capture", json={})
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
