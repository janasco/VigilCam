from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.services import inactivity_alerts as ia


def _fresh():
    ia.clear()
    ia.enable()
    ia.configure(stillness_timeout_seconds=60, silence_timeout_seconds=60, cooldown_seconds=300)


def test_inactivity_alerts_imports():
    import app.services.inactivity_alerts  # noqa: F401
    import app.api.inactivity_alerts  # noqa: F401
    from app.services.inactivity_alerts import send_inactivity_alert, inactivity_alert_service  # noqa: F401
    from app.api.inactivity_alerts import router  # noqa: F401
    assert hasattr(router, "routes")


def test_inactivity_alerts_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/inactivity-alerts/status" in paths
    assert "/api/inactivity-alerts/summary" in paths


def test_inactivity_alerts_stillness_timeout():
    _fresh()
    cid = 7711
    base = datetime.utcnow()
    # Fresh motion report: not inactive yet.
    ia.report_motion(cid, now=base)
    ia.report_sound(cid, now=base)
    st = ia.check_camera(cid, now=base, auto_alert=False)
    assert st.is_still is False
    assert st.is_silent is False
    assert ia.should_alert(cid, now=base) is False
    # After stillness + silence timeouts elapse, both fire.
    later = base + timedelta(seconds=61)
    assert ia.should_alert(cid, now=later) is True
    assert ia.should_alert(cid, now=later, kind="stillness") is True
    assert ia.should_alert(cid, now=later, kind="silence") is True
    res = ia.send_inactivity_alert(cid, now=later, reason="inactivity")
    assert res["ok"] is True
    assert res["alerted"] is True
    assert res["camera_id"] == cid
    # Second immediate send is throttled.
    res2 = ia.send_inactivity_alert(cid, now=later, reason="inactivity")
    assert res2["alerted"] is False
    assert res2["throttled"] is True
    _fresh()


def test_inactivity_alerts_motion_resets_stillness():
    _fresh()
    cid = 7712
    base = datetime.utcnow()
    ia.report_motion(cid, now=base)
    ia.report_sound(cid, now=base)
    later = base + timedelta(seconds=120)
    assert ia.should_alert(cid, now=later, kind="stillness") is True
    # Fresh motion clears stillness but silence remains due.
    ia.report_motion(cid, now=later)
    st = ia.check_camera(cid, now=later, auto_alert=False)
    assert st.is_still is False
    assert st.is_silent is True
    assert ia.should_alert(cid, now=later, kind="stillness") is False
    # Fresh sound clears silence too.
    ia.report_sound(cid, now=later)
    st2 = ia.check_camera(cid, now=later, auto_alert=False)
    assert st2.inactive is False
    assert ia.should_alert(cid, now=later) is False
    _fresh()


def test_inactivity_alerts_per_camera_timeout_and_force():
    _fresh()
    ia.configure(stillness_timeout_seconds=60, silence_timeout_seconds=60, cooldown_seconds=60)
    cid = 7713
    base = datetime.utcnow()
    ia.report_motion(cid, now=base)
    ia.report_sound(cid, now=base)
    # Per-camera stillness timeout of 10s fires earlier.
    ia.set_stillness_timeout(cid, stillness_timeout_seconds=10, per_camera=True)
    early = base + timedelta(seconds=11)
    assert ia.should_alert(cid, now=early, kind="stillness") is True
    first = ia.send_inactivity_alert(cid, now=early, kind="stillness")
    assert first["alerted"] is True
    assert ia.cooldown_remaining(cid, now=early) > 0
    assert ia.is_throttled(cid, now=early) is True
    forced = ia.send_inactivity_alert(cid, now=early, kind="stillness", force=True)
    assert forced["alerted"] is True
    ia.reset(cid)
    st = ia.get_status(cid)
    assert st is not None
    assert st.alert_count == 0
    ia.remove(cid)
    assert ia.get_status(cid) is None
    _fresh()


def test_inactivity_alerts_check_and_summary():
    _fresh()
    cid = 7714
    base = datetime.utcnow()
    ia.report_motion(cid, now=base)
    ia.report_sound(cid, now=base)
    st = ia.check_camera(cid, now=base, auto_alert=False)
    assert st.camera_id == cid
    assert st.inactive is False
    summary = ia.get_summary()
    assert summary["total"] >= 1
    assert "stillness_timeout_seconds" in summary
    assert "silence_timeout_seconds" in summary
    assert "cooldown_seconds" in summary
    ia.resolve(cid)
    assert ia.get_status(cid).inactive is False
    _fresh()


def test_inactivity_alerts_endpoints():
    _fresh()
    client = TestClient(app)
    r = client.get("/api/inactivity-alerts/status")
    assert r.status_code == 200, r.text
    assert "summary" in r.json()

    r2 = client.get("/api/inactivity-alerts/config")
    assert r2.status_code == 200, r.text
    assert "stillness_timeout_seconds" in r2.json()
    assert "silence_timeout_seconds" in r2.json()

    r3 = client.get("/api/inactivity-alerts/summary")
    assert r3.status_code == 200, r.text
    assert "total" in r3.json()

    cid = 7715
    base = datetime.utcnow()
    ia.report_motion(cid, now=base)
    ia.report_sound(cid, now=base)
    ia.configure(stillness_timeout_seconds=0, silence_timeout_seconds=0)
    ra = client.post(f"/api/inactivity-alerts/alert/{cid}", json={"reason": "inactivity"})
    assert ra.status_code == 200, ra.text
    assert ra.json()["alerted"] is True

    rb = client.post(f"/api/inactivity-alerts/alert/{cid}", json={"reason": "inactivity"})
    assert rb.status_code == 200, rb.text
    assert rb.json()["throttled"] is True

    rc = client.get(f"/api/inactivity-alerts/cameras/{cid}")
    assert rc.status_code == 200, rc.text
    assert rc.json()["camera_id"] == cid

    rl = client.get("/api/inactivity-alerts/cameras")
    assert rl.status_code == 200, rl.text
    assert isinstance(rl.json(), list)

    rk = client.post("/api/inactivity-alerts/check", json={})
    assert rk.status_code == 200, rk.text
    assert rk.json()["ok"] is True

    # Report endpoints record fresh activity.
    rm = client.post(f"/api/inactivity-alerts/report/{cid}", json={"kind": "motion"})
    assert rm.status_code == 200, rm.text
    assert rm.json()["ok"] is True

    rs = client.post(f"/api/inactivity-alerts/report/{cid}", json={"kind": "sound"})
    assert rs.status_code == 200, rs.text
    assert rs.json()["ok"] is True

    # Alias prefix serves the same data.
    r_alias = client.get("/api/alerts/inactivity/summary")
    assert r_alias.status_code == 200, r.text

    # 404 for unknown camera.
    r404 = client.get("/api/inactivity-alerts/cameras/999998")
    assert r404.status_code == 404
    _fresh()
