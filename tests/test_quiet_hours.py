from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.quiet_hours import router as quiet_hours_router
from app.services import quiet_hours
from app.services.quiet_hours import (
    clear,
    create_rule,
    delete_rule,
    get_active,
    get_status,
    is_quiet,
    list_rules,
    quiet_hours_service,
)


def _fresh():
    clear()


def _dt(hour: int, minute: int = 0, day: int = 4) -> datetime:
    # 2026-05-0X is a known weekday set; day=4 -> Mon 2026-05-04.
    return datetime(2026, 5, day, hour, minute, 0, tzinfo=timezone.utc)


def test_imports():
    import app.services.quiet_hours  # noqa: F401
    import app.api.quiet_hours  # noqa: F401
    from app.services.quiet_hours import is_quiet, quiet_hours_service  # noqa: F401


def test_create_and_is_quiet_per_camera():
    _fresh()
    r = create_rule(camera_id="cam1", start_time="22:00", end_time="06:00")
    assert r["ok"] is True
    assert r["recurring"] is True
    assert r["recurrence"] == "daily"
    # Inside window (late night).
    assert is_quiet("cam1", now=_dt(23, 0)) is True
    assert is_quiet(camera_id="cam1", now=_dt(2, 0, day=5)) is True
    # Outside window (midday).
    assert is_quiet("cam1", now=_dt(12, 0)) is False
    # Other camera unaffected.
    assert is_quiet("cam2", now=_dt(23, 0)) is False
    st = get_status(camera_id="cam1", now=_dt(23, 0))
    assert st["quiet"] is True
    assert st["suppressed"] is True
    st2 = get_status(camera_id="cam2", now=_dt(23, 0))
    assert st2["quiet"] is False


def test_recurs_daily_not_one_off():
    # Same wall-clock time matches on different dates -> recurring.
    _fresh()
    create_rule(camera_id="camR", start_time="09:00", end_time="17:00")
    assert is_quiet("camR", now=_dt(10, 0, day=4)) is True
    assert is_quiet("camR", now=_dt(10, 0, day=11)) is True
    assert is_quiet("camR", now=_dt(10, 0, day=18)) is True
    assert is_quiet("camR", now=_dt(20, 0, day=4)) is False


def test_overnight_window_tail_belongs_to_start_day():
    _fresh()
    # Weekday-only overnight rule: early Tuesday morning still quiet
    # because Monday's window runs past midnight.
    create_rule(camera_id="camN", start_time="22:00", end_time="06:00", days="weekdays")
    assert is_quiet("camN", now=_dt(23, 0, day=4)) is True  # Mon night
    assert is_quiet("camN", now=_dt(2, 0, day=5)) is True  # Tue early (Mon's window)
    # Saturday: no window starts Fri? Fri IS a weekday so Sat early is quiet.
    assert is_quiet("camN", now=_dt(2, 0, day=9)) is True  # Sat early (Fri's window)
    # Sunday midday never quiet; Sunday night no window (Sun not weekday).
    assert is_quiet("camN", now=_dt(12, 0, day=10)) is False
    assert is_quiet("camN", now=_dt(23, 0, day=10)) is False


def test_global_rule_quiets_all_cameras():
    _fresh()
    create_rule(camera_id="*", start_time="00:00", end_time="23:59", days="daily")
    assert is_quiet("cam1", now=_dt(12, 0)) is True
    assert is_quiet("anything-else", now=_dt(12, 0)) is True


def test_days_filter():
    _fresh()
    create_rule(camera_id="camW", start_time="09:00", end_time="17:00", days="weekends")
    # 2026-05-04 is a Monday, 2026-05-09 a Saturday.
    assert is_quiet("camW", now=_dt(10, 0, day=4)) is False
    assert is_quiet("camW", now=_dt(10, 0, day=9)) is True


