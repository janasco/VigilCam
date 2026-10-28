"""Tests for time-based zone-set switching (day/night zone schedules)."""

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.zone_schedules import router as zone_schedules_router
from app.services import zone_schedules
from app.services.zone_schedules import (
    active_zones,
    clear,
    create_schedule,
    delete_schedule,
    get_active,
    get_status,
    list_schedules,
    resolve_zones,
    set_day_night,
    zone_schedules_service,
)


DAY_ZONES = [{"points": [[0.0, 0.0], [1.0, 0.0], [1.0, 0.5], [0.0, 0.5]], "mode": "include", "name": "yard-day"}]
NIGHT_ZONES = [{"points": [[0.0, 0.5], [1.0, 0.5], [1.0, 1.0], [0.0, 1.0]], "mode": "include", "name": "porch-night"}]


def _fresh():
    clear()


def _dt(hour: int, minute: int = 0, day: int = 4) -> datetime:
    return datetime(2026, 5, day, hour, minute, 0, tzinfo=timezone.utc)


def test_imports():
    import app.services.zone_schedules  # noqa: F401
    import app.api.zone_schedules  # noqa: F401
    from app.services.zone_schedules import (  # noqa: F401
        create_schedule, set_day_night, resolve_zones, active_zones, get_active,
    )
    from app.api.zone_schedules import router  # noqa: F401


def test_create_requires_zones():
    _fresh()
    bad = create_schedule(camera_id="c1", set_name="day", start_time="06:00", end_time="18:00")
    assert bad["ok"] is False
    assert "zones" in bad["error"].lower()


def test_day_night_switching_by_time_window():
    _fresh()
    out = set_day_night(camera_id="cam1", day_zones=DAY_ZONES, night_zones=NIGHT_ZONES,
                        day_start="06:00", day_end="18:00")
    assert out["ok"] is True
    assert out["day"]["set_name"] == "day"
    assert out["night"]["set_name"] == "night"

    midday = resolve_zones(camera_id="cam1", now=_dt(12, 0))
    assert midday["fallback_used"] is False
    assert midday["set_name"] == "day"
    assert midday["zones"][0]["name"] == "yard-day"

    midnight = resolve_zones(camera_id="cam1", now=_dt(23, 0))
    assert midnight["set_name"] == "night"
    assert midnight["zones"][0]["name"] == "porch-night"

    # Other camera unaffected -> fallback.
    other = resolve_zones(camera_id="cam2", now=_dt(12, 0))
    assert other["fallback_used"] is True
    assert other["zones"] == []


def test_overnight_window():
    _fresh()
    create_schedule(camera_id="camN", set_name="night", zones=NIGHT_ZONES,
                    start_time="22:00", end_time="06:00")
    create_schedule(camera_id="camN", set_name="day", zones=DAY_ZONES,
                    start_time="06:00", end_time="22:00")
    assert resolve_zones("camN", now=_dt(23, 0))["set_name"] == "night"
    assert resolve_zones("camN", now=_dt(2, 0, day=5))["set_name"] == "night"
    assert resolve_zones("camN", now=_dt(12, 0))["set_name"] == "day"


def test_recurs_daily_not_one_off():
    _fresh()
    create_schedule(camera_id="camR", set_name="day", zones=DAY_ZONES,
                    start_time="09:00", end_time="17:00")
    assert resolve_zones("camR", now=_dt(10, 0, day=4))["set_name"] == "day"
    assert resolve_zones("camR", now=_dt(10, 0, day=11))["set_name"] == "day"
    assert resolve_zones("camR", now=_dt(20, 0, day=4))["fallback_used"] is True


def test_fallback_and_active_zones():
    _fresh()
    create_schedule(camera_id="camF", set_name="day", zones=DAY_ZONES,
                    start_time="09:00", end_time="17:00")
    static = [{"points": [[0, 0], [1, 0], [1, 1], [0, 1]], "name": "static"}]
    assert active_zones("camF", now=_dt(10, 0)) == DAY_ZONES
    assert active_zones("camF", now=_dt(20, 0), fallback=static) == static
    assert active_zones("camF", now=_dt(20, 0)) == []


def test_disabled_set_falls_back():
    _fresh()
    r = create_schedule(camera_id="camD", set_name="day", zones=DAY_ZONES,
                        start_time="00:00", end_time="00:00")  # full-day
    assert resolve_zones("camD", now=_dt(12, 0))["set_name"] == "day"
    zone_schedules.disable(id=r["id"])
    assert resolve_zones("camD", now=_dt(12, 0))["fallback_used"] is True
    zone_schedules.enable(id=r["id"])
    assert resolve_zones("camD", now=_dt(12, 0))["set_name"] == "day"


