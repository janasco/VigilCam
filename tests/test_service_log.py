from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.service_log import router as service_log_router  # noqa: F401
from app.services import service_log as service_log_service_mod  # noqa: F401
from app.services.service_log import (  # noqa: F401
    create_entry,
    list_entries,
    get_entry,
    update_entry,
    delete_entry,
    clear,
    service_log_service,
)


def test_service_log_import():
    import app.api.service_log  # noqa: F401
    import app.services.service_log  # noqa: F401
    from app.api.service_log import router  # noqa: F401
    from app.services.service_log import create_entry, list_entries  # noqa: F401


def test_service_log_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/cameras/{camera_id}/service-log" in paths
    assert "/api/camera-service-log" in paths or "/api/camera_service_log" in paths


def test_service_log_distinct_from_notes_audit_maintenance():
    # camera_notes are freeform (text/body), audit is system-wide,
    # maintenance is calendar suppression windows -- none is a per-camera
    # structured service history.
    import app.services.camera_notes as notes
    import app.services.audit as audit
    import app.services.maintenance as maint
    import app.services.service_log as slog

    assert hasattr(slog, "create_entry")
    assert hasattr(slog, "list_entries")
    # service log carries structured service fields notes lack
    slog.clear()
    entry = slog.create_entry(camera_id=1, service_type="cleaning", summary="lens cleaned")
    assert entry["service_type"] == "cleaning"
    assert entry["summary"] == "lens cleaned"
    assert "performed_at" in entry
    slog.clear()
    # neighbours do not expose the service-log API
    assert not hasattr(notes, "create_entry")
    assert not hasattr(notes, "list_entries")
    assert not hasattr(maint, "create_entry")
    assert not hasattr(audit, "create_entry")


def test_service_log_service_crud():
    service_log_service_mod.clear()
    entry = create_entry(camera_id=1, service_type="cleaning", summary="lens cleaned")
    assert entry["camera_id"] == 1
    assert entry["service_type"] == "cleaning"
    assert entry["summary"] == "lens cleaned"
    assert entry["id"] is not None

    items = list_entries(camera_id=1)
    assert len(items) == 1

    fetched = get_entry(entry["id"])
    assert fetched is not None
    assert fetched["summary"] == "lens cleaned"

    updated = update_entry(entry["id"], summary="world")
    assert updated["summary"] == "world"

    assert delete_entry(entry["id"]) is True
    assert get_entry(entry["id"]) is None
    assert list_entries(camera_id=1) == []


def test_service_log_service_filter_by_type():
    service_log_service_mod.clear()
    create_entry(camera_id=5, service_type="cleaning", summary="a")
    create_entry(camera_id=5, service_type="firmware", summary="b")
    assert len(list_entries(camera_id=5)) == 2
    assert len(list_entries(camera_id=5, service_type="cleaning")) == 1
    service_log_service_mod.clear()


def test_service_log_api_crud():
    service_log_service_mod.clear()
    a = FastAPI()
    a.include_router(service_log_router)
    client = TestClient(a)

    r = client.post("/api/cameras/42/service-log",
                    json={"service_type": "cleaning", "summary": "lens cleaned", "performed_by": "op"})
    assert r.status_code == 200, r.text
    created = r.json()
    assert int(created["camera_id"]) == 42
    assert created["service_type"] == "cleaning"
    assert created["summary"] == "lens cleaned"
    eid = created["id"]

    lst = client.get("/api/cameras/42/service-log")
    assert lst.status_code == 200
    assert any(n["id"] == eid for n in lst.json())

    single = client.get(f"/api/cameras/42/service-log/{eid}")
    assert single.status_code == 200
    assert single.json()["id"] == eid

    upd = client.put(f"/api/cameras/42/service-log/{eid}", json={"summary": "updated"})
    assert upd.status_code == 200
    assert upd.json()["summary"] == "updated"

    top = client.get(f"/api/camera-service-log/{eid}")
    assert top.status_code == 200

    dele = client.delete(f"/api/cameras/42/service-log/{eid}")
    assert dele.status_code == 200
    assert client.get(f"/api/cameras/42/service-log/{eid}").status_code == 404


def test_service_log_api_top_level():
    service_log_service_mod.clear()
    a = FastAPI()
    a.include_router(service_log_router)
    client = TestClient(a)

    r = client.post("/api/camera-service-log",
                    json={"camera_id": 7, "service_type": "firmware", "summary": "top-level"})
    assert r.status_code == 200, r.text
    eid = r.json()["id"]

    lst = client.get("/api/camera-service-log", params={"camera_id": 7})
    assert lst.status_code == 200
    assert any(n["id"] == eid for n in lst.json())

    bad = client.post("/api/camera-service-log", json={"summary": "missing camera"})
    assert bad.status_code == 400
