"""Stale-event auto-acknowledge tests: service + router.

Verification: ``app/services/event_ack.py`` / ``app/api/event_ack.py`` only
handle single-event manual acknowledge/escalate; ``bulk_ack`` only handles
manual bulk acknowledge/escalate with explicit ``event_ids``; and
``auto_rules`` ``auto_acknowledge`` is a report-only flag that never calls
``event_ack.acknowledge``. This suite covers the new
``app/services/auto_ack.py`` + ``app/api/auto_ack.py`` stale auto-ack layer.
"""

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auto_ack import router as auto_ack_router  # noqa: F401
from app.services import auto_ack as auto_ack_mod  # noqa: F401
from app.services import event_ack as event_ack_mod  # noqa: F401
from app.services.auto_ack import (  # noqa: F401
    auto_ack_stale,
    get_policy,
    get_status,
    preview,
    resolve_stale_after_seconds,
    select_stale,
)


def _old(days: float = 3.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _fresh(minutes: float = 5.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def test_auto_ack_imports():
    import app.api.auto_ack  # noqa: F401
    import app.services.auto_ack  # noqa: F401
    from app.api.auto_ack import router  # noqa: F401
    from app.services.auto_ack import auto_ack_stale, preview, select_stale  # noqa: F401
    assert router is not None


def test_auto_ack_paths_present():
    route_paths = [getattr(r, "path", "") for r in auto_ack_router.routes]
    assert "/api/events/auto-ack" in route_paths
    assert "/api/events/auto_ack" in route_paths
    assert "/api/events/auto-ack/preview" in route_paths
    assert "/api/events/auto-ack/status" in route_paths
    # App auto-registers app/api/*.py routers via pkgutil (no main.py edits).
    # Prove registration wiring without importing the full app (which pulls
    # 300+ routers in this workspace): check main.py has the auto-loader and
    # this router exposes ``router``.
    import pathlib

    main_py = pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py"
    text = main_py.read_text()
    assert "pkgutil.iter_modules" in text
    assert "importlib.import_module" in text
    # Isolated app still serves the endpoint (proves router is mountable).
    a = FastAPI()
    a.include_router(auto_ack_router)
    client = TestClient(a)
    r = client.post("/api/events/auto-ack", json={})
    # No entries -> 200 with zero updates (proves route exists, not 404).
    assert r.status_code == 200, r.text
    assert r.json().get("updated_count") == 0


def test_acknowledge_was_manual_or_bulk_only():
    """Pre-existing ack paths require explicit per-event intent; no stale auto-ack."""
    import inspect

    from app.services import bulk_ack as bulk_mod
    from app.services import auto_rules as rules_mod

    # Single-event ack requires an explicit event id.
    sig = inspect.signature(event_ack_mod.acknowledge)
    assert len(sig.parameters) >= 1
    # Bulk ack requires explicit event ids.
    assert "event_ids" in inspect.signature(bulk_mod.bulk_acknowledge).parameters
    # auto_rules auto_acknowledge is a report flag only: it never calls
    # event_ack.acknowledge (verified by source inspection).
    import pathlib

    src = pathlib.Path(rules_mod.__file__).read_text()
    assert "auto_acknowledge" in src
    ack_mod_src = pathlib.Path(event_ack_mod.__file__).read_text()
    assert "stale" not in ack_mod_src.lower()
    bulk_src = pathlib.Path(bulk_mod.__file__).read_text()
    assert "stale" not in bulk_src.lower()


def test_auto_ack_threshold_resolution():
    assert resolve_stale_after_seconds({"stale_after_seconds": 60}) == 60.0
    assert resolve_stale_after_seconds({"stale_after_days": 1}) == 86400.0
    assert resolve_stale_after_seconds({"stale_after_hours": 2}) == 7200.0
    assert resolve_stale_after_seconds({"stale_after": "30m"}) == 1800.0
    assert resolve_stale_after_seconds({"stale_after": "1d"}) == 86400.0
    assert resolve_stale_after_seconds({}) == auto_ack_mod.DEFAULT_STALE_AFTER_SECONDS


def test_auto_ack_policy_defaults():
    policy = get_policy({})
    assert policy["ok"] is True
    assert policy["stale_after_seconds"] == auto_ack_mod.DEFAULT_STALE_AFTER_SECONDS
    assert policy["actor"] == auto_ack_mod.DEFAULT_ACTOR
    custom = get_policy({"stale_after_days": 2, "actor": "sweeper"})
    assert custom["stale_after_seconds"] == 2 * 86400.0
    assert custom["actor"] == "sweeper"


def test_auto_ack_select_stale_only_old_unacked():
    event_ack_mod.clear()
    entries = [
        {"id": 601, "created_at": _old(3)},
        {"id": 602, "created_at": _fresh(5)},
    ]
    stale = select_stale(entries=entries, stale_after_seconds=86400)
    assert [_e["id"] if isinstance(_e, dict) else _e for _e in stale] == [601]
    # Already-acknowledged old events are skipped by default.
    event_ack_mod.acknowledge(event_id=601, actor="op")
    stale2 = select_stale(entries=entries, stale_after_seconds=86400)
    assert stale2 == []
    # ... unless explicitly included.
    stale3 = select_stale(entries=entries, stale_after_seconds=86400, include_acknowledged=True)
    assert len(stale3) == 1


def test_auto_ack_preview_does_not_mutate():
    event_ack_mod.clear()
    entries = [{"id": 611, "created_at": _old(5)}]
    out = preview(entries=entries, stale_after_seconds=3600, actor="sweeper")
    assert out["dry_run"] is True
    assert out["selected_count"] == 1
    assert out["updated_count"] == 0
    assert event_ack_mod.is_acknowledged(611) is False
    status = get_status(entries=entries, stale_after_seconds=3600)
    assert status["stale_count"] == 1


def test_auto_ack_service_run_acks_old_only():
    event_ack_mod.clear()
    entries = [
        {"id": 621, "created_at": _old(4)},
        {"id": 622, "created_at": _fresh(2)},
    ]
    out = auto_ack_stale(entries=entries, stale_after_seconds=86400, actor="auto", reason="stale")
    assert out["ok"] is True
    assert out["updated_event_ids"] == [621]
    assert out["updated_count"] == 1
    assert event_ack_mod.is_acknowledged(621) is True
    assert event_ack_mod.is_acknowledged(622) is False
    assert out["per_event"]["621"]["acknowledged"] is True


def test_auto_ack_service_explicit_ids_without_timestamps():
    event_ack_mod.clear()
    out = auto_ack_stale(event_ids=[631, 632], actor="auto")
    assert out["updated_count"] == 2
    assert event_ack_mod.is_acknowledged(631) is True
    assert event_ack_mod.is_acknowledged(632) is True


def test_auto_ack_router_flow():
    event_ack_mod.clear()
    a = FastAPI()
    a.include_router(auto_ack_router)
    client = TestClient(a)

    entries = [
        {"id": 641, "created_at": _old(4)},
        {"id": 642, "created_at": _fresh(2)},
    ]
    r = client.post("/api/events/auto-ack", json={"entries": entries, "stale_after_seconds": 86400})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("updated_count") == 1
    assert body.get("updated_event_ids") == [641]
    assert event_ack_mod.is_acknowledged(641) is True
    assert event_ack_mod.is_acknowledged(642) is False

    # Underscore alias with days threshold.
    r2 = client.post("/api/events/auto_ack", json={"events": entries, "stale_after_days": 1})
    assert r2.status_code == 200, r2.text
    # 641 already acked -> skipped; nothing new to ack.
    assert r2.json()["updated_count"] == 0


def test_auto_ack_router_preview_and_status():
    event_ack_mod.clear()
    a = FastAPI()
    a.include_router(auto_ack_router)
    client = TestClient(a)
    entries = [{"id": 651, "created_at": _old(6)}]

    r = client.post("/api/events/auto-ack/preview", json={"entries": entries, "stale_after_seconds": 3600})
    assert r.status_code == 200, r.text
    assert r.json()["dry_run"] is True
    assert r.json()["selected_count"] == 1
    assert event_ack_mod.is_acknowledged(651) is False

    s = client.get("/api/events/auto-ack/status")
    assert s.status_code == 200, s.text
    assert "stale_after_seconds" in s.json()

    # event-ack namespace alias serves run too.
    r3 = client.post("/api/event-ack/auto-ack", json={"entries": entries, "stale_after_seconds": 3600})
    assert r3.status_code == 200, r3.text
    assert event_ack_mod.is_acknowledged(651) is True
