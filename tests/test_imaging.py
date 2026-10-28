"""ONVIF imaging write path tests (service + router, mocked transport)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

GET_SETTINGS_XML = """<Envelope><Body><timg:GetImagingSettingsResponse>
<tt:Brightness>50</tt:Brightness><tt:Contrast>60</tt:Contrast>
<tt:ColorSaturation>70</tt:ColorSaturation><tt:Sharpness>80</tt:Sharpness>
</timg:GetImagingSettingsResponse></Body></Envelope>"""

VIDEO_SOURCES_XML = """<Envelope><Body><trt:GetVideoSourcesResponse>
<tt:VideoSources token="vs0"><tt:Framerate>25</tt:Framerate></tt:VideoSources>
</trt:GetVideoSourcesResponse></Body></Envelope>"""


def _ok_canned(monkeypatch, settings_xml: str = GET_SETTINGS_XML):
    """Patch ods transport with canned Imaging/Media responses; capture Set body."""
    from app.services import onvif_digest_service as ods

    captured: dict = {}

    async def fake_request(url, body_xml, username=None, password=None, timeout=10.0):
        if "GetVideoSources" in body_xml:
            return 200, VIDEO_SOURCES_XML
        if "GetImagingSettings" in body_xml:
            return 200, settings_xml
        if "SetImagingSettings" in body_xml:
            captured["url"] = url
            captured["body"] = body_xml
            return 200, "<Envelope><Body><timg:SetImagingSettingsResponse/></Body></Envelope>"
        return 200, "<Envelope/>"

    async def fake_services(ip, username, password, port=80, scheme="http"):
        return {"imaging": "http://cam/onvif/Imaging", "media": "http://cam/onvif/Media"}

    async def fake_caps(ip, username, password, port=80, scheme="http"):
        return {"imaging": "http://cam/onvif/Imaging", "media": "http://cam/onvif/Media"}

    async def fake_resolve(ip, port_hint=None, scheme_hint=None):
        return (scheme_hint or "http"), (port_hint or 80)

    monkeypatch.setattr(ods, "_onvif_request", fake_request)
    monkeypatch.setattr(ods, "get_services", fake_services)
    monkeypatch.setattr(ods, "get_capabilities", fake_caps)
    monkeypatch.setattr(ods, "resolve_control_endpoint", fake_resolve)
    return captured


def test_imports():
    import app.services.imaging  # noqa: F401
    import app.api.imaging  # noqa: F401
    from app.services.imaging import (  # noqa: F401
        get_imaging_settings,
        set_imaging_settings,
        get_imaging,
        set_imaging,
    )


def test_clamp_and_validation():
    from app.services import imaging as svc

    assert svc._clamp_level(1000) == 100
    assert svc._clamp_level(-5) == 0
    with pytest.raises(HTTPException) as exc:
        svc._clamp_level("not-a-number")
    assert exc.value.status_code == 422


def test_get_imaging_settings(monkeypatch):
    _ok_canned(monkeypatch)
    from app.services import imaging as svc

    result = asyncio.run(svc.get_imaging_settings("1.2.3.4", "admin", "pw"))
    assert result["supported"] is True
    assert result["settings"]["brightness"] == 50
    assert result["settings"]["contrast"] == 60
    assert result["settings"]["saturation"] == 70


def test_set_imaging_settings_roundtrip(monkeypatch):
    captured = _ok_canned(monkeypatch)
    from app.services import imaging as svc

    result = asyncio.run(
        svc.set_imaging_settings(
            "1.2.3.4", "admin", "pw", brightness=10, contrast=20, saturation=30
        )
    )
    assert result["supported"] is True
    body = captured["body"]
    assert "<tt:Brightness>10</tt:Brightness>" in body
    assert "<tt:Contrast>20</tt:Contrast>" in body
    assert "<tt:ColorSaturation>30</tt:ColorSaturation>" in body
    assert "ForcePersistence" in body


def test_set_requires_at_least_one_field(monkeypatch):
    _ok_canned(monkeypatch)
    from app.services import imaging as svc

    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.set_imaging_settings("1.2.3.4", "admin", "pw"))
    assert exc.value.status_code == 422


def test_graceful_fallback_when_unsupported(monkeypatch):
    from app.services import onvif_digest_service as ods
    from app.services import imaging as svc

    async def fake_request(url, body_xml, username=None, password=None, timeout=10.0):
        if "GetVideoSources" in body_xml:
            return 200, VIDEO_SOURCES_XML
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

    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.get_imaging_settings("1.2.3.4", "admin", "pw"))
    assert exc.value.status_code == 502


def _all_route_paths(router) -> list:
    """Flatten route paths, recursing into lazily-included sub-routers.

    Sub-router routes already carry their full (prefixed) path, so no
    prefix prepending is needed here.
    """
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
    # Mirror app.main's auto-loader: the module exposes ``router`` and its
    # flattened paths cover the imaging get/set endpoints.
    import app.api.imaging as imaging_api

    assert getattr(imaging_api, "router", None) is not None
    paths = _all_route_paths(imaging_api.router)
    assert any("imaging" in p and p.rstrip("/").endswith("/set") for p in paths), paths
    assert any("imaging" in p and p.rstrip("/").endswith("/get") for p in paths), paths
