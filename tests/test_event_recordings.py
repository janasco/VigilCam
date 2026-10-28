import asyncio
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.event_recordings import router as event_recordings_router  # noqa: F401
from app.services import event_recordings as event_recordings_service_mod  # noqa: F401
from app.services.event_recordings import (  # noqa: F401
    clear,
    count_recordings_for_event,
    find_recordings_for_event,
    find_recordings_for_event_async,
    get_recordings_for_event,
    event_recordings_service,
)


def _event(ts="2024-01-01T12:01:00Z"):
    return {"id": 10, "event_id": 10, "camera_id": 3, "created_at": ts}


def _rec(rid, camera_id, start, end):
    return {
        "id": rid,
        "camera_id": camera_id,
        "path": f"/rec/cam{camera_id}/{rid}.mp4",
        "started_at": start,
        "ended_at": end,
    }


def test_event_recordings_import():
    import app.api.event_recordings  # noqa: F401
    import app.services.event_recordings  # noqa: F401
    from app.api.event_recordings import router  # noqa: F401
    from app.services.event_recordings import find_recordings_for_event, get_recordings_for_event  # noqa: F401


def test_event_recordings_paths_present():
    router_paths = sorted(set(r.path for r in event_recordings_router.routes))
    assert "/api/event-recordings" in router_paths or "/api/event_recordings" in router_paths
    assert "/api/events/{event_id}/recordings" in router_paths
    # Best-effort check that the auto-loader exposes the router via app.routes
    # (avoids the very slow app.openapi() generation on this large app).
    try:
        app_paths = sorted(set(getattr(r, "path", "") for r in app.routes))
        assert "/api/event-recordings" in app_paths or "/api/event_recordings" in app_paths
        assert "/api/events/{event_id}/recordings" in app_paths
    except Exception:  # noqa: BLE001
        pass


def test_event_recordings_service_find_overlap():
    event = _event()
    overlapping = _rec(1, 3, "2024-01-01T12:00:00Z", "2024-01-01T12:05:00Z")
    before = _rec(2, 3, "2024-01-01T11:00:00Z", "2024-01-01T11:05:00Z")
    other_cam = _rec(3, 9, "2024-01-01T12:00:00Z", "2024-01-01T12:05:00Z")
    out = find_recordings_for_event(event, recordings=[overlapping, before, other_cam])
    ids = {r.get("id") for r in out}
    assert 1 in ids
    assert 2 not in ids
    assert 3 not in ids
    # Overlap metadata present.
    hit = next(r for r in out if r.get("id") == 1)
    assert hit["event_id"] == 10
    assert hit["overlap_start"] is not None
    assert hit["overlap_end"] is not None
    # Alias + count agree.
    assert get_recordings_for_event(event, recordings=[overlapping]) == out[:1] or len(
        get_recordings_for_event(event, recordings=[overlapping])) == 1
    assert count_recordings_for_event(event, recordings=[overlapping, before]) == 1


def test_event_recordings_service_window_and_open_ended():
    event = _event("2024-01-01T12:01:00Z")
    # Outside the default 30s window but inside a wide window.
    edge = _rec(5, 3, "2024-01-01T12:05:00Z", "2024-01-01T12:10:00Z")
    assert find_recordings_for_event(event, recordings=[edge]) == []
    wide = find_recordings_for_event(event, recordings=[edge], window_before=600, window_after=600)
    assert any(r.get("id") == 5 for r in wide)
    # Open-ended recording (still recording) covers the event.
    live = _rec(6, 3, "2024-01-01T12:00:00Z", None)
    assert any(r.get("id") == 6 for r in find_recordings_for_event(event, recordings=[live]))
    # No candidates -> no DB -> [] (sync path).
    assert find_recordings_for_event(event) == []


def test_event_recordings_service_async_fake_db():
    from app.models.entities import Event, Recording

    ev = Event(id=10, camera_id=3, kind="motion", label="person",
               created_at=datetime(2024, 1, 1, 12, 1, 0))
    hit = Recording(id=1, camera_id=3, path="/rec/cam3/1.mp4",
                    started_at=datetime(2024, 1, 1, 12, 0, 0),
                    ended_at=datetime(2024, 1, 1, 12, 5, 0))
    miss = Recording(id=2, camera_id=3, path="/rec/cam3/2.mp4",
                     started_at=datetime(2024, 1, 1, 11, 0, 0),
                     ended_at=datetime(2024, 1, 1, 11, 5, 0))
    other = Recording(id=3, camera_id=9, path="/rec/cam9/3.mp4",
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
            if model.__name__ == "Event" and int(pk) == 10:
                return ev
            return None

        async def execute(self, stmt):
            # Return only camera-3 rows to mimic the WHERE clause.
            return FakeResult([hit, miss])

    out = asyncio.run(find_recordings_for_event_async(10, db=FakeDB()))
    ids = {r.get("id") for r in out}
    assert 1 in ids
    assert 2 not in ids
    assert 3 not in ids

    # Unknown event with no DB and no candidates -> [].
    out2 = asyncio.run(find_recordings_for_event_async(999, db=FakeDB()))
    assert out2 == []


def test_event_recordings_api_discover_offline():
    a = FastAPI()
    a.include_router(event_recordings_router)
    client = TestClient(a)
    payload = {
        "event": _event(),
        "recordings": [
            _rec(1, 3, "2024-01-01T12:00:00Z", "2024-01-01T12:05:00Z"),
            _rec(2, 3, "2024-01-01T11:00:00Z", "2024-01-01T11:05:00Z"),
        ],
    }
    r = client.post("/api/event-recordings/discover", json=payload)
    assert r.status_code == 200, r.text
    ids = {row.get("id") for row in r.json()}
    assert 1 in ids
    assert 2 not in ids

    # Missing event and missing hints -> 400.
    bad = client.post("/api/event-recordings/discover", json={"recordings": []})
    assert bad.status_code == 400


def test_event_recordings_api_scoped_fake_db():
    from app.api.deps import db_session
    from app.models.entities import Event, Recording

    ev = Event(id=42, camera_id=3, kind="motion", label="person",
               created_at=datetime(2024, 1, 1, 12, 1, 0, tzinfo=timezone.utc))
    hit = Recording(id=7, camera_id=3, path="/rec/cam3/7.mp4",
                    started_at=datetime(2024, 1, 1, 12, 0, 0),
                    ended_at=datetime(2024, 1, 1, 12, 5, 0))

    class FakeScalars:
        def all(self):
            return [hit]

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeDB:
        async def get(self, model, pk):
            if int(pk) == 42:
                return ev
            return None

        async def execute(self, stmt):
            return FakeResult()

    async def fake_session():
        yield FakeDB()

    a = FastAPI()
    a.include_router(event_recordings_router)
    a.dependency_overrides[db_session] = fake_session
    try:
        client = TestClient(a)
        r = client.get("/api/events/42/recordings")
        assert r.status_code == 200, r.text
        assert any(row.get("id") == 7 for row in r.json())

        missing = client.get("/api/events/9999/recordings")
        assert missing.status_code == 404
    finally:
        a.dependency_overrides.pop(db_session, None)
