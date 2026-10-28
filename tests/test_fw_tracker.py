from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.fw_tracker import router as fw_tracker_router
import app.services.fw_tracker as svc
import app.services.firmware as fw


def _make_client():
    app = FastAPI()
    app.include_router(fw_tracker_router)
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
    import app.api.fw_tracker  # noqa: F401
    import app.services.fw_tracker  # noqa: F401
    assert fw_tracker_router is not None
    assert hasattr(svc, "record_sample")
    assert hasattr(svc, "record_history") or hasattr(svc, "log_sample")
    assert hasattr(svc, "check_and_record") or hasattr(svc, "record_check")
    assert hasattr(svc, "track") or hasattr(svc, "track_check")
    assert hasattr(svc, "list_history")
    assert hasattr(svc, "get_entry")
    assert hasattr(svc, "delete_entry")
    assert hasattr(svc, "clear_history")
    assert hasattr(svc, "snapshot_and_record") or hasattr(svc, "get_stats")
    assert hasattr(svc, "status")


def test_firmware_mock_has_no_version_history():
    # Verify the mock firmware check is stateless: no history store,
    # no append log, no time series — only the mock latest table.
    assert not hasattr(fw, "FW_TRACKER")
    assert not hasattr(fw, "FIRMWARE_HISTORY")
    assert not hasattr(fw, "FW_HISTORY")
    assert not hasattr(fw, "list_history")
    assert not hasattr(fw, "record_sample")
    assert not hasattr(fw, "check_and_record")
    fw.reset_mock_data()
    first = fw.check_firmware(current_version="1.0.0", model="generic", camera_id=70001)
    second = fw.check_firmware(current_version="1.0.0", model="generic", camera_id=70001)
    assert first == second
    assert "history" not in {k.lower() for k in first.keys()}
    # Stateless: repeated checks do not accumulate anywhere on the module.
    assert len([k for k in dir(fw) if "histor" in k.lower() or "track" in k.lower()]) == 0


def test_main_app_paths_present():
    # app.main auto-registers any app.api module exposing `router`
    # (see app/main.py pkgutil auto-loader), so no edit to app/main.py
    # is required. Verify our router exposes the expected history paths
    # directly without importing the heavy app.main module (which scans
    # ~300 api modules on import).
    paths = [getattr(r, "path", "") for r in fw_tracker_router.routes]
    assert any(
        p.startswith("/api/firmware/history")
        or p.startswith("/api/firmware-history")
        or p.startswith("/api/firmware_history")
        or p.startswith("/api/fw-tracker")
        or p.startswith("/api/fw_tracker")
        or p.startswith("/api/fw/tracker")
        or p.startswith("/api/firmware/tracker")
        or (p.startswith("/api/cameras/") and "firmware/history" in p)
        for p in paths
    ), paths
    # Static check: main.py contains the auto-loader.
    import pathlib

    main_py = pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py"
    text = main_py.read_text()
    assert "pkgutil.iter_modules" in text
    assert "import_module" in text


def test_service_record_list_get_delete():
    svc.clear_history()
    entry = svc.record_sample(camera_id=70101, model="generic", current_version="1.0.0")
    assert entry.get("id")
    assert entry.get("camera_id") == 70101
    assert entry.get("model") == "generic"
    assert entry.get("current_version") == "1.0.0"
    assert entry.get("latest_version") == "2.5.0"
    assert entry.get("update_available") is True
    assert entry.get("checked_at")
    assert entry.get("created_at")

    items = svc.list_history()
    assert any(str(e.get("id")) == str(entry.get("id")) for e in items)

    fetched = svc.get_entry(entry.get("id"))
    assert fetched is not None
    assert str(fetched.get("id")) == str(entry.get("id"))

    filtered = svc.list_history(camera_id=70101)
    assert filtered
    assert all(int(str(e.get("camera_id")).split(":")[-1]) == 70101 for e in filtered)

    filtered_model = svc.list_history(model="generic")
    assert any(str(e.get("id")) == str(entry.get("id")) for e in filtered_model)

    assert svc.delete_entry(entry.get("id")) is True
    assert svc.get_entry(entry.get("id")) is None
    svc.clear_history()


def test_service_history_tracking_appends():
    svc.clear_history()
    fw.reset_mock_data()
    # Stateless mock check returns the same result twice...
    r1 = fw.check_firmware(current_version="1.0.0", model="generic", camera_id=70201)
    r2 = fw.check_firmware(current_version="1.0.0", model="generic", camera_id=70201)
    assert r1 == r2
    # ...while the tracker appends every check as its own entry.
    svc.record_sample(camera_id=70201, model="generic", current_version="1.0.0")
    svc.record_sample(camera_id=70201, model="generic", current_version="2.5.0")
    items = svc.list_history(camera_id=70201)
    assert len(items) == 2
    assert items[0].get("current_version") == "1.0.0"
    assert items[1].get("current_version") == "2.5.0"
    # Per-camera stats.
    stats = svc.get_stats(camera_id=70201)
    assert stats["total"] == 2
    assert stats["update_available"] == 1
    assert stats["up_to_date"] == 1
    # Latest helper.
    latest = svc.get_latest_for_camera(70201)
    assert latest is not None
    assert latest.get("current_version") == "2.5.0"
    svc.clear_history()


