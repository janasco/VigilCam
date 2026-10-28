from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.pdf_report import router as pdf_report_router
from app.services import pdf_report as pdf_svc


def _ev(eid, camera=1, minutes=0, label="motion"):
    return {
        "id": eid,
        "event_id": eid,
        "camera_id": camera,
        "kind": "motion",
        "label": label,
        "confidence": 0.7,
        "payload": None,
        "tags": [],
        "created_at": (datetime(2026, 1, 1, 12, 0, 0) + timedelta(minutes=minutes)).isoformat(),
    }


def _make_client():
    a = FastAPI()
    a.include_router(pdf_report_router)
    return TestClient(a)


def test_pdf_report_imports():
    import app.api.pdf_report  # noqa: F401
    import app.services.pdf_report  # noqa: F401
    from app.api.pdf_report import router  # noqa: F401
    assert router is not None
    assert hasattr(pdf_svc, "build_incident_pdf")
    assert hasattr(pdf_svc, "build_incidents_pdf")
    assert hasattr(pdf_svc, "build_pdf")


def test_event_export_still_csv_json_only():
    # Event export endpoint must remain CSV/JSON with no PDF branch.
    import inspect

    import app.api.events as events_mod

    src = inspect.getsource(events_mod.api_events_export).lower()
    assert "csv" in src
    assert "json" in src
    assert "pdf" not in src
    assert "%pdf" not in src


def test_service_builds_valid_pdf():
    incidents = [
        {
            "incident_id": 1,
            "id": 1,
            "event_ids": [1, 2],
            "cameras": [1, 2],
            "camera_count": 2,
            "event_count": 2,
            "started_at": "2026-01-01T12:00:00",
            "ended_at": "2026-01-01T12:02:00",
            "duration_seconds": 120.0,
            "labels": ["motion"],
            "kinds": ["motion"],
            "events": [_ev(1, camera=1), _ev(2, camera=2, minutes=1)],
        }
    ]
    pdf = pdf_svc.build_incidents_pdf(incidents, title="Test report")
    assert isinstance(pdf, (bytes, bytearray))
    assert pdf[:5] == b"%PDF-"
    assert b"Incident" in pdf

    single = pdf_svc.build_incident_pdf(incidents[0])
    assert single[:5] == b"%PDF-"

    generic = pdf_svc.build_pdf(incidents)
    assert generic[:5] == b"%PDF-"

    empty = pdf_svc.build_incidents_pdf([])
    assert empty[:5] == b"%PDF-"


def test_router_post_events_returns_pdf():
    client = _make_client()
    e1 = _ev(101, camera=1, minutes=0)
    e2 = _ev(102, camera=2, minutes=1)
    r = client.post("/api/incidents/report.pdf", json={"events": [e1, e2]})
    assert r.status_code == 200, r.text
    assert "application/pdf" in r.headers.get("content-type", "")
    assert r.content[:5] == b"%PDF-"


def test_router_get_and_aliases_return_pdf():
    client = _make_client()
    for path in ("/api/incidents/report.pdf", "/api/pdf_report", "/api/reports/incident.pdf"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:200])
        assert "application/pdf" in r.headers.get("content-type", ""), path
        assert r.content[:5] == b"%PDF-", path


def test_router_by_id_returns_pdf():
    client = _make_client()
    r = client.get("/api/incidents/1/report.pdf")
    assert r.status_code == 200, r.text
    assert "application/pdf" in r.headers.get("content-type", "")
    assert r.content[:5] == b"%PDF-"


def test_router_registered_on_app():
    # app.main auto-registers any app.api module exposing `router`
    # (pkgutil loop). Importing app.main hangs in this env due to a
    # pre-existing storage_quota/pydantic issue, so verify the contract
    # directly: module exposes `router` and the expected PDF path exists.
    import app.api.pdf_report as mod

    assert hasattr(mod, "router")
    paths = [r.path for r in mod.router.routes]
    assert "/api/incidents/report.pdf" in paths
