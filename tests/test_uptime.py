from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.services import uptime as up


def test_uptime_imports():
    import app.api.uptime as uptime_api  # noqa: F401
    from app.services import uptime as uptime_svc  # noqa: F401
    assert uptime_svc.DEFAULT_WINDOW_SECONDS > 0
    uptime_router = uptime_api.router
    paths = [getattr(r, "path", None) for r in uptime_router.routes]
    assert "/api/uptime/summary" in paths
    assert "/api/uptime/cameras" in paths
    assert "/api/uptime/cameras/{camera_id}" in paths
    # Auto-loader in app.main must have picked the router up (FastAPI 0.14x
    # defers include_router() via _IncludedRouter placeholders, so scan those).
    included = [r for r in app.routes if type(r).__name__ == "_IncludedRouter"]
    assert any(getattr(r, "original_router", None) is uptime_router for r in included)


def test_uptime_service_basic():
    up.clear()
    cid = 99101
    now = datetime.utcnow()
    # Online for the last hour within a 2h window -> ~50%.
    up.mark_online(cid, when=now - timedelta(hours=1))
    stats = up.get_uptime(cid, window_seconds=7200, now=now)
    assert stats["camera_id"] == cid
    assert stats["online"] is True
    assert 49.0 <= stats["uptime_percent"] <= 51.0
    assert stats["online_seconds"] > 0
    # Going offline now -> still ~50% but currently offline.
    up.mark_offline(cid, when=now)
    stats2 = up.get_uptime(cid, window_seconds=7200, now=now)
    assert stats2["online"] is False
    assert 49.0 <= stats2["uptime_percent"] <= 51.0
    # Always online -> 100%.
    up.clear(cid)
    up.mark_online(cid, when=now - timedelta(hours=3))
    stats3 = up.get_uptime(cid, window_seconds=3600, now=now)
    assert stats3["uptime_percent"] == 100.0
    # No data -> 0% and offline.
    up.clear(cid)
    stats4 = up.get_uptime(99102, window_seconds=3600, now=now)
    assert stats4["uptime_percent"] == 0.0
    assert stats4["online"] is False
    up.clear()


def test_uptime_summary():
    up.clear()
    now = datetime.utcnow()
    up.mark_online(99201, when=now - timedelta(hours=2))
    up.mark_online(99202, when=now - timedelta(minutes=30))
    up.mark_offline(99202, when=now - timedelta(minutes=20))
    summary = up.get_summary(window_seconds=3600, now=now)
    assert summary["total"] >= 2
    assert summary["online"] >= 1
    assert 0.0 <= summary["avg_uptime_percent"] <= 100.0
    rows = up.list_uptime(window_seconds=3600, now=now)
    by_id = {r["camera_id"]: r for r in rows}
    assert by_id[99201]["uptime_percent"] == 100.0
    assert by_id[99202]["uptime_percent"] < 100.0
    up.clear()


def test_uptime_endpoints():
    up.clear()
    client = TestClient(app)
    r = client.get("/api/uptime/summary")
    assert r.status_code == 200
    assert "avg_uptime_percent" in r.json()
    r = client.get("/api/uptime/cameras")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    # Record transitions via API.
    cid = 99301
    ro = client.post(f"/api/uptime/cameras/{cid}/online", json={})
    assert ro.status_code == 200
    assert ro.json()["online"] is True
    rg = client.get(f"/api/uptime/cameras/{cid}?window_seconds=3600")
    assert rg.status_code == 200
    body = rg.json()
    assert body["camera_id"] == cid
    assert body["online"] is True
    assert body["transitions"] >= 1
    # Deterministic windowed percent: online since 1h ago within a 2h window -> ~50%.
    past = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    up.clear(cid)
    ro2 = client.post(f"/api/uptime/cameras/{cid}/online", json={"when": past})
    assert ro2.status_code == 200
    rg2 = client.get(f"/api/uptime/cameras/{cid}?window_seconds=7200")
    assert rg2.status_code == 200
    assert 49.0 <= rg2.json()["uptime_percent"] <= 51.0
    rf = client.post(f"/api/uptime/cameras/{cid}/offline", json={})
    assert rf.status_code == 200
    assert rf.json()["online"] is False
    # Unknown camera -> 404.
    rn = client.get("/api/uptime/cameras/999999999?window_seconds=60")
    assert rn.status_code == 404
    # Reset.
    rr = client.post(f"/api/uptime/reset/{cid}")
    assert rr.status_code == 200
    up.clear()