def test_disabled_rule_does_not_suppress():
    _fresh()
    r = create_rule(camera_id="camD", start_time="00:00", end_time="23:59")
    assert is_quiet("camD", now=_dt(12, 0)) is True
    quiet_hours.disable(id=r["id"])
    assert is_quiet("camD", now=_dt(12, 0)) is False
    quiet_hours.enable(id=r["id"])
    assert is_quiet("camD", now=_dt(12, 0)) is True


def test_delete_clears_quiet():
    _fresh()
    r = create_rule(camera_id="camX", start_time="00:00", end_time="23:59")
    assert is_quiet("camX", now=_dt(12, 0)) is True
    out = delete_rule(id=r["id"])
    assert out["deleted"] is True
    assert is_quiet("camX", now=_dt(12, 0)) is False


def test_service_singleton_mirrors_module():
    _fresh()
    quiet_hours_service.create_rule(camera_id="svc1", start_time="20:00", end_time="21:00")
    assert quiet_hours_service.is_quiet(camera_id="svc1", now=_dt(20, 30)) is True
    assert quiet_hours_service.get_status(camera_id="svc1", now=_dt(20, 30))["quiet"] is True
    quiet_hours_service.clear()
    assert quiet_hours_service.is_quiet(camera_id="svc1", now=_dt(20, 30)) is False


def test_distinct_from_maintenance_one_off_windows():
    # Quiet hours recur daily via time-of-day + days; maintenance windows are
    # one-off [start, end] date ranges with epoch start_ts/end_ts.
    _fresh()
    import app.services.maintenance as maintenance

    assert hasattr(quiet_hours, "is_quiet")
    assert hasattr(quiet_hours, "create_rule")
    assert hasattr(maintenance, "is_suppressed")
    assert hasattr(maintenance, "create_window")
    # Storage shape differs: recurring minutes+days vs one-off timestamps.
    rule = create_rule(camera_id="camZ", start_time="22:00", end_time="06:00")
    assert "start_minutes" in rule and "end_minutes" in rule and "days" in rule
    assert "start_ts" not in rule and "end_ts" not in rule
    # Maintenance has no recurring quiet-hours API.
    assert not hasattr(maintenance, "is_quiet")
    assert not hasattr(maintenance, "create_rule")


def test_router_create_check_delete_flow():
    _fresh()
    app = FastAPI()
    app.include_router(quiet_hours_router)
    client = TestClient(app)

    r = client.post("/api/quiet-hours", json={
        "camera_id": "r1",
        "start_time": "22:00",
        "end_time": "06:00",
    })
    assert r.status_code == 200, r.text
    rid = r.json()["id"]

    r = client.get("/api/quiet-hours/check", params={"camera_id": "r1", "now": _dt(23, 0).isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["quiet"] is True

    r = client.get("/api/quiet-hours/check", params={"camera_id": "other", "now": _dt(23, 0).isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["quiet"] is False

    r = client.get(f"/api/quiet-hours/{rid}")
    assert r.status_code == 200, r.text
    assert r.json()["camera_id"] == "r1"

    r = client.delete(f"/api/quiet-hours/{rid}")
    assert r.status_code == 200, r.text

    r = client.get("/api/quiet-hours/check", params={"camera_id": "r1", "now": _dt(23, 0).isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["quiet"] is False


def test_router_underscore_alias_and_health():
    _fresh()
    app = FastAPI()
    app.include_router(quiet_hours_router)
    client = TestClient(app)
    r = client.get("/api/quiet_hours/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    r = client.get("/api/quiet-hours/health")
    assert r.status_code == 200
    r = client.post("/api/quiet_hours", json={
        "camera_id": "hy1",
        "start_time": "20:00",
        "end_time": "21:00",
    })
    assert r.status_code == 200, r.text
    assert is_quiet("hy1", now=_dt(20, 30)) is True
    # active listing reflects the rule
    assert get_active(camera_id="hy1", now=_dt(20, 30)) != []
    assert list_rules(camera_id="hy1") != []
