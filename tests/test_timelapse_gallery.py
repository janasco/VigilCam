from pathlib import Path

from fastapi.testclient import TestClient

from app.services import timelapse_gallery as gallery
from app.api import timelapse_gallery as gallery_api


def _make_outputs(d: Path, names=("a.mp4", "b.mp4")) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"fake-video-" + n.encode())
    return d


def test_imports():
    assert gallery.list_gallery is not None
    assert gallery.TimelapseGalleryService is not None
    assert gallery_api.router is not None


def test_gallery_reuses_timelapse_dir():
    from app.services import timelapse as tl

    assert str(gallery._resolve_gallery_dir()) == str(tl._default_timelapse_dir())
    assert str(gallery.GALLERY_DIR) == str(tl.TIMELAPSE_DIR)


def test_list_empty(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert gallery.list_gallery(timelapse_dir=str(d)) == []
    assert gallery.count_gallery(timelapse_dir=str(d)) == 0


def test_list_with_files(tmp_path):
    d = _make_outputs(tmp_path / "outs")
    items = gallery.list_gallery(timelapse_dir=str(d))
    assert len(items) == 2
    names = {i["name"] for i in items}
    assert "a.mp4" in names and "b.mp4" in names
    first = items[0]
    for key in ("id", "name", "filename", "path", "size", "size_bytes", "mtime"):
        assert key in first
    # limit / offset
    assert len(gallery.list_gallery(timelapse_dir=str(d), limit=1)) == 1
    assert len(gallery.list_gallery(timelapse_dir=str(d), offset=1)) == 1
    assert len(gallery.list_gallery(timelapse_dir=str(d), limit=1, offset=1)) == 1
    # aliases
    assert len(gallery.list_timelapses(timelapse_dir=str(d))) == 2
    assert len(gallery.list_items(timelapse_dir=str(d))) == 2


def test_get_and_delete(tmp_path):
    d = _make_outputs(tmp_path / "outs2", names=("x.mp4",))
    got = gallery.get_gallery_item("x.mp4", timelapse_dir=str(d))
    assert got is not None
    assert got["name"] == "x.mp4"
    assert gallery.get_item("missing.mp4", timelapse_dir=str(d)) is None
    assert gallery.delete_gallery_item("x.mp4", timelapse_dir=str(d)) is True
    assert not (d / "x.mp4").exists()
    assert gallery.delete_gallery_item("x.mp4", timelapse_dir=str(d)) is False


def test_router_status():
    from app.main import app

    client = TestClient(app)
    for path in ("/api/timelapse/gallery/health", "/api/timelapse/gallery/status"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        assert r.json().get("status") == "ok"


def test_router_list_and_detail(tmp_path):
    from app.main import app

    client = TestClient(app)
    d = _make_outputs(tmp_path / "router_outs", names=("r1.mp4", "r2.mp4"))
    r = client.get("/api/timelapse/gallery", params={"directory": str(d)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("count") == 2
    assert len(body.get("items")) == 2

    r2 = client.get("/api/timelapse/gallery/r1.mp4", params={"directory": str(d)})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("name") == "r1.mp4"

    r404 = client.get("/api/timelapse/gallery/nope.mp4", params={"directory": str(d)})
    assert r404.status_code == 404

    c = client.get("/api/timelapse/gallery/count", params={"directory": str(d)})
    assert c.status_code == 200
    assert c.json().get("count") == 2

    rm = client.delete("/api/timelapse/gallery/r1.mp4", params={"directory": str(d)})
    assert rm.status_code == 200, rm.text
    assert not (d / "r1.mp4").exists()

    rm404 = client.delete("/api/timelapse/gallery/r1.mp4", params={"directory": str(d)})
    assert rm404.status_code == 404
