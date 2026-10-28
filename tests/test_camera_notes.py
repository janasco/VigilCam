from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.camera_notes import router as camera_notes_router  # noqa: F401
from app.services import camera_notes as camera_notes_service_mod  # noqa: F401
from app.services.camera_notes import (  # noqa: F401
    create_note,
    list_notes,
    get_note,
    update_note,
    delete_note,
    clear,
    camera_notes_service,
)


def test_camera_notes_import():
    import app.api.camera_notes  # noqa: F401
    import app.services.camera_notes  # noqa: F401
    from app.api.camera_notes import router  # noqa: F401
    from app.services.camera_notes import create_note, list_notes  # noqa: F401


def test_camera_notes_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/cameras/{camera_id}/notes" in paths
    assert "/api/camera-notes" in paths or "/api/camera_notes" in paths


def test_camera_notes_service_crud():
    camera_notes_service_mod.clear()
    note = create_note(camera_id=1, text="hello")
    assert note["camera_id"] == 1
    assert note["text"] == "hello"
    assert note["id"] is not None

    items = list_notes(camera_id=1)
    assert len(items) == 1

    fetched = get_note(note["id"])
    assert fetched is not None
    assert fetched["text"] == "hello"

    updated = update_note(note["id"], text="world")
    assert updated["text"] == "world"

    assert delete_note(note["id"]) is True
    assert get_note(note["id"]) is None
    assert list_notes(camera_id=1) == []


def test_camera_notes_api_crud():
    camera_notes_service_mod.clear()
    a = FastAPI()
    a.include_router(camera_notes_router)
    client = TestClient(a)

    r = client.post("/api/cameras/42/notes", json={"text": "lens cleaned", "author": "op"})
    assert r.status_code == 200, r.text
    created = r.json()
    assert int(created["camera_id"]) == 42
    assert created["text"] == "lens cleaned"
    nid = created["id"]

    lst = client.get("/api/cameras/42/notes")
    assert lst.status_code == 200
    assert any(n["id"] == nid for n in lst.json())

    single = client.get(f"/api/cameras/42/notes/{nid}")
    assert single.status_code == 200
    assert single.json()["id"] == nid

    upd = client.put(f"/api/cameras/42/notes/{nid}", json={"text": "updated"})
    assert upd.status_code == 200
    assert upd.json()["text"] == "updated"

    top = client.get(f"/api/camera-notes/{nid}")
    assert top.status_code == 200

    dele = client.delete(f"/api/cameras/42/notes/{nid}")
    assert dele.status_code == 200
    assert client.get(f"/api/cameras/42/notes/{nid}").status_code == 404


def test_camera_notes_api_top_level():
    camera_notes_service_mod.clear()
    a = FastAPI()
    a.include_router(camera_notes_router)
    client = TestClient(a)

    r = client.post("/api/camera-notes", json={"camera_id": 7, "body": "top-level"})
    assert r.status_code == 200, r.text
    nid = r.json()["id"]

    lst = client.get("/api/camera-notes", params={"camera_id": 7})
    assert lst.status_code == 200
    assert any(n["id"] == nid for n in lst.json())

    bad = client.post("/api/camera-notes", json={"text": "missing camera"})
    assert bad.status_code == 400
