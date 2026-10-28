"""Bulk event acknowledge tests: service + router.

Verification: ``app/services/event_ack.py`` / ``app/api/event_ack.py`` only
handle single-event acknowledge/escalate (``acknowledge(event_id=...)``);
there is no bulk endpoint there. This suite covers the new
``app/services/bulk_ack.py`` + ``app/api/bulk_ack.py`` bulk layer.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.bulk_ack import router as bulk_ack_router  # noqa: F401
from app.services import bulk_ack as bulk_ack_mod  # noqa: F401
from app.services import event_ack as event_ack_mod  # noqa: F401
from app.services.bulk_ack import (  # noqa: F401
    bulk_ack_events,
    bulk_acknowledge,
    bulk_escalate,
    coerce_id_list,
    normalize_action,
)


def test_bulk_ack_imports():
    import app.api.bulk_ack  # noqa: F401
    import app.services.bulk_ack  # noqa: F401
    from app.api.bulk_ack import router  # noqa: F401
    from app.services.bulk_ack import bulk_ack_events, bulk_acknowledge, bulk_escalate  # noqa: F401
    assert router is not None


def test_bulk_ack_paths_present():
    route_paths = [getattr(r, "path", "") for r in bulk_ack_router.routes]
    assert "/api/events/bulk-acknowledge" in route_paths
    assert "/api/events/bulk-ack" in route_paths
    assert "/api/events/bulk-escalate" in route_paths
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
    a.include_router(bulk_ack_router)
    client = TestClient(a)
    r = client.post("/api/events/bulk-acknowledge", json={"actor": "op"})
    # missing ids -> 400 proves the route exists (not 404).
    assert r.status_code == 400, r.text


def test_bulk_ack_service_normalize():
    assert coerce_id_list([1, "2", "x", None]) == [1, 2]
    assert coerce_id_list("3,4") == [3, 4]
    assert coerce_id_list(7) == [7]
    assert coerce_id_list([{"id": 9}, {"event_id": 10}]) == [9, 10]
    assert normalize_action("ACK") == "acknowledge"
    assert normalize_action("escalation") == "escalate"
    assert normalize_action(None) == "acknowledge"
    assert normalize_action("acknowledge") == "acknowledge"


def test_bulk_ack_service_aliases():
    assert bulk_ack_mod.bulk_ack is bulk_ack_events
    assert bulk_ack_mod.bulk_service is bulk_ack_mod.bulk_ack_service
    assert callable(bulk_ack_mod.bulk_acknowledge)
    assert callable(bulk_ack_mod.bulk_escalate)


def test_bulk_ack_service_acknowledge():
    event_ack_mod.clear()
    result = bulk_acknowledge(event_ids=[101, 102], actor="op", reason="seen")
    assert result["ok"] is True
    assert result["updated_count"] == 2
    assert sorted(result["updated_event_ids"]) == [101, 102]
    assert result["per_event"]["101"]["acknowledged"] is True
    for eid in (101, 102):
        state = event_ack_mod.get_status(eid)
        assert state is not None
        assert state["acknowledged"] is True
        assert state["status"] == "acknowledged"


def test_bulk_ack_service_escalate():
    event_ack_mod.clear()
    result = bulk_escalate(event_ids=[201, 202], actor="op2", reason="urgent", level="high")
    assert result["updated_count"] == 2
    assert result["action"] == "escalate"
    for eid in (201, 202):
        state = event_ack_mod.get_status(eid)
        assert state["escalated"] is True
        assert state["acknowledged"] is True
        assert state["status"] == "escalated"


def test_bulk_ack_service_generic_dispatch():
    event_ack_mod.clear()
    r1 = bulk_ack_events(event_ids=[301], action="acknowledge", actor="a")
    assert r1["action"] == "acknowledge"
    assert event_ack_mod.is_acknowledged(301) is True
    r2 = bulk_ack_events(event_ids=[302], action="escalate", level="high")
    assert r2["action"] == "escalate"
    assert event_ack_mod.is_escalated(302) is True


def test_bulk_ack_service_validation():
    event_ack_mod.clear()
    try:
        bulk_acknowledge(event_ids=[])
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "event_ids" in str(exc)
    try:
        bulk_ack_events(event_ids=None)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "event_ids" in str(exc)


def test_bulk_ack_router_validation():
    event_ack_mod.clear()
    a = FastAPI()
    a.include_router(bulk_ack_router)
    client = TestClient(a)
    # missing event_ids -> 400 without touching state
    r = client.post("/api/events/bulk-acknowledge", json={"actor": "op"})
    assert r.status_code == 400, r.text
    # empty ids -> 400
    r2 = client.post("/api/events/bulk-ack", json={"event_ids": []})
    assert r2.status_code == 400, r2.text
    # escalate without ids -> 400
    r3 = client.post("/api/events/bulk-escalate", json={"level": "high"})
    assert r3.status_code == 400, r3.text


def test_bulk_ack_router_flow():
    event_ack_mod.clear()
    a = FastAPI()
    a.include_router(bulk_ack_router)
    client = TestClient(a)

    r = client.post("/api/events/bulk-acknowledge", json={"event_ids": [401, 402], "actor": "op", "reason": "seen"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("updated_count") == 2
    assert sorted(body.get("updated_event_ids", [])) == [401, 402]

    # underscore alias also works with ids alias
    r2 = client.post("/api/events/bulk_ack", json={"ids": [403], "actor": "op2"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["updated_event_ids"] == [403]

    # generic bulk-ack with explicit escalate action
    r3 = client.post("/api/events/bulk-ack", json={"event_ids": [401], "action": "escalate", "level": "high"})
    assert r3.status_code == 200, r3.text
    assert r3.json()["action"] == "escalate"

    assert event_ack_mod.is_acknowledged(402) is True
    assert event_ack_mod.is_escalated(401) is True

    # dedicated escalate endpoint
    r4 = client.post("/api/events/bulk-escalate", json={"event_ids": [402], "reason": "urgent"})
    assert r4.status_code == 200, r4.text
    assert event_ack_mod.is_escalated(402) is True


def test_bulk_ack_csv_and_alias_ids():
    event_ack_mod.clear()
    a = FastAPI()
    a.include_router(bulk_ack_router)
    client = TestClient(a)
    r = client.post("/api/events/bulk-acknowledge", json={"event_ids": "501,502", "actor": "op"})
    assert r.status_code == 200, r.text
    assert r.json()["updated_count"] == 2
    assert event_ack_mod.is_acknowledged(501) is True
    assert event_ack_mod.is_acknowledged(502) is True
