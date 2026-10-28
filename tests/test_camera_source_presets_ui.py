import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

BASE = Path(__file__).resolve().parents[1]
HTML = (BASE / "app" / "templates" / "index.html").read_text(encoding="utf-8")


def test_source_presets_ui_elements_present():
    # Manufacturer + model selects
    assert "sourcePresetManufacturer" in HTML
    assert "sourcePresetSelect" in HTML
    assert "cameraSourcePresetSelect" in HTML
    # Quick-add entry points
    assert "quickAddSourcePreset" in HTML
    assert "quickAddCameraSourcePreset" in HTML
    assert "quickAddPreset" in HTML
    # Loaders
    assert "loadSourcePresets" in HTML
    assert "loadCameraSourcePresets" in HTML
    assert "loadSourcePresetModels" in HTML


def test_source_presets_ui_fetches_backend():
    assert "/api/source_presets" in HTML


def test_camera_source_presets_endpoints_available():
    client = TestClient(app)
    for path in (
        "/api/cameras/source-presets",
        "/api/cameras/source_presets",
        "/api/cameras/presets",
        "/api/source_presets",
    ):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)


def test_source_preset_quick_add_builds_source():
    # Smoke: the helper exists in the template
    assert "buildSourceFromPreset" in HTML
    assert "applySourcePreset" in HTML
