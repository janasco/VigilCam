"""Day/night imaging profiles tests (service + router, mocked transport).

Covers: imaging on-demand verification (no day/night auto-switching),
profile CRUD, clamping/validation, on-demand apply via the imaging write
path, and router path coverage (mirrors ``test_imaging.py`` conventions).
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

GET_SETTINGS_XML = """<Envelope><Body><timg:GetImagingSettingsResponse>
<tt:Brightness>50</tt:Brightness><tt:Contrast>60</tt:Contrast>
<tt:ColorSaturation>70</tt:ColorSaturation><tt:Sharpness>80</tt:Sharpness>
</timg:GetImagingSettingsResponse></Body></Envelope>"""

VIDEO_SOURCES_XML = """<Envelope><Body><trt:GetVideoSourcesResponse>
<tt:VideoSources token="vs0"><tt:Framerate>25</tt:Framerate></tt:VideoSources>
</trt:GetVideoSourcesResponse></Body></Envelope>"""


def _ok_canned(monkeypatch):
    """Patch ods transport with canned Imaging/Media responses; capture Set body."""
    from app.services import onvif_digest_service as ods

    captured: dict = {}

    async def fake_request(url, body_xml, username=None, password=None, timeout=10.0):
        if "GetVideoSources" in body_xml:
            return 200, VIDEO_SOURCES_XML
        if "GetImagingSettings" in body_xml:
            return 200, GET_SETTINGS_XML
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
    import app.services.daynight  # noqa: F401
    import app.api.daynight  # noqa: F401
    from app.services.daynight import (  # noqa: F401
        get_profile,
        set_profile,
        apply_profile,
        get_daynight,
        list_profiles,
        verify_no_auto_switch,
        normalize_profile_name,
    )


def test_imaging_sets_values_on_demand_no_auto_switching():
    """Baseline: imaging writes on demand; no day/night auto-switch exists."""
    import app.services.imaging as imaging

    # On-demand write path exists.
    assert callable(getattr(imaging, "get_imaging_settings", None))
    assert callable(getattr(imaging, "set_imaging_settings", None))
    # Aliases from the driver-layer note.
    assert callable(getattr(imaging, "get_imaging", None))
    assert callable(getattr(imaging, "set_imaging", None))

    # No day/night, schedule, cron, or background-task logic in imaging.
    source = inspect.getsource(imaging).lower()
    for marker in ("daynight", "day_night", "day-night",
                   "day_profile", "night_profile", "auto_switch",
                   "autoswitch", "scheduler", "cron", "threading.timer",
                   "create_task"):
        assert marker not in source, f"imaging should not contain {marker!r}"

    # Service-level verifier agrees.
    from app.services import daynight as dn

    report = dn.verify_no_auto_switch()
    assert report["on_demand_only"] is True
    assert report["auto_switch"] is False
    assert report["has_set_imaging_settings"] is True
    assert report["has_get_imaging_settings"] is True


def test_normalize_profile_names():
    from app.services import daynight as dn

    assert dn.normalize_profile_name("day") == "day"
    assert dn.normalize_profile_name("DAY") == "day"
    assert dn.normalize_profile_name("daytime") == "day"
    assert dn.normalize_profile_name("night") == "night"
    assert dn.normalize_profile_name("NightTime") == "night"
    assert dn.normalize_profile_name("evening") == "night"
    assert dn.normalize_profile_name("bogus") is None
    assert dn.is_valid_profile("day") is True
    assert dn.is_valid_profile("night") is True
    assert dn.is_valid_profile("dusk") is False


def test_set_get_day_night_roundtrip():
    from app.services import daynight as dn

    dn.clear()
    day = dn.set_profile(camera_id="cam1", profile="day",
                         brightness=60, contrast=55, saturation=65)
    assert day["profile"] == "day"
    assert day["brightness"] == 60
    night = dn.set_profile(camera_id="cam1", profile="night",
                           brightness=80, contrast=85, saturation=90)
    assert night["profile"] == "night"

    assert dn.get_profile("cam1", "day")["brightness"] == 60
    assert dn.get_day_profile("cam1")["contrast"] == 55
    assert dn.get_night_profile("cam1")["brightness"] == 80

    both = dn.get_daynight("cam1")
    assert both["day"]["brightness"] == 60
    assert both["night"]["brightness"] == 80

    items = dn.list_profiles(camera_id="cam1")
    assert len(items) == 2
    dn.clear()
    assert dn.list_profiles(camera_id="cam1") == []


def test_clamp_and_validation():
    from app.services import daynight as dn

    dn.clear()
    # Clamped to 0-100.
    prof = dn.set_profile(camera_id="camC", profile="day",
                          brightness=1000, contrast=-5, saturation=50)
    assert prof["brightness"] == 100
    assert prof["contrast"] == 0
    with pytest.raises(ValueError):
        dn.set_profile(camera_id="camC", profile="day")
    with pytest.raises(ValueError):
        dn.set_profile(camera_id="camC", profile="dusk", brightness=10)
    with pytest.raises(ValueError):
        dn.set_profile(profile="day", brightness=10)
    with pytest.raises(ValueError):
        dn.get_daynight(None)
    dn.clear()


def test_apply_uses_stored_profile_on_demand(monkeypatch):
    captured = _ok_canned(monkeypatch)
    from app.services import daynight as dn

    dn.clear()
    dn.set_profile(camera_id="camA", profile="day",
                   brightness=11, contrast=22, saturation=33)
    dn.set_profile(camera_id="camA", profile="night",
                   brightness=77, contrast=88, saturation=99)

    day_result = asyncio.run(dn.apply_profile(
        camera_id="camA", profile="day",
        ip="1.2.3.4", username="admin", password="pw"))
    assert day_result["status"] == "ok"
    assert day_result["profile"] == "day"
    assert day_result["applied"]["brightness"] == 11
    assert "<tt:Brightness>11</tt:Brightness>" in captured["body"]

    night_result = asyncio.run(dn.apply_night_profile(
        camera_id="camA", ip="1.2.3.4",
        username="admin", password="pw"))
    assert night_result["profile"] == "night"
    assert night_result["applied"]["brightness"] == 77
    assert "<tt:Brightness>77</tt:Brightness>" in captured["body"]
    dn.clear()


def test_apply_explicit_levels_without_stored_profile(monkeypatch):
    _ok_canned(monkeypatch)
    from app.services import daynight as dn

    dn.clear()
    result = asyncio.run(dn.apply_profile(
        camera_id="camB", profile="night", ip="1.2.3.4",
        username="admin", password="pw",
        brightness=5, contrast=6, saturation=7))
    assert result["applied"] == {"brightness": 5, "contrast": 6, "saturation": 7}
    dn.clear()


def test_apply_requires_connection_and_levels(monkeypatch):
    _ok_canned(monkeypatch)
    from app.services import daynight as dn

    dn.clear()
    with pytest.raises(ValueError):
        asyncio.run(dn.apply_profile(
            camera_id="camZ", profile="day",
            ip="1.2.3.4", username="a", password="p"))
    with pytest.raises(ValueError):
        asyncio.run(dn.apply_profile(
            camera_id="camZ", profile="day",
            brightness=10, contrast=10, saturation=10))
    dn.clear()


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
    import app.api.daynight as daynight_api

    assert getattr(daynight_api, "router", None) is not None
    paths = _all_route_paths(daynight_api.router)
    assert any("daynight" in p or "day-night" in p or "day_night" in p
               for p in paths), paths
    assert any(p.rstrip("/").endswith("/set") for p in paths), paths
    assert any(p.rstrip("/").endswith("/apply") for p in paths), paths
    assert any(p.rstrip("/").endswith("/status") or p.rstrip("/").endswith("/verify")
               for p in paths), paths


def test_router_set_get_apply_flow(monkeypatch):
    _ok_canned(monkeypatch)
    import app.api.daynight as daynight_api
    from app.services import daynight as dn

    dn.clear()
    app = FastAPI()
    app.include_router(daynight_api.router)
    client = TestClient(app)

    r = client.post("/api/daynight/set", json={
        "camera_id": "r1", "profile": "day",
        "brightness": 60, "contrast": 55, "saturation": 65,
    })
    assert r.status_code == 200, r.text
    assert r.json()["brightness"] == 60

    r = client.post("/api/daynight/set", json={
        "camera_id": "r1", "profile": "night",
        "brightness": 80, "contrast": 85, "saturation": 90,
    })
    assert r.status_code == 200, r.text

    r = client.post("/api/daynight/get", json={"camera_id": "r1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["day"]["brightness"] == 60
    assert body["night"]["brightness"] == 80

    r = client.get("/api/daynight/r1/day")
    assert r.status_code == 200, r.text
    assert r.json()["brightness"] == 60

    r = client.get("/api/daynight/status")
    assert r.status_code == 200, r.text
    assert r.json()["on_demand_only"] is True

    r = client.post("/api/daynight/apply", json={
        "camera_id": "r1", "profile": "day",
        "ip": "1.2.3.4", "username": "admin", "password": "pw",
    })
    assert r.status_code == 200, r.text
    assert r.json()["profile"] == "day"
    assert r.json()["applied"]["brightness"] == 60

    r = client.post("/api/day-night/apply-night", json={
        "camera_id": "r1", "ip": "1.2.3.4",
        "username": "admin", "password": "pw",
    })
    assert r.status_code == 200, r.text
    assert r.json()["profile"] == "night"
    dn.clear()


def test_router_validation_errors():
    import app.api.daynight as daynight_api
    from app.services import daynight as dn

    dn.clear()
    app = FastAPI()
    app.include_router(daynight_api.router)
    client = TestClient(app)

    r = client.post("/api/daynight/set", json={"camera_id": "x"})
    assert r.status_code == 400, r.text

    r = client.post("/api/daynight/apply", json={
        "camera_id": "x", "profile": "day",
        "ip": "1.2.3.4", "username": "a", "password": "p",
    })
    # No stored profile and no levels -> 422 (mirrors imaging write path).
    assert r.status_code == 422, r.text
    dn.clear()
