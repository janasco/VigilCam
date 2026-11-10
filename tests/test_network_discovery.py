"""Tests for network discovery and ONVIF integration."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.services import network_discovery as nd
from app.services import ws_discovery as wsd


def test_http_probe_continues_on_get_exception():
    """Verify _http_probe does not abort when GET raises, and tests SOAP probes."""
    class FakeResponse:
        def __init__(self, status_code, text, headers=None):
            self.status_code = status_code
            self.text = text
            self.headers = headers or {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            raise Exception("Connection reset by peer")

        async def post(self, url, **kwargs):
            if "/onvif/device_service" in url:
                return FakeResponse(
                    200,
                    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                    '<s:Body><tds:GetSystemDateAndTimeResponse/></s:Body></s:Envelope>',
                    {"server": "hsoap/2.8"},
                )
            return FakeResponse(404, "")

    async def _run():
        with patch("httpx.AsyncClient", return_value=FakeClient()):
            info = await nd._http_probe("10.0.0.163", 8899, timeout=1.0)
            assert info["reachable"] is True
            assert info["onvif"] is True
            assert info["server"] == "hsoap/2.8"

    asyncio.run(_run())


def test_scan_host_verifies_onvif():
    """Verify scan_host verifies ONVIF across open HTTP ports and sets stream_url."""
    async def fake_tcp(ip, port, timeout):
        return port in (8899, 554)

    async def fake_http_probe(ip, port, timeout):
        return {"reachable": True, "status": 200, "server": "hsoap/2.8", "onvif": True}

    async def fake_onvif_verify(ip, port, timeout):
        return port == 8899

    async def _run():
        with patch.object(nd, "_tcp_open", side_effect=fake_tcp), \
             patch.object(nd, "_http_probe", side_effect=fake_http_probe), \
             patch.object(nd, "_onvif_verify", side_effect=fake_onvif_verify):

            dev = await nd.scan_host("10.0.0.163", timeout=1.0)
            assert dev is not None
            assert dev["onvif"] is True
            assert dev["kind"] == "onvif"
            assert dev["verified"] is True
            assert dev["onvif_verified"] is True
            assert dev["verify_method"] == "onvif:8899"
            assert "onvif" in dev["protocols"]
            assert dev["stream_url"] == "rtsp://10.0.0.163:554/"

    asyncio.run(_run())


def test_discover_network_merges_ws_discovery():
    """Verify discover_network merges WS-Discovery with subnet scan results."""
    from app.api import discovery as disc_api

    fake_subnet_dev = {
        "ip": "10.0.0.163",
        "ip_address": "10.0.0.163",
        "ports": [554, 8899],
        "http_ports": [8899],
        "rtsp_ports": [554],
        "http": True,
        "rtsp": True,
        "onvif": True,
        "verified": True,
        "verify_method": "onvif:8899",
        "protocols": ["http", "onvif", "rtsp"],
        "name": "Camera 10.0.0.163",
        "kind": "onvif",
        "source": "onvif://10.0.0.163:8899",
        "stream_url": "rtsp://10.0.0.163:554/",
    }

    class FakeWsDevice:
        def __init__(self):
            self.address = "10.0.0.163"
            self.xaddresses = ["http://10.0.0.163:8899/onvif/device_service"]
            self.types = ["dn:NetworkVideoTransmitter"]
            self.model = "CamModel"
            self.mfr = "IP-Camera"
            self.scopes = ["onvif://scope1"]
            self.endpoint = "urn:uuid:123"
            self.metadata_version = "1"

    async def _run():
        with patch.object(disc_api.nd, "detect_local_subnets", return_value=["10.0.0.0/24"]), \
             patch.object(disc_api.nd, "scan_subnet", new_callable=AsyncMock, return_value=[fake_subnet_dev]), \
             patch.object(disc_api.ws_discovery, "discover_devices", new_callable=AsyncMock, return_value=[FakeWsDevice()]), \
             patch.object(disc_api.cameras_service, "list_cameras", new_callable=AsyncMock, return_value=[]):

            devices = await disc_api.discover_network(subnet="10.0.0.0/24", verified_only=True, verify=True, db=AsyncMock())
            assert len(devices) == 1
            d = devices[0]
            assert d["ip"] == "10.0.0.163"
            assert d["kind"] == "onvif"
            assert d["onvif"] is True
            assert d["verified"] is True
            assert d["verify_method"] == "onvif:ws-discovery"
            assert d["manufacturer"] == "IP-Camera"
            assert d["model"] == "CamModel"
            assert d["name"] == "IP-Camera 10.0.0.163"
            assert d["source"] == "rtsp://10.0.0.163:554/"
            assert d["stream_url"] == "rtsp://10.0.0.163:554/"
            assert d["onvif_url"] == "http://10.0.0.163:8899/onvif/device_service"

    asyncio.run(_run())
