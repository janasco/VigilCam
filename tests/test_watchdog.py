from fastapi.testclient import TestClient

from app.main import app
from app.services import watchdog as wd
from app.services import camera_health as ch


def test_watchdog_imports():
    from app.main import app as _app
    from app.api import watchdog as watchdog_api

    assert hasattr(watchdog_api, "router")
    paths = list(_app.openapi()["paths"].keys())
    assert "/api/watchdog/status" in paths
    assert "/api/watchdog/cameras" in paths


def test_watchdog_service_basic():
    wd.clear()
    ch.clear()
    svc = wd.get_watchdog()
    assert svc is not None
    # configure + enable/disable
    svc.configure(check_interval_seconds=5, max_restarts=3, restart_cooldown_seconds=0)
    assert svc.check_interval_seconds == 5.0
    assert svc.max_restarts == 3
    wd.disable()
    assert wd.is_enabled() is False
    wd.enable()
    assert wd.is_enabled() is True

    cid = 9911
    st = wd.restart_camera(cid, reason="manual", force=True)
    assert st is not None
    assert st.camera_id == cid
    assert st.restart_attempts >= 1
    got = wd.get_status(cid)
    assert got is not None
    assert got.restart_attempts >= 1
    # reset clears attempts
    wd.reset(cid)
    assert wd.get_status(cid).restart_attempts == 0
    wd.remove(cid)
    assert wd.get_status(cid) is None
    wd.clear()
    ch.clear()


def test_watchdog_check_offline_triggers_restart():
    wd.clear()
    ch.clear()
    wd.configure(max_restarts=5, restart_cooldown_seconds=0)
    wd.enable()
    cid = 9912
    # offline camera (no frames ever) should be flagged and restarted on check
    ch.mark_offline(cid)
    before = ch.get_health(cid).reconnect_attempts
    results = wd.check_all(auto_restart=True)
    by_id = {s.camera_id: s for s in results}
    assert cid in by_id
    st = wd.get_status(cid)
    assert st is not None
    assert st.restart_attempts >= 1
    # online camera should not restart
    cid2 = 9913
    ch.mark_frame_received(cid2)
    wd.check_camera(cid2, auto_restart=True)
    assert wd.get_status(cid2).restart_attempts == 0
    wd.clear()
    ch.clear()


def test_watchdog_max_restarts_cap():
    wd.clear()
    ch.clear()
    wd.configure(max_restarts=2, restart_cooldown_seconds=0)
    wd.enable()
    cid = 9914
    wd.restart_camera(cid, force=True)
    wd.restart_camera(cid, force=True)
    assert wd.get_status(cid).restart_attempts == 2
    # non-forced restart beyond cap is ignored
    wd.restart_camera(cid, force=False)
    assert wd.get_status(cid).restart_attempts == 2
    wd.clear()
    ch.clear()


def test_watchdog_endpoints():
    wd.clear()
    ch.clear()
    client = TestClient(app)
    r = client.get("/api/watchdog/status")
    assert r.status_code == 200
    data = r.json()
    assert "enabled" in data
    assert "cameras" in data

    r2 = client.get("/api/watchdog/config")
    assert r2.status_code == 200
    assert "max_restarts" in r2.json()

    r3 = client.post("/api/watchdog/disable")
    assert r3.status_code == 200
    r4 = client.post("/api/watchdog/enable")
    assert r4.status_code == 200
    assert r4.json()["enabled"] is True

    # restart creates an entry even without prior health
    cid = 9915
    rr = client.post(f"/api/watchdog/restart/{cid}", json={"reason": "manual"})
    assert rr.status_code == 200
    assert rr.json()["camera_id"] == cid

    rc = client.get(f"/api/watchdog/cameras/{cid}")
    assert rc.status_code == 200
    assert rc.json()["camera_id"] == cid

    rl = client.get("/api/watchdog/cameras")
    assert rl.status_code == 200
    assert isinstance(rl.json(), list)

    rk = client.post("/api/watchdog/check", json={})
    assert rk.status_code == 200
    assert rk.json()["ok"] is True

    # alias restart path
    rr2 = client.post(f"/api/watchdog/cameras/{cid}/restart", json={"reason": "manual"})
    assert rr2.status_code == 200

    # 404 for unknown camera
    r404 = client.get("/api/watchdog/cameras/999998")
    assert r404.status_code == 404
    wd.clear()
    ch.clear()
