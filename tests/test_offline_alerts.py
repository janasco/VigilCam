from fastapi.testclient import TestClient

from app.main import app
from app.services import offline_alerts as oa
from app.services import camera_health as ch


def _fresh():
    oa.clear()
    ch.clear()
    oa.enable()
    oa.configure(cooldown_seconds=300, offline_threshold_seconds=60)


def test_offline_alerts_imports():
    import app.services.offline_alerts  # noqa: F401
    import app.api.offline_alerts  # noqa: F401
    from app.services.offline_alerts import send_offline_alert, offline_alert_service  # noqa: F401
    from app.api.offline_alerts import router  # noqa: F401
    assert hasattr(router, "routes")


def test_offline_alerts_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/offline-alerts/status" in paths
    assert "/api/offline-alerts/summary" in paths


def test_offline_alerts_service_basic():
    _fresh()
    cid = 9811
    # Offline camera (no frames ever) should be alertable.
    ch.mark_offline(cid)
    assert oa.should_alert(cid) is True
    res = oa.send_offline_alert(cid, reason="offline")
    assert res["ok"] is True
    assert res["alerted"] is True
    assert res["camera_id"] == cid
    # Second immediate send is throttled.
    res2 = oa.send_offline_alert(cid, reason="offline")
    assert res2["alerted"] is False
    assert res2["throttled"] is True
    # Online camera should not alert.
    cid2 = 9812
    ch.mark_frame_received(cid2)
    oa.check_camera(cid2)
    assert oa.should_alert(cid2) is False
    _fresh()


def test_offline_alerts_cooldown_and_force():
    _fresh()
    oa.configure(cooldown_seconds=60)
    cid = 9813
    ch.mark_offline(cid)
    first = oa.send_offline_alert(cid)
    assert first["alerted"] is True
    assert oa.cooldown_remaining(cid) > 0
    assert oa.is_throttled(cid) is True
    # Forced send bypasses cooldown.
    forced = oa.send_offline_alert(cid, force=True)
    assert forced["alerted"] is True
    # Reset clears alert state.
    oa.reset(cid)
    st = oa.get_status(cid)
    assert st is not None
    assert st.alert_count == 0
    oa.remove(cid)
    assert oa.get_status(cid) is None
    _fresh()


def test_offline_alerts_check_and_summary():
    _fresh()
    cid = 9814
    ch.mark_offline(cid)
    st = oa.check_camera(cid, auto_alert=False)
    assert st.camera_id == cid
    assert st.offline is True
    summary = oa.get_summary()
    assert summary["total"] >= 1
    assert summary["offline"] >= 1
    assert "cooldown_seconds" in summary
    # Resolve marks online.
    oa.resolve(cid)
    assert oa.get_status(cid).offline is False
    _fresh()


def test_offline_alerts_endpoints():
    _fresh()
    client = TestClient(app)
    r = client.get("/api/offline-alerts/status")
    assert r.status_code == 200, r.text
    assert "summary" in r.json()

    r2 = client.get("/api/offline-alerts/config")
    assert r2.status_code == 200, r2.text
    assert "cooldown_seconds" in r2.json()

    r3 = client.get("/api/offline-alerts/summary")
    assert r3.status_code == 200, r3.text
    assert "total" in r3.json()

    cid = 9815
    ch.mark_offline(cid)
    ra = client.post(f"/api/offline-alerts/alert/{cid}", json={"reason": "offline"})
    assert ra.status_code == 200, ra.text
    assert ra.json()["alerted"] is True

    # Throttled second alert.
    rb = client.post(f"/api/offline-alerts/alert/{cid}", json={"reason": "offline"})
    assert rb.status_code == 200, rb.text
    assert rb.json()["throttled"] is True

    rc = client.get(f"/api/offline-alerts/cameras/{cid}")
    assert rc.status_code == 200, rc.text
    assert rc.json()["camera_id"] == cid

    rl = client.get("/api/offline-alerts/cameras")
    assert rl.status_code == 200, rl.text
    assert isinstance(rl.json(), list)

    rk = client.post("/api/offline-alerts/check", json={})
    assert rk.status_code == 200, rk.text
    assert rk.json()["ok"] is True

    # Alias prefix serves the same data.
    r_alias = client.get("/api/alerts/offline/summary")
    assert r_alias.status_code == 200, r_alias.text

    # 404 for unknown camera.
    r404 = client.get("/api/offline-alerts/cameras/999998")
    assert r404.status_code == 404
    _fresh()
