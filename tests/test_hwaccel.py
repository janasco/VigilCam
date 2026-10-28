import os

from app.core.config import settings
from app.services import hwaccel


def test_hwaccel_import():
    import app.services.hwaccel  # noqa: F401
    from app.services.hwaccel import (  # noqa: F401
        get_video_hwaccel,
        get_video_hwaccel_device,
        get_ai_runtime,
        build_ffmpeg_hwaccel_args,
        get_ffmpeg_hwaccel_args,
        hwaccel_service,
    )


def test_hwaccel_config_fields():
    assert hasattr(settings, "VIDEO_HWACCEL")
    assert hasattr(settings, "VIDEO_HWACCEL_DEVICE")
    assert hasattr(settings, "AI_RUNTIME")


def test_hwaccel_defaults():
    assert isinstance(settings.VIDEO_HWACCEL, str)
    assert settings.AI_RUNTIME is not None


def test_hwaccel_getters_reflect_settings(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_HWACCEL", "vaapi", raising=False)
    monkeypatch.setattr(settings, "VIDEO_HWACCEL_DEVICE", "/dev/dri/renderD128", raising=False)
    monkeypatch.setattr(settings, "AI_RUNTIME", "cuda", raising=False)
    assert hwaccel.get_video_hwaccel() == "vaapi"
    assert hwaccel.get_video_hwaccel_device() == "/dev/dri/renderD128"
    assert hwaccel.get_ai_runtime() == "cuda"


def test_hwaccel_ffmpeg_args():
    args = hwaccel.get_ffmpeg_hwaccel_args(hwaccel="vaapi", device="/dev/dri/renderD128")
    assert args == ["-hwaccel", "vaapi", "-vaapi_device", "/dev/dri/renderD128", "-hwaccel_output_format", "vaapi"]


def test_hwaccel_none_disables_args():
    args = hwaccel.build_ffmpeg_hwaccel_args(hwaccel="none")
    assert args == []


def test_hwaccel_cuda_device():
    args = hwaccel.build_ffmpeg_hwaccel_args(hwaccel="cuda", device="0")
    assert args == ["-hwaccel", "cuda", "-hwaccel_device", "0"]


def test_hwaccel_config_snapshot():
    cfg = hwaccel.get_hwaccel_config()
    assert "video_hwaccel" in cfg
    assert "ai_runtime" in cfg