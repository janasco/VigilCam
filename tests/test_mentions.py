import pathlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.mentions import router as mentions_router  # noqa: F401
from app.services import mentions as mentions_mod  # noqa: F401
from app.services.mentions import (  # noqa: F401
    clear,
    count,
    delete_notification,
    get_notification,
    list_notifications,
    mark_read,
    notify_for_camera_note,
    notify_for_event_note,
    notify_for_text,
    parse_mentions,
    unread_count,
    mentions_service,
)
import app.services.camera_notes as camera_notes_mod  # noqa: F401
import app.services.event_notes as event_notes_mod  # noqa: F401


def test_mentions_import():
    import app.api.mentions  # noqa: F401
    import app.services.mentions  # noqa: F401
    from app.api.mentions import router  # noqa: F401
    from app.services.mentions import parse_mentions, list_notifications  # noqa: F401


def test_notes_are_plain_text_no_mention_parsing():
    # Source inspection: notes modules must not parse @mentions.
    base = pathlib.Path(__file__).resolve().parents[1] / "app" / "services"
    for name in ("event_notes.py", "camera_notes.py"):
        src = (base / name).read_text(encoding="utf-8", errors="replace")
        assert "@" not in src or "mention" not in src.lower(), f"{name} should not parse @mentions"
        assert "parse_mentions" not in src
        assert "notify_for" not in src
    # Behavioural: @ text is stored verbatim.
    event_notes_mod.clear()
    camera_notes_mod.clear()
    en = event_notes_mod.create_note(event_id=1, text="hey @alice review this")
    assert en["text"] == "hey @alice review this"
    assert "@alice" in en["body"]
    cn = camera_notes_mod.create_note(camera_id=1, text="ping @bob check lens")
    assert cn["text"] == "ping @bob check lens"
    event_notes_mod.clear()
    camera_notes_mod.clear()


def test_parse_mentions_basic():
    assert parse_mentions("hey @alice review") == ["alice"]
    assert parse_mentions("hi @alice and @bob") == ["alice", "bob"]
    # dedupe, order preserved
    assert parse_mentions("@alice hi @bob hi @alice") == ["alice", "bob"]
    # trailing punctuation stripped
    assert parse_mentions("ping @bob, cc @carol.") == ["bob", "carol"]
    # no mentions
    assert parse_mentions("no mentions here") == []
    assert parse_mentions("") == []
    # dict + kwarg spellings
    assert parse_mentions({"text": "hey @dave"}) == ["dave"]
    assert parse_mentions(text="hey @erin") == ["erin"]
    assert parse_mentions(body="hey @frank") == ["frank"]
    # email-like spans should not produce a mention for the domain part
    assert "example" not in parse_mentions("mail me at joe@example.com")


def test_notify_for_text_service():
    clear()
    created = notify_for_text("hey @alice and @bob", author="op", event_id=3)
    assert len(created) == 2
    assert {c["user"] for c in created} == {"alice", "bob"}
    assert created[0]["author"] == "op"
    assert created[0]["event_id"] == 3
    assert created[0]["read"] is False
    # listing / filtering
    assert len(list_notifications()) == 2
    assert len(list_notifications(user="alice")) == 1
    assert unread_count(user="alice") == 1
    # mark read
    first = created[0]
    updated = mark_read(first["id"])
    assert updated["read"] is True
    assert unread_count(user="alice") == 0
    # get / delete
    assert get_notification(first["id"])["id"] == first["id"]
    assert delete_notification(first["id"]) is True
    assert get_notification(first["id"]) is None
    assert count() == 1
    clear()
    assert count() == 0


def test_notify_for_notes_helpers():
    clear()
    event_notes_mod.clear()
    note = event_notes_mod.create_note(event_id=9, text="needs review @alice @bob")
    created = notify_for_event_note(note)
    assert len(created) == 2
    assert created[0]["note_type"] == "event"
    assert created[0]["event_id"] == 9
    clear()
    camera_notes_mod.clear()
    cnote = camera_notes_mod.create_note(camera_id=5, text="check with @carol")
    created2 = notify_for_camera_note(cnote)
    assert len(created2) == 1
    assert created2[0]["user"] == "carol"
    assert created2[0]["note_type"] == "camera"
    # notes without mentions create nothing
    plain = event_notes_mod.create_note(event_id=9, text="no pings here")
    assert notify_for_event_note(plain) == []
    clear()
    event_notes_mod.clear()
    camera_notes_mod.clear()


def test_mentions_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/mentions" in paths
    assert any(p.startswith("/api/mentions") for p in paths)


def test_mentions_api_flow():
    clear()
    a = FastAPI()
    a.include_router(mentions_router)
    client = TestClient(a)

    r = client.post("/api/mentions/parse", json={"text": "hey @alice, meet @bob"})
    assert r.status_code == 200, r.text
    assert r.json()["mentions"] == ["alice", "bob"]

    r = client.post("/api/mentions/notify", json={"text": "hey @alice fix cam", "author": "op", "event_id": 42})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    nid = body["notifications"][0]["id"]

    lst = client.get("/api/mentions", params={"user": "alice"})
    assert lst.status_code == 200
    assert any(n["id"] == nid for n in lst.json())

    single = client.get(f"/api/mentions/{nid}")
    assert single.status_code == 200
    assert single.json()["user"] == "alice"

    rd = client.post(f"/api/mentions/{nid}/read")
    assert rd.status_code == 200
    assert rd.json()["read"] is True

    dele = client.delete(f"/api/mentions/{nid}")
    assert dele.status_code == 200
    assert client.get(f"/api/mentions/{nid}").status_code == 404
    clear()


def test_mentions_api_note_helpers():
    clear()
    a = FastAPI()
    a.include_router(mentions_router)
    client = TestClient(a)

    r = client.post("/api/mentions/event-note", json={"text": "review @alice", "event_id": 7, "author": "op"})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    assert r.json()["notifications"][0]["note_type"] == "event"

    r = client.post("/api/mentions/camera-note", json={"text": "check @bob", "camera_id": 7})
    assert r.status_code == 200, r.text
    assert r.json()["notifications"][0]["note_type"] == "camera"

    bad = client.post("/api/mentions/notify", json={"author": "op"})
    assert bad.status_code == 400
    clear()


def test_mentions_unread_count_api():
    clear()
    a = FastAPI()
    a.include_router(mentions_router)
    client = TestClient(a)
    client.post("/api/mentions/notify", json={"text": "@alice one @alice two"})
    # deduped: single notification for alice
    r = client.get("/api/mentions/unread-count", params={"user": "alice"})
    assert r.status_code == 200
    assert r.json()["unread"] == 1
    clear()
