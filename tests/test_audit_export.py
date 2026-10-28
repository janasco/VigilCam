from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.audit_export import router as audit_export_router
from app.services import audit_export as svc
from app.services.audit import _default_store, append_audit_entry


def _make_client():
    app = FastAPI()
    app.include_router(audit_export_router)
    return TestClient(app)


def test_audit_export_import():
    import app.api.audit_export  # noqa: F401
    import app.services.audit_export  # noqa: F401
    assert audit_export_router is not None
    assert hasattr(svc, "to_csv")
    assert hasattr(svc, "to_json")


def test_audit_export_service_csv_json():
    entries = [
        {"action": "login", "actor": "admin", "ip": "127.0.0.1"},
        {"action": "logout", "actor": "bob", "camera_id": "1"},
    ]
    csv_text = svc.to_csv(entries)
    assert "action" in csv_text
    assert "login" in csv_text
    assert "logout" in csv_text
    # header + 2 rows
    lines = [ln for ln in csv_text.strip().splitlines() if ln.strip()]
    assert len(lines) == 3

    json_text = svc.to_json(entries)
    import json as _json

    data = _json.loads(json_text)
    assert isinstance(data, list)
    assert len(data) == 2

    # export_entries dispatches on format
    assert "login" in svc.export_entries(entries, format="csv")
    assert "login" in svc.export_entries(entries, format="json")
    # aliases
    assert svc.build_csv(entries) == csv_text
    assert svc.export_to_csv(entries) == csv_text


def test_audit_export_fetch_and_filter():
    _default_store.clear()
    append_audit_entry({"action": "login", "actor": "alice"})
    append_audit_entry({"action": "logout", "actor": "bob"})
    all_entries = svc.fetch_entries()
    assert any(e.get("action") == "login" for e in all_entries)
    filtered = svc.fetch_entries(action="login")
    assert filtered
    assert all(e.get("action") == "login" for e in filtered)


def test_audit_export_router_csv_json():
    _default_store.clear()
    append_audit_entry({"action": "login", "actor": "admin"})
    client = _make_client()

    r_json = client.get("/api/audit/export", params={"format": "json"})
    assert r_json.status_code == 200, r_json.text
    data = r_json.json()
    assert isinstance(data, list)
    assert any(e.get("action") == "login" for e in data)

    r_csv = client.get("/api/audit/export", params={"format": "csv"})
    assert r_csv.status_code == 200, r_csv.text
    assert "action" in r_csv.text
    assert "login" in r_csv.text
    assert "text/csv" in r_csv.headers.get("content-type", "")

    r_csv_path = client.get("/api/audit/export/csv")
    assert r_csv_path.status_code == 200, r_csv_path.text
    assert "action" in r_csv_path.text

    r_json_path = client.get("/api/audit/export/json")
    assert r_json_path.status_code == 200, r_json_path.text
    assert isinstance(r_json_path.json(), list)

    # filter via query
    r_filt = client.get("/api/audit/export", params={"format": "json", "action": "login"})
    assert r_filt.status_code == 200
    assert all(e.get("action") == "login" for e in r_filt.json())
