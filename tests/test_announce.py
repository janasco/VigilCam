from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.announce import router as announce_router
from app.services import announce
from app.services.announce import (
    announce_service,
    clear,
    clear_announcement,
    disable,
    enable,
    get_announcement,
    get_status,
    is_active,
    list_announcements,
    set_announcement,
)


def _fresh():
    clear()


def test_imports():
    import app.services.announce  # noqa: F401
    import app.api.announce  # noqa: F401
    from app.services.announce import announce_service, is_active  # noqa: F401


def test_set_get_clear():
    _fresh()
    assert get_announcement() is None
    assert is_active() is False
    r = set_announcement(message="System maintenance at 2am")
    assert r["ok"] is True
    assert r["message"] == "System maintenance at 2am"
    assert r["level"] == "info"
    b = get_announcement()
    assert b is not None
    assert b["message"] == "System maintenance at 2am"
    assert is_active() is True
    st = get_status()
    assert st["active"] is True
    assert st["message"] == "System maintenance at 2am"
    out = clear_announcement()
    assert out["ok"] is True
    assert get_announcement() is None
    assert is_active() is False


def test_message_spellings():
    _fresh()
    assert set_announcement(text="hello text")["ok"] is True
    assert get_announcement()["message"] == "hello text"
    _fresh()
    assert set_announcement({"text": "dict text"})["ok"] is True
    assert get_announcement()["message"] == "dict text"
    _fresh()
    assert set_announcement("bare string message")["ok"] is True
    assert get_announcement()["message"] == "bare string message"
    _fresh()
    assert set_announcement(payload={"message": "nested"})["ok"] is True
    assert get_announcement()["message"] == "nested"


def test_empty_message_rejected():
    _fresh()
    r = set_announcement(message="")
    assert r["ok"] is False
    assert get_announcement() is None
    r = set_announcement(message="   ")
    assert r["ok"] is False
    r = set_announcement({})
    assert r["ok"] is False


def test_level_normalization():
    _fresh()
    set_announcement(message="m", level="warning")
    assert get_announcement()["level"] == "warning"
    _fresh()
    set_announcement(message="m", level="WARN")
    assert get_announcement()["level"] == "warning"
    _fresh()
    set_announcement(message="m")
    assert get_announcement()["level"] == "info"


def test_enable_disable():
    _fresh()
    set_announcement(message="hello")
    assert is_active() is True
    disable()
    assert is_active() is False
    assert get_announcement() is not None  # still stored, just hidden
    enable()
    assert is_active() is True


def test_expiry():
    _fresh()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    set_announcement(message="old", expires_at=past.isoformat())
    assert is_active() is False
    st = get_status()
    assert st["active"] is False
    assert st["expired"] is True
    _fresh()
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    set_announcement(message="new", expires_at=future.isoformat())
    assert is_active() is True


def test_singleton_mirrors_module():
    _fresh()
    announce_service.set_announcement(message="via service", level="success")
    assert is_active() is True
    assert announce_service.is_active() is True
    assert announce_service.get_status()["message"] == "via service"
    announce_service.clear()
    assert is_active() is False


def test_list_shape():
    _fresh()
    assert list_announcements() == []
    set_announcement(message="one")
    items = list_announcements()
    assert len(items) == 1
    assert items[0]["message"] == "one"


def test_distinct_from_other_broadcasts():
    # Guard: admin banner must not be confused with push/talk/event/agent broadcasts.
    # Note: app.services.talk / node_dispatch hang on import in this repo
    # (pre-existing pydantic recursion via app.schemas), so verify by source
    # inspection instead of importing them.
    import pathlib

    _fresh()
    import app.services.web_push as web_push
    import app.services.event_bus as event_bus

    assert hasattr(web_push, "broadcast")
    assert hasattr(event_bus.WebSocketManager, "broadcast")

    base = pathlib.Path(__file__).resolve().parents[1] / "app" / "services"
    for name in ("talk.py", "node_dispatch.py", "event_bus.py", "web_push.py"):
        src = (base / name).read_text(encoding="utf-8", errors="replace")
        assert "broadcast" in src, f"{name} should define broadcast"
        assert "def set_announcement" not in src
        assert "def get_announcement" not in src
    # Agent compat shim is a different broadcast (event-bus fan-out).
    compat_src = (base.parents[1] / "app" / "api" / "agent_compat.py").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "/command/broadcast" in compat_src
    assert "set_announcement" not in compat_src
    # None of the imported broadcast owners manage the banner.
    for mod in (web_push, event_bus):
        assert not hasattr(mod, "set_announcement")
        assert not hasattr(mod, "get_announcement")
    # Banner service exposes banner API they lack.
    assert hasattr(announce, "set_announcement")
    assert hasattr(announce, "get_announcement")
    assert hasattr(announce, "is_active")
    assert not hasattr(announce, "send_web_push")
    assert not hasattr(announce, "queue_talk")


def test_router_set_get_clear_flow():
    _fresh()
    app = FastAPI()
    app.include_router(announce_router)
    client = TestClient(app)

    r = client.get("/api/announce")
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False

    r = client.post("/api/announce", json={"message": "Outage tonight", "level": "warning"})
    assert r.status_code == 200, r.text
    assert r.json()["message"] == "Outage tonight"
    assert r.json()["level"] == "warning"

    r = client.get("/api/announce")
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True
    assert r.json()["message"] == "Outage tonight"

    r = client.get("/api/announce/status")
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True

    r = client.get("/api/announce/active")
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True

    r = client.delete("/api/announce")
    assert r.status_code == 200, r.text

    r = client.get("/api/announce")
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False


def test_router_aliases_and_health():
    _fresh()
    app = FastAPI()
    app.include_router(announce_router)
    client = TestClient(app)

    r = client.get("/api/announce/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = client.post("/api/announcement", json={"message": "alias check"})
    assert r.status_code == 200, r.text

    r = client.get("/api/banner")
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True
    assert r.json()["message"] == "alias check"

    r = client.get("/api/banners")
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True

    r = client.post("/api/banner/clear")
    assert r.status_code == 200, r.text
    r = client.get("/api/announce")
    assert r.json()["active"] is False


def test_router_validation():
    _fresh()
    app = FastAPI()
    app.include_router(announce_router)
    client = TestClient(app)

    r = client.post("/api/announce", json={})
    assert r.status_code == 400, r.text
    r = client.post("/api/announce", json={"message": ""})
    assert r.status_code == 400, r.text
