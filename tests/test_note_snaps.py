import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.services import camera_notes
from app.services import event_notes
from app.services import note_snaps
from app.api.note_snaps import router as note_snaps_router  # noqa: F401
from app.services.note_snaps import (  # noqa: F401
    attach_snapshot,
    capture_note_snapshot,
    clear,
    count,
    delete_note_snapshot,
    get_note_snapshot,
    list_note_snapshots,
    note_snaps_service,
    save_note_with_snapshot,
)


def _reset():
    note_snaps.clear()
    event_notes.clear()
    camera_notes.clear()


def test_note_snaps_imports():
    import app.services.note_snaps  # noqa: F401
    import app.api.note_snaps  # noqa: F401
    from app.services.note_snaps import NoteSnapsService, capture_note_snapshot, list_note_snapshots  # noqa: F401
    from app.api.note_snaps import router  # noqa: F401
    assert router is not None


def test_event_and_camera_notes_are_text_only():
    # Verify the gap this feature fills: notes store text with no image fields.
    _reset()
    created_event = event_notes.create_note(event_id=1, text="door left open")
    for field in ("snapshot_path", "thumbnail_base64", "snapshot_base64", "image", "data_url", "thumbnail"):
        assert field not in created_event, f"event_notes should not store {field}"
    created_cam = camera_notes.create_note(camera_id=1, text="lens cleaned")
    for field in ("snapshot_path", "thumbnail_base64", "snapshot_base64", "image", "data_url", "thumbnail"):
        assert field not in created_cam, f"camera_notes should not store {field}"


def test_capture_attaches_snapshot_to_event_note():
    _reset()
    created = event_notes.create_note(event_id=11, text="motion at gate")
    snap = capture_note_snapshot(note_id=created["id"])
    assert isinstance(snap, dict)
    assert snap.get("note_id") == created["id"]
    assert snap.get("ok") is True
    assert snap.get("snapshot_path")
    assert snap.get("thumbnail_base64")
    assert snap.get("data_url", "").startswith("data:image/")
    raw = base64.b64decode(snap["thumbnail_base64"])
    assert raw[:2] == b"\xff\xd8"

    # Attached back onto the event-note entry.
    fetched = event_notes.get_note(created["id"])
    assert fetched is not None
    assert fetched.get("snapshot_path") == snap["snapshot_path"]
    assert fetched.get("thumbnail_base64") == snap["thumbnail_base64"]


def test_capture_attaches_snapshot_to_camera_note():
    _reset()
    created = camera_notes.create_note(camera_id=13, text="check focus")
    snap = attach_snapshot(note_id=created["id"], note_type="camera")
    assert snap.get("note_id") == created["id"]
    assert snap.get("snapshot_path")
    fetched = camera_notes.get_note(created["id"])
    assert fetched is not None
    assert fetched.get("snapshot_path") == snap["snapshot_path"]


def test_attach_custom_image_bytes():
    _reset()
    created = event_notes.create_note(event_id=21, text="with photo")
    custom = b"\xff\xd8\xff" + b"\x00" * 64
    snap = attach_snapshot(note_id=created["id"], image_base64=base64.b64encode(custom).decode("ascii"))
    assert snap.get("method") == "attached"
    stored = note_snaps.get_snapshot_bytes(created["id"])
    assert stored is not None
    assert bytes(stored) == custom


def test_missing_note_raises_keyerror():
    _reset()
    try:
        capture_note_snapshot(note_id=99999)
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for missing note")
    assert get_note_snapshot(99999) is None
    assert delete_note_snapshot(99999) is False


def test_service_list_get_delete_clear():
    _reset()
    a = event_notes.create_note(event_id=31, text="a")
    b = camera_notes.create_note(camera_id=32, text="b")
    sa = save_note_with_snapshot(note_id=a["id"], note_type="event")
    sb = save_note_with_snapshot(note_id=b["id"], note_type="camera")
    assert count() == 2
    assert len(list_note_snapshots()) == 2
    assert get_note_snapshot(sa["note_id"], note_type="event") is not None
    assert get_note_snapshot(sb["note_id"], note_type="camera") is not None
    assert get_note_snapshot("missing") is None
    assert note_snaps.has_snapshot(sa["note_id"], note_type="event") is True
    assert delete_note_snapshot(sa["note_id"], note_type="event") is True
    assert get_note_snapshot(sa["note_id"], note_type="event") is None
    # Same numeric id may still exist for the other kind.
    assert get_note_snapshot(sb["note_id"], note_type="camera") is not None
    assert delete_note_snapshot("missing") is False
    assert sb["note_id"] in [x["note_id"] for x in list_note_snapshots()]
    n = clear()
    assert n == 1
    assert count() == 0


def test_note_snaps_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/note_snaps" in paths
    assert any("note" in p and "snap" in p for p in paths)


def test_note_snaps_endpoints():
    _reset()
    created = event_notes.create_note(event_id=41, text="front door")
    note_id = created["id"]
    client = TestClient(app)
    r = client.post("/api/note_snaps/capture", json={"note_id": note_id})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body.get("note_id") == note_id
    assert body.get("snapshot_path")
    assert body.get("thumbnail_base64")

    # List + detail.
    r2 = client.get("/api/note_snaps")
    assert r2.status_code == 200
    assert isinstance(r2.json(), list)
    assert any(x.get("note_id") == note_id for x in r2.json())

    r3 = client.get(f"/api/note_snaps/{note_id}")
    assert r3.status_code == 200
    assert r3.json().get("note_id") == note_id

    # Visual bytes endpoint.
    img = client.get(f"/api/note_snaps/{note_id}/image")
    assert img.status_code == 200
    assert img.headers["content-type"].startswith("image/")
    assert img.content[:2] == b"\xff\xd8"

    # Scoped note convenience route serves the same snapshot.
    scoped = client.get(f"/api/event-notes/{note_id}/snapshot")
    assert scoped.status_code == 200
    assert scoped.json().get("note_id") == note_id

    # Missing snapshot -> 404.
    assert client.get("/api/note_snaps/99999").status_code == 404

    # Delete.
    d = client.delete(f"/api/note_snaps/{note_id}")
    assert d.status_code == 200
    assert client.get(f"/api/note_snaps/{note_id}").status_code == 404


def test_note_snaps_isolated_router():
    _reset()
    created = camera_notes.create_note(camera_id=51, text="side cam")
    isolated = FastAPI()
    from app.api.note_snaps import router

    isolated.include_router(router)
    client = TestClient(isolated)

    r = client.post("/api/note-snaps/capture", json={"note_id": created["id"]})
    assert r.status_code in (200, 201), r.text
    nid = r.json().get("note_id")
    assert nid == created["id"]

    g = client.get("/api/note-snaps")
    assert g.status_code == 200
    assert any(x.get("note_id") == nid for x in g.json())

    bad = client.post("/api/note-snaps/capture", json={})
    assert bad.status_code in (400, 404)
