import asyncio

from fastapi.testclient import TestClient

from app.main import app


def test_summary_import():
    import app.api.summary  # noqa: F401
    import app.services.summary  # noqa: F401
    from app.api.summary import router  # noqa: F401
    from app.services.summary import get_dashboard_summary  # noqa: F401


def test_dashboard_path_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/summary/dashboard" in paths


def test_dashboard_returns_payload():
    client = TestClient(app)
    r = client.get("/api/summary/dashboard")
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, dict)
    # Core aggregate sections from single call.
    for key in ("cameras", "events", "recordings", "storage", "system"):
        assert key in data, f"missing section: {key}"
    assert isinstance(data["cameras"], dict)
    assert isinstance(data["events"], dict)
    assert isinstance(data["recordings"], dict)
    assert isinstance(data["storage"], dict)
    assert "timestamp" in data or "time" in data


def test_dashboard_graceful_values():
    client = TestClient(app)
    r = client.get("/api/summary/dashboard")
    assert r.status_code == 200
    data = r.json()
    # Camera/event counts may be 0/None on empty DB but keys must exist.
    assert "total" in data["cameras"] or "count" in data["cameras"]
    assert "total" in data["events"] or "count" in data["events"]


def test_service_single_call():
    from app.services.summary import get_dashboard_summary

    data = asyncio.run(get_dashboard_summary(None))
    assert isinstance(data, dict)
    for key in ("cameras", "events", "recordings", "storage", "system"):
        assert key in data