def test_priority_wins_overlap():
    _fresh()
    create_schedule(camera_id="camP", set_name="wide", zones=DAY_ZONES,
                    start_time="00:00", end_time="00:00", priority=0)
    create_schedule(camera_id="camP", set_name="narrow", zones=NIGHT_ZONES,
                    start_time="12:00", end_time="13:00", priority=10)
    assert resolve_zones("camP", now=_dt(12, 30))["set_name"] == "narrow"
    assert resolve_zones("camP", now=_dt(9, 0))["set_name"] == "wide"


def test_status_and_service_singleton():
    _fresh()
    zone_schedules_service.set_day_night(camera_id="svc1", day_zones=DAY_ZONES,
                                         night_zones=NIGHT_ZONES)
    st = zone_schedules_service.get_status(camera_id="svc1", now=_dt(12, 0))
    assert st["set_name"] == "day"
    assert st["zones"][0]["name"] == "yard-day"
    assert zone_schedules_service.resolve_zones(camera_id="svc1", now=_dt(23, 0))["set_name"] == "night"
    assert zone_schedules_service.get_active_zones(camera_id="svc1", now=_dt(23, 0))[0]["name"] == "porch-night"


def test_engine_integration_with_active_zones():
    _fresh()
    from app.services.motion_zoning import MotionZoningEngine

    set_day_night(camera_id="camE", day_zones=DAY_ZONES, night_zones=NIGHT_ZONES)
    zones = active_zones("camE", now=_dt(12, 0))
    engine = MotionZoningEngine(40, 40, zones=zones)
    assert len(engine.zones) == 1
    assert engine.zones[0].name == "yard-day"
    engine2 = zone_schedules.build_engine(camera_id="camE", now=_dt(23, 0),
                                          frame_width=40, frame_height=40)
    assert engine2.zones[0].name == "porch-night"


def test_distinct_from_static_quiet_rec():
    # Static motion_zones have no time; quiet_hours suppress alerts; rec
    # schedules control recording. Zone schedules switch zone geometry.
    _fresh()
    import app.services.quiet_hours as qh
    import app.services.rec_schedules as rs

    assert hasattr(zone_schedules, "resolve_zones")
    assert hasattr(zone_schedules, "active_zones")
    assert hasattr(zone_schedules, "set_day_night")
    assert not hasattr(qh, "resolve_zones")
    assert not hasattr(qh, "active_zones")
    assert not hasattr(rs, "resolve_zones")
    assert not hasattr(rs, "active_zones")
    r = create_schedule(camera_id="camZ", set_name="day", zones=DAY_ZONES,
                        start_time="06:00", end_time="18:00")
    assert "zones" in r and "start_minutes" in r and "end_minutes" in r


def test_update_delete_clear():
    _fresh()
    r = create_schedule(camera_id="camU", set_name="day", zones=DAY_ZONES,
                        start_time="06:00", end_time="18:00")
    updated = zone_schedules.update_schedule(r["id"], zones=NIGHT_ZONES)
    assert updated["zones"][0]["name"] == "porch-night"
    out = delete_schedule(id=r["id"])
    assert out["deleted"] is True
    assert get_active(camera_id="camU", now=_dt(12, 0)) == []
    create_schedule(camera_id="camU", set_name="day", zones=DAY_ZONES)
    assert len(list_schedules(camera_id="camU")) == 1
    clear(camera_id="camU")
    assert list_schedules(camera_id="camU") == []


def test_router_create_resolve_delete_flow():
    _fresh()
    app = FastAPI()
    app.include_router(zone_schedules_router)
    client = TestClient(app)

    r = client.post("/api/zone_schedules", json={
        "camera_id": "r1", "set_name": "day",
        "zones": DAY_ZONES, "start_time": "06:00", "end_time": "18:00",
    })
    assert r.status_code == 200, r.text
    rid = r.json()["id"]

    r = client.post("/api/zone_schedules/set-day-night", json={
        "camera_id": "r2", "day_zones": DAY_ZONES, "night_zones": NIGHT_ZONES,
    })
    assert r.status_code == 200, r.text
    assert r.json()["day"]["set_name"] == "day"

    r = client.get("/api/zone_schedules/resolve",
                   params={"camera_id": "r2", "now": _dt(12, 0).isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["set_name"] == "day"

    r = client.get("/api/zone_schedules/resolve",
                   params={"camera_id": "r2", "now": _dt(23, 0).isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["set_name"] == "night"

    r = client.get("/api/zone_schedules/active",
                   params={"camera_id": "r2", "now": _dt(23, 0).isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1

    r = client.get(f"/api/zone_schedules/{rid}")
    assert r.status_code == 200, r.text
    assert r.json()["camera_id"] == "r1"

    r = client.delete(f"/api/zone_schedules/{rid}")
    assert r.status_code == 200, r.text

    r = client.get("/api/zone-schedules/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_paths_present_in_main_app():
    from app.main import app as main_app

    paths = list(main_app.openapi()["paths"].keys())
    assert "/api/zone_schedules" in paths
    assert "/api/zone-schedules" in paths
