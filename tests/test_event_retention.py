from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.event_retention import router as event_retention_router
from app.services import event_retention as svc


def _old(days_ago: float) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)).isoformat()


def _ev(eid, camera=1, days_ago=0, label="motion"):
    return {
        "id": eid,
        "event_id": eid,
        "camera_id": camera,
        "kind": "motion",
        "label": label,
        "confidence": 0.9,
        "payload": None,
        "tags": [],
        "created_at": (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)).isoformat(),
    }


def test_event_retention_imports():
    import app.api.event_retention  # noqa: F401
    import app.services.event_retention  # noqa: F401
    from app.api.event_retention import router  # noqa: F401
    from app.services.event_retention import (  # noqa: F401
        EventRetentionService,
        event_retention_service,
        prune,
        preview,
        select_for_deletion,
        prune_async,
        get_policy,
    )
    assert event_retention_router is not None
    assert hasattr(svc, "prune")
    assert hasattr(svc, "preview")


def test_event_retention_paths_present():
    routes = [getattr(r, "path", "") for r in event_retention_router.routes]
    assert "/api/events/retention" in routes
    assert "/api/events/retention/preview" in routes
    assert "/api/events/retention/status" in routes


def test_event_retention_age_dry_run():
    entries = [_ev(1, days_ago=30), _ev(2, days_ago=1)]
    before = [dict(e) for e in entries]
    result = svc.prune(entries=entries, retention_days=7, dry_run=True)
    assert result.get("dry_run") is True
    selected = result.get("selected") or result.get("candidates") or []
    ids = {e.get("id") for e in selected if isinstance(e, dict)}
    assert 1 in ids
    assert 2 not in ids
    # dry run removes nothing
    assert len(entries) == len(before)


def test_event_retention_age_deletes_old():
    entries = [_ev(1, days_ago=30), _ev(2, days_ago=1)]
    result = svc.prune(entries=entries, retention_days=7)
    assert result.get("dry_run") is False
    assert result.get("deleted_count") == 1
    remaining_ids = {e.get("id") for e in entries}
    assert remaining_ids == {2}


def test_event_retention_max_count_per_camera():
    entries = [
        _ev(1, camera=1, days_ago=5),
        _ev(2, camera=1, days_ago=4),
        _ev(3, camera=1, days_ago=1),
        _ev(4, camera=2, days_ago=5),
        _ev(5, camera=2, days_ago=1),
    ]
    result = svc.prune(entries=entries, max_count=2, dry_run=True)
    selected = result.get("selected") or []
    ids = {e.get("id") for e in selected if isinstance(e, dict)}
    # camera 1 has 3 events, keep newest 2 -> oldest (id 1) selected
    assert 1 in ids
    assert 2 not in ids
    assert 3 not in ids
    # camera 2 has exactly 2, none selected
    assert 4 not in ids
    assert 5 not in ids


def test_event_retention_per_camera_age_dict():
    entries = [_ev(1, camera=1, days_ago=10), _ev(2, camera=2, days_ago=10)]
    result = svc.prune(
        entries=entries,
        max_age_days_by_camera={"1": 5, "2": 30},
        dry_run=True,
    )
    selected = result.get("selected") or []
    ids = {e.get("id") for e in selected if isinstance(e, dict)}
    assert 1 in ids
    assert 2 not in ids


def test_event_retention_camera_filter():
    entries = [_ev(1, camera=1, days_ago=30), _ev(2, camera=2, days_ago=30)]
    result = svc.prune(entries=entries, retention_days=7, camera_id=1, dry_run=True)
    selected = result.get("selected") or []
    ids = {e.get("id") for e in selected if isinstance(e, dict)}
    assert ids == {1}


def test_event_retention_router_inline():
    app = FastAPI()
    app.include_router(event_retention_router)
    client = TestClient(app)
    payload = {"events": [_ev(1, days_ago=30), _ev(2, days_ago=1)], "retention_days": 7, "dry_run": True}
    r = client.post("/api/events/retention/preview", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("dry_run") is True
    selected = data.get("selected") or data.get("candidates") or []
    assert any((e.get("id") == 1) for e in selected if isinstance(e, dict))
