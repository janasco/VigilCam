import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.alert_throttle import router as alert_throttle_router
from app.services import alert_throttle
from app.services.alert_throttle import (
    alert_throttle_service,
    check,
    check_and_record,
    clear,
    cooldown_remaining,
    get_config,
    get_status,
    is_throttled,
    record,
    reset,
    set_cooldown,
    set_throttle,
    should_send,
)


def _fresh():
    clear()
    # reset default cooldown/throttle overrides that may leak between tests
    import app.services.alert_throttle as m

    m._DEFAULT_COOLDOWN = float(m.DEFAULT_COOLDOWN_SECONDS)
    m._DEFAULT_MAX = m.DEFAULT_MAX_PER_WINDOW
    m._DEFAULT_WINDOW = float(m.DEFAULT_WINDOW_SECONDS)
    m._COOLDOWNS.clear()
    m._WINDOWS.clear()
    m._MAX_PER_WINDOW.clear()


def test_imports():
    import app.services.alert_throttle  # noqa: F401
    import app.api.alert_throttle  # noqa: F401
    from app.services.alert_throttle import check_and_record, alert_throttle_service  # noqa: F401


def test_cooldown_blocks_second_alert():
    _fresh()
    set_cooldown(key="cam1", cooldown_seconds=60)
    first = check_and_record(key="cam1")
    assert first["allowed"] is True
    second = check_and_record(key="cam1")
    assert second["allowed"] is False
    assert second["throttled"] is True
    assert second["remaining_seconds"] > 0
    assert should_send(key="cam1") is False
    assert is_throttled(key="cam1") is True


def test_reset_clears_cooldown():
    _fresh()
    set_cooldown(key="cam2", cooldown_seconds=60)
    assert check_and_record(key="cam2")["allowed"] is True
    assert check_and_record(key="cam2")["allowed"] is False
    reset(key="cam2")
    assert check_and_record(key="cam2")["allowed"] is True


def test_cooldown_expiry_allows_resend():
    _fresh()
    set_cooldown(key="cam3", cooldown_seconds=10)
    now = time.time()
    assert check_and_record(key="cam3", now=now)["allowed"] is True
    assert check(key="cam3", now=now + 5)["allowed"] is False
    assert check(key="cam3", now=now + 11)["allowed"] is True
    assert cooldown_remaining(key="cam3", now=now + 5) > 0


def test_sliding_window_throttle():
    _fresh()
    set_cooldown(key="burst", cooldown_seconds=0)
    set_throttle(key="burst", max_per_window=2, window_seconds=100)
    now = time.time()
    assert check_and_record(key="burst", now=now)["allowed"] is True
    assert check_and_record(key="burst", now=now + 1)["allowed"] is True
    third = check_and_record(key="burst", now=now + 2)
    assert third["allowed"] is False
    assert third["window_limited"] is True
    # after window passes, allowed again
    assert check(key="burst", now=now + 101)["allowed"] is True


def test_record_and_status():
    _fresh()
    set_cooldown(key="stat1", cooldown_seconds=30)
    r = record(key="stat1")
    assert r["recorded"] is True
    st = get_status(key="stat1")
    assert st["key"] == "stat1"
    assert st["throttled"] is True
    assert st["last_sent"] is not None
    cfg = get_config(key="stat1")
    assert cfg["cooldown_seconds"] == 30


def test_service_singleton_mirrors_module():
    _fresh()
    set_cooldown(key="svc1", cooldown_seconds=60)
    assert alert_throttle_service.check_and_record(key="svc1")["allowed"] is True
    assert alert_throttle_service.check(key="svc1")["allowed"] is False
    alert_throttle_service.reset(key="svc1")
    assert alert_throttle_service.check(key="svc1")["allowed"] is True


def test_router_check_record_reset_flow():
    _fresh()
    app = FastAPI()
    app.include_router(alert_throttle_router)
    client = TestClient(app)

    r = client.post("/api/alert_throttle/config", json={"key": "r1", "cooldown_seconds": 60})
    assert r.status_code == 200, r.text

    r = client.post("/api/alert_throttle/allow", json={"key": "r1"})
    assert r.status_code == 200, r.text
    assert r.json()["allowed"] is True

    r = client.get("/api/alert_throttle/check", params={"key": "r1"})
    assert r.status_code == 200, r.text
    assert r.json()["allowed"] is False

    r = client.get("/api/alert_throttle/status", params={"key": "r1"})
    assert r.status_code == 200, r.text
    assert r.json()["throttled"] is True

    r = client.post("/api/alert_throttle/reset", json={"key": "r1"})
    assert r.status_code == 200, r.text

    r = client.get("/api/alert_throttle/check", params={"key": "r1"})
    assert r.status_code == 200, r.text
    assert r.json()["allowed"] is True


def test_router_hyphen_alias():
    _fresh()
    app = FastAPI()
    app.include_router(alert_throttle_router)
    client = TestClient(app)
    r = client.get("/api/alert-throttle/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
