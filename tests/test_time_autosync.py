"""Tests for the opt-in auto time-sync scheduler."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
import app.services.time_autosync as svc
from app.services.time_autosync import TimeAutosyncScheduler


@pytest.fixture(autouse=True)
def _clean_state():
    prev = svc.get_config()
    svc.update_config(
        enabled=False,
        interval_seconds=3600,
        tolerance_seconds=5.0,
        timeout=10.0,
        auto_sync=True,
    )
    try:
        svc.stop_time_autosync_scheduler()
    except Exception:  # noqa: BLE001
        pass
    yield
    svc.update_config(**prev)
    try:
        svc.stop_time_autosync_scheduler()
    except Exception:  # noqa: BLE001
        pass


def test_time_sync_is_manual_only_without_autosync():
    """time_sync itself never auto-syncs; autosync is opt-in and off by default."""
    import inspect

    import app.services.time_sync as ts

    # No scheduler state / entrypoints live on the time_sync module itself
    # (check vars() because the module has a tolerant __getattr__ shim).
    for name in ("run_scheduled_sync", "start_time_autosync_scheduler",
                 "stop_time_autosync_scheduler", "time_autosync_scheduler",
                 "enabled", "interval_seconds"):
        assert name not in vars(ts), name
    # No background loop / drift-triggered auto push in the on-demand module.
    src = inspect.getsource(ts)
    for token in ("threading", "BackgroundScheduler", "create_task", "asyncio.sleep",
                  "interval_seconds", "auto_sync", "autosync"):
        assert token not in src, token
    cfg = svc.get_config()
    assert cfg["enabled"] is False
    assert svc.get_status()["enabled"] is False


def test_config_update_roundtrip():
    out = svc.update_config(enabled=True, interval_seconds=120, tolerance_seconds=2.5)
    assert out["enabled"] is True
    assert out["interval_seconds"] == 120
    assert out["tolerance_seconds"] == 2.5
    # Interval floors at the minimum.
    out = svc.update_config(interval_seconds=1)
    assert out["interval_seconds"] == svc.MIN_INTERVAL_SECONDS
    svc.update_config(enabled=False)


def test_scheduled_sync_pushes_only_drifted(monkeypatch):
    import asyncio

    import app.services.time_sync as ts

    async def fake_status(ip="", port=80, scheme="http", tolerance_seconds=5.0, timeout=10.0):
        drifted = ip == "10.0.0.2"
        return {
            "ok": True,
            "ip": ip,
            "in_sync": (not drifted),
            "status": "drifted" if drifted else "in-sync",
            "drift_seconds": 60.0 if drifted else 0.5,
            "abs_drift_seconds": 60.0 if drifted else 0.5,
            "camera_time": "2026-01-01T00:00:00+00:00",
        }

    pushed = []

    async def fake_sync(ip, username="", password="", port=80, scheme="http", timeout=10.0):
        pushed.append(ip)
        return {"ok": True, "ip": ip, "status": "synced", "synced_utc": "2026-01-01T00:00:00+00:00"}

    monkeypatch.setattr(ts, "get_time_sync_status", fake_status)
    monkeypatch.setattr(ts, "sync_camera_time", fake_sync)

    targets = [
        {"ip": "10.0.0.1", "username": "admin", "password": "pw"},
        {"ip": "10.0.0.2", "username": "admin", "password": "pw"},
    ]
    out = asyncio.run(svc.run_scheduled_sync(targets=targets))
    assert out["ok"] is True
    assert out["total"] == 2
    assert out["synced_count"] == 1
    assert pushed == ["10.0.0.2"]
    assert out["results"][1]["synced"] is True
    assert out["results"][0]["synced"] is False


def test_check_only_never_pushes(monkeypatch):
    import asyncio

    import app.services.time_sync as ts

    async def fake_status(ip="", port=80, scheme="http", tolerance_seconds=5.0, timeout=10.0):
        return {
            "ok": True,
            "ip": ip,
            "in_sync": False,
            "status": "drifted",
            "drift_seconds": 120.0,
            "abs_drift_seconds": 120.0,
            "camera_time": "2026-01-01T00:00:00+00:00",
        }

    async def fake_sync(**kwargs):
        raise AssertionError("sync must not be called in check-only mode")

    monkeypatch.setattr(ts, "get_time_sync_status", fake_status)
    monkeypatch.setattr(ts, "sync_camera_time", fake_sync)

    out = asyncio.run(
        svc.run_scheduled_sync(
            targets=[{"ip": "10.0.0.9", "username": "a", "password": "b"}],
            auto_sync=False,
        )
    )
    assert out["check_only"] is True
    assert out["synced_count"] == 0
    assert out["results"][0]["action"] == "drifted-check-only"


def test_missing_credentials_skips_push(monkeypatch):
    import asyncio

    import app.services.time_sync as ts

    async def fake_status(ip="", port=80, scheme="http", tolerance_seconds=5.0, timeout=10.0):
        return {
            "ok": True,
            "ip": ip,
            "in_sync": False,
            "status": "drifted",
            "drift_seconds": 30.0,
            "abs_drift_seconds": 30.0,
            "camera_time": "2026-01-01T00:00:00+00:00",
        }

    async def fake_sync(**kwargs):
        raise AssertionError("sync must not run without credentials")

    monkeypatch.setattr(ts, "get_time_sync_status", fake_status)
    monkeypatch.setattr(ts, "sync_camera_time", fake_sync)

    out = asyncio.run(svc.run_scheduled_sync(targets=[{"ip": "10.0.0.9"}]))
    assert out["synced_count"] == 0
    assert out["results"][0]["action"] == "drifted-no-credentials"


def test_resync_event_published(monkeypatch):
    import asyncio

    import app.services.time_sync as ts
    from app.services.event_bus import event_bus as bus

    seen = []
    orig_publish = bus.publish

    async def spy(event, payload=None, *args, **kwargs):
        seen.append((event, payload))
        return await orig_publish(event, payload, *args, **kwargs)

    monkeypatch.setattr(bus, "publish", spy)

    async def fake_status(ip="", port=80, scheme="http", tolerance_seconds=5.0, timeout=10.0):
        return {
            "ok": True,
            "ip": ip,
            "in_sync": False,
            "status": "drifted",
            "drift_seconds": 45.0,
            "abs_drift_seconds": 45.0,
            "camera_time": "2026-01-01T00:00:00+00:00",
        }

    async def fake_sync(ip, username="", password="", port=80, scheme="http", timeout=10.0):
        return {"ok": True, "ip": ip, "status": "synced", "synced_utc": "x"}

    monkeypatch.setattr(ts, "get_time_sync_status", fake_status)
    monkeypatch.setattr(ts, "sync_camera_time", fake_sync)

    out = asyncio.run(
        svc.run_scheduled_sync(targets=[{"ip": "10.0.0.7", "username": "a", "password": "b"}])
    )
    assert out["synced_count"] == 1
    kinds = [e for e, _ in seen]
    assert svc.CAMERA_RESYNCED_EVENT in kinds
    assert svc.SYNC_COMPLETED_EVENT in kinds


def test_scheduler_start_stop_nonblocking():
    sched = TimeAutosyncScheduler(interval_seconds=10_000)
    sched.start()
    assert sched.is_running is True
    sched.stop()
    assert sched.is_running is False


def _registered_paths():
    # This FastAPI version keeps auto-loaded routers as lazy _IncludedRouter
    # entries; look inside original_router as well as flattened routes.
    paths: set[str] = set()
    for r in app.routes:
        p = getattr(r, "path", None)
        if p:
            paths.add(p)
        orig = getattr(r, "original_router", None)
        if orig is not None:
            for sub in getattr(orig, "routes", []):
                sp = getattr(sub, "path", None)
                if sp:
                    paths.add(sp)
    return paths


def test_router_registered_and_endpoints(monkeypatch):
    import app.api.time_autosync as api_mod  # noqa: F401

    assert hasattr(api_mod, "router")
    assert hasattr(api_mod, "underscore_router")
    # Route registration check via app.routes (openapi() schema generation is
    # prohibitively slow on this large app) — mirrors the auto-loader contract.
    registered = _registered_paths()
    for expected in (
        "/api/time-autosync/status",
        "/api/time_autosync/status",
        "/api/time-autosync/config",
        "/api/time-autosync/run",
    ):
        assert expected in registered, expected

    async def fake_run(**kwargs):
        return {"ok": True, "success": True, "total": 1, "synced_count": 1,
                "synced": [{"ip": "1.2.3.4"}], "results": [{"ip": "1.2.3.4"}]}

    monkeypatch.setattr(svc, "run_scheduled_sync", fake_run)
    client = TestClient(app)

    r = client.get("/api/time-autosync/status")
    assert r.status_code == 200
    assert "enabled" in r.json()

    r = client.get("/api/time_autosync/status")
    assert r.status_code == 200

    r = client.get("/api/time-autosync/config")
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    r = client.post("/api/time-autosync/config", json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is True

    r = client.post("/api/time-autosync/run", json={})
    assert r.status_code == 200
    assert r.json()["synced_count"] == 1

    r = client.post("/api/time_autosync/check", json={})
    assert r.status_code == 200
