from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.export_history import router as export_history_router
import app.services.export_history as svc


def _make_client():
    app = FastAPI()
    app.include_router(export_history_router)
    return TestClient(app)


def test_imports():
    import app.api.export_history  # noqa: F401
    import app.services.export_history  # noqa: F401
    assert export_history_router is not None
    assert hasattr(svc, "record_export")
    assert hasattr(svc, "list_history")
    assert hasattr(svc, "get_entry")
    assert hasattr(svc, "delete_entry")
    assert hasattr(svc, "clear_history")


def test_service_record_list_get_delete():
    svc.clear_history()
    entry = svc.record_export(camera_id="1", camera_name="cam1", start="2024-01-01", end="2024-01-02", format="mp4", status="completed")
    assert entry.get("id")
    assert entry.get("camera_id") == "1"

    items = svc.list_history()
    assert any(str(e.get("id")) == str(entry.get("id")) for e in items)

    fetched = svc.get_entry(entry.get("id"))
    assert fetched is not None
    assert str(fetched.get("id")) == str(entry.get("id"))

    # filter by camera
    filtered = svc.list_history(camera_id="1")
    assert filtered
    assert all(str(e.get("camera_id")) == "1" for e in filtered)

    assert svc.delete_entry(entry.get("id")) is True
    assert svc.get_entry(entry.get("id")) is None


def test_service_aliases_and_clear():
    svc.clear_history()
    a = svc.log_export(camera_id="7", status="completed")
    b = svc.add_entry(camera_id="8", status="failed")
    assert a.get("id") and b.get("id")
    assert len(svc.fetch_history()) >= 2
    assert svc.get(a.get("id")) is not None
    n = svc.clear()
    assert n >= 2
    assert svc.list_history(include_jobs=False) == []


def test_router_list_create_get_delete():
    svc.clear_history()
    client = _make_client()

    r = client.post("/api/export/history", json={"camera_id": "3", "status": "completed", "format": "mp4"})
    assert r.status_code in (200, 201), r.text
    created = r.json()
    eid = created.get("id") or created.get("entry_id") or created.get("job_id")
    assert eid

    r_list = client.get("/api/export/history")
    assert r_list.status_code == 200, r_list.text
    assert isinstance(r_list.json(), list)
    assert any(str(e.get("id")) == str(eid) for e in r_list.json())

    r_get = client.get(f"/api/export/history/{eid}")
    assert r_get.status_code == 200, r_get.text

    r_del = client.delete(f"/api/export/history/{eid}")
    assert r_del.status_code == 200, r_del.text

    r_missing = client.get(f"/api/export/history/{eid}")
    assert r_missing.status_code == 404


def test_router_alias_paths_and_filters():
    svc.clear_history()
    svc.record_export(camera_id="10", status="completed")
    svc.record_export(camera_id="11", status="failed")
    client = _make_client()

    r = client.get("/api/exports/history", params={"camera_id": "10"})
    assert r.status_code == 200, r.text
    assert all(str(e.get("camera_id")) == "10" for e in r.json())

    r2 = client.get("/api/export-history")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)

    r_health = client.get("/api/export/history/health")
    assert r_health.status_code == 200
    assert r_health.json().get("status") == "ok"
