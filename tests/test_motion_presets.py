from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import motion_presets as mp
from app.api import motion_presets as mp_api


def test_import():
    import app.services.motion_presets  # noqa: F401
    import app.api.motion_presets  # noqa: F401
    from app.services.motion_presets import MotionPreset, get_preset, list_presets  # noqa: F401
    from app.api.motion_presets import router  # noqa: F401


def test_service_basic():
    presets = mp.list_motion_presets()
    assert isinstance(presets, list)
    assert len(presets) >= 5
    names = {p["name"] for p in presets}
    for expected in ("low", "medium", "high"):
        assert expected in names

    for p in presets:
        assert "threshold" in p
        assert "contour_area" in p
        assert "threshold_percentage" in p
        assert "sensitivity" in p

    # Sensitivity ordering: very_low < low < medium < high < very_high.
    by_name = {p["name"]: p for p in presets}
    assert by_name["very_low"]["sensitivity"] < by_name["low"]["sensitivity"]
    assert by_name["low"]["sensitivity"] < by_name["medium"]["sensitivity"]
    assert by_name["medium"]["sensitivity"] < by_name["high"]["sensitivity"]
    assert by_name["high"]["sensitivity"] < by_name["very_high"]["sensitivity"]
    # Thresholds decrease as sensitivity increases.
    assert by_name["very_low"]["threshold"] > by_name["low"]["threshold"]
    assert by_name["low"]["threshold"] > by_name["medium"]["threshold"]
    assert by_name["medium"]["threshold"] > by_name["high"]["threshold"]
    assert by_name["high"]["threshold"] > by_name["very_high"]["threshold"]


def test_service_aliases():
    assert mp.get_preset("high") == mp.get_motion_preset("high")
    assert mp.get_preset("high") == mp.get_sensitivity_preset("high")
    assert mp.list_presets() == mp.list_motion_presets()
    assert mp.normalize_preset_name("Very High") == "very_high"
    assert mp.normalize_preset_name("very-high") == "very_high"
    assert mp.normalize_preset_name("veryHigh") == "very_high"
    assert mp.normalize_preset_name("HIGH") == "high"
    assert mp.is_valid_preset("medium") is True
    assert mp.is_valid_preset("nope-unknown") is False


def test_service_apply():
    config = mp.apply_preset("high", update_singleton=False)
    assert config["name"] == "high"
    assert "threshold" in config
    direct = mp.get_preset("high")
    assert config["threshold"] == direct["threshold"]


def test_router_endpoints():
    app = FastAPI()
    app.include_router(mp_api.router)
    client = TestClient(app)

    r = client.get("/api/motion_presets")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) >= 5

    r2 = client.get("/api/motion_presets/high")
    assert r2.status_code == 200
    assert r2.json()["name"] == "high"

    r3 = client.get("/api/motion-presets")
    assert r3.status_code == 200

    r4 = client.post("/api/motion_presets/apply", json={"preset": "low"})
    assert r4.status_code == 200
    assert r4.json()["name"] == "low"

    r5 = client.get("/api/motion_presets/does-not-exist")
    assert r5.status_code == 404


def test_paths_present():
    from app.main import app

    paths = list(app.openapi()["paths"].keys())
    assert "/api/motion_presets" in paths
