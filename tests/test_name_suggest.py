from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import camera_onboarding as co
from app.services import name_suggest as svc


def test_camera_creation_requires_manual_name():
    from pydantic import ValidationError

    from app.schemas import CameraCreate

    # Name is mandatory: camera creation cannot invent one.
    try:
        CameraCreate(kind="onvif", source="onvif://192.168.1.10:80")
        raise AssertionError("CameraCreate without name should fail")
    except ValidationError:
        pass

    ok = CameraCreate(kind="onvif", source="onvif://192.168.1.10:80", name="Front Door")
    assert ok.name == "Front Door"


def test_onboarding_derives_ip_only_no_name_suggestions():
    # Onboarding derives IP/host only; no device-info name helpers exist there.
    assert co.derive_ip_address("rtsp://192.168.1.10:554/live", None) == "192.168.1.10"
    assert co.derive_ip_address(None, "192.168.1.20:8088") == "192.168.1.20"
    assert co.derive_ip_address(None, None) is None
    for attr in ("suggest_name", "suggest_names", "build_name_suggestions", "name_suggest"):
        assert not hasattr(co, attr), f"onboarding should not provide {attr}"


def test_normalize_device_info_handles_onvif_keys():
    # Keys as returned by onvif_digest_service.get_device_info (lowercase).
    norm = svc.normalize_device_info({
        "manufacturer": "Hikvision",
        "model": "DS-2CD2143G0-I",
        "firmwareversion": "V5.6.3",
        "serialnumber": "ABC123",
        "hardwareid": "HW1",
    })
    assert norm["manufacturer"] == "Hikvision"
    assert norm["model"] == "DS-2CD2143G0-I"
    assert norm["firmware_version"] == "V5.6.3"
    assert norm["serial_number"] == "ABC123"


def test_build_name_suggestions_prefers_device_info():
    suggestions = svc.build_name_suggestions(
        {"manufacturer": "Hikvision", "model": "DS-2CD2143G0-I"},
        ip="192.168.1.10",
    )
    assert suggestions
    assert suggestions[0] == "Hikvision DS-2CD2143G0-I"
    assert any("192.168.1.10" in s for s in suggestions)
    # No duplicates, within CameraCreate length limit.
    assert len(suggestions) == len({s.lower() for s in suggestions})
    assert all(len(s) <= 120 for s in suggestions)


def test_build_name_suggestions_unknown_and_ip_fallback():
    assert svc.build_name_suggestions({"manufacturer": "Unknown", "model": "Unknown"}, ip=None) == ["Camera"]
    ip_only = svc.build_name_suggestions({}, ip="10.0.0.5")
    assert ip_only[0] == "Camera 10.0.0.5"
    model_only = svc.build_name_suggestions({"model": "Tapo C200"}, ip="10.0.0.6")
    assert model_only[0] == "Tapo C200"


def test_suggest_camera_name_uniquifies():
    name = svc.suggest_camera_name(
        {"manufacturer": "Axis", "model": "M3045-V"},
        ip="192.168.1.11",
        existing_names=["Axis M3045-V"],
    )
    assert name == "Axis M3045-V 2"


def _mini_client():
    from app.api import name_suggest as api_mod

    assert hasattr(api_mod, "router")
    assert api_mod.router.prefix == "/api/name-suggest"
    assert api_mod.underscore_router.prefix == "/api/name_suggest"
    mini = FastAPI()
    mini.include_router(api_mod.router)
    mini.include_router(api_mod.underscore_router)
    return TestClient(mini)


def test_imports_and_routes():
    import pkgutil

    import app.api as api_pkg

    names = {m.name for m in pkgutil.iter_modules(api_pkg.__path__)}
    assert "name_suggest" in names
    import app.services.name_suggest as svc_mod

    assert hasattr(svc_mod, "build_name_suggestions")
    assert hasattr(svc_mod, "suggest_camera_name")
    assert hasattr(svc_mod, "normalize_device_info")
    client = _mini_client()
    r = client.get("/api/name-suggest/suggest", params={"manufacturer": "H", "model": "M"})
    assert r.status_code == 200


def test_endpoints_suggest_and_fetch():
    client = _mini_client()

    r = client.get("/api/name-suggest/suggest", params={
        "manufacturer": "Hikvision", "model": "DS-2CD2143G0-I", "ip": "192.168.1.10",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["primary"] == "Hikvision DS-2CD2143G0-I"
    assert "Hikvision DS-2CD2143G0-I" in body["suggestions"]

    r2 = client.post("/api/name-suggest/suggest", json={
        "device_info": {"manufacturer": "Axis", "model": "M3045-V"},
        "ip": "192.168.1.11",
    })
    assert r2.status_code == 200
    assert r2.json()["primary"] == "Axis M3045-V"

    r3 = client.post("/api/name_suggest/suggest", json={
        "manufacturer": "Reolink", "model": "RLC-810A", "ip_address": "10.0.0.7",
    })
    assert r3.status_code == 200
    assert "Reolink RLC-810A" in r3.json()["suggestions"]

    # Live fetch path is mocked (no network): patch service fetch.
    async def _fake_fetch(ip, username="", password="", port=None, scheme=None, **kwargs):
        return {"device_info": {"manufacturer": "MockCam", "model": "M1"}, "ip": ip,
                "ip_address": ip, "suggestions": ["MockCam M1", f"MockCam M1 ({ip})"],
                "names": ["MockCam M1"], "primary": "MockCam M1",
                "suggested_name": "MockCam M1", "name": "MockCam M1"}

    orig = svc.suggest_names_for_device
    svc.suggest_names_for_device = _fake_fetch
    try:
        rf = client.post("/api/name-suggest/fetch", json={
            "ip": "192.168.1.99", "username": "admin", "password": "secret",
        })
        assert rf.status_code == 200
        assert rf.json()["primary"] == "MockCam M1"
    finally:
        svc.suggest_names_for_device = orig

    # Missing ip on fetch is a 422, not a crash.
    r422 = client.post("/api/name-suggest/fetch", json={})
    assert r422.status_code == 422
