import os

from fastapi.testclient import TestClient

from app.main import app


def test_system_metrics_path_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/system/metrics" in paths


def test_system_metrics_returns_payload():
    client = TestClient(app)
    r = client.get("/api/system/metrics")
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, dict)
    # CPU load
    assert "cpu" in data
    assert data["cpu"].get("load_1m") is not None or data.get("cpu_load") is not None
    # Memory usage
    assert "memory" in data
    assert data["memory"].get("total") is not None
    assert data["memory"].get("used") is not None
    # Disk usage
    assert "disk" in data
    assert data["disk"].get("total") is not None
    assert data["disk"].get("used") is not None
    # Active camera/process counts
    assert "cameras" in data
    assert "active" in data["cameras"] or "active_count" in data["cameras"]
    assert "processes" in data
    assert ("count" in data["processes"]) or ("running" in data["processes"])


def test_system_metrics_graceful_values():
    client = TestClient(app)
    r = client.get("/api/system/metrics")
    assert r.status_code == 200
    data = r.json()
    # Should not crash even if some values missing; keys present.
    for key in ("cpu", "memory", "disk", "cameras", "processes"):
        assert key in data
