from fastapi.testclient import TestClient

from app.main import app
from app.services import plate_watch as pw


def _fresh():
    pw.clear()
    pw.configure(alert_on_unknown=True, alert_on_blocklist=True, enabled=True)


def test_plate_watch_imports():
    import app.services.plate_watch  # noqa: F401
    import app.api.plate_watch  # noqa: F401
    from app.services.plate_watch import check_plate, add_allowlist, add_blocklist  # noqa: F401
    from app.api.plate_watch import router  # noqa: F401
    assert hasattr(router, "routes")
    # Recognition stays in license_plate; watcher must not duplicate it.
    import app.services.license_plate  # noqa: F401
    assert not hasattr(pw, "recognize_plate")


def test_plate_watch_normalize():
    assert pw.normalize_plate(" abc-123 ") == "ABC123"
    assert pw.normalize_plate("xyz 987") == "XYZ987"


def test_plate_watch_allowlist_no_alert():
    _fresh()
    pw.add_allowlist({"plate": "ABC123"})
    assert pw.is_allowed("abc-123")
    res = pw.check_plate({"plate": "ABC 123"})
    assert res["allowed"] is True
    assert res["blocked"] is False
    assert res["unknown"] is False
    assert res["alert"] is False
    assert res["reason"] == "allowed"
    _fresh()


def test_plate_watch_blocklist_alert():
    _fresh()
    pw.add_blocklist({"plate": "BAD999"})
    assert pw.is_blocked("bad999")
    res = pw.check_plate({"plate": "BAD999", "camera_id": 1})
    assert res["blocked"] is True
    assert res["alert"] is True
    assert res["reason"] == "blocklist"
    alerts = pw.list_alerts()
    assert any(a["plate"] == "BAD999" for a in alerts)
    _fresh()


def test_plate_watch_unknown_alerts():
    _fresh()
    res = pw.check_plate({"plate": "NEW000"})
    assert res["unknown"] is True
    assert res["alert"] is True
    assert res["reason"] == "unknown"
    # Opt-out of unknown alerts: blocklist still alerts.
    pw.configure(alert_on_unknown=False)
    res2 = pw.check_plate({"plate": "OTHER1"})
    assert res2["unknown"] is True
    assert res2["alert"] is False
    pw.add_blocklist({"plate": "BAD999"})
    res3 = pw.check_plate({"plate": "BAD999"})
    assert res3["alert"] is True
    _fresh()


def test_plate_watch_add_remove_lists():
    _fresh()
    pw.add_allowlist({"plate": "AAA111"})
    pw.add_blocklist({"plate": "BBB222"})
    assert "AAA111" in pw.list_allowlist()
    assert "BBB222" in pw.list_blocklist()
    pw.remove_allowlist({"plate": "AAA111"})
    pw.remove_blocklist({"plate": "BBB222"})
    assert "AAA111" not in pw.list_allowlist()
    assert "BBB222" not in pw.list_blocklist()
    _fresh()


def test_plate_watch_endpoints():
    _fresh()
    client = TestClient(app)
    r = client.get("/api/plate_watch/summary")
    assert r.status_code == 200, r.text
    assert "allowlist_count" in r.json()

    a = client.post("/api/plate_watch/allowlist", json={"plate": "GOOD123"})
    assert a.status_code == 200, a.text

    b = client.post("/api/plate_watch/blocklist", json={"plate": "EVIL9"})
    assert b.status_code == 200, b.text

    c = client.post("/api/plate_watch/check", json={"plate": "EVIL9"})
    assert c.status_code == 200, c.text
    assert c.json()["blocked"] is True
    assert c.json()["alert"] is True

    u = client.post("/api/plate_watch/check", json={"plate": "STRANGER1"})
    assert u.status_code == 200, u.text
    assert u.json()["unknown"] is True
    assert u.json()["alert"] is True

    g = client.post("/api/plate_watch/check", json={"plate": "GOOD123"})
    assert g.status_code == 200, g.text
    assert g.json()["alert"] is False

    wl = client.get("/api/plate_watch/watchlists")
    assert wl.status_code == 200, wl.text
    assert "allowlist" in wl.json()

    al = client.get("/api/plate_watch/alerts")
    assert al.status_code == 200, al.text
    assert isinstance(al.json(), list)
    assert len(al.json()) >= 2  # blocklist hit + unknown

    rd = client.get("/api/plate_watch/reads")
    assert rd.status_code == 200, rd.text
    assert isinstance(rd.json(), list)

    # Alias prefix serves the same data.
    alias = client.get("/api/plate-watch/summary")
    assert alias.status_code == 200, alias.text
    _fresh()
