from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

BASE = Path(__file__).resolve().parents[1]
HTML = (BASE / "app" / "templates" / "index.html").read_text(encoding="utf-8")


def test_ui_elements():
    for name in ("motionTunerCameraSelect", "motionTunerThreshold", "motionTunerContourArea",
                 "motionTunerImproveContrast", "motionTunerImage", "motionTunerCanvas",
                 "motionTunerStatus", "tab-motion-tuner"):
        assert name in HTML


def test_ui_fetches_backend():
    assert "/api/motion_tuner" in HTML or "motion_tuner" in HTML


def test_index_loads():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "Motion Tuner" in r.text
