from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.event_ack import router as event_ack_router  # noqa: F401
from app.services import event_ack as event_ack_service_mod  # noqa: F401
from app.services.event_ack import (  # noqa: F401
    acknowledge,
    escalate,
    get_status,
    get_state,
    clear,
    event_ack_service,
)


def test_event_ack_import():
    import app.api.event_ack  # noqa: F401
    import app.services.event_ack  # noqa: F401
    from app.api.event_ack import router  # noqa: F401
    from app.services.event_ack import acknowledge, escalate  # noqa: F401


def test_event_ack_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any("acknowledge" in p for p in paths), paths
    assert any("escalate" in p for p in paths), paths


def test_event_ack_service_acknowledge():
    event_ack_service_mod.clear()
    state = acknowledge(event_id=1, actor="op")
    assert state["event_id"] == 1
    assert state["acknowledged"] is True
    assert state["status"] == "acknowledged"

    fetched = get_status(1)
    assert fetched is not None
    assert fetched["acknowledged"] is True


def test_event_ack_service_escalate():
    event_ack_service_mod.clear()
    state = escalate(event_id=2, reason="urgent", level="high")
    assert state["event_id"] == 2
    assert state["escalated"] is True
    assert state["status"] == "escalated"
    # Escalation implies acknowledgement.
    assert state["acknowledged"] is True

    fetched = get_state(event_id=2)
    assert fetched is not None
    assert fetched["escalated"] is True


def test_event_ack_api_event_scoped():
    event_ack_service_mod.clear()
    a = FastAPI()
    a.include_router(event_ack_router)
    client = TestClient(a)

    r = client.post("/api/events/42/acknowledge", json={"actor": "op"})
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["event_id"] == 42
    assert created["acknowledged"] is True

    r2 = client.post("/api/events/42/escalate", json={"reason": "needs review"})
    assert r2.status_code == 200, r2.text
    escalated = r2.json()
    assert escalated["escalated"] is True
    assert escalated["status"] == "escalated"

    s = client.get("/api/events/42/ack-status")
    assert s.status_code == 200
    assert s.json()["event_id"] == 42

    bad = client.post("/api/events/43/escalate", json={})
    # empty body still escalates (event_id from path)
    assert bad.status_code == 200, bad.text


def test_event_ack_api_top_level():
    event_ack_service_mod.clear()
    a = FastAPI()
    a.include_router(event_ack_router)
    client = TestClient(a)

    r = client.post("/api/event-ack/acknowledge", json={"event_id": 7})
    assert r.status_code == 200, r.text
    assert r.json()["event_id"] == 7

    r2 = client.post("/api/event-ack/escalate", json={"event_id": 7, "level": "high"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["escalated"] is True

    bad = client.post("/api/event-ack/acknowledge", json={})
    assert bad.status_code == 400