def test_service_aliases_and_clear():
    svc.clear_history()
    a = svc.log_sample(camera_id=70301, model="generic", current_version="1.0.0")
    b = svc.record_history(camera_id=70301, model="generic", current_version="2.5.0")
    c = svc.add_entry(camera_id=70302, model="vigilcam-pro", current_version="3.1.2")
    d = svc.track(camera_id=70303, model="generic", current_version="0.1.0")
    e = svc.check_and_record(camera_id=70303, model="generic", current_version="2.5.0")
    assert a.get("id") and b.get("id") and c.get("id") and d.get("id") and e.get("id")
    assert len(svc.fetch_history()) >= 5
    assert svc.get(a.get("id")) is not None
    assert svc.status().get("total") >= 5
    # Snapshot from live mock firmware check.
    snap = svc.snapshot_and_record(70304, model="generic", current_version="1.0.0")
    assert snap.get("camera_id") == 70304
    assert snap.get("update_available") is True
    n = svc.clear()
    assert n >= 6
    assert svc.list_history() == []
    svc.clear_history()


def test_service_check_and_record_csv_json():
    svc.clear_history()
    fw.reset_mock_data()
    entry = svc.check_and_record(camera_id=70401, model="generic", current_version="1.0.0")
    assert entry.get("camera_id") == 70401
    assert entry.get("latest_version") == "2.5.0"
    assert entry.get("status") == "update_available"
    assert len(svc.list_history(camera_id=70401)) == 1
    # Model-level tracking without a camera id.
    m_entry = svc.check_and_record(model="generic", current_version="1.0.0")
    assert m_entry.get("model") == "generic"
    assert m_entry.get("latest_version") == "2.5.0"
    text = svc.to_csv(svc.list_history())
    assert "camera_id" in text
    assert "current_version" in text
    js = svc.to_json(svc.list_history())
    assert "70401" in js
    svc.clear_history()


def test_router_list_create_get_delete():
    svc.clear_history()
    client = _make_client()

    r = client.post("/api/firmware/history", json={"camera_id": 70501, "model": "generic", "current_version": "1.0.0"})
    assert r.status_code in (200, 201), r.text
    created = r.json()
    eid = created.get("id") or created.get("entry_id") or created.get("history_id")
    assert eid
    assert created.get("latest_version") == "2.5.0"

    r_list = client.get("/api/firmware/history")
    assert r_list.status_code == 200, r_list.text
    assert isinstance(r_list.json(), list)
    assert any(str(e.get("id")) == str(eid) for e in r_list.json())

    r_get = client.get(f"/api/firmware/history/{eid}")
    assert r_get.status_code == 200, r_get.text

    r_del = client.delete(f"/api/firmware/history/{eid}")
    assert r_del.status_code == 200, r_del.text

    r_missing = client.get(f"/api/firmware/history/{eid}")
    assert r_missing.status_code == 404
    svc.clear_history()


def test_router_alias_paths_and_filters():
    svc.clear_history()
    svc.record_sample(camera_id=70601, model="generic", current_version="1.0.0")
    svc.record_sample(camera_id=70602, model="vigilcam-pro", current_version="3.1.2")
    client = _make_client()

    r = client.get("/api/firmware/history", params={"camera_id": 70601})
    assert r.status_code == 200, r.text
    assert all(int(str(e.get("camera_id")).split(":")[-1]) == 70601 for e in r.json())

    r2 = client.get("/api/firmware-history")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)

    r3 = client.get("/api/firmware_history")
    assert r3.status_code == 200, r3.text

    r4 = client.get("/api/fw-tracker")
    assert r4.status_code == 200, r4.text
    assert isinstance(r4.json(), list)

    r_health = client.get("/api/firmware/history/health")
    assert r_health.status_code == 200
    assert r_health.json().get("status") == "ok"

    r_count = client.get("/api/firmware/history/count")
    assert r_count.status_code == 200
    assert r_count.json().get("count") >= 2

    r_status = client.get("/api/firmware/history/status")
    assert r_status.status_code == 200
    assert r_status.json().get("ok") is True

    r_summary = client.get("/api/firmware/history/summary")
    assert r_summary.status_code == 200
    assert r_summary.json().get("total") >= 2

    r_cam = client.get("/api/firmware/history/camera/70601")
    assert r_cam.status_code == 200
    assert all(int(str(e.get("camera_id")).split(":")[-1]) == 70601 for e in r_cam.json())

    r_cam2 = client.get("/api/cameras/70601/firmware/history")
    assert r_cam2.status_code == 200
    assert all(int(str(e.get("camera_id")).split(":")[-1]) == 70601 for e in r_cam2.json())

    r_latest = client.get("/api/firmware/history/latest/70601")
    assert r_latest.status_code == 200
    assert int(str(r_latest.json().get("camera_id")).split(":")[-1]) == 70601

    r_model = client.get("/api/firmware/history/model/generic")
    assert r_model.status_code == 200
    assert all(e.get("model") == "generic" for e in r_model.json())
    svc.clear_history()
