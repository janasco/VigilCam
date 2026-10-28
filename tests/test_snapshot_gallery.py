from pathlib import Path

from fastapi.testclient import TestClient

from app.services import snapshot_gallery as gallery
from app.api import snapshot_gallery as gallery_api


def _make_outputs(d: Path, names=("a.jpg", "b.png")) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    # Minimal valid headers so extension detection stays image-like.
    payloads = {
        ".jpg": b"\xff\xd8\xff" + b"\x00" * 8,
        ".jpeg": b"\xff\xd8\xff" + b"\x00" * 8,
        ".png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 8,
    }
    for n in names:
        suffix = Path(n).suffix.lower()
        (d / n).write_bytes(payloads.get(suffix, b"fake-image-" + n.encode()))
    return d


def test_imports():
    assert gallery.list_gallery is not None
    assert gallery.SnapshotGalleryService is not None
    assert gallery_api.router is not None


def test_gallery_reuses_snapshot_dir():
    from app.services import snapshots as snaps

    assert str(gallery._resolve_gallery_dir()) == str(snaps._snapshot_dir())
    assert str(gallery.GALLERY_DIR) == str(snaps.SNAPSHOT_DIR)


def test_list_empty(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert gallery.list_gallery(snapshot_dir=str(d)) == []
    assert gallery.count_gallery(snapshot_dir=str(d)) == 0


def test_list_with_files(tmp_path):
    d = _make_outputs(tmp_path / "outs")
    items = gallery.list_gallery(snapshot_dir=str(d))
    assert len(items) == 2
    names = {i["name"] for i in items}
    assert "a.jpg" in names and "b.png" in names
    first = items[0]
    for key in ("id", "name", "filename", "path", "size", "size_bytes", "mtime"):
        assert key in first
    # limit / offset
    assert len(gallery.list_gallery(snapshot_dir=str(d), limit=1)) == 1
    assert len(gallery.list_gallery(snapshot_dir=str(d), offset=1)) == 1
    assert len(gallery.list_gallery(snapshot_dir=str(d), limit=1, offset=1)) == 1
    # aliases
    assert len(gallery.list_snapshots(snapshot_dir=str(d))) == 2
    assert len(gallery.list_items(snapshot_dir=str(d))) == 2
    # non-image files are ignored
    (d / "notes.txt").write_text("ignore me")
    assert len(gallery.list_gallery(snapshot_dir=str(d))) == 2


def test_event_id_filter(tmp_path):
    d = _make_outputs(tmp_path / "evts", names=("event_7_snapshot_abc.jpg", "event_9_snapshot_def.jpg"))
    all_items = gallery.list_gallery(snapshot_dir=str(d))
    assert len(all_items) == 2
    only7 = gallery.list_gallery(snapshot_dir=str(d), event_id=7)
    assert len(only7) == 1
    assert only7[0]["event_id"] == 7
    assert gallery.get_gallery_item("event_7_snapshot_abc.jpg", snapshot_dir=str(d))["event_id"] == 7


def test_get_and_delete(tmp_path):
    d = _make_outputs(tmp_path / "outs2", names=("x.jpg",))
    got = gallery.get_gallery_item("x.jpg", snapshot_dir=str(d))
    assert got is not None
    assert got["name"] == "x.jpg"
    assert gallery.get_item("missing.jpg", snapshot_dir=str(d)) is None
    assert gallery.delete_gallery_item("x.jpg", snapshot_dir=str(d)) is True
    assert not (d / "x.jpg").exists()
    assert gallery.delete_gallery_item("x.jpg", snapshot_dir=str(d)) is False


def test_router_status():
    from app.main import app

    client = TestClient(app)
    for path in ("/api/snapshots/gallery/health", "/api/snapshots/gallery/status"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        assert r.json().get("status") == "ok"


def test_router_list_and_detail(tmp_path):
    from app.main import app

    client = TestClient(app)
    d = _make_outputs(tmp_path / "router_outs", names=("r1.jpg", "r2.png"))
    r = client.get("/api/snapshots/gallery", params={"directory": str(d)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("count") == 2
    assert len(body.get("items")) == 2

    r2 = client.get("/api/snapshots/gallery/r1.jpg", params={"directory": str(d)})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("name") == "r1.jpg"

    r404 = client.get("/api/snapshots/gallery/nope.jpg", params={"directory": str(d)})
    assert r404.status_code == 404

    c = client.get("/api/snapshots/gallery/count", params={"directory": str(d)})
    assert c.status_code == 200
    assert c.json().get("count") == 2

    f = client.get("/api/snapshots/gallery/r1.jpg/file", params={"directory": str(d)})
    assert f.status_code == 200, f.text
    assert f.headers["content-type"].startswith("image/")

    rm = client.delete("/api/snapshots/gallery/r1.jpg", params={"directory": str(d)})
    assert rm.status_code == 200, rm.text
    assert not (d / "r1.jpg").exists()

    rm404 = client.delete("/api/snapshots/gallery/r1.jpg", params={"directory": str(d)})
    assert rm404.status_code == 404
