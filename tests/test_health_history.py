from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.health_history import router as health_history_router
import app.services.health_history as svc
import app.services.camera_health as ch
import app.services.uptime as up


def _make_client():
    app = FastAPI()
    app.include_router(health_history_router)
    return TestClient(app)


def _all_paths(_app):
    out = []
    for r in _app.routes:
        if r.__class__.__name__ == "_IncludedRouter":
            try:
                for sub in r.original_router.routes:
                    p = getattr(sub, "path", None)
                    if p:
                        out.append(p)
            except Exception:  # noqa: BLE001
                continue
        else:
            p = getattr(r, "path", None)
            if p:
                out.append(p)
    try:
        for r in getattr(_app.router, "routes", []):
            p = getattr(r, "path", None)
            if p and p not in out:
                out.append(p)
    except Exception:  # noqa: BLE001
        pass
    return out


def test_imports():
    import app.api.health_history  # noqa: F401
    import app.services.health_history  # noqa: F401
    assert health_history_router is not None
    assert hasattr(svc, "record_sample")
    assert hasattr(svc, "record_history") or hasattr(svc, "log_sample")
    assert hasattr(svc, "list_history")
    assert hasattr(svc, "get_entry")
    assert hasattr(svc, "delete_entry")
    assert hasattr(svc, "clear_history")
    assert hasattr(svc, "snapshot_and_record") or hasattr(svc, "get_stats")
    assert hasattr(svc, "status")


def test_camera_health_tracks_current_state_only():
    # Verify camera_health is current-state only: single last-known record
    # per camera with no history accumulation, no time series.
    ch.clear()
    assert not hasattr(ch, "HEALTH_HISTORY")
    assert not hasattr(ch, "list_history")
    st1 = ch.mark_frame_received(61001)
    assert st1.online is True
    n1 = len(ch.list_health())
    st2 = ch.mark_frame_received(61001)
    assert st2.online is True
    # Same camera overwrites; no history accumulation in camera_health.
    assert len(ch.list_health()) == n1
    assert len([s for s in ch.list_health() if s.camera_id == 61001]) == 1
    ch.clear()


def test_uptime_tracks_percentages_no_history_series():
    # Verify uptime exposes windowed percentages, not a timestamped
    # health-sample series.
    assert not hasattr(up, "HEALTH_HISTORY")
    assert not hasattr(up, "list_history")
    assert hasattr(up, "get_uptime")
    assert hasattr(up, "get_summary")
    up.clear()
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    up.mark_online(61101, when=now - timedelta(hours=1))
    stats = up.get_uptime(61101, window_seconds=7200, now=now)
    assert "uptime_percent" in stats
    assert 49.0 <= stats["uptime_percent"] <= 51.0
    up.clear()


def test_main_app_paths_present():
    from app.main import app as main_app
    paths = _all_paths(main_app)
    assert any(
        p.startswith("/api/health/history")
        or p.startswith("/api/health-history")
        or p.startswith("/api/health_history")
        or p.startswith("/api/cameras/")
        and "health/history" in p
        for p in paths
    ), paths


def test_service_record_list_get_delete():
    svc.clear_history()
    entry = svc.record_sample(camera_id=61201, online=True, status="online")
    assert entry.get("id")
    assert entry.get("camera_id") == 61201
    assert entry.get("online") is True
    assert entry.get("status") == "online"
    assert entry.get("sampled_at")
    assert entry.get("created_at")

    items = svc.list_history()
    assert any(str(e.get("id")) == str(entry.get("id")) for e in items)

    fetched = svc.get_entry(entry.get("id"))
    assert fetched is not None
    assert str(fetched.get("id")) == str(entry.get("id"))

    filtered = svc.list_history(camera_id=61201)
    assert filtered
    assert all(int(str(e.get("camera_id")).split(":")[-1]) == 61201 for e in filtered)

    assert svc.delete_entry(entry.get("id")) is True
    assert svc.get_entry(entry.get("id")) is None
    svc.clear_history()


