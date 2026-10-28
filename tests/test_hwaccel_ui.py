import os

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import hwaccel


def test_hwaccel_ui_config_fields():
    for name in ("VIDEO_HWACCEL", "VIDEO_HWACCEL_DEVICE", "AI_RUNTIME", "VIDEO_ENCODER", "VIDEO_ENCODER_DEVICE"):
        assert hasattr(settings, name)


def test_hwaccel_ui_getters_reflect_settings(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_ENCODER", "h264_nvenc", raising=False)
    monkeypatch.setattr(settings, "VIDEO_ENCODER_DEVICE", "0", raising=False)
    assert hwaccel.get_video_encoder() == "h264_nvenc"
    assert hwaccel.get_video_encoder_device() == "0"


def test_hwaccel_ui_alias_getters():
    assert "h264_vaapi" in hwaccel.available_video_encoders()
    assert hwaccel.get_encoder() == hwaccel.get_video_encoder()


def test_hwaccel_ui_api_roundtrip():
    client = TestClient(app)
    r = client.get("/api/hwaccel/settings")
    assert r.status_code == 200
    data = r.json()
    assert "VIDEO_ENCODER" in data
    r2 = client.put("/api/hwaccel/settings", json={"VIDEO_ENCODER": "h264_qsv", "VIDEO_HWACCEL": "vaapi"})
    assert r2.status_code == 200
    assert r2.json()["VIDEO_ENCODER"] == "h264_qsv"
    assert r2.json()["VIDEO_HWACCEL"] == "vaapi"


def test_hwaccel_ui_frontend_panel():
    html = open("app/templates/index.html", encoding="utf-8").read()
    assert "set-hardware" in html
    assert "hwSettingsForm" in html
    assert "hwVideoEncoder" in html
