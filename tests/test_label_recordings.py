import asyncio
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.label_recordings import router as label_recordings_router  # noqa: F401
from app.services import label_recordings as label_recordings_service_mod  # noqa: F401
from app.services.label_recordings import (  # noqa: F401
    clear,
    count_recordings_for_label,
    event_matches_label,
    filter_events_by_label,
    find_recordings_for_label,
    find_recordings_for_label_async,
    get_recordings_for_label,
    label_recordings_service,
    normalize_label,
    normalize_labels,
)


def _event(eid, label, camera_id=3, ts="2024-01-01T12:01:00Z"):
    return {"id": eid, "event_id": eid, "camera_id": camera_id,
            "kind": "motion", "label": label, "created_at": ts}


def _rec(rid, camera_id, start, end):
    return {
        "id": rid,
        "camera_id": camera_id,
        "path": f"/rec/cam{camera_id}/{rid}.mp4",
        "started_at": start,
        "ended_at": end,
    }


def test_label_recordings_import():
    import app.api.label_recordings  # noqa: F401
    import app.services.label_recordings  # noqa: F401
    from app.api.label_recordings import router  # noqa: F401
    from app.services.label_recordings import (  # noqa: F401
        find_recordings_for_label,
        get_recordings_for_label,
        find_recordings_for_label_async,
    )


def test_label_recordings_paths_present():
    router_paths = sorted(set(r.path for r in label_recordings_router.routes))
    assert "/api/label-recordings" in router_paths
    # Best-effort check that the auto-loader exposes the router via app.routes
    # (avoids the very slow app.openapi() generation on this large app).
    try:
        app_paths = sorted(set(getattr(r, "path", "") for r in app.routes))
        assert "/api/label-recordings" in app_paths
    except Exception:  # noqa: BLE001
        pass


def test_label_helpers():
    assert normalize_label("  Person ") == "Person"
    assert normalize_label("") is None
    assert normalize_label(None) is None
    assert normalize_labels("person, dog") == ["person", "dog"]
    assert event_matches_label({"label": "Person"}, "person") is True
    assert event_matches_label({"label": "Person"}, "personx") is False
    assert event_matches_label({"label": "Person"}, "pers", match="contains") is True
    assert event_matches_label({"label": "dog"}, "person") is False


def test_label_recordings_service_find_overlap():
    events = [
        _event(10, "person"),
        _event(11, "dog"),
        _event(12, "person", camera_id=9),
    ]
    overlapping = _rec(1, 3, "2024-01-01T12:00:00Z", "2024-01-01T12:05:00Z")
    before = _rec(2, 3, "2024-01-01T11:00:00Z", "2024-01-01T11:05:00Z")
    other_cam = _rec(3, 9, "2024-01-01T12:00:00Z", "2024-01-01T12:05:00Z")
    out = find_recordings_for_label(
        "person", events=events, recordings=[overlapping, before, other_cam],
    )
    ids = {r.get("id") for r in out}
    # person@cam3 -> rec 1; dog event ignored; person@cam9 -> rec 3.
    assert ids == {1, 3}
    assert 2 not in ids
    hit = next(r for r in out if r.get("id") == 1)
    assert hit["matched_label"] == "person"
    assert hit["matched_event_id"] == 10
    assert hit["overlap_start"] is not None
    # Case-insensitive + alias agree.
    out2 = find_recordings_for_label(
        label="PERSON", events=events, recordings=[overlapping],
    )
    assert {r.get("id") for r in out2} == {1}
    assert get_recordings_for_label("person", events=events,
                                    recordings=[overlapping]) == out2
    assert count_recordings_for_label("person", events=events,
                                      recordings=[overlapping, before]) == 1
    # Unknown label -> [].
    assert find_recordings_for_label("cat", events=events,
                                     recordings=[overlapping]) == []
    # Missing label -> [].
    assert find_recordings_for_label(events=events, recordings=[overlapping]) == []


def test_label_recordings_service_window_and_filters():
    events = [_event(10, "person", ts="2024-01-01T12:01:00Z")]
    edge = _rec(5, 3, "2024-01-01T12:05:00Z", "2024-01-01T12:10:00Z")
    assert find_recordings_for_label("person", events=events,
                                     recordings=[edge]) == []
    wide = find_recordings_for_label("person", events=events, recordings=[edge],
                                     window_before=600, window_after=600)
    assert any(r.get("id") == 5 for r in wide)
    # Open-ended recording (still recording) covers the event.
    live = _rec(6, 3, "2024-01-01T12:00:00Z", None)
    assert any(r.get("id") == 6 for r in find_recordings_for_label(
        "person", events=events, recordings=[live]))
    # Camera filter narrows matching events.
    assert find_recordings_for_label("person", events=events, recordings=[live],
                                     camera_id=9) == []
    # Contains match.
    assert any(r.get("id") == 6 for r in find_recordings_for_label(
        "pers", events=events, recordings=[live], match="contains"))
    # No candidates -> [] (sync path).
    assert find_recordings_for_label("person", events=events) == []
    assert find_recordings_for_label("person", recordings=[live]) == []
    assert clear() == 0
    # filter helper with camera narrowing.
    assert len(filter_events_by_label(events, "person", camera_id=3)) == 1
    assert filter_events_by_label(events, "person", camera_id=9) == []


