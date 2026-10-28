from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.event_merge import router as event_merge_router  # noqa: F401
from app.services import event_merge as event_merge_service_mod  # noqa: F401
from app.services.event_merge import (  # noqa: F401
    auto_merge,
    event_merge_service,
    group_events_for_merge,
    merge_event_dicts,
    merge_events,
    split_event,
    split_event_dict,
    suggest_merges,
)


def _ev(eid, camera=1, minutes=0, tags=None, label="motion"):
    return {
        "id": eid,
        "event_id": eid,
        "camera_id": camera,
        "kind": "motion",
        "label": label,
        "confidence": 0.5 + eid * 0.01,
        "payload": None,
        "tags": tags or [],
        "created_at": (datetime(2026, 1, 1, 12, 0, 0) + timedelta(minutes=minutes)).isoformat(),
    }


def test_event_merge_imports():
    import app.api.event_merge  # noqa: F401
    import app.services.event_merge  # noqa: F401
    from app.api.event_merge import router  # noqa: F401
    from app.services.event_merge import merge_events, split_event  # noqa: F401


def test_event_merge_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/events/merge" in paths
    assert "/api/events/split" in paths


def test_merge_event_dicts_basic():
    e1 = _ev(1, minutes=0, tags=["a"])
    e2 = _ev(2, minutes=1, tags=["b"])
    merged = merge_event_dicts([e1, e2])
    assert merged["merged_from"] == [1, 2]
    assert merged["merge_count"] == 2
    assert set(merged["tags"]) == {"a", "b"}
    # earliest created kept
    assert merged["created_at"] == e1["created_at"]
    # confidence is max
    assert merged["confidence"] == max(e1["confidence"], e2["confidence"])


def test_merge_events_flexible_inputs():
    e1 = _ev(10, minutes=0)
    e2 = _ev(11, minutes=2)
    m1 = merge_events([e1, e2])
    assert m1["merge_count"] == 2
    m2 = merge_events(events=[e1, e2])
    assert m2["merge_count"] == 2
    m3 = merge_events(event_ids=[10, 11])
    assert m3["merge_count"] == 2
    assert m3["merged_from"] == [10, 11]


def test_split_event_dict_basic():
    base = _ev(5, minutes=0)
    parts = split_event_dict(base, count=3)
    assert len(parts) == 3
    assert parts[0]["split_from"] == 5
    assert parts[0]["total_parts"] == 3
    assert parts[2]["part_number"] == 3


def test_split_event_flexible():
    parts = split_event({"id": 7, "event_id": 7, "camera_id": 1,
                         "kind": "motion", "label": "motion"}, count=2)
    assert len(parts) == 2
    parts2 = split_event(event_id=7, count=4)
    assert len(parts2) == 4


def test_group_events_for_merge():
    events = [_ev(1, minutes=0), _ev(2, minutes=1), _ev(3, minutes=30)]
    groups = group_events_for_merge(events, gap_seconds=120)
    assert len(groups) == 1
    assert len(groups[0]) == 2
    # different camera breaks group
    events2 = [_ev(1, camera=1, minutes=0), _ev(2, camera=2, minutes=1)]
    assert group_events_for_merge(events2, gap_seconds=120) == []


def test_suggest_and_auto_merge():
    events = [_ev(1, minutes=0), _ev(2, minutes=1), _ev(3, minutes=30)]
    suggestions = suggest_merges(events=events, gap_seconds=120)
    assert len(suggestions) == 1
    assert suggestions[0]["event_ids"] == [1, 2]
    merged = auto_merge(events=events, gap_seconds=120)
    assert len(merged) == 1
    assert merged[0]["merge_count"] == 2


def test_event_merge_api_pure():
    a = FastAPI()
    a.include_router(event_merge_router)
    client = TestClient(a)

    e1 = _ev(101, minutes=0, tags=["x"])
    e2 = _ev(102, minutes=1, tags=["y"])

    r = client.post("/api/events/merge", json={"events": [e1, e2]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["merged"]["merge_count"] == 2

    s = client.post("/api/events/split", json={"event": e1, "count": 3})
    assert s.status_code == 200, s.text
    assert s.json()["count"] == 3
    assert len(s.json()["parts"]) == 3

    g = client.post("/api/event-merge", json={"events": [e1, e2]})
    assert g.status_code == 200, g.text

    sp = client.post("/api/events/101/split", json={"event": e1, "count": 2})
    assert sp.status_code == 200, sp.text
    assert sp.json()["count"] == 2

    missing = client.post("/api/events/999999/split", json={"count": 2})
    assert missing.status_code == 404

    bad = client.post("/api/events/merge", json={"event_ids": [999]})
    assert bad.status_code == 400
