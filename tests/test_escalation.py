import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.escalation import router as escalation_router  # noqa: F401
from app.services import escalation as escalation_mod  # noqa: F401
from app.services import event_ack as event_ack_mod
from app.services.escalation import (
    clear,
    create_policy,
    delete_policy,
    disable_policy,
    due_for_event,
    enable_policy,
    escalation_service,
    evaluate,
    get_policy,
    list_escalations,
    list_policies,
    list_tracked,
    process_due,
    status,
    track_event,
    untrack_event,
    update_policy,
)


def _fresh():
    clear()
    event_ack_mod.clear()


def test_imports():
    import app.services.escalation  # noqa: F401
    import app.api.escalation  # noqa: F401
    from app.api.escalation import router  # noqa: F401
    from app.services.escalation import create_policy, evaluate, process_due  # noqa: F401


def test_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any("escalation" in p for p in paths), paths


def test_policy_crud():
    _fresh()
    policy = create_policy(name="unacked-15", after_minutes=15, escalate_to="sms")
    assert policy["after_minutes"] == 15
    assert policy["escalate_to"] == "sms"
    assert policy["enabled"] is True
    pid = policy["policy_id"]

    fetched = get_policy(pid)
    assert fetched is not None
    assert fetched["policy_id"] == pid

    assert len(list_policies()) == 1

    updated = update_policy(pid, after_minutes=30)
    assert updated["after_minutes"] == 30

    disabled = disable_policy(pid)
    assert disabled["enabled"] is False
    assert list_policies(enabled=True) == []
    enabled = enable_policy(pid)
    assert enabled["enabled"] is True

    assert delete_policy(pid) is True
    assert get_policy(pid) is None
    assert list_policies() == []


def test_policy_validation():
    _fresh()
    try:
        create_policy(name="no-channel", after_minutes=5)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for missing channel")
    try:
        create_policy(name="no-delay", escalate_to="sms")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for missing after_minutes")


def test_unacked_event_becomes_due_after_n_minutes():
    _fresh()
    now = time.time()
    policy = create_policy(name="p1", after_minutes=10, escalate_to="sms")
    track_event(event_id=101, channel="email", now=now)
    # before N minutes: not due
    assert evaluate(now=now + 5 * 60) == []
    assert due_for_event(101, now=now + 5 * 60) == []
    # after N minutes: due with escalation channel
    due = evaluate(now=now + 11 * 60)
    assert len(due) == 1
    assert due[0]["event_id"] == 101
    assert due[0]["policy_id"] == policy["policy_id"]
    assert due[0]["escalate_to"] == "sms"


def test_acked_event_is_not_due():
    _fresh()
    now = time.time()
    create_policy(name="p1", after_minutes=10, escalate_to="webhook")
    track_event(event_id=202, now=now)
    event_ack_mod.acknowledge(event_id=202, actor="op")
    assert evaluate(now=now + 60 * 60) == []
    assert process_due(now=now + 60 * 60) == []


def test_process_due_dedupes_and_records_history():
    _fresh()
    now = time.time()
    create_policy(name="p1", after_minutes=5, escalate_to="sms")
    track_event(event_id=303, now=now)
    fired = process_due(now=now + 6 * 60)
    assert len(fired) == 1
    assert fired[0]["escalate_to"] == "sms"
    # second run does not re-fire the same policy+event
    assert process_due(now=now + 60 * 60) == []
    history = list_escalations()
    assert len(history) == 1
    assert history[0]["event_id"] == 303
    # evaluate is read-only: does not record history
    _fresh()
    create_policy(name="p1", after_minutes=5, escalate_to="sms")
    track_event(event_id=404, now=now)
    assert len(evaluate(now=now + 6 * 60)) == 1
    assert list_escalations() == []


def test_disabled_policy_never_due_and_filters():
    _fresh()
    now = time.time()
    p = create_policy(name="quiet", after_minutes=1, escalate_to="sms", enabled=False)
    track_event(event_id=505, channel="email", severity="high", now=now)
    assert evaluate(now=now + 3600) == []
    enable_policy(p["policy_id"])
    assert len(evaluate(now=now + 3600)) == 1
    # from_channel filter: event raised on sms does not match email-only chain
    _fresh()
    create_policy(name="email-chain", after_minutes=1, escalate_to="sms",
                  from_channel="email")
    track_event(event_id=506, channel="sms", now=now)
    assert evaluate(now=now + 3600) == []
    track_event(event_id=507, channel="email", now=now)
    due = evaluate(now=now + 3600)
    assert [d["event_id"] for d in due] == [507]


def test_untrack_stops_due_and_status():
    _fresh()
    now = time.time()
    create_policy(name="p1", after_minutes=1, escalate_to="sms")
    track_event(event_id=606, now=now)
    assert len(list_tracked()) == 1
    assert untrack_event(606) is True
    assert evaluate(now=now + 3600) == []
    st = status(now=now + 3600)
    assert st["ok"] is True
    assert st["policies"] == 1
    assert st["due_count"] == 0


def test_service_singleton_mirrors_module():
    _fresh()
    now = time.time()
    escalation_service.create_policy(name="p1", after_minutes=5, escalate_to="sms")
    escalation_service.track_event(event_id=707, now=now)
    assert len(escalation_service.evaluate(now=now + 600)) == 1
    assert len(escalation_service.process_due(now=now + 600)) == 1
    assert len(escalation_service.history()) == 1


def test_router_policy_track_evaluate_process_flow():
    _fresh()
    a = FastAPI()
    a.include_router(escalation_router)
    client = TestClient(a)
    now = time.time()

    r = client.post("/api/escalation/policies",
                    json={"name": "r1", "after_minutes": 10, "escalate_to": "sms"})
    assert r.status_code == 200, r.text
    pid = r.json()["policy_id"]

    r = client.post("/api/escalation/track",
                    json={"event_id": 808, "channel": "email", "now": now})
    assert r.status_code == 200, r.text
    assert r.json()["event_id"] == 808

    r = client.get("/api/escalation/due", params={"event_id": 808, "now": now + 11 * 60})
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1
    assert r.json()["items"][0]["escalate_to"] == "sms"

    r = client.post("/api/escalation/process", json={"event_id": 808, "now": now + 11 * 60})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1

    r = client.post("/api/escalation/process", json={"event_id": 808, "now": now + 3600})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0  # deduped

    r = client.get("/api/escalation/history")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1

    r = client.get(f"/api/escalation/policies/{pid}")
    assert r.status_code == 200, r.text
    assert r.json()["policy_id"] == pid

    r = client.delete(f"/api/escalation/policies/{pid}")
    assert r.status_code == 200, r.text

    r = client.get("/api/escalation/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_router_validation_errors():
    _fresh()
    a = FastAPI()
    a.include_router(escalation_router)
    client = TestClient(a)

    r = client.post("/api/escalation/policies", json={"name": "bad"})
    assert r.status_code == 400

    r = client.post("/api/escalation/track", json={})
    assert r.status_code == 400

    r = client.get("/api/escalations/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