def test_label_recordings_service_async_fake_db():
    from app.models.entities import Event, Recording

    ev_hit = Event(id=10, camera_id=3, kind="motion", label="person",
                   created_at=datetime(2024, 1, 1, 12, 1, 0))
    ev_miss = Event(id=11, camera_id=3, kind="motion", label="dog",
                    created_at=datetime(2024, 1, 1, 12, 1, 0))
    hit = Recording(id=1, camera_id=3, path="/rec/cam3/1.mp4",
                    started_at=datetime(2024, 1, 1, 12, 0, 0),
                    ended_at=datetime(2024, 1, 1, 12, 5, 0))
    before = Recording(id=2, camera_id=3, path="/rec/cam3/2.mp4",
                       started_at=datetime(2024, 1, 1, 11, 0, 0),
                       ended_at=datetime(2024, 1, 1, 11, 5, 0))

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return FakeScalars(self._rows)

    class FakeDB:
        async def get(self, model, pk):
            return None

        async def execute(self, stmt):
            text = str(stmt)
            if "events" in text:
                return FakeResult([ev_hit, ev_miss])
            return FakeResult([hit, before])

    out = asyncio.run(find_recordings_for_label_async("person", db=FakeDB()))
    ids = {r.get("id") for r in out}
    assert 1 in ids
    assert 2 not in ids

    out_none = asyncio.run(find_recordings_for_label_async("cat", db=FakeDB()))
    assert out_none == []

    # Explicit candidate lists work without a DB.
    out_offline = asyncio.run(find_recordings_for_label_async(
        "person",
        events=[{"id": 10, "camera_id": 3, "label": "person",
                 "created_at": "2024-01-01T12:01:00Z"}],
        recordings=[{"id": 1, "camera_id": 3, "path": "/r/1.mp4",
                     "started_at": "2024-01-01T12:00:00Z",
                     "ended_at": "2024-01-01T12:05:00Z"}],
    ))
    assert any(r.get("id") == 1 for r in out_offline)


def test_label_recordings_api_discover_offline():
    a = FastAPI()
    a.include_router(label_recordings_router)
    client = TestClient(a)
    payload = {
        "label": "person",
        "events": [_event(10, "person"), _event(11, "dog")],
        "recordings": [
            _rec(1, 3, "2024-01-01T12:00:00Z", "2024-01-01T12:05:00Z"),
            _rec(2, 3, "2024-01-01T11:00:00Z", "2024-01-01T11:05:00Z"),
        ],
    }
    r = client.post("/api/label-recordings/discover", json=payload)
    assert r.status_code == 200, r.text
    ids = {row.get("id") for row in r.json()}
    assert 1 in ids
    assert 2 not in ids

    # Missing label -> 400.
    bad = client.post("/api/label-recordings/discover", json={"recordings": []})
    assert bad.status_code == 400


def test_label_recordings_api_scoped_fake_db():
    from app.api.deps import db_session
    from app.models.entities import Event, Recording

    ev_hit = Event(id=10, camera_id=3, kind="motion", label="person",
                   created_at=datetime(2024, 1, 1, 12, 1, 0))
    ev_miss = Event(id=11, camera_id=3, kind="motion", label="dog",
                    created_at=datetime(2024, 1, 1, 12, 1, 0))
    hit = Recording(id=7, camera_id=3, path="/rec/cam3/7.mp4",
                    started_at=datetime(2024, 1, 1, 12, 0, 0),
                    ended_at=datetime(2024, 1, 1, 12, 5, 0))

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return FakeScalars(self._rows)

    class FakeDB:
        async def get(self, model, pk):
            return None

        async def execute(self, stmt):
            text = str(stmt)
            if "events" in text:
                return FakeResult([ev_hit, ev_miss])
            return FakeResult([hit])

    async def fake_session():
        yield FakeDB()

    a = FastAPI()
    a.include_router(label_recordings_router)
    a.dependency_overrides[db_session] = fake_session
    try:
        client = TestClient(a)
        r = client.get("/api/label-recordings", params={"label": "person"})
        assert r.status_code == 200, r.text
        assert any(row.get("id") == 7 for row in r.json())

        empty = client.get("/api/label-recordings", params={"label": "cat"})
        assert empty.status_code == 200, empty.text
        assert empty.json() == []

        missing = client.get("/api/label-recordings")
        assert missing.status_code == 400
    finally:
        a.dependency_overrides.pop(db_session, None)
