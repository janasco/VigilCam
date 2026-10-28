"""Solar day/night switching tests (service + router).

Covers stdlib solar approximation (sunrise/sunset from lat/lon/date),
day/night resolution, per-camera location configs, and router coverage
(mirrors ``test_daynight.py`` conventions).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def test_imports():
    import app.services.solar_schedule  # noqa: F401
    import app.api.solar_schedule  # noqa: F401
    from app.services.solar_schedule import (  # noqa: F401
        calculate,
        sunrise,
        sunset,
        solar_noon,
        is_day,
        is_night,
        current_profile,
        resolve_profile,
        next_transition,
        configure,
        get_config,
        list_configs,
        clear,
        status,
        apply_solar_profile,
    )


def test_calculate_equator_equinox():
    from app.services import solar_schedule as ss

    ss.clear()
    # Equator on equinox: ~06:00 sunrise / ~18:00 sunset UTC.
    out = ss.calculate(lat=0.0, lon=0.0, date="2026-03-20", utc_offset=0)
    assert out["ok"] is True
    assert out["sunrise"] is not None and out["sunset"] is not None
    assert abs(_mins(out["sunrise"]) - 6 * 60) <= 20, out
    assert abs(_mins(out["sunset"]) - 18 * 60) <= 20, out
    assert _mins(out["sunrise"]) < _mins(out["solar_noon"]) < _mins(out["sunset"])
    assert out["method"] == "noaa-approx-stdlib"
    ss.clear()


def test_sunrise_sunset_helpers_and_ordering():
    from app.services import solar_schedule as ss

    ss.clear()
    rise = ss.sunrise(lat=51.5, lon=0.0, date="2026-06-21")
    set_ = ss.sunset(lat=51.5, lon=0.0, date="2026-06-21")
    noon = ss.solar_noon(lat=51.5, lon=0.0, date="2026-06-21")
    assert rise is not None and set_ is not None and noon is not None
    # London midsummer: long day, sunrise well before sunset.
    assert (_mins(set_) - _mins(rise)) % 1440 > 12 * 60
    ss.clear()


def test_validation_errors():
    from app.services import solar_schedule as ss

    ss.clear()
    # Valid baseline succeeds.
    ok = ss.calculate(lat=0.0, lon=0.0, date="2026-03-20")
    assert ok["sunrise"] is not None
    # Now check bad inputs.
    with pytest.raises(ValueError):
        ss.calculate(lon=0.0, date="2026-03-20")
    with pytest.raises(ValueError):
        ss.calculate(lat=0.0, date="2026-03-20")
    with pytest.raises(ValueError):
        ss.calculate(lat=100.0, lon=0.0)
    with pytest.raises(ValueError):
        ss.calculate(lat=0.0, lon=200.0)
    with pytest.raises(ValueError):
        ss.configure(camera_id="c1", lat=0.0, lon=0.0, utc_offset=99)
    with pytest.raises(ValueError):
        ss.configure(lat=0.0, lon=0.0)
    ss.clear()


def test_is_day_midday_vs_midnight():
    from app.services import solar_schedule as ss

    ss.clear()
    # Greenwich equinox: noon UTC is day, midnight UTC is night.
    assert ss.is_day(lat=0.0, lon=0.0, now="2026-03-20T12:00:00Z") is True
    assert ss.is_day(lat=0.0, lon=0.0, now="2026-03-20T00:00:00Z") is False
    assert ss.is_night(lat=0.0, lon=0.0, now="2026-03-20T00:00:00Z") is True
    assert ss.current_profile(lat=0.0, lon=0.0, now="2026-03-20T12:00:00Z") == "day"
    assert ss.current_profile(lat=0.0, lon=0.0, now="2026-03-20T00:00:00Z") == "night"
    ss.clear()


def test_configure_resolve_roundtrip():
    from app.services import solar_schedule as ss

    ss.clear()
    cfg = ss.configure(camera_id="cam1", lat=40.7, lon=-74.0, utc_offset=-4)
    assert cfg["camera_id"] == "cam1"
    assert cfg["lat"] == pytest.approx(40.7)
    assert cfg["utc_offset"] == pytest.approx(-4.0)
    assert ss.get_config("cam1")["lon"] == pytest.approx(-74.0)
    assert len(ss.list_configs()) == 1

    # Noon local (16:00Z in EDT) should be day; midnight local (04:00Z) night.
    day = ss.resolve_profile(camera_id="cam1", now="2026-06-21T16:00:00Z")
    assert day["profile"] == "day" and day["is_day"] is True
    night = ss.resolve_profile(camera_id="cam1", now="2026-06-21T04:00:00Z")
    assert night["profile"] == "night" and night["is_night"] is True

    nxt = ss.next_transition(camera_id="cam1", now="2026-06-21T16:00:00Z")
    assert nxt["next"] is not None and nxt["event"] in ("sunrise", "sunset")
    assert ss.status()["count"] == 1
    ss.clear()
    assert ss.list_configs() == []


def test_apply_solar_profile_uses_daynight_on_demand(monkeypatch):
    from app.services import solar_schedule as ss
    from app.services import daynight as dn
    from app.services import onvif_digest_service as ods

    captured: dict = {}

    async def fake_request(url, body_xml, username=None, password=None, timeout=10.0):
        if "GetVideoSources" in body_xml:
            return 200, ("<Envelope><Body><trt:GetVideoSourcesResponse>"
                          "<tt:VideoSources token=\"vs0\"/></trt:GetVideoSourcesResponse></Body></Envelope>")
        if "GetImagingSettings" in body_xml:
            return 200, ("<Envelope><Body><timg:GetImagingSettingsResponse>"
                          "<tt:Brightness>50</tt:Brightness></tt:ImagingSettingsResponse></Body></Envelope>")
        if "SetImagingSettings" in body_xml:
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

    import asyncio

    ss.clear()
    dn.clear()
    dn.set_profile(camera_id="sol1", profile="day", brightness=11, contrast=22, saturation=33)
    dn.set_profile(camera_id="sol1", profile="night", brightness=77, contrast=88, saturation=99)
    ss.configure(camera_id="sol1", lat=0.0, lon=0.0, utc_offset=0)

    day_res = asyncio.run(ss.apply_solar_profile(
        camera_id="sol1", now="2026-03-20T12:00:00Z",
        ip="1.2.3.4", username="admin", password="pw"))
    assert day_res["profile"] == "day"
    assert day_res["solar"]["profile"] == "day"

    night_res = asyncio.run(ss.apply_solar_profile(
        camera_id="sol1", now="2026-03-20T00:00:00Z",
        ip="1.2.3.4", username="admin", password="pw"))
    assert night_res["profile"] == "night"
    assert "<tt:Brightness>77</tt:Brightness>" in captured["body"]
    ss.clear()
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
    import app.api.solar_schedule as solar_api

    assert getattr(solar_api, "router", None) is not None
    paths = _all_route_paths(solar_api.router)
    assert any("solar" in p for p in paths), paths
    assert any(p.rstrip("/").endswith("/calculate") for p in paths), paths
    assert any(p.rstrip("/").endswith("/resolve") for p in paths), paths
    assert any(p.rstrip("/").endswith("/apply") for p in paths), paths
    assert any(p.rstrip("/").endswith("/status") or p.rstrip("/").endswith("/health")
               for p in paths), paths


def test_router_calculate_configure_resolve_flow():
    import app.api.solar_schedule as solar_api
    from app.services import solar_schedule as ss

    ss.clear()
    app = FastAPI()
    app.include_router(solar_api.router)
    client = TestClient(app)

    r = client.post("/api/solar_schedule/calculate", json={
        "lat": 0.0, "lon": 0.0, "date": "2026-03-20", "utc_offset": 0,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sunrise"] is not None and body["sunset"] is not None

    r = client.post("/api/solar_schedule/configure", json={
        "camera_id": "r1", "lat": 40.7, "lon": -74.0, "utc_offset": -4,
    })
    assert r.status_code == 200, r.text
    assert r.json()["camera_id"] == "r1"

    r = client.post("/api/solar_schedule/resolve", json={
        "camera_id": "r1", "now": "2026-06-21T16:00:00Z",
    })
    assert r.status_code == 200, r.text
    assert r.json()["profile"] == "day"

    r = client.get("/api/solar_schedule/status")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    ss.clear()


def test_router_validation_errors():
    import app.api.solar_schedule as solar_api
    from app.services import solar_schedule as ss

    ss.clear()
    app = FastAPI()
    app.include_router(solar_api.router)
    client = TestClient(app)

    r = client.post("/api/solar_schedule/calculate", json={"lat": 0.0})
    assert r.status_code == 400, r.text

    r = client.post("/api/solar_schedule/resolve", json={"camera_id": "nope"})
    assert r.status_code == 400, r.text
    ss.clear()
