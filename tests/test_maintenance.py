import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.maintenance import router as maintenance_router
from app.services import maintenance
from app.services.maintenance import (
    clear,
    create_window,
    delete_window,
    get_active,
    get_status,
    is_suppressed,
    list_windows,
    maintenance_service,
)


def _fresh():
    clear()


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def test_imports():
    import app.services.maintenance  # noqa: F401
    import app.api.maintenance  # noqa: F401
    from app.services.maintenance import is_suppressed, maintenance_service  # noqa: F401


def test_create_and_is_suppressed_per_camera():
    _fresh()
    now = datetime.now(timezone.utc)
    w = create_window(camera_id="cam1", start=_iso(now - timedelta(minutes=5)), end=_iso(now + timedelta(minutes=5)), reason="cleaning")
    assert w["ok"] is True
    assert is_suppressed("cam1") is True
    assert is_suppressed(camera_id="cam1") is True
    assert is_suppressed(camera_id="cam2") is False
    st = get_status(camera_id="cam1")
    assert st["suppressed"] is True
    st2 = get_status(camera_id="cam2")
    assert st2["suppressed"] is False


def test_global_window_suppresses_all_cameras():
    _fresh()
    now = datetime.now(timezone.utc)
    create_window(camera_id="*", start=_iso(now - timedelta(minutes=1)), end=_iso(now + timedelta(minutes=30)))
    assert is_suppressed("cam1") is True
    assert is_suppressed("anything-else") is True


def test_expired_and_future_windows_do_not_suppress():
    _fresh()
    now = datetime.now(timezone.utc)
    create_window(camera_id="cam9", start=_iso(now - timedelta(hours=2)), end=_iso(now - timedelta(hours=1)))
    assert is_suppressed("cam9") is False
    create_window(camera_id="cam9", start=_iso(now + timedelta(hours=1)), end=_iso(now + timedelta(hours=2)))
    assert is_suppressed("cam9") is False
    # sanity: no active windows right now
    assert get_active(camera_id="cam9") == []


def test_disabled_window_does_not_suppress():
    _fresh()
    now = datetime.now(timezone.utc)
    w = create_window(camera_id="camD", start=_iso(now - timedelta(minutes=5)), end=_iso(now + timedelta(minutes=5)))
    assert is_suppressed("camD") is True
    maintenance.disable(id=w["id"])
    assert is_suppressed("camD") is False
    maintenance.enable(id=w["id"])
    assert is_suppressed("camD") is True


def test_delete_clears_suppression():
    _fresh()
    now = datetime.now(timezone.utc)
    w = create_window(camera_id="camX", start=_iso(now - timedelta(minutes=5)), end=_iso(now + timedelta(minutes=5)))
    assert is_suppressed("camX") is True
    out = delete_window(id=w["id"])
    assert out["deleted"] is True
    assert is_suppressed("camX") is False


def test_explicit_now_reference():
    _fresh()
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    create_window(camera_id="camT", start=_iso(base), end=_iso(base + timedelta(hours=1)))
    assert is_suppressed("camT", now=_iso(base + timedelta(minutes=30))) is True
    assert is_suppressed("camT", now=_iso(base - timedelta(minutes=1))) is False
    assert is_suppressed("camT", now=_iso(base + timedelta(hours=2))) is False
    # epoch form also accepted
    ts = (base + timedelta(minutes=10)).timestamp()
    assert is_suppressed("camT", now=ts) is True


def test_service_singleton_mirrors_module():
    _fresh()
    now = datetime.now(timezone.utc)
    maintenance_service.create_window(camera_id="svc1", start=_iso(now - timedelta(minutes=2)), end=_iso(now + timedelta(minutes=2)))
    assert maintenance_service.is_suppressed(camera_id="svc1") is True
    assert maintenance_service.get_status(camera_id="svc1")["suppressed"] is True
    maintenance_service.clear()
    assert maintenance_service.is_suppressed(camera_id="svc1") is False


def test_distinct_from_throttle_and_retention():
    # Maintenance suppresses by calendar window, not by send-rate (throttle)
    # and it never touches recordings (retention). Guard against regressions
    # that would re-implement either neighbour here.
    _fresh()
    import app.services.alert_throttle as throttle
    import app.services.retention as retention

    assert hasattr(maintenance, "is_suppressed")
    assert hasattr(maintenance, "create_window")
    # throttle has no calendar-window API
    assert not hasattr(throttle, "is_suppressed")
    assert not hasattr(throttle, "create_window")
    # retention manages files, not alert suppression
    assert not hasattr(retention, "is_suppressed")
    assert hasattr(retention, "RetentionManager")


def test_router_create_check_delete_flow():
    _fresh()
    app = FastAPI()
    app.include_router(maintenance_router)
    client = TestClient(app)

    now = datetime.now(timezone.utc)
    r = client.post("/api/maintenance", json={
        "camera_id": "r1",
        "start": _iso(now - timedelta(minutes=5)),
        "end": _iso(now + timedelta(minutes=5)),
        "reason": "firmware upgrade",
    })
    assert r.status_code == 200, r.text
    wid = r.json()["id"]

    r = client.get("/api/maintenance/check", params={"camera_id": "r1"})
    assert r.status_code == 200, r.text
    assert r.json()["suppressed"] is True

    r = client.get("/api/maintenance/check", params={"camera_id": "other"})
    assert r.status_code == 200, r.text
    assert r.json()["suppressed"] is False

    r = client.get(f"/api/maintenance/{wid}")
    assert r.status_code == 200, r.text
    assert r.json()["camera_id"] == "r1"

    r = client.delete(f"/api/maintenance/{wid}")
    assert r.status_code == 200, r.text

    r = client.get("/api/maintenance/check", params={"camera_id": "r1"})
    assert r.status_code == 200, r.text
    assert r.json()["suppressed"] is False


def test_router_hyphen_alias_and_health():
    _fresh()
    app = FastAPI()
    app.include_router(maintenance_router)
    client = TestClient(app)
    r = client.get("/api/maintenance-windows/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    r = client.get("/api/maintenance/health")
    assert r.status_code == 200
    now = time.time()
    r = client.post("/api/maintenance-windows", json={
        "camera_id": "hy1",
        "start": now - 60,
        "end": now + 600,
    })
    assert r.status_code == 200, r.text
    assert is_suppressed("hy1") is True
