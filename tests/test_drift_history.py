from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.drift_history import router as drift_history_router
import app.services.drift_history as svc
import app.services.tamper as tm


def _make_client():
    app = FastAPI()
    app.include_router(drift_history_router)
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


def _mid_gray(n=256, value=128):
    return [value] * n


def _dark(n=256):
    return [2] * n


def test_imports():
    import app.api.drift_history  # noqa: F401
    import app.services.drift_history  # noqa: F401
    assert drift_history_router is not None
    assert hasattr(svc, "record_sample")
    assert hasattr(svc, "record_history") or hasattr(svc, "log_sample")
    assert hasattr(svc, "record_drift") or hasattr(svc, "compare_and_record")
    assert hasattr(svc, "list_history")
    assert hasattr(svc, "get_entry")
    assert hasattr(svc, "delete_entry")
    assert hasattr(svc, "clear_history")
    assert hasattr(svc, "compare_and_record") or hasattr(svc, "sample_and_record")
    assert hasattr(svc, "status")


def test_tamper_point_in_time_has_no_history():
    # Verify base tamper compare is point-in-time only: single drift per call,
    # no history accumulation in the tamper service.
    tm.clear()
    cid = 9911
    tm.set_reference(cid, _mid_gray())
    r1 = tm.compare_frame(cid, _mid_gray())
    r2 = tm.compare_frame(cid, _mid_gray())
    assert r1["drift"] == r2["drift"]
    assert "history" not in r1
    assert not hasattr(tm, "DRIFT_HISTORY")
    assert not hasattr(tm, "list_history")
    assert not hasattr(tm, "drift_history")
    tm.clear()


def test_main_app_paths_present():
    from app.main import app as main_app
    paths = _all_paths(main_app)
    assert any(
        p.startswith("/api/drift/history")
        or p.startswith("/api/drift-history")
        or p.startswith("/api/drift_history")
        or p.startswith("/api/tamper/drift/history")
        or p.startswith("/api/tamper/history")
        for p in paths
    ), paths


def test_service_record_list_get_delete():
    svc.clear_history()
    entry = svc.record_sample(camera_id=9912, drift=0.12, threshold=0.35)
    assert entry.get("id")
    assert entry.get("camera_id") == 9912
    assert entry.get("drift") == 0.12
    assert entry.get("tampered") is False

    items = svc.list_history()
    assert any(str(e.get("id")) == str(entry.get("id")) for e in items)

    fetched = svc.get_entry(entry.get("id"))
    assert fetched is not None
    assert str(fetched.get("id")) == str(entry.get("id"))

    filtered = svc.list_history(camera_id=9912)
    assert filtered
    assert all(int(e.get("camera_id")) == 9912 for e in filtered)

    assert svc.delete_entry(entry.get("id")) is True
    assert svc.get_entry(entry.get("id")) is None
    svc.clear_history()


def test_service_history_tracking_appends():
    svc.clear_history()
    tm.clear()
    # Same camera compared twice -> tamper itself stores no series...
    cid = 9913
    tm.set_reference(cid, _mid_gray())
    tm.compare_frame(cid, _mid_gray())
    tm.compare_frame(cid, _dark())
    assert not hasattr(tm, "DRIFT_HISTORY")
    # ...while drift history appends every sample as its own entry.
    svc.record_sample(camera_id=cid, drift=0.02, threshold=0.35)
    svc.record_sample(camera_id=cid, drift=0.9, threshold=0.35)
    items = svc.list_history(camera_id=cid)
    assert len(items) == 2
    assert items[0].get("drift") == 0.02
    assert items[1].get("drift") == 0.9
    # Per-camera stats.
    stats = svc.get_stats(camera_id=cid)
    assert stats["total"] == 2
    assert stats["tampered"] == 1
    assert stats["avg_drift"] == round((0.02 + 0.9) / 2, 6)
    assert stats["max_drift"] == 0.9
    assert stats["min_drift"] == 0.02
    # Latest helper.
    latest = svc.get_latest_for_camera(cid)
    assert latest is not None
    assert latest.get("drift") == 0.9
    svc.clear_history()
    tm.clear()


