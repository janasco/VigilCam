from fastapi.testclient import TestClient

from app.main import app
from app.services import bookmarks


def test_bookmarks_import():
    import app.services.bookmarks  # noqa: F401
    import app.api.bookmarks  # noqa: F401
    from app.services.bookmarks import bookmark_service, create_bookmark  # noqa: F401
    from app.api.bookmarks import router  # noqa: F401


def test_bookmarks_service_basic():
    bookmarks.clear()
    bm = bookmarks.create_bookmark(recording_id=1, camera_id=2, offset_seconds=12.5, label="doorbell")
    assert isinstance(bm, dict)
    assert bm.get("recording_id") == 1
    assert bm.get("offset_seconds") == 12.5
    assert bm.get("label") == "doorbell"
    assert bookmarks.get_bookmark(bm["id"]) is not None
    assert len(bookmarks.list_bookmarks()) == 1
    assert len(bookmarks.list_bookmarks(recording_id=1)) == 1
    assert len(bookmarks.list_bookmarks(recording_id=999)) == 0


def test_bookmarks_service_update_delete():
    bookmarks.clear()
    bm = bookmarks.create_bookmark(recording_id=5, label="start")
    updated = bookmarks.update_bookmark(bm["id"], label="updated")
    assert updated is not None
    assert updated.get("label") == "updated"
    assert bookmarks.delete_bookmark(bm["id"]) is True
    assert bookmarks.get_bookmark(bm["id"]) is None
    assert bookmarks.delete_bookmark("missing") is False


def test_bookmarks_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/bookmarks" in paths


def test_bookmarks_endpoints():
    bookmarks.clear()
    client = TestClient(app)
    r = client.post(
        "/api/bookmarks",
        json={"recording_id": 7, "camera_id": 3, "offset_seconds": 42, "label": "motion"},
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    bid = body.get("id") or body.get("bookmark_id")
    assert bid is not None
    assert body.get("offset_seconds") == 42

    r2 = client.get("/api/bookmarks")
    assert r2.status_code == 200
    assert len(r2.json()) >= 1

    r3 = client.get("/api/bookmarks/{}".format(bid))
    assert r3.status_code == 200
    assert r3.json().get("id") == bid

    r4 = client.get("/api/bookmarks", params={"recording_id": 7})
    assert r4.status_code == 200
    assert all(str(x.get("recording_id")) == "7" for x in r4.json())

    r5 = client.put("/api/bookmarks/{}".format(bid), json={"label": "renamed"})
    assert r5.status_code == 200
    assert r5.json().get("label") == "renamed"

    r6 = client.delete("/api/bookmarks/{}".format(bid))
    assert r6.status_code == 200
    r7 = client.get("/api/bookmarks/{}".format(bid))
    assert r7.status_code == 404


def test_bookmarks_nested_recording_route():
    bookmarks.clear()
    client = TestClient(app)
    r = client.post("/api/recordings/9/bookmarks", json={"label": "nested", "offset_seconds": 5})
    assert r.status_code in (200, 201), r.text
    g = client.get("/api/recordings/9/bookmarks")
    assert g.status_code == 200
    assert len(g.json()) >= 1
