"""Tests for time-of-day event search (02:00-04:00 style windows).

Note: the router is exercised through an isolated ``FastAPI()`` app (same
pattern as ``tests/test_color_search.py``) rather than ``app.main``.
"""

import asyncio
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.tod_search import router as tod_search_router  # noqa: F401
from app.services import tod_search as tod  # noqa: F401


def _events_at_hours(hours, base=None, **extra):
    base = base or datetime(2026, 1, 1, 0, 0, 0)
    return [
        {"id": i + 1, "camera_id": 1, "label": "person",
         "created_at": base + timedelta(hours=h), **extra}
        for i, h in enumerate(hours)
    ]


def test_tod_search_import():
    import app.api.tod_search  # noqa: F401
    import app.services.tod_search  # noqa: F401
    from app.api.tod_search import router  # noqa: F401
    from app.services.tod_search import (  # noqa: F401
        parse_tod, parse_hour, search_events, search_events_async,
        in_window, event_tod, event_hour,
    )
    assert router is not None


def test_verify_no_hour_filter_elsewhere():
    """Heatmap aggregates per-hour but must not gain a tod filter here.

    Guards the premise: hour-of-day filtering lives only in tod_search.
    """
    import app.services.heatmap as hm
    import inspect

    src = inspect.getsource(hm)
    assert "per_hour" in src  # aggregates by hour
    assert "tod_search" not in src
    assert "def search_events" not in src or "tod" not in src


def test_parse_tod_spellings():
    assert tod.parse_tod("02:00") == 2 * 3600
    assert tod.parse_tod("02:00:30") == 2 * 3600 + 30
    assert tod.parse_tod("2") == 2 * 3600
    assert tod.parse_tod(2) == 2 * 3600
    assert tod.parse_tod("2pm") == 14 * 3600
    assert tod.parse_tod("2am") == 2 * 3600
    assert tod.parse_tod("12am") == 0
    assert tod.parse_tod("12pm") == 12 * 3600
    assert tod.parse_tod("0200") == 2 * 3600
    assert tod.parse_tod(datetime(2026, 5, 4, 3, 15, 0)) == 3 * 3600 + 15 * 60
    assert tod.parse_tod("2026-05-04T03:15:00") == 3 * 3600 + 15 * 60
    assert tod.parse_tod(None) is None
    assert tod.parse_tod("not-a-time") is None
    assert tod.parse_tod("25:00") is None


def test_parse_hour():
    assert tod.parse_hour(2) == 2
    assert tod.parse_hour("02:00") == 2
    assert tod.parse_hour("14:30") == 14
    assert tod.parse_hour(None) is None


def test_search_between_02_and_04():
    events = _events_at_hours([1, 2, 3, 4, 5])
    res = tod.search_events(events, start="02:00", end="04:00")
    assert [e["id"] for e in res] == [2, 3, 4]
    # Enriched with hour / time-of-day fields.
    assert res[0]["hour"] == 2
    assert res[0]["time_of_day"] == "02:00:00"
    assert res[0]["tod_seconds"] == 2 * 3600


def test_search_start_hour_end_hour_ints():
    events = _events_at_hours([1, 2, 3, 4, 5])
    res = tod.search_events(events, start_hour=2, end_hour=4)
    assert [e["id"] for e in res] == [2, 3, 4]


def test_search_time_range_string():
    events = _events_at_hours([1, 2, 3, 4, 5])
    res = tod.search_events(events, time_range="02:00-04:00")
    assert [e["id"] for e in res] == [2, 3, 4]


def test_search_overnight_wrap():
    events = _events_at_hours([21, 22, 23, 0, 1, 2, 3])
    res = tod.search_events(events, start="22:00", end="02:00")
    assert sorted(e["hour"] for e in res) == [0, 1, 2, 22, 23]


def test_search_hours_list_and_single_hour():
    events = _events_at_hours([1, 2, 3, 4, 5])
    assert [e["hour"] for e in tod.search_events(events, hours=[2, 4])] == [2, 4]
    assert [e["hour"] for e in tod.search_events(events, hours="2-3")] == [2, 3]
    base = datetime(2026, 1, 1, 0, 0, 0)
    mixed = [
        {"id": 1, "created_at": base + timedelta(hours=2, minutes=30)},
        {"id": 2, "created_at": base + timedelta(hours=3, minutes=5)},
    ]
    assert [e["id"] for e in tod.search_events(mixed, hour=2)] == [1]


