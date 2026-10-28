"""Tests for NTP / camera time-sync status service."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.services import time_sync as ts


def test_time_sync_imports():
    import app.api.time_sync as api_mod  # noqa: F401
    import app.services.time_sync as svc_mod  # noqa: F401

    assert hasattr(api_mod, "router")
    assert hasattr(svc_mod, "get_time_sync_status")
    assert hasattr(svc_mod, "sync_camera_time")


def test_time_sync_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/time-sync/status" in paths
    assert "/api/time-sync/nvr-time" in paths
    assert "/api/time-sync/ntp" in paths
    assert "/api/time-sync/camera-time" in paths
    assert "/api/time-sync/sync" in paths
    # Underscore alias served too.
    assert "/api/time_sync/status" in paths


def test_get_nvr_time_shape():
    out = ts.get_nvr_time()
    assert out["ok"] is True
    assert "nvr_time" in out
    # ISO parseable.
    assert ts._parse_iso(out["nvr_time"]) is not None


def test_evaluate_sync_thresholds():
    now = datetime.now(UTC)
    cam = (now + timedelta(seconds=2)).isoformat()
    verdict = ts.evaluate_sync(cam, tolerance_seconds=5.0, nvr_iso=now.isoformat())
    assert verdict["in_sync"] is True
    assert verdict["status"] == "in-sync"
    far = (now + timedelta(seconds=60)).isoformat()
    verdict2 = ts.evaluate_sync(far, tolerance_seconds=5.0, nvr_iso=now.isoformat())
    assert verdict2["in_sync"] is False
    assert verdict2["status"] == "drifted"


def test_status_nvr_only():
    import asyncio

    out = asyncio.run(ts.get_time_sync_status())
    assert out["ok"] is True
    assert out["status"] == "nvr-only"
    assert out["camera_time"] is None


def test_get_camera_time_reports_without_ip():
    import asyncio

    out = asyncio.run(ts.get_camera_time(ip=""))
    assert out["ok"] is False
    assert "required" in str(out.get("error", "")).lower()


def test_get_camera_time_via_onvif_mock(monkeypatch):
    import asyncio

    now = datetime.now(UTC)

    async def fake_get(ip, port=80, scheme="http", timeout=10.0):
        return {"utc_datetime": now.isoformat(), "camera_timezone": "UTC0"}

    monkeypatch.setattr(ts._ods, "get_system_datetime", fake_get, raising=True)
    out = asyncio.run(ts.get_camera_time(ip="10.0.0.9"))
    assert out["ok"] is True
    assert out["camera_time"] == now.isoformat()
    assert out["drift_seconds"] is not None
    assert abs(out["drift_seconds"]) < 30


def test_sync_camera_time_via_onvif_mock(monkeypatch):
    import asyncio

    now = datetime.now(UTC)

    async def fake_set(ip, username, password, port=80, scheme="http"):
        return {"synced_utc": now.isoformat()}

    async def fake_get(ip, port=80, scheme="http", timeout=10.0):
        return {"utc_datetime": now.isoformat(), "camera_timezone": "UTC0"}

    monkeypatch.setattr(ts._ods, "set_system_datetime", fake_set, raising=True)
    monkeypatch.setattr(ts._ods, "get_system_datetime", fake_get, raising=True)
    out = asyncio.run(ts.sync_camera_time(ip="10.0.0.9", username="admin", password="pw"))
    assert out["ok"] is True
    assert out["status"] == "synced"
    assert out["method"] == "onvif"


def test_sync_requires_credentials():
    import asyncio

    out = asyncio.run(ts.sync_camera_time(ip="10.0.0.9", username="", password=""))
    # Either unavailable-method or missing-credential report; both ok False.
    assert out["ok"] is False


def test_endpoints_live():
    client = TestClient(app)
    r = client.get("/api/time-sync/status")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r2 = client.get("/api/time-sync/nvr-time")
    assert r2.status_code == 200
    assert "nvr_time" in r2.json()

    r3 = client.get("/api/time-sync/ntp")
    assert r3.status_code == 200
    assert "synchronized" in r3.json() or "ntp_synchronized" in r3.json()

    # Missing ip -> report, not 500.
    r4 = client.get("/api/time-sync/camera-time")
    assert r4.status_code == 200

    # Underscore alias.
    ru = client.get("/api/time_sync/status")
    assert ru.status_code == 200
