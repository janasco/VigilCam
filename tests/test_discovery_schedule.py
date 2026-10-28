"""Tests for the opt-in periodic discovery scheduler."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
import app.services.discovery_schedule as svc
from app.services.discovery_schedule import DiscoveryScheduleScheduler


@pytest.fixture(autouse=True)
def _clean_state():
    svc.reset_known()
    prev = svc.get_config()
    svc.update_config(
        enabled=False,
        interval_seconds=3600,
        subnets=None,
        network_timeout=0.5,
        ws_timeout=4.0,
        include_network=True,
        include_ws=True,
        notify_new=True,
    )
    try:
        svc.stop_discovery_schedule_scheduler()
    except Exception:  # noqa: BLE001
        pass
    yield
    svc.reset_known()
    svc.update_config(**{k: v for k, v in prev.items() if k != "subnets"},
                      subnets=prev.get("subnets"))
    try:
        svc.stop_discovery_schedule_scheduler()
    except Exception:  # noqa: BLE001
        pass


def test_discovery_is_manual_only_by_default():
    cfg = svc.get_config()
    assert cfg["enabled"] is False
    assert svc.get_status()["enabled"] is False


def test_config_update_roundtrip():
    out = svc.update_config(enabled=True, interval_seconds=120, subnets=["10.0.0.0/24"])
    assert out["enabled"] is True
    assert out["interval_seconds"] == 120
    assert out["subnets"] == ["10.0.0.0/24"]
    # Interval floors at the minimum.
    out = svc.update_config(interval_seconds=1)
    assert out["interval_seconds"] == svc.MIN_INTERVAL_SECONDS
    svc.update_config(enabled=False)


def test_scheduled_scan_reports_new_devices_only_once(monkeypatch):
    async def fake_network(**kwargs):
        return [
            {"ip": "192.168.1.10", "ip_address": "192.168.1.10", "kind": "onvif"},
            {"ip": "192.168.1.11", "ip_address": "192.168.1.11", "kind": "ip"},
        ]

    class FakeWsDevice:
        def __init__(self, address, xaddr):
            self.address = address
            self.xaddresses = [xaddr]
            self.types = ["dn:NetworkVideoTransmitter"]
            self.model = "M"
            self.mfr = "V"
            self.scopes = []
            self.endpoint = ""
            self.metadata_version = "1"

    async def fake_ws(**kwargs):
        return [FakeWsDevice("192.168.1.20", "http://192.168.1.20/onvif/device_service")]

    import app.services.network_discovery as nd
    import app.services.ws_discovery as wsd

    monkeypatch.setattr(nd, "discover_network_cameras", fake_network)
    monkeypatch.setattr(wsd, "discover", fake_ws)

    import asyncio

    first = asyncio.run(svc.run_scheduled_discovery(notify_new=False))
    assert first["total"] == 3
    assert first["new_count"] == 3
    assert len(first["new_devices"]) == 3

    second = asyncio.run(svc.run_scheduled_discovery(notify_new=False))
    assert second["total"] == 3
    assert second["new_count"] == 0


def test_new_device_event_published(monkeypatch):
    seen = []

    from app.services.event_bus import event_bus as bus

    orig_publish = bus.publish

    async def spy(event, payload=None, *args, **kwargs):
        seen.append((event, payload))
        return await orig_publish(event, payload, *args, **kwargs)

    monkeypatch.setattr(bus, "publish", spy)

    async def fake_network(**kwargs):
        return [{"ip": "192.168.1.99", "kind": "onvif"}]

    async def fake_ws(**kwargs):
        return []

    import app.services.network_discovery as nd
    import app.services.ws_discovery as wsd

    monkeypatch.setattr(nd, "discover_network_cameras", fake_network)
    monkeypatch.setattr(wsd, "discover", fake_ws)

    import asyncio

    result = asyncio.run(svc.run_scheduled_discovery())
    assert result["new_count"] == 1
    kinds = [e for e, _ in seen]
    assert svc.NEW_DEVICE_EVENT in kinds
    assert svc.SCAN_COMPLETED_EVENT in kinds


def test_scheduler_start_stop_nonblocking():
    sched = DiscoveryScheduleScheduler(interval_seconds=10_000)
    sched.start()
    assert sched.is_running is True
    sched.stop()
    assert sched.is_running is False


def test_router_registered_and_endpoints(monkeypatch):
    import app.api.discovery_schedule as api_mod  # noqa: F401

    assert hasattr(api_mod, "router")
    assert hasattr(api_mod, "underscore_router")
    paths = list(app.openapi()["paths"].keys())
    assert "/api/discovery-schedule/status" in paths
    assert "/api/discovery_schedule/status" in paths
    assert "/api/discovery-schedule/config" in paths
    assert "/api/discovery-schedule/run" in paths

    async def fake_run(**kwargs):
        return {"success": True, "total": 1, "new_count": 1,
                "devices": [{"ip": "1.2.3.4"}], "new_devices": [{"ip": "1.2.3.4"}]}

    monkeypatch.setattr(svc, "run_scheduled_discovery", fake_run)
    client = TestClient(app)

    r = client.get("/api/discovery-schedule/status")
    assert r.status_code == 200
    assert "enabled" in r.json()

    r = client.get("/api/discovery_schedule/status")
    assert r.status_code == 200

    r = client.get("/api/discovery-schedule/config")
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    r = client.post("/api/discovery-schedule/config", json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is True

    r = client.post("/api/discovery-schedule/run", json={})
    assert r.status_code == 200
    assert r.json()["total"] == 1

    r = client.post("/api/discovery_schedule/scan", json={})
    assert r.status_code == 200

    r = client.post("/api/discovery-schedule/reset-known")
    assert r.status_code == 200