def test_search_no_window_returns_all():
    events = _events_at_hours([1, 9, 17])
    assert len(tod.search_events(events)) == 3


def test_search_iso_string_timestamps():
    events = [
        {"id": 1, "created_at": "2026-01-01T01:30:00"},
        {"id": 2, "created_at": "2026-06-15T02:30:00"},
        {"id": 3, "created_at": "2026-12-31T05:00:00"},
    ]
    res = tod.search_events(events, start="02:00", end="04:00")
    assert [e["id"] for e in res] == [2]


def test_search_skips_missing_timestamps_when_windowed():
    base = datetime(2026, 1, 1, 0, 0, 0)
    events = [
        {"id": 1},
        {"id": 2, "created_at": base + timedelta(hours=3)},
    ]
    assert [e["id"] for e in tod.search_events(events, start="02:00", end="04:00")] == [2]
    assert len(tod.search_events(events)) == 2


def test_search_combined_camera_label_filters():
    base = datetime(2026, 1, 1, 0, 0, 0)
    events = [
        {"id": 1, "camera_id": 1, "label": "person", "created_at": base + timedelta(hours=2)},
        {"id": 2, "camera_id": 2, "label": "person", "created_at": base + timedelta(hours=3)},
        {"id": 3, "camera_id": 1, "label": "car", "created_at": base + timedelta(hours=3, minutes=30)},
    ]
    res = tod.search_events(events, start="02:00", end="04:00", camera_id=1, label="person")
    assert [e["id"] for e in res] == [1]


def test_search_limit_offset():
    events = _events_at_hours([1, 2, 3, 4, 5])
    res = tod.search_events(events, start="00:00", end="23:59", limit=2)
    assert len(res) == 2
    res = tod.search_events(events, start="00:00", end="23:59", offset=4)
    assert len(res) == 1


def test_search_end_exclusive_option():
    events = _events_at_hours([2, 3, 4])
    res = tod.search_events(events, start="02:00", end="04:00", include_end=False)
    assert [e["hour"] for e in res] == [2, 3]


def test_search_async_no_db():
    data = asyncio.run(tod.search_events_async(None, start="02:00", end="04:00"))
    assert data == []


def test_search_async_in_memory_list():
    events = _events_at_hours([1, 2, 3, 4, 5])
    data = asyncio.run(tod.search_events_async(events, start="02:00", end="04:00"))
    assert [e["id"] for e in data] == [2, 3, 4]


def _client():
    app = FastAPI()
    app.include_router(tod_search_router)
    return TestClient(app)


def test_api_router_paths_present():
    paths = sorted({getattr(r, "path", "") for r in tod_search_router.routes})
    assert "/api/tod-search" in paths
    assert "/api/tod_search" in paths
    assert "/api/events/by-tod" in paths
    assert "/api/tod-search/search" in paths
    assert "/api/tod-search/filter" in paths


def test_api_search_with_events_override():
    client = _client()
    body = {
        "start": "02:00",
        "end": "04:00",
        "events": [
            {"id": 1, "camera_id": 1, "label": "person", "created_at": "2026-01-01T01:00:00"},
            {"id": 2, "camera_id": 1, "label": "person", "created_at": "2026-01-01T02:30:00"},
            {"id": 3, "camera_id": 1, "label": "car", "created_at": "2026-01-02T03:15:00"},
            {"id": 4, "camera_id": 1, "label": "car", "created_at": "2026-01-01T05:00:00"},
        ],
    }
    r = client.post("/api/tod-search/search", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert isinstance(out, list)
    assert [e["id"] for e in out] == [2, 3]


def test_api_get_search_query_params():
    client = _client()
    r = client.get("/api/tod-search", params={"start": "02:00", "end": "04:00"})
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)
    r2 = client.get("/api/events/by-tod", params={"start_hour": 2, "end_hour": 4})
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)
