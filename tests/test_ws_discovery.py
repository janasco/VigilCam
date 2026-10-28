"""Tests for the WS-Discovery port (app/services/ws_discovery.py)."""

from __future__ import annotations

import asyncio
import socket

import pytest
from fastapi.testclient import TestClient

from app.main import app
import app.services.ws_discovery as svc
from app.services.ws_discovery import (
    DiscoveryDevice,
    build_probe_message,
    create_device,
    deduplicate_devices,
    merge_device,
    new_message_id,
    parse_mfr_from_scopes,
    parse_model_from_scopes,
    parse_probe_response,
    parse_probe_responses,
    split_list,
)


MID = "11111111-2222-3333-4444-555555555555"

SCOPES = (
    "onvif://www.onvif.org/type/NetworkVideoTransmitter "
    "onvif://www.onvif.org/hardware/AXIS-M3045 "
    "onvif://www.onvif.org/name/AXIS-M3045 "
    "onvif://www.onvif.org/mfr/AXIS "
    "onvif://www.onvif.org/location/country/china"
)


def _fixture_response(relates_to=MID, scopes=SCOPES, xaddrs=None):
    xaddrs = xaddrs or "http://192.168.1.64/onvif/device_service"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
<e:Header><w:RelatesTo>uuid:{relates_to}</w:RelatesTo></e:Header>
<e:Body><d:ProbeMatches><d:ProbeMatch>
<w:EndpointReference><w:Address>urn:uuid:abcdef-1234</w:Address></w:EndpointReference>
<d:Types>dn:NetworkVideoTransmitter</d:Types>
<d:Scopes>{scopes}</d:Scopes>
<d:XAddrs>{xaddrs}</d:XAddrs>
<d:MetadataVersion>1</d:MetadataVersion>
</d:ProbeMatch></d:ProbeMatches></e:Body>
</e:Envelope>"""


def test_probe_message_contents():
    raw = build_probe_message(MID)
    text = raw.decode("ascii")
    assert f"uuid:{MID}" in text
    assert "urn:schemas-xmlsoap-org:ws:2005:04:discovery" in text
    assert "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe" in text
    assert "dn:NetworkVideoTransmitter" in text
    # No scopes element unless a filter is requested.
    assert "<d:Scopes>" not in text


def test_probe_message_with_scopes():
    raw = build_probe_message(MID, scopes="onvif://www.onvif.org/hardware/AXIS-M3045")
    assert b"<d:Scopes>onvif://www.onvif.org/hardware/AXIS-M3045</d:Scopes>" in raw


def test_probe_message_rejects_empty_id():
    with pytest.raises(ValueError):
        build_probe_message("")
    with pytest.raises(ValueError):
        build_probe_message("00000000-0000-0000-0000-000000000000")
    assert new_message_id()


def test_split_list():
    assert split_list("a  b c") == ["a", "b", "c"]
    assert split_list("") == []
    assert split_list(None) == []


def test_scope_parsing():
    assert parse_model_from_scopes(SCOPES) == "AXIS-M3045"
    assert parse_mfr_from_scopes(SCOPES) == "AXIS"
    assert parse_model_from_scopes("") == ""
    assert parse_mfr_from_scopes("") == ""


def test_mfr_fallback_to_name_scope():
    scopes = (
        "onvif://www.onvif.org/type/NetworkVideoTransmitter "
        "onvif://www.onvif.org/hardware/CAM123 "
        "onvif://www.onvif.org/name/Hikvision-DS "
    )
    assert parse_mfr_from_scopes(scopes) == "Hikvision-DS"


def test_parse_valid_response():
    device = parse_probe_response(_fixture_response(), MID, "192.168.1.64")
    assert device is not None
    assert device.address == "192.168.1.64"
    assert device.model == "AXIS-M3045"
    assert device.mfr == "AXIS"
    assert device.xaddresses == ["http://192.168.1.64/onvif/device_service"]
    assert device.types == ["dn:NetworkVideoTransmitter"]
    assert device.endpoint == "urn:uuid:abcdef-1234"
    assert device.metadata_version == "1"
    assert any("hardware/AXIS-M3045" in s for s in device.scopes)


def test_parse_rejects_wrong_relates_to():
    assert parse_probe_response(_fixture_response(), "other-id", "192.168.1.64") is None


def test_parse_rejects_empty_scopes():
    assert parse_probe_response(_fixture_response(scopes=""), MID, "x") is None
    assert parse_probe_response("not xml at all ((((", MID, "x") is None
    assert parse_probe_response(None, MID, "x") is None


def test_parse_all_matches():
    multi = _fixture_response().replace(
        "</d:ProbeMatch>",
        "</d:ProbeMatch><d:ProbeMatch>"
        "<w:EndpointReference><w:Address>urn:uuid:second</w:Address></w:EndpointReference>"
        "<d:Types>dn:NetworkVideoTransmitter</d:Types>"
        f"<d:Scopes>{SCOPES}</d:Scopes>"
        "<d:XAddrs>http://192.168.1.65/onvif/device_service</d:XAddrs>"
        "<d:MetadataVersion>1</d:MetadataVersion>"
        "</d:ProbeMatch>",
    )
    devices = parse_probe_responses(multi, MID, "192.168.1.64")
    assert len(devices) == 2
    assert {d.xaddresses[0] for d in devices} == {
        "http://192.168.1.64/onvif/device_service",
        "http://192.168.1.65/onvif/device_service",
    }


def test_duplicate_suppression_by_xaddr():
    seen: set[str] = set()
    first = create_device(
        {"types": "t", "scopes": SCOPES,
         "xaddrs": "http://192.168.1.64/onvif/device_service",
         "metadata_version": "1", "endpoint": ""},
        "192.168.1.64",
    )
    assert merge_device(seen, first) is True
    # Same XAddr from a second interface is a duplicate.
    assert merge_device(seen, first) is False
    other = DiscoveryDevice(
        types=["t"], xaddresses=["http://192.168.1.65/onvif/device_service"],
        model="m", mfr="v", address="192.168.1.65", scopes=["s"],
    )
    assert merge_device(seen, other) is True
    assert len(deduplicate_devices([first, first, other])) == 2


def test_local_addresses_never_raise():
    assert isinstance(svc.get_local_ipv4_addresses(), list)


def test_discover_rejects_bad_timeout():
    with pytest.raises(ValueError):
        asyncio.run(svc.discover(timeout=0))
    with pytest.raises(ValueError):
        asyncio.run(svc.discover(timeout=61))


def test_discover_sync_loop_with_fake_socket(monkeypatch):
    payload = _fixture_response().encode("utf-8")

    class FakeSocket:
        instances: list["FakeSocket"] = []

        def __init__(self, *a, **k):
            self.sent: list[tuple] = []
            self.calls = 0
            FakeSocket.instances.append(self)

        def setsockopt(self, *a, **k):
            return None

        def bind(self, *a, **k):
            return None

        def settimeout(self, *a, **k):
            return None

        def sendto(self, data, addr):
            self.sent.append((data, addr))
            return len(data)

        def recvfrom(self, n):
            self.calls += 1
            # First receive per socket returns the same camera (as if heard
            # on two interfaces); afterwards behave like a quiet network.
            if self.calls == 1:
                return payload, ("192.168.1.64", 3702)
            raise socket.timeout()

        def close(self):
            return None

    FakeSocket.instances.clear()
    monkeypatch.setattr(socket, "socket", FakeSocket)
    monkeypatch.setattr(svc, "new_message_id", lambda: MID)
    monkeypatch.setattr(
        svc, "get_local_ipv4_addresses", lambda: ["192.168.1.10", "192.168.1.11"]
    )
    devices = svc._discover_sync(timeout=0.6)
    assert len(devices) == 1
    assert devices[0].model == "AXIS-M3045"
    # Probes were re-sent to the WS-Discovery multicast endpoint.
    assert FakeSocket.instances
    for fake in FakeSocket.instances:
        assert fake.sent
        assert fake.sent[0][1] == (svc.WS_MULTICAST_ADDRESS, svc.WS_MULTICAST_PORT)


def test_ws_discovery_router_registered():
    import app.api.ws_discovery as api_mod  # noqa: F401

    assert hasattr(api_mod, "router")
    assert hasattr(api_mod, "underscore_router")
    paths = list(app.openapi()["paths"].keys())
    assert "/api/ws-discovery/discover" in paths
    assert "/api/ws_discovery/discover" in paths
    assert "/api/ws-discovery/probe-message" in paths
    assert "/api/ws-discovery/info" in paths


def test_ws_discovery_endpoints(monkeypatch):
    device = DiscoveryDevice(
        types=["dn:NetworkVideoTransmitter"],
        xaddresses=["http://192.168.1.64/onvif/device_service"],
        model="AXIS-M3045",
        mfr="AXIS",
        address="192.168.1.64",
        scopes=["onvif://www.onvif.org/hardware/AXIS-M3045"],
        endpoint="urn:uuid:abcdef-1234",
        metadata_version="1",
    )

    async def fake_discover(**kwargs):
        return [device]

    monkeypatch.setattr(svc, "discover", fake_discover)
    client = TestClient(app)

    r = client.get("/api/ws-discovery/discover?timeout=1")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["model"] == "AXIS-M3045"
    assert body[0]["address"] == "192.168.1.64"

    r_alias = client.get("/api/ws_discovery/discover?timeout=1")
    assert r_alias.status_code == 200

    r_post = client.post("/api/ws-discovery/discover", json={"timeout": 1})
    assert r_post.status_code == 200
    assert r_post.json()[0]["mfr"] == "AXIS"

    r_msg = client.get("/api/ws-discovery/probe-message")
    assert r_msg.status_code == 200
    assert "dn:NetworkVideoTransmitter" in r_msg.json()["xml"]

    r_info = client.get("/api/ws-discovery/info")
    assert r_info.status_code == 200
    assert r_info.json()["multicast_address"] == "239.255.255.250"

    # Query validation rejects non-positive timeouts before any scan.
    assert client.get("/api/ws-discovery/discover?timeout=0").status_code == 422
    assert client.post(
        "/api/ws-discovery/discover", json={"timeout": 0}
    ).status_code == 422
