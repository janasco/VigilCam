from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.incidents import router as incidents_router  # noqa: F401
from app.services import incidents as incidents_service_mod  # noqa: F401
from app.services.incidents import (  # noqa: F401
    build_incident,
    correlate_events,
    group_events_into_incidents,
    incident_grouper,
    suggest_incidents,
)


def _ev(eid, camera=1, minutes=0, label="motion"):
    return {
        "id": eid,
        "event_id": eid,
        "camera_id": camera,
        "kind": "motion",
        "label": label,
        "confidence": 0.7,
        "payload": None,
        "tags": [],
        "created_at": (datetime(2026, 1, 1, 12, 0, 0) + timedelta(minutes=minutes)).isoformat(),
    }


def test_incidents_imports():
    import app.api.incidents  # noqa: F401
    import app.services.incidents  # noqa: F401
    from app.api.incidents import router  # noqa: F401
    from app.services.incidents import correlate_events, group_events_into_incidents  # noqa: F401


def test_incidents_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/incidents" in paths
    assert "/api/incidents/correlate" in paths


def test_group_across_cameras():
    events = [
        _ev(1, camera=1, minutes=0),
        _ev(2, camera=2, minutes=1),
        _ev(3, camera=1, minutes=30),
    ]
    groups = group_events_into_incidents(events, window_seconds=120)
    assert len(groups) == 1
    assert len(groups[0]) == 2
    cams = {e["camera_id"] for e in groups[0]}
    assert cams == {1, 2}


def test_single_camera_excluded_by_default():
    events = [_ev(1, camera=1, minutes=0), _ev(2, camera=1, minutes=1)]
    assert group_events_into_incidents(events, window_seconds=120) == []
    # opt-in to single-camera grouping
    groups = group_events_into_incidents(events, window_seconds=120, min_cameras=1)
    assert len(groups) == 1


def test_build_incident_fields():
    group = [_ev(1, camera=1, minutes=0), _ev(2, camera=2, minutes=2)]
    inc = build_incident(group, incident_id=1)
    assert inc["incident_id"] == 1
    assert inc["event_ids"] == [1, 2]
    assert inc["camera_count"] == 2
    assert set(inc["cameras"]) == {1, 2}
    assert inc["event_count"] == 2
    assert inc["started_at"] is not None
    assert inc["ended_at"] is not None
    assert inc["duration_seconds"] >= 0


def test_correlate_flexible_inputs():
    e1 = _ev(10, camera=1, minutes=0)
    e2 = _ev(11, camera=2, minutes=1)
    assert len(correlate_events([e1, e2])) == 1
    assert len(correlate_events(events=[e1, e2])) == 1
    out = suggest_incidents(events=[e1, e2], window_seconds=120)
    assert len(out) == 1
    assert out[0]["camera_count"] == 2
    # far apart: no incident
    e3 = _ev(12, camera=2, minutes=60)
    assert correlate_events([e1, e3], window_seconds=120) == []


def test_incidents_api_pure():
    a = FastAPI()
    a.include_router(incidents_router)
    client = TestClient(a)

    e1 = _ev(101, camera=1, minutes=0)
    e2 = _ev(102, camera=2, minutes=1)

    r = client.post("/api/incidents/correlate", json={"events": [e1, e2]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["count"] == 1
    assert body["incidents"][0]["camera_count"] == 2

    g = client.post("/api/incidents", json={"events": [e1, e2]})
    assert g.status_code == 200, g.text
    assert g.json()["count"] == 1

    # single-camera events produce zero incidents (not a merge)
    s1 = _ev(201, camera=1, minutes=0)
    s2 = _ev(202, camera=1, minutes=1)
    r2 = client.post("/api/incidents/correlate", json={"events": [s1, s2]})
    assert r2.status_code == 200, r2.text
    assert r2.json()["count"] == 0
