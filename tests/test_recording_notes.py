from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.recording_notes import router as recording_notes_router  # noqa: F401
from app.services import recording_notes as recording_notes_service_mod  # noqa: F401
from app.services.recording_notes import (  # noqa: F401
    create_note,
    list_notes,
    get_note,
    update_note,
    delete_note,
    clear,
    recording_notes_service,
)


def test_recording_notes_import():
    import app.api.recording_notes  # noqa: F401
    import app.services.recording_notes  # noqa: F401
    from app.api.recording_notes import router  # noqa: F401
    from app.services.recording_notes import create_note, list_notes  # noqa: F401


def test_recording_notes_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/recordings/{recording_id}/notes" in paths
    assert "/api/recording-notes" in paths or "/api/recording_notes" in paths


def test_recording_notes_service_crud():
    recording_notes_service_mod.clear()
    note = create_note(recording_id=1, text="hello")
    assert note["recording_id"] == 1
    assert note["text"] == "hello"
    assert note["id"] is not None

    items = list_notes(recording_id=1)
    assert len(items) == 1

    fetched = get_note(note["id"])
    assert fetched is not None
    assert fetched["text"] == "hello"

    updated = update_note(note["id"], text="world")
    assert updated["text"] == "world"

    assert delete_note(note["id"]) is True
    assert get_note(note["id"]) is None
    assert list_notes(recording_id=1) == []


def test_recording_notes_api_crud():
    recording_notes_service_mod.clear()
    a = FastAPI()
    a.include_router(recording_notes_router)
    client = TestClient(a)

    r = client.post("/api/recordings/42/notes", json={"text": "keep segment", "author": "op"})
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["recording_id"] == 42
    assert created["text"] == "keep segment"
    nid = created["id"]

    lst = client.get("/api/recordings/42/notes")
    assert lst.status_code == 200
    assert any(n["id"] == nid for n in lst.json())

    single = client.get(f"/api/recordings/42/notes/{nid}")
    assert single.status_code == 200
    assert single.json()["id"] == nid

    upd = client.put(f"/api/recordings/42/notes/{nid}", json={"text": "updated"})
    assert upd.status_code == 200
    assert upd.json()["text"] == "updated"

    top = client.get(f"/api/recording-notes/{nid}")
    assert top.status_code == 200

    dele = client.delete(f"/api/recordings/42/notes/{nid}")
    assert dele.status_code == 200
    assert client.get(f"/api/recordings/42/notes/{nid}").status_code == 404


def test_recording_notes_api_top_level():
    recording_notes_service_mod.clear()
    a = FastAPI()
    a.include_router(recording_notes_router)
    client = TestClient(a)

    r = client.post("/api/recording-notes", json={"recording_id": 7, "body": "top-level"})
    assert r.status_code == 200, r.text
    nid = r.json()["id"]

    lst = client.get("/api/recording-notes", params={"recording_id": 7})
    assert lst.status_code == 200
    assert any(n["id"] == nid for n in lst.json())

    bad = client.post("/api/recording-notes", json={"text": "missing recording"})
    assert bad.status_code == 400
