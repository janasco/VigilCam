from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.event_notes import router as event_notes_router  # noqa: F401
from app.services import event_notes as event_notes_service_mod  # noqa: F401
from app.services.event_notes import (  # noqa: F401
    create_note,
    list_notes,
    get_note,
    update_note,
    delete_note,
    clear,
    event_notes_service,
)


def test_event_notes_import():
    import app.api.event_notes  # noqa: F401
    import app.services.event_notes  # noqa: F401
    from app.api.event_notes import router  # noqa: F401
    from app.services.event_notes import create_note, list_notes  # noqa: F401


def test_event_notes_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/events/{event_id}/notes" in paths
    assert "/api/event-notes" in paths or "/api/event_notes" in paths


def test_event_notes_service_crud():
    event_notes_service_mod.clear()
    note = create_note(event_id=1, text="hello")
    assert note["event_id"] == 1
    assert note["text"] == "hello"
    assert note["id"] is not None

    items = list_notes(event_id=1)
    assert len(items) == 1

    fetched = get_note(note["id"])
    assert fetched is not None
    assert fetched["text"] == "hello"

    updated = update_note(note["id"], text="world")
    assert updated["text"] == "world"

    assert delete_note(note["id"]) is True
    assert get_note(note["id"]) is None
    assert list_notes(event_id=1) == []


def test_event_notes_api_crud():
    event_notes_service_mod.clear()
    a = FastAPI()
    a.include_router(event_notes_router)
    client = TestClient(a)

    r = client.post("/api/events/42/notes", json={"text": "door left open", "author": "op"})
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["event_id"] == 42
    assert created["text"] == "door left open"
    nid = created["id"]

    lst = client.get("/api/events/42/notes")
    assert lst.status_code == 200
    assert any(n["id"] == nid for n in lst.json())

    single = client.get(f"/api/events/42/notes/{nid}")
    assert single.status_code == 200
    assert single.json()["id"] == nid

    upd = client.put(f"/api/events/42/notes/{nid}", json={"text": "updated"})
    assert upd.status_code == 200
    assert upd.json()["text"] == "updated"

    top = client.get(f"/api/event-notes/{nid}")
    assert top.status_code == 200

    dele = client.delete(f"/api/events/42/notes/{nid}")
    assert dele.status_code == 200
    assert client.get(f"/api/events/42/notes/{nid}").status_code == 404


def test_event_notes_api_top_level():
    event_notes_service_mod.clear()
    a = FastAPI()
    a.include_router(event_notes_router)
    client = TestClient(a)

    r = client.post("/api/event-notes", json={"event_id": 7, "body": "top-level"})
    assert r.status_code == 200, r.text
    nid = r.json()["id"]

    lst = client.get("/api/event-notes", params={"event_id": 7})
    assert lst.status_code == 200
    assert any(n["id"] == nid for n in lst.json())

    bad = client.post("/api/event-notes", json={"text": "missing event"})
    assert bad.status_code == 400
