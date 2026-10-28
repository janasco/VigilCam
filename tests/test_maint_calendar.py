from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.maint_calendar import router as maint_calendar_router
from app.services import maint_calendar
from app.services import maintenance


def _fresh():
    maintenance.clear()


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def test_imports():
    import app.services.maint_calendar  # noqa: F401
    import app.api.maint_calendar  # noqa: F401
    from app.services.maint_calendar import (  # noqa: F401
        get_calendar,
        build_month_calendar,
        aggregate_daily,
        aggregate_windows,
        daily_counts,
        per_day_counts,
        maint_calendar_service,
        MaintCalendarService,
    )
    from app.api.maint_calendar import router  # noqa: F401


def test_flat_list_has_no_calendar_aggregation():
    # Premise: maintenance.list_windows is a flat list with no calendar grid.
    _fresh()
    now = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
    maintenance.create_window(camera_id="cam1", start=_iso(now), end=_iso(now + timedelta(hours=2)))
    items = maintenance.list_windows()
    assert isinstance(items, list)
    assert len(items) == 1
    for forbidden in ("by_date", "by_day", "month_key", "daily", "daily_counts", "days"):
        assert forbidden not in items[0], forbidden
    assert not hasattr(maintenance, "get_calendar")
    assert not hasattr(maintenance, "build_month_calendar")
    assert not hasattr(maintenance, "aggregate_daily")


def test_aggregate_daily_points():
    counts = maint_calendar.aggregate_daily([
        datetime(2026, 10, 5, 10, 0, 0),
        datetime(2026, 10, 5, 12, 0, 0),
        datetime(2026, 10, 6, 9, 0, 0),
    ])
    assert counts.get("2026-10-05") == 2
    assert counts.get("2026-10-06") == 1
    assert maint_calendar.count_by_day([datetime(2026, 10, 5)])["2026-10-05"] == 1


def test_build_month_calendar_expands_ranges():
    _fresh()
    # Window spanning Oct 5 midday -> Oct 6 midday covers both UTC days.
    maintenance.create_window(
        camera_id="cam1",
        start=_iso(datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)),
        end=_iso(datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)),
    )
    out = maint_calendar.build_month_calendar(2026, 10)
    assert out.get("year") == 2026
    assert out.get("month") == 10
    assert out.get("ok") is True
    assert out.get("month_key") == "2026-10"
    days = out.get("days") or []
    assert len(days) == 31
    assert out.get("num_days") == 31
    by_date = out.get("by_date") or {}
    assert by_date["2026-10-05"]["windows"] == 1
    assert by_date["2026-10-06"]["windows"] == 1
    assert by_date["2026-10-07"]["windows"] == 0
    assert out.get("total_windows") == 1
    assert out.get("active_days") == 2
    assert out.get("total_downtime_seconds", 0) > 0


def test_calendar_camera_filter_includes_global():
    _fresh()
    maintenance.create_window(
        camera_id="*",
        start=_iso(datetime(2026, 10, 5, 0, 0, 0, tzinfo=timezone.utc)),
        end=_iso(datetime(2026, 10, 5, 23, 0, 0, tzinfo=timezone.utc)),
    )
    maintenance.create_window(
        camera_id="other",
        start=_iso(datetime(2026, 10, 5, 0, 0, 0, tzinfo=timezone.utc)),
        end=_iso(datetime(2026, 10, 5, 23, 0, 0, tzinfo=timezone.utc)),
    )
    out = maint_calendar.get_calendar(year=2026, month=10, camera_id="cam1")
    by_date = out.get("by_date") or {}
    # Global window matches cam1; "other" camera window does not.
    assert by_date["2026-10-05"]["windows"] == 1
    out_all = maint_calendar.get_calendar(year=2026, month=10)
    assert (out_all.get("by_date") or {})["2026-10-05"]["windows"] == 2


def test_disabled_excluded_by_default():
    _fresh()
    w = maintenance.create_window(
        camera_id="camD",
        start=_iso(datetime(2026, 10, 8, 0, 0, 0, tzinfo=timezone.utc)),
        end=_iso(datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)),
    )
    maintenance.disable(id=w["id"])
    out = maint_calendar.get_calendar(year=2026, month=10, camera_id="camD")
    assert (out.get("by_date") or {})["2026-10-08"]["windows"] == 0
    out2 = maint_calendar.get_calendar(year=2026, month=10, camera_id="camD", include_disabled=True)
    assert (out2.get("by_date") or {})["2026-10-08"]["windows"] == 1


def test_calendar_aliases():
    _fresh()
    out = maint_calendar.calendar(year=2026, month=10)
    assert out.get("month_key") == "2026-10"
    assert len(out.get("days") or []) == 31
    out2 = maint_calendar.daily_counts(year=2026, month=10)
    assert out2.get("month_key") == "2026-10"
    out3 = maint_calendar.per_day_counts(year=2026, month=10)
    assert out3.get("total_windows") == 0


def test_api_calendar_empty_month():
    _fresh()
    _app = FastAPI()
    _app.include_router(maint_calendar_router)
    client = TestClient(_app)
    r = client.get("/api/maint_calendar", params={"year": 2026, "month": 10})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("year") == 2026
    assert data.get("month") == 10
    assert isinstance(data.get("days"), list)
    assert len(data["days"]) == 31

    r2 = client.post("/api/maint_calendar", json={"year": 2026, "month": 10})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("month_key") == "2026-10"


def test_api_calendar_with_window():
    _fresh()
    maintenance.create_window(
        camera_id="api1",
        start=_iso(datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)),
        end=_iso(datetime(2026, 10, 5, 14, 0, 0, tzinfo=timezone.utc)),
    )
    _app = FastAPI()
    _app.include_router(maint_calendar_router)
    client = TestClient(_app)
    r = client.get("/api/maint_calendar", params={"year": 2026, "month": 10, "camera_id": "api1"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("by_date", {}).get("2026-10-05", {}).get("windows", 0) >= 1
    assert data.get("total_windows", 0) >= 1

    # Camera-scoped path.
    r2 = client.get("/api/maint_calendar/cameras/api1", params={"year": 2026, "month": 10})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("by_date", {}).get("2026-10-05", {}).get("windows", 0) >= 1


def test_router_autoregistered_in_main_app():
    # app.main auto-includes every app.api module's `router`: no main.py edit needed.
    from app.main import app as main_app

    paths = list(main_app.openapi()["paths"].keys())
    assert "/api/maint_calendar" in paths
    assert any("maint" in p and "calendar" in p for p in paths)
