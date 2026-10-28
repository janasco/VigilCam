"""ONVIF relay-output control tests (service + router, mocked transport)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

RELAY_LIST_XML = """<Envelope><Body><tds:GetRelayOutputsResponse>
<tds:RelayOutputs token="Relay1"><tt:Properties>
<tt:Mode>Monostable</tt:Mode><tt:DelayTime>PT5S</tt:DelayTime><tt:IdleState>open</tt:IdleState>
</tt:Properties></tds:RelayOutputs>
<tds:RelayOutputs token="Relay2"><tt:Properties>
<tt:Mode>Bistable</tt:Mode><tt:DelayTime>PT0S</tt:DelayTime><tt:IdleState>closed</tt:IdleState>
</tt:Properties></tds:RelayOutputs>
</tds:GetRelayOutputsResponse></Body></Envelope>"""


def _ok_canned(monkeypatch, list_xml: str = RELAY_LIST_XML):
    """Patch ods transport with canned Device responses; capture Set bodies."""
    from app.services import onvif_digest_service as ods

    captured: dict = {"states": [], "settings": []}

    async def fake_request(url, body_xml, username=None, password=None, timeout=10.0):
        if "GetRelayOutputs" in body_xml:
            return 200, list_xml
        if "SetRelayOutputState" in body_xml:
            captured["states"].append({"url": url, "body": body_xml})
            return 200, "<Envelope><Body><tds:SetRelayOutputStateResponse/></Body></Envelope>"
        if "SetRelayOutputSettings" in body_xml:
            captured["settings"].append({"url": url, "body": body_xml})
            return 200, "<Envelope><Body><tds:SetRelayOutputSettingsResponse/></Body></Envelope>"
        return 200, "<Envelope/>"

    async def fake_services(ip, username, password, port=80, scheme="http"):
        return {"device": "http://cam/onvif/device_service"}

    async def fake_caps(ip, username, password, port=80, scheme="http"):
        return {"device": "http://cam/onvif/device_service"}

    async def fake_resolve(ip, port_hint=None, scheme_hint=None):
        return (scheme_hint or "http"), (port_hint or 80)

    monkeypatch.setattr(ods, "_onvif_request", fake_request)
    monkeypatch.setattr(ods, "get_services", fake_services)
    monkeypatch.setattr(ods, "get_capabilities", fake_caps)
    monkeypatch.setattr(ods, "resolve_control_endpoint", fake_resolve)
    return captured


def test_imports():
    import app.services.relay_output  # noqa: F401
    import app.api.relay_output  # noqa: F401
    from app.services.relay_output import (  # noqa: F401
        get_relay_outputs,
        set_relay_output_state,
        set_relay_output_settings,
        trigger_relay_output,
        get_relays,
        set_relay,
        trigger_relay,
    )


def test_no_relay_support_in_digest_service():
    # Guard: onvif_digest_service itself must not grow relay helpers; the
    # relay_output service owns that SOAP. Native-helper preference in the
    # service is a forward-compat shim only.
    from app.services import onvif_digest_service as ods

    assert not hasattr(ods, "get_relay_outputs")
    assert not hasattr(ods, "set_relay_output_state")
    assert not hasattr(ods, "set_relay_output_settings")
    assert not hasattr(ods, "trigger_relay_output")


def test_state_and_token_validation():
    from app.services import relay_output as svc

    assert svc._normalize_state("ON") == "active"
    assert svc._normalize_state("off") == "inactive"
    assert svc._normalize_state("trigger") == "active"
    with pytest.raises(HTTPException) as exc:
        svc._normalize_state("bogus")
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        svc._checked_token("bad token!")
    assert exc.value.status_code == 422
    assert svc._normalize_mode("monostable") == "Monostable"
    assert svc._normalize_idle_state("CLOSED") == "closed"
    assert svc._parse_delay_seconds("PT1M30S") == pytest.approx(90.0)
    assert svc._format_delay(5) == "PT5S"


def test_get_relay_outputs(monkeypatch):
    _ok_canned(monkeypatch)
    from app.services import relay_output as svc

    result = asyncio.run(svc.get_relay_outputs("1.2.3.4", "admin", "pw"))
    assert result["supported"] is True
    assert len(result["outputs"]) == 2
    first = result["outputs"][0]
    assert first["token"] == "Relay1"
    assert first["mode"] == "Monostable"
    assert first["delay_seconds"] == pytest.approx(5.0)
    assert first["idle_state"] == "open"


def test_set_state_sends_logical_state(monkeypatch):
    captured = _ok_canned(monkeypatch)
    from app.services import relay_output as svc

    result = asyncio.run(
        svc.set_relay_output_state("1.2.3.4", "admin", "pw", "Relay1", "on")
    )
    assert result["state"] == "active"
    assert len(captured["states"]) == 1
    body = captured["states"][0]["body"]
    assert "Relay1" in body
    assert "<tds:LogicalState>active</tds:LogicalState>" in body


def test_set_settings_roundtrip(monkeypatch):
    captured = _ok_canned(monkeypatch)
    from app.services import relay_output as svc

    result = asyncio.run(
        svc.set_relay_output_settings(
            "1.2.3.4", "admin", "pw", "Relay1", mode="Bistable", idle_state="closed"
        )
    )
    assert result["mode"] == "Bistable"
    assert result["idle_state"] == "closed"
    assert len(captured["settings"]) == 1
    body = captured["settings"][0]["body"]
    assert "<tt:Mode>Bistable</tt:Mode>" in body
    assert "<tt:IdleState>closed</tt:IdleState>" in body


def test_set_settings_requires_a_field(monkeypatch):
    _ok_canned(monkeypatch)
    from app.services import relay_output as svc

    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.set_relay_output_settings("1.2.3.4", "admin", "pw", "Relay1"))
    assert exc.value.status_code == 422


def test_trigger_pulses_active_then_inactive(monkeypatch):
    captured = _ok_canned(monkeypatch)
    from app.services import relay_output as svc

    result = asyncio.run(svc.trigger_relay_output("1.2.3.4", "admin", "pw", "Relay1"))
    assert result["triggered"] is True
    assert len(captured["states"]) == 2
    assert "active" in captured["states"][0]["body"]
    assert "inactive" in captured["states"][1]["body"]


def test_graceful_fallback_when_unsupported(monkeypatch):
    from app.services import onvif_digest_service as ods

    async def fake_request(url, body_xml, username=None, password=None, timeout=10.0):
        return (
            200,
            "<Envelope><Body><Fault><Reason><Text>Not found</Text></Reason></Fault></Body></Envelope>",
        )

    async def fake_services(ip, username, password, port=80, scheme="http"):
        return {}

    async def fake_caps(ip, username, password, port=80, scheme="http"):
        return {}

    monkeypatch.setattr(ods, "_onvif_request", fake_request)
    monkeypatch.setattr(ods, "get_services", fake_services)
    monkeypatch.setattr(ods, "get_capabilities", fake_caps)

    from app.services import relay_output as svc

    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.get_relay_outputs("1.2.3.4", "admin", "pw"))
    assert exc.value.status_code == 502


def _all_route_paths(router) -> list:
    out: list = []
    for r in getattr(router, "routes", []):
        orig = getattr(r, "original_router", None)
        if orig is not None:
            out.extend(_all_route_paths(orig))
        else:
            p = getattr(r, "path", None)
            if p:
                out.append(p)
    return out


def test_router_paths_present():
    import app.api.relay_output as relay_api

    assert getattr(relay_api, "router", None) is not None
    paths = _all_route_paths(relay_api.router)
    assert any("relay" in p and p.rstrip("/").endswith("/list") for p in paths), paths
    assert any("relay" in p and p.rstrip("/").endswith("/set") for p in paths), paths
    assert any("relay" in p and p.rstrip("/").endswith("/trigger") for p in paths), paths
    assert any("relay" in p and p.rstrip("/").endswith("/settings") for p in paths), paths


def test_no_collision_with_http_relay_router():
    import app.api.relay as http_relay
    import app.api.relay_output as relay_api

    http_paths = set(_all_route_paths(http_relay.router))
    out_paths = set(_all_route_paths(relay_api.router))
    assert not (http_paths & out_paths), http_paths & out_paths