def test_service_aliases_and_clear():
    svc.clear_history()
    a = svc.log_sample(camera_id=9914, drift=0.1)
    b = svc.record_history(camera_id=9914, drift=0.2)
    c = svc.record_drift(camera_id=9915, drift=0.5)
    assert a.get("id") and b.get("id") and c.get("id")
    assert len(svc.fetch_history()) >= 3
    assert svc.get(a.get("id")) is not None
    assert svc.status().get("total") >= 3
    n = svc.clear()
    assert n >= 3
    assert svc.list_history() == []
    svc.clear_history()


def test_service_compare_and_record():
    svc.clear_history()
    tm.clear()
    cid = 9916
    tm.set_reference(cid, _mid_gray())
    entry = svc.compare_and_record(cid, _mid_gray())
    assert entry.get("camera_id") == cid
    assert entry.get("drift") is not None
    assert entry.get("drift") < 0.05
    assert entry.get("tampered") is False
    assert len(svc.list_history(camera_id=cid)) == 1
    # CSV / JSON helpers.
    text = svc.to_csv(svc.list_history())
    assert "drift" in text
    js = svc.to_json(svc.list_history())
    assert str(cid) in js
    svc.clear_history()
    tm.clear()


def test_router_list_create_get_delete():
    svc.clear_history()
    client = _make_client()

    r = client.post("/api/drift/history", json={"camera_id": 9917, "drift": 0.22})
    assert r.status_code in (200, 201), r.text
    created = r.json()
    eid = created.get("id") or created.get("entry_id") or created.get("history_id")
    assert eid

    r_list = client.get("/api/drift/history")
    assert r_list.status_code == 200, r_list.text
    assert isinstance(r_list.json(), list)
    assert any(str(e.get("id")) == str(eid) for e in r_list.json())

    r_get = client.get(f"/api/drift/history/{eid}")
    assert r_get.status_code == 200, r_get.text

    r_del = client.delete(f"/api/drift/history/{eid}")
    assert r_del.status_code == 200, r_del.text

    r_missing = client.get(f"/api/drift/history/{eid}")
    assert r_missing.status_code == 404
    svc.clear_history()


def test_router_alias_paths_and_filters():
    svc.clear_history()
    svc.record_sample(camera_id=9918, drift=0.05, tampered=False)
    svc.record_sample(camera_id=9919, drift=0.8, tampered=True)
    client = _make_client()

    r = client.get("/api/drift-history", params={"camera_id": 9918})
    assert r.status_code == 200, r.text
    assert all(int(e.get("camera_id")) == 9918 for e in r.json())

    r2 = client.get("/api/tamper/drift/history")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)

    r3 = client.get("/api/tamper/history")
    assert r3.status_code == 200, r3.text

    r_health = client.get("/api/drift/history/health")
    assert r_health.status_code == 200
    assert r_health.json().get("status") == "ok"

    r_count = client.get("/api/drift/history/count")
    assert r_count.status_code == 200
    assert r_count.json().get("count") >= 2

    r_status = client.get("/api/drift/history/status")
    assert r_status.status_code == 200
    assert r_status.json().get("ok") is True

    r_summary = client.get("/api/drift/history/summary")
    assert r_summary.status_code == 200
    assert r_summary.json().get("total") >= 2

    r_cam = client.get("/api/drift/history/camera/9918")
    assert r_cam.status_code == 200
    assert all(int(e.get("camera_id")) == 9918 for e in r_cam.json())

    r_latest = client.get("/api/drift/history/latest/9918")
    assert r_latest.status_code == 200
    assert int(r_latest.json().get("camera_id")) == 9918
    svc.clear_history()
