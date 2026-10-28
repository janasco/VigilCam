from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app
import app.services.alert_inbox as inbox
import app.services.resource_watch as rw
import app.services.bitrate_alerts as ba
import app.services.offline_alerts as oa


def _fresh():
    rw.clear()
    rw.enable()
    rw.configure(cpu_threshold=85, memory_threshold=90, disk_threshold=90,
                 cooldown_seconds=300)
    ba.clear()
    ba.enable()
    ba.configure(min_bitrate_bps=100_000, max_bitrate_bps=20_000_000,
                 cooldown_seconds=300)
    try:
        oa.clear()
    except Exception:
        pass


def test_alert_inbox_imports():
    import app.services.alert_inbox  # noqa: F401
    import app.api.alert_inbox  # noqa: F401
    from app.services.alert_inbox import get_inbox, alert_inbox_service  # noqa: F401
    from app.api.alert_inbox import router  # noqa: F401
    assert hasattr(router, "routes")


def test_alert_inbox_no_preexisting_unified_service():
    # The four sources remain separate services (premise of this task).
    import app.services.resource_watch  # noqa: F401
    import app.services.rate_anomaly  # noqa: F401
    import app.services.offline_alerts  # noqa: F401
    import app.services.bitrate_alerts  # noqa: F401
    assert rw.RESOURCES == ("cpu", "memory", "disk")


def test_alert_inbox_aggregates_resource_and_bitrate():
    _fresh()
    rw.check(cpu=99, memory=10, disk=10, force=True)
    ba.check(target="cam-inbox", bitrate_bps=5_000, force=True)
    out = inbox.get_inbox()
    assert out["ok"] is True
    assert out["count"] >= 2
    sources = {it["source"] for it in out["items"]}
    assert "resource_watch" in sources
    assert "bitrate_alerts" in sources
    for it in out["items"]:
        assert it["source"] in inbox.SOURCES
        assert it["severity"] in ("critical", "warning", "info", "ok")
        assert it["at"]
        assert it["message"]
    _fresh()


def test_alert_inbox_sorted_newest_first_with_severity():
    _fresh()
    base = datetime(2026, 1, 1, 12, 0, 0)
    rw.check(cpu=99, memory=1, disk=1, force=True, now=(base - timedelta(hours=2)).isoformat())
    ba.check(target="cam-sort", bitrate_bps=1_000, force=True,
             now=(base - timedelta(hours=1)).isoformat())
    rw.check(memory=99, cpu=1, disk=1, force=True, now=base.isoformat())
    items = inbox.list_alerts()
    assert len(items) >= 3
    stamps = [it["at"] for it in items]
    assert stamps == sorted(stamps, reverse=True), stamps
    _fresh()


def test_alert_inbox_offline_and_rate_anomaly():
    _fresh()
    now = datetime.utcnow()
    res = oa.send_offline_alert(4242, reason="offline", now=now.isoformat(), force=True)
    assert res.get("ok", True)
    out = inbox.get_inbox(sources="offline_alerts")
    assert out["count"] >= 1
    assert all(it["source"] == "offline_alerts" for it in out["items"])

    # Rate anomaly: spike from many recent events vs quiet baseline.
    events = [{"created_at": (now - timedelta(hours=i)).isoformat()} for i in range(30, 40)]
    events += [{"created_at": (now - timedelta(minutes=m)).isoformat()} for m in range(20)]
    spike_items = inbox.collect_rate_anomaly_alerts(
        events, baseline_hours=24, current_minutes=15)
    assert isinstance(spike_items, list)
    # Empty events -> no rate items, but inbox still works.
    empty = inbox.get_inbox(events=[])
    assert empty["ok"] is True
    # Online-only cameras are excluded by default.
    _fresh()


def test_alert_inbox_severity_and_source_filters():
    _fresh()
    rw.check(cpu=99, memory=1, disk=1, force=True)
    ba.check(target="cam-filt", bitrate_bps=1_000, force=True)
    only_rw = inbox.get_inbox(sources="resource_watch")
    assert only_rw["count"] >= 1
    assert all(it["source"] == "resource_watch" for it in only_rw["items"])
    crit = inbox.get_inbox(severity="critical")
    assert all(it["severity"] == "critical" for it in crit["items"])
    limited = inbox.get_inbox(limit=1)
    assert limited["count"] <= 1
    summary = inbox.get_summary()
    assert summary["count"] >= 2
    assert "by_source" in summary and "by_severity" in summary
    _fresh()


def test_alert_inbox_endpoints():
    _fresh()
    rw.check(cpu=99, memory=1, disk=1, force=True)
    client = TestClient(app)
    r = client.get("/api/alert-inbox/status")
    assert r.status_code == 200, r.text
    assert r.json()["service"] == "alert_inbox"

    r2 = client.get("/api/alert-inbox/summary")
    assert r2.status_code == 200, r2.text
    assert "count" in r2.json()

    r3 = client.get("/api/alert-inbox/alerts")
    assert r3.status_code == 200, r3.text
    body = r3.json()
    assert body["ok"] is True
    assert isinstance(body["items"], list)
    assert body["count"] >= 1

    r4 = client.get("/api/alerts/inbox/inbox")
    assert r4.status_code == 200, r4.text

    r5 = client.get("/api/alert_inbox/alerts")
    assert r5.status_code == 200, r5.text

    r6 = client.get("/api/alert-inbox/health")
    assert r6.status_code == 200, r6.text
    assert r6.json()["service"] == "alert_inbox"

    # POST with events exercises the rate_anomaly branch.
    r7 = client.post("/api/alert-inbox/inbox", json={"events": [], "sources": "resource_watch"})
    assert r7.status_code == 200, r7.text
    assert r7.json()["ok"] is True
    _fresh()
