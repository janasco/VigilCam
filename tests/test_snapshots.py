import base64
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services import snapshots


def test_snapshots_import():
    import app.services.snapshots  # noqa: F401
    from app.services.snapshots import save_snapshot, resolve_snapshot_path  # noqa: F401


def test_save_snapshot_creates_file(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr("app.core.config.SNAPSHOT_DIR", tmp_path)
    b64 = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 10).decode()
    path = snapshots.save_snapshot(b64)
    assert Path(path).is_file()


def test_save_snapshot_png(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr("app.core.config.SNAPSHOT_DIR", tmp_path)
    b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 5).decode()
    path = snapshots.save_snapshot(b64)
    assert path.endswith(".png")


def test_safe_filename_no_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr("app.core.config.SNAPSHOT_DIR", tmp_path)
    b64 = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 10).decode()
    path = snapshots.save_snapshot(b64, filename="../../evil.png")
    assert ".." not in Path(path).name


def test_event_snapshot_endpoints():
    client = TestClient(app)
    cam = client.post("/api/cameras", json={"name": "snapcam", "kind": "ip", "source": "rtsp://y"}).json()
    client.post("/api/cameras/{}/detect".format(cam["id"]), json={"simulate": True})
    events = client.get("/api/events").json()
    assert events
    eid = events[0]["id"]
    b64 = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 10).decode()
    r = client.post("/api/events/{}/snapshot".format(eid), json={"image_base64": b64})
    assert r.status_code == 200
    assert r.json().get("snapshot_path")
    g = client.get("/api/events/{}/snapshot".format(eid))
    assert g.status_code == 200
    assert g.headers["content-type"].startswith("image/")