def test_service_history_tracking_appends():
    svc.clear_history()
    ch.clear()
    # Same camera marked twice -> camera_health keeps 1 current-state record...
    ch.mark_frame_received(61202)
    ch.mark_frame_received(61202)
    assert len([s for s in ch.list_health() if s.camera_id == 61202]) == 1
    # ...while health history appends every sample as its own entry.
    svc.record_sample(camera_id=61202, online=True)
    svc.record_sample(camera_id=61202, online=False)
    items = svc.list_history(camera_id=61202)
    assert len(items) == 2
    assert items[0].get("online") is True
    assert items[1].get("online") is False
    # Per-camera stats.
    stats = svc.get_stats(camera_id=61202)
    assert stats["total"] == 2
    assert stats["online"] == 1
    assert stats["offline"] == 1
    # Latest helper.
    latest = svc.get_latest_for_camera(61202)
    assert latest is not None
    assert latest.get("online") is False
    svc.clear_history()
    ch.clear()


def test_service_aliases_and_clear():
    svc.clear_history()
    a = svc.log_sample(camera_id=61301, online=True)
    b = svc.record_history(camera_id=61301, online=False)
    c = svc.add_entry(camera_id=61302, status="offline")
    assert a.get("id") and b.get("id") and c.get("id")
    assert len(svc.fetch_history()) >= 3
    assert svc.get(a.get("id")) is not None
    assert svc.status().get("total") >= 3
    # Snapshot from live health.
    ch.clear()
    ch.mark_frame_received(61303)
    snap = svc.snapshot_and_record(61303)
    assert snap.get("camera_id") == 61303
    assert snap.get("online") is True
    n = svc.clear()
    assert n >= 4
    assert svc.list_history() == []
    svc.clear_history()
    ch.clear()


def test_service_snapshot_and_record_csv_json():
    svc.clear_history()
    ch.clear()
    ch.mark_offline(61401)
    entry = svc.snapshot_and_record(61401)
    assert entry.get("camera_id") == 61401
    assert entry.get("online") is False
    assert len(svc.list_history(camera_id=61401)) == 1
    text = svc.to_csv(svc.list_history())
    assert "camera_id" in text
    js = svc.to_json(svc.list_history())
    assert "61401" in js
    svc.clear_history()
    ch.clear()


def test_router_list_create_get_delete():
    svc.clear_history()
    client = _make_client()

    r = client.post("/api/health/history", json={"camera_id": 61501, "online": True})
    assert r.status_code in (200, 201), r.text
    created = r.json()
    eid = created.get("id") or created.get("entry_id") or created.get("history_id")
    assert eid

    r_list = client.get("/api/health/history")
    assert r_list.status_code == 200, r_list.text
    assert isinstance(r_list.json(), list)
    assert any(str(e.get("id")) == str(eid) for e in r_list.json())

    r_get = client.get(f"/api/health/history/{eid}")
    assert r_get.status_code == 200, r_get.text

    r_del = client.delete(f"/api/health/history/{eid}")
    assert r_del.status_code == 200, r_del.text

    r_missing = client.get(f"/api/health/history/{eid}")
    assert r_missing.status_code == 404
    svc.clear_history()


def test_router_alias_paths_and_filters():
    svc.clear_history()
    svc.record_sample(camera_id=61601, online=True, status="online")
    svc.record_sample(camera_id=61602, online=False, status="offline")
    client = _make_client()

    r = client.get("/api/health/history", params={"camera_id": 61601})
    assert r.status_code == 200, r.text
    assert all(int(str(e.get("camera_id")).split(":")[-1]) == 61601 for e in r.json())

    r2 = client.get("/api/health-history")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)

    r3 = client.get("/api/health_history")
    assert r3.status_code == 200, r3.text

    r_health = client.get("/api/health/history/health")
    assert r_health.status_code == 200
    assert r_health.json().get("status") == "ok"

    r_count = client.get("/api/health/history/count")
    assert r_count.status_code == 200
    assert r_count.json().get("count") >= 2

    r_status = client.get("/api/health/history/status")
    assert r_status.status_code == 200
    assert r_status.json().get("ok") is True

    r_summary = client.get("/api/health/history/summary")
    assert r_summary.status_code == 200
    assert r_summary.json().get("total") >= 2

    r_cam = client.get("/api/health/history/camera/61601")
    assert r_cam.status_code == 200
    assert all(int(str(e.get("camera_id")).split(":")[-1]) == 61601 for e in r_cam.json())

    r_cam2 = client.get("/api/cameras/61601/health/history")
    assert r_cam2.status_code == 200
    assert all(int(str(e.get("camera_id")).split(":")[-1]) == 61601 for e in r_cam2.json())

    r_latest = client.get("/api/health/history/latest/61601")
    assert r_latest.status_code == 200
    assert int(str(r_latest.json().get("camera_id")).split(":")[-1]) == 61601
    svc.clear_history()
