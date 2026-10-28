from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.notify_export import router as notify_export_router
from app.services import notify_export as svc
from app.services import notify_history as hist


def _make_client():
    app = FastAPI()
    app.include_router(notify_export_router)
    return TestClient(app)


def test_notify_export_import():
    import app.api.notify_export  # noqa: F401
    import app.services.notify_export  # noqa: F401
    assert notify_export_router is not None
    assert hasattr(svc, "to_csv")
    assert hasattr(svc, "to_json")
    assert hasattr(svc, "fetch_entries")
    assert hasattr(svc, "export_entries")


def test_notify_export_service_csv_json():
    entries = [
        {"channel": "email", "recipient": "a@example.com", "status": "sent", "message": "hello"},
        {"channel": "sms", "recipient": "123", "status": "failed", "message": "hi"},
    ]
    csv_text = svc.to_csv(entries)
    assert "channel" in csv_text
    assert "email" in csv_text
    assert "sms" in csv_text
    lines = [ln for ln in csv_text.strip().splitlines() if ln.strip()]
    assert len(lines) == 3

    json_text = svc.to_json(entries)
    import json as _json

    data = _json.loads(json_text)
    assert isinstance(data, list)
    assert len(data) == 2

    assert "email" in svc.export_entries(entries, format="csv")
    assert "email" in svc.export_entries(entries, format="json")
    assert svc.build_csv(entries) == csv_text
    assert svc.export_to_csv(entries) == csv_text


def test_notify_export_fetch_and_filter():
    hist.clear_history()
    try:
        from app.services import notify_digest as _nd
        _nd.clear()
    except Exception:
        pass
    try:
        from app.services import webhook_queue as _wq
        _wq.clear()
    except Exception:
        pass
    hist.record_history(channel="email", recipient="a@example.com", status="sent", message="hello", camera_id="cam1")
    hist.record_history(channel="sms", recipient="123", status="failed", message="hi", camera_id="cam2")
    all_entries = svc.fetch_entries()
    assert any(e.get("channel") == "email" for e in all_entries)
    filtered = svc.fetch_entries(channel="email")
    assert filtered
    assert all(e.get("channel") == "email" for e in filtered)
    hist.clear_history()


def test_notify_export_router_csv_json():
    hist.clear_history()
    try:
        from app.services import notify_digest as _nd
        _nd.clear()
    except Exception:
        pass
    try:
        from app.services import webhook_queue as _wq
        _wq.clear()
    except Exception:
        pass
    hist.record_history(channel="email", recipient="a@example.com", status="sent", message="hello")
    client = _make_client()

    r_json = client.get("/api/notify/export", params={"format": "json"})
    assert r_json.status_code == 200, r_json.text
    data = r_json.json()
    assert isinstance(data, list)
    assert any(e.get("channel") == "email" for e in data)

    r_csv = client.get("/api/notify/export", params={"format": "csv"})
    assert r_csv.status_code == 200, r_csv.text
    assert "channel" in r_csv.text
    assert "email" in r_csv.text
    assert "text/csv" in r_csv.headers.get("content-type", "")

    r_csv_path = client.get("/api/notify/export/csv")
    assert r_csv_path.status_code == 200, r_csv_path.text
    assert "channel" in r_csv_path.text

    r_json_path = client.get("/api/notify/export/json")
    assert r_json_path.status_code == 200, r_json_path.text
    assert isinstance(r_json_path.json(), list)

    r_filt = client.get("/api/notify/export", params={"format": "json", "channel": "email"})
    assert r_filt.status_code == 200
    assert all(e.get("channel") == "email" for e in r_filt.json())
    hist.clear_history()


def test_main_app_paths_present():
    from app.main import app as main_app

    paths = list(main_app.openapi()["paths"].keys())
    assert any(
        p.startswith("/api/notify/export")
        or p.startswith("/api/notify-export")
        or p.startswith("/api/notifications/export")
        for p in paths
    ), paths
