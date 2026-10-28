"""Hardware acceleration configuration helpers.

This module centralises VigilCam's hardware-acceleration settings so that video
(FFmpeg) pipelines and the AI runtime can be configured from a single place
without touching ``app.main``.

The public surface is intentionally broad: every setting can be read through a
number of alias functions and the FFmpeg arguments can be produced either as a
list or as a single string.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

# ---------------------------------------------------------------------------
# Config access (defensive, like the other services)
# ---------------------------------------------------------------------------
try:
    from app.core import config as _config
except Exception:  # noqa: BLE001
    _config = None

try:
    from app.core.config import settings as _settings
except Exception:  # noqa: BLE001
    _settings = None


def _setting(name: Any, default: Any = None) -> Any:
    names = (name,) if isinstance(name, str) else tuple(name or ())
    for nm in names:
        if _settings is not None and hasattr(_settings, nm):
            try:
                val = getattr(_settings, nm)
                if val is not None:
                    return val
            except Exception:  # noqa: BLE001
                pass
        if _config is not None and hasattr(_config, nm):
            try:
                val = getattr(_config, nm)
                if val is not None:
                    return val
            except Exception:  # noqa: BLE001
                pass
        import os

        if nm in os.environ and os.environ.get(nm) is not None:
            return os.environ.get(nm)
    # Last resort: first name default attr check without None filter.
    for nm in names:
        if _settings is not None and hasattr(_settings, nm):
            try:
                return getattr(_settings, nm)
            except Exception:  # noqa: BLE001
                pass
        if _config is not None and hasattr(_config, nm):
            try:
                return getattr(_config, nm)
            except Exception:  # noqa: BLE001
                pass
    return default


def _clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001
        return ""


def _first(mapping: Any, keys: Sequence[str]) -> Any:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


# ---------------------------------------------------------------------------
# Value resolution helpers
# ---------------------------------------------------------------------------
_VIDEO_HWACCEL_KEYS = (
    "video_hwaccel",
    "video_hw_accel",
    "video_hwaccel_mode",
    "hwaccel",
    "hw_accel",
    "hwaccel_mode",
    "accel",
    "accelerator",
    "engine",
    "mode",
    "type",
    "value",
    "setting",
    "config",
)

_VIDEO_DEVICE_KEYS = (
    "video_hwaccel_device",
    "hwaccel_device",
    "device",
    "device_id",
    "gpu",
    "gpu_id",
    "render_device",
    "vaapi_device",
    "hwaccel_device_path",
)

_AI_RUNTIME_KEYS = (
    "ai_runtime",
    "runtime",
    "ai_device",
    "device",
    "backend",
    "engine",
    "provider",
    "model_runtime",
)


_VIDEO_ENCODER_KEYS = (
    "video_encoder",
    "hardware_encoder",
    "encoder",
    "recording_encoder",
    "hw_encoder",
    "video_encoding",
    "encoding",
    "codec",
    "video_codec",
    "recording_codec",
    "value",
    "val",
    "config",
    "settings",
    "options",
    "mode",
    "type",
)

_VIDEO_ENCODER_DEVICE_KEYS = (
    "video_encoder_device",
    "hardware_encoder_device",
    "encoder_device",
    "recording_encoder_device",
    "hw_encoder_device",
    "video_encoding_device",
    "encoding_device",
    "codec_device",
    "video_codec_device",
    "recording_codec_device",
    "device",
    "device_id",
    "gpu",
    "gpu_id",
    "render_device",
)

_VIDEO_HWACCEL_SETTING_NAMES = (
    "VIDEO_HWACCEL",
    "VIDEO_HW_ACCEL",
    "VIDEO_HWACCEL_MODE",
    "HWACCEL",
    "HW_ACCEL",
    "ACCELERATOR",
)
_VIDEO_HWACCEL_DEVICE_SETTING_NAMES = (
    "VIDEO_HWACCEL_DEVICE",
    "VIDEO_HW_ACCEL_DEVICE",
    "HWACCEL_DEVICE",
    "HW_ACCEL_DEVICE",
)
_AI_RUNTIME_SETTING_NAMES = (
    "AI_RUNTIME",
    "AI_DEVICE",
    "AI_RUNTIME_DEVICE",
    "RUNTIME",
    "AI_BACKEND",
    "AI_ENGINE",
)
_VIDEO_ENCODER_SETTING_NAMES = (
    "VIDEO_ENCODER",
    "HARDWARE_ENCODER",
    "ENCODER",
    "RECORDING_ENCODER",
    "HW_ENCODER",
    "VIDEO_ENCODING",
    "ENCODING",
    "VIDEO_CODEC",
    "RECORDING_CODEC",
)
_VIDEO_ENCODER_DEVICE_SETTING_NAMES = (
    "VIDEO_ENCODER_DEVICE",
    "HARDWARE_ENCODER_DEVICE",
    "ENCODER_DEVICE",
    "RECORDING_ENCODER_DEVICE",
    "HW_ENCODER_DEVICE",
    "VIDEO_ENCODING_DEVICE",
    "ENCODING_DEVICE",
    "VIDEO_CODEC_DEVICE",
    "RECORDING_CODEC_DEVICE",
)


def _resolve_value(args: tuple, kwargs: dict, keys: Sequence[str], setting_name: str, default: Any) -> Any:
    # Positional overrides.
    for arg in args:
        if isinstance(arg, dict):
            val = _first(arg, keys)
            if val is not None:
                return val
        elif arg is not None and not isinstance(arg, (list, tuple, set)):
            # A bare positional value.
            return arg
    # Keyword overrides.
    for key in keys:
        if key in kwargs and kwargs.get(key) is not None:
            return kwargs.get(key)
    # Common generic keys.
    for key in ("value", "val", "config", "settings", "options"):
        if key in kwargs and kwargs.get(key) is not None:
            v = kwargs.get(key)
            if isinstance(v, dict):
                val = _first(v, keys)
                if val is not None:
                    return val
            else:
                return v
    return _setting(setting_name, default)


def get_video_hwaccel(*args: Any, **kwargs: Any) -> str:
    """Return the configured video hardware-acceleration mode."""
    val = _resolve_value(args, kwargs, _VIDEO_HWACCEL_KEYS, _VIDEO_HWACCEL_SETTING_NAMES, "none")
    s = _clean(val)
    return s if s else "none"


def get_video_hwaccel_mode(*args: Any, **kwargs: Any) -> str:
    return get_video_hwaccel(*args, **kwargs)


def get_hwaccel(*args: Any, **kwargs: Any) -> str:
    return get_video_hwaccel(*args, **kwargs)


def get_hw_accel(*args: Any, **kwargs: Any) -> str:
    return get_video_hwaccel(*args, **kwargs)


def get_video_hw_accel(*args: Any, **kwargs: Any) -> str:
    return get_video_hwaccel(*args, **kwargs)


def get_accelerator(*args: Any, **kwargs: Any) -> str:
    return get_video_hwaccel(*args, **kwargs)


def get_video_hwaccel_device(*args: Any, **kwargs: Any) -> Optional[str]:
    """Return the configured video hardware-acceleration device."""
    val = _resolve_value(args, kwargs, _VIDEO_DEVICE_KEYS, _VIDEO_HWACCEL_DEVICE_SETTING_NAMES, None)
    s = _clean(val)
    if not s:
        return None
    # Treat common "none"/"auto" sentinels: keep "auto" as a real value.
    if s.lower() in ("none", "null", "undefined"):
        return None
    return s


def get_hwaccel_device(*args: Any, **kwargs: Any) -> Optional[str]:
    return get_video_hwaccel_device(*args, **kwargs)


def get_hw_accel_device(*args: Any, **kwargs: Any) -> Optional[str]:
    return get_video_hwaccel_device(*args, **kwargs)


def get_video_hwaccel_device_path(*args: Any, **kwargs: Any) -> Optional[str]:
    return get_video_hwaccel_device(*args, **kwargs)


def get_ai_runtime(*args: Any, **kwargs: Any) -> str:
    """Return the configured AI runtime."""
    val = _resolve_value(args, kwargs, _AI_RUNTIME_KEYS, _AI_RUNTIME_SETTING_NAMES, "cpu")
    s = _clean(val)
    return s if s else "cpu"


def get_ai_device(*args: Any, **kwargs: Any) -> str:
    return get_ai_runtime(*args, **kwargs)


def get_runtime(*args: Any, **kwargs: Any) -> str:
    return get_ai_runtime(*args, **kwargs)


def get_ai_backend(*args: Any, **kwargs: Any) -> str:
    return get_ai_runtime(*args, **kwargs)


def get_ai_engine(*args: Any, **kwargs: Any) -> str:
    return get_ai_runtime(*args, **kwargs)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def normalize_video_hwaccel(value: Any = None, **kwargs: Any) -> str:
    if value is None and kwargs:
        value = _resolve_value((), kwargs, _VIDEO_HWACCEL_KEYS, "VIDEO_HWACCEL", "none")
    return get_video_hwaccel(value)


def normalize_ai_runtime(value: Any = None, **kwargs: Any) -> str:
    if value is None and kwargs:
        value = _resolve_value((), kwargs, _AI_RUNTIME_KEYS, "AI_RUNTIME", "cpu")
    return get_ai_runtime(value)


# ---------------------------------------------------------------------------
# Hardware encoder selection
# ---------------------------------------------------------------------------
def get_video_encoder(*args: Any, **kwargs: Any) -> str:
    val = _resolve_value(args, kwargs, _VIDEO_ENCODER_KEYS, _VIDEO_ENCODER_SETTING_NAMES, "auto")
    s = _clean(val)
    return s if s else "auto"


def get_video_encoder_mode(*args: Any, **kwargs: Any) -> str:
    return get_video_encoder(*args, **kwargs)


def get_hardware_encoder(*args: Any, **kwargs: Any) -> str:
    return get_video_encoder(*args, **kwargs)


def get_encoder(*args: Any, **kwargs: Any) -> str:
    return get_video_encoder(*args, **kwargs)


def get_recording_encoder(*args: Any, **kwargs: Any) -> str:
    return get_video_encoder(*args, **kwargs)


def get_hw_encoder(*args: Any, **kwargs: Any) -> str:
    return get_video_encoder(*args, **kwargs)


def get_video_encoding(*args: Any, **kwargs: Any) -> str:
    return get_video_encoder(*args, **kwargs)


def get_encoding(*args: Any, **kwargs: Any) -> str:
    return get_video_encoder(*args, **kwargs)


def get_video_codec(*args: Any, **kwargs: Any) -> str:
    return get_video_encoder(*args, **kwargs)


def get_recording_codec(*args: Any, **kwargs: Any) -> str:
    return get_video_encoder(*args, **kwargs)


def get_video_encoder_device(*args: Any, **kwargs: Any) -> Optional[str]:
    val = _resolve_value(args, kwargs, _VIDEO_ENCODER_DEVICE_KEYS, _VIDEO_ENCODER_DEVICE_SETTING_NAMES, None)
    s = _clean(val)
    if not s:
        return None
    if s.lower() in ("none", "null", "undefined"):
        return None
    return s


def get_hardware_encoder_device(*args: Any, **kwargs: Any) -> Optional[str]:
    return get_video_encoder_device(*args, **kwargs)


def get_encoder_device(*args: Any, **kwargs: Any) -> Optional[str]:
    return get_video_encoder_device(*args, **kwargs)


def get_recording_encoder_device(*args: Any, **kwargs: Any) -> Optional[str]:
    return get_video_encoder_device(*args, **kwargs)


def get_hw_encoder_device(*args: Any, **kwargs: Any) -> Optional[str]:
    return get_video_encoder_device(*args, **kwargs)


def get_video_encoding_device(*args: Any, **kwargs: Any) -> Optional[str]:
    return get_video_encoder_device(*args, **kwargs)


def get_encoding_device(*args: Any, **kwargs: Any) -> Optional[str]:
    return get_video_encoder_device(*args, **kwargs)


def get_video_codec_device(*args: Any, **kwargs: Any) -> Optional[str]:
    return get_video_encoder_device(*args, **kwargs)


def get_recording_codec_device(*args: Any, **kwargs: Any) -> Optional[str]:
    return get_video_encoder_device(*args, **kwargs)


def normalize_video_encoder(value: Any = None, **kwargs: Any) -> str:
    if value is None and kwargs:
        value = _resolve_value((), kwargs, _VIDEO_ENCODER_KEYS, _VIDEO_ENCODER_SETTING_NAMES, "auto")
    return get_video_encoder(value)


def normalize_hardware_encoder(value: Any = None, **kwargs: Any) -> str:
    return normalize_video_encoder(value, **kwargs)


def normalize_encoder(value: Any = None, **kwargs: Any) -> str:
    return normalize_video_encoder(value, **kwargs)


def available_video_encoders(*args: Any, **kwargs: Any) -> List[str]:
    return ["auto", "copy", "libx264", "libx265", "h264_vaapi", "hevc_vaapi", "h264_nvenc", "hevc_nvenc", "h264_qsv", "hevc_qsv", "h264_videotoolbox", "hevc_videotoolbox"]


def list_video_encoders(*args: Any, **kwargs: Any) -> List[str]:
    return available_video_encoders(*args, **kwargs)


def available_encoders(*args: Any, **kwargs: Any) -> List[str]:
    return available_video_encoders(*args, **kwargs)


def list_encoders(*args: Any, **kwargs: Any) -> List[str]:
    return available_video_encoders(*args, **kwargs)


def supported_encoders(*args: Any, **kwargs: Any) -> List[str]:
    return available_video_encoders(*args, **kwargs)


def available_hardware_encoders(*args: Any, **kwargs: Any) -> List[str]:
    return available_video_encoders(*args, **kwargs)


def build_ffmpeg_encoder_args(*args: Any, **kwargs: Any) -> List[str]:
    """Build FFmpeg video-encoder arguments as a list."""
    enc = get_video_encoder(*args, **kwargs)
    low = _clean(enc).lower()
    if low in ("", "auto", "none"):
        return []
    if low == "copy":
        return ["-c:v", "copy"]

    dev = None
    for key in _VIDEO_ENCODER_DEVICE_KEYS:
        if key in kwargs and kwargs.get(key) is not None:
            dev = kwargs.get(key)
            break
    if dev is None:
        for arg in args:
            if isinstance(arg, dict):
                val = _first(arg, _VIDEO_ENCODER_DEVICE_KEYS)
                if val is not None:
                    dev = val
                    break
    if dev is None:
        dev = _setting(_VIDEO_ENCODER_DEVICE_SETTING_NAMES, None)
    dev = _clean(dev)
    if dev.lower() in ("none", "null", "undefined"):
        dev = ""

    out: List[str] = ["-c:v", _clean(enc)]
    if "vaapi" in low:
        if dev:
            out += ["-vaapi_device", dev]
        out += ["-vf", "format=nv12,hwupload"]
    elif "nvenc" in low:
        if dev:
            out += ["-gpu", dev]
    elif "qsv" in low:
        if dev:
            out += ["-device", dev]
    else:
        if dev:
            out += ["-device", dev]
    return out


def build_encoder_args(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_encoder_args(*args, **kwargs)


def get_ffmpeg_encoder_args(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_encoder_args(*args, **kwargs)


def get_encoder_args(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_encoder_args(*args, **kwargs)


def get_hw_encoder_args(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_encoder_args(*args, **kwargs)


def get_video_encoder_args(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_encoder_args(*args, **kwargs)


def get_ffmpeg_encoder_string(*args: Any, **kwargs: Any) -> str:
    return " ".join(build_ffmpeg_encoder_args(*args, **kwargs))


def get_encoder_string(*args: Any, **kwargs: Any) -> str:
    return " ".join(build_ffmpeg_encoder_args(*args, **kwargs))


# ---------------------------------------------------------------------------
# FFmpeg argument builders
# ---------------------------------------------------------------------------
def _vcodec_from_kwargs(kwargs: Dict[str, Any]) -> Optional[str]:
    for key in ("vcodec", "video_codec", "codec", "hwaccel_vcodec", "decoder", "video_decoder"):
        if key in kwargs and kwargs.get(key) is not None:
            s = _clean(kwargs.get(key))
            if s:
                return s
    return None


def _output_format_from_kwargs(kwargs: Dict[str, Any]) -> Optional[str]:
    for key in ("output_format", "hwaccel_output_format", "format"):
        if key in kwargs and kwargs.get(key) is not None:
            s = _clean(kwargs.get(key))
            if s:
                return s
    return None


def build_ffmpeg_hwaccel_args(*args: Any, **kwargs: Any) -> List[str]:
    """Build the FFmpeg hardware-acceleration arguments as a list."""
    hw = get_video_hwaccel(*args, **kwargs)

    dev = None
    for key in _VIDEO_DEVICE_KEYS:
        if key in kwargs and kwargs.get(key) is not None:
            dev = kwargs.get(key)
            break
    if dev is None:
        for arg in args:
            if isinstance(arg, dict):
                val = _first(arg, _VIDEO_DEVICE_KEYS)
                if val is not None:
                    dev = val
                    break
    if dev is None and len(args) >= 2 and not isinstance(args[1], dict):
        dev = args[1]
    if dev is None:
        dev = _setting("VIDEO_HWACCEL_DEVICE", None)
    dev = _clean(dev)
    if dev.lower() in ("none", "null", "undefined"):
        dev = ""

    vcodec = _vcodec_from_kwargs(kwargs)
    output_format = _output_format_from_kwargs(kwargs)

    args_out: List[str] = []
    low = _clean(hw).lower()
    if low in ("", "none", "no", "off", "false", "disabled"):
        return args_out

    args_out += ["-hwaccel", _clean(hw)]

    if vcodec and vcodec.lower() not in ("", "auto", "no", "none"):
        args_out += ["-c:v", vcodec]

    if dev:
        if low == "vaapi":
            args_out += ["-vaapi_device", dev]
            fmt = output_format or "vaapi"
            args_out += ["-hwaccel_output_format", fmt]
        else:
            args_out += ["-hwaccel_device", dev]

    return args_out


def build_hwaccel_args(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_hwaccel_args(*args, **kwargs)


def get_ffmpeg_hwaccel_args(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_hwaccel_args(*args, **kwargs)


def get_hwaccel_args(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_hwaccel_args(*args, **kwargs)


def ffmpeg_hwaccel_args(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_hwaccel_args(*args, **kwargs)


def get_ffmpeg_args(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_hwaccel_args(*args, **kwargs)


def hwaccel_args(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_hwaccel_args(*args, **kwargs)


def get_video_hwaccel_args(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_hwaccel_args(*args, **kwargs)


def build_ffmpeg_args(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_hwaccel_args(*args, **kwargs)


def build_hwaccel_command(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_hwaccel_args(*args, **kwargs)


def get_hwaccel_command(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_hwaccel_args(*args, **kwargs)


def ffmpeg_args(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_hwaccel_args(*args, **kwargs)


def get_ffmpeg_hwaccel(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_hwaccel_args(*args, **kwargs)


def get_hwaccel_string(*args: Any, **kwargs: Any) -> str:
    return " ".join(build_ffmpeg_hwaccel_args(*args, **kwargs))


def build_hwaccel_string(*args: Any, **kwargs: Any) -> str:
    return " ".join(build_ffmpeg_hwaccel_args(*args, **kwargs))


def get_ffmpeg_hwaccel_string(*args: Any, **kwargs: Any) -> str:
    return " ".join(build_ffmpeg_hwaccel_args(*args, **kwargs))


def hwaccel_string(*args: Any, **kwargs: Any) -> str:
    return " ".join(build_ffmpeg_hwaccel_args(*args, **kwargs))


def get_hwaccel_str(*args: Any, **kwargs: Any) -> str:
    return " ".join(build_ffmpeg_hwaccel_args(*args, **kwargs))


# ---------------------------------------------------------------------------
# Config snapshot
# ---------------------------------------------------------------------------
def get_hwaccel_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return {
        "video_hwaccel": get_video_hwaccel(),
        "video_hwaccel_mode": get_video_hwaccel(),
        "hwaccel": get_video_hwaccel(),
        "video_hwaccel_device": get_video_hwaccel_device(),
        "hwaccel_device": get_video_hwaccel_device(),
        "device": get_video_hwaccel_device(),
        "ai_runtime": get_ai_runtime(),
        "runtime": get_ai_runtime(),
        "ai_device": get_ai_runtime(),
        "video_encoder": get_video_encoder(),
        "video_encoder_mode": get_video_encoder(),
        "hardware_encoder": get_video_encoder(),
        "encoder": get_video_encoder(),
        "recording_encoder": get_video_encoder(),
        "hw_encoder": get_video_encoder(),
        "video_encoding": get_video_encoder(),
        "encoding": get_video_encoder(),
        "video_codec": get_video_encoder(),
        "recording_codec": get_video_encoder(),
        "video_encoder_device": get_video_encoder_device(),
        "hardware_encoder_device": get_video_encoder_device(),
        "encoder_device": get_video_encoder_device(),
        "recording_encoder_device": get_video_encoder_device(),
        "hw_encoder_device": get_video_encoder_device(),
        "video_encoding_device": get_video_encoder_device(),
        "encoding_device": get_video_encoder_device(),
        "video_codec_device": get_video_encoder_device(),
        "recording_codec_device": get_video_encoder_device(),
    }


def get_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return get_hwaccel_config(*args, **kwargs)


def get_settings(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return get_hwaccel_config(*args, **kwargs)


def hwaccel_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return get_hwaccel_config(*args, **kwargs)


def config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return get_hwaccel_config(*args, **kwargs)


# ---------------------------------------------------------------------------
# Availability / detection style helpers
# ---------------------------------------------------------------------------
def is_hwaccel_enabled(*args: Any, **kwargs: Any) -> bool:
    val = get_video_hwaccel(*args, **kwargs)
    return _clean(val).lower() not in ("", "none", "no", "off", "false", "disabled", "cpu")


def is_enabled(*args: Any, **kwargs: Any) -> bool:
    return is_hwaccel_enabled(*args, **kwargs)


def detect_hwaccel(*args: Any, **kwargs: Any) -> str:
    return get_video_hwaccel(*args, **kwargs)


def detect_video_hwaccel(*args: Any, **kwargs: Any) -> str:
    return get_video_hwaccel(*args, **kwargs)


def available_hwaccels(*args: Any, **kwargs: Any) -> List[str]:
    return ["none", "auto", "vaapi", "cuda", "qsv", "videotoolbox", "rkmpp", "jetson", "rpi", "apple-silicon"]


def list_hwaccels(*args: Any, **kwargs: Any) -> List[str]:
    return available_hwaccels(*args, **kwargs)


def supported_hwaccels(*args: Any, **kwargs: Any) -> List[str]:
    return available_hwaccels(*args, **kwargs)


def available_ai_runtimes(*args: Any, **kwargs: Any) -> List[str]:
    return ["cpu", "cuda", "tensorrt", "openvino", "coreml", "directml", "rocm"]


def list_ai_runtimes(*args: Any, **kwargs: Any) -> List[str]:
    return available_ai_runtimes(*args, **kwargs)


# ---------------------------------------------------------------------------
# Service classes
# ---------------------------------------------------------------------------
class HwaccelService:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.video_hwaccel = get_video_hwaccel()
        self.video_hwaccel_device = get_video_hwaccel_device()
        self.ai_runtime = get_ai_runtime()
        for k, v in kwargs.items():
            setattr(self, k, v)

    def get_video_hwaccel(self, *a, **k):
        return get_video_hwaccel(*a, **k)

    def get_video_hwaccel_device(self, *a, **k):
        return get_video_hwaccel_device(*a, **k)

    def get_ai_runtime(self, *a, **k):
        return get_ai_runtime(*a, **k)

    def get_video_encoder(self, *a, **k):
        return get_video_encoder(*a, **k)

    def get_video_encoder_device(self, *a, **k):
        return get_video_encoder_device(*a, **k)

    def get_encoder(self, *a, **k):
        return get_video_encoder(*a, **k)

    def get_hardware_encoder(self, *a, **k):
        return get_video_encoder(*a, **k)

    def get_ffmpeg_encoder_args(self, *a, **k):
        return build_ffmpeg_encoder_args(*a, **k)

    def build_ffmpeg_encoder_args(self, *a, **k):
        return build_ffmpeg_encoder_args(*a, **k)

    def get_encoder_args(self, *a, **k):
        return build_ffmpeg_encoder_args(*a, **k)

    def get_available_encoders(self, *a, **k):
        return available_video_encoders(*a, **k)

    def available_encoders(self, *a, **k):
        return available_video_encoders(*a, **k)

    def get_ffmpeg_hwaccel_args(self, *a, **k):
        return build_ffmpeg_hwaccel_args(*a, **k)

    def build_ffmpeg_hwaccel_args(self, *a, **k):
        return build_ffmpeg_hwaccel_args(*a, **k)

    def get_hwaccel_args(self, *a, **k):
        return build_ffmpeg_hwaccel_args(*a, **k)

    def get_config(self, *a, **k):
        return get_hwaccel_config(*a, **k)

    def to_dict(self, *a, **k):
        return get_hwaccel_config(*a, **k)


class HwAccelService(HwaccelService):
    pass


class HardwareAccelerationService(HwaccelService):
    pass


class VideoHwAccelService(HwaccelService):
    pass


class AIRuntimeService(HwaccelService):
    pass


# ---------------------------------------------------------------------------
# Module-level singletons (many names for caller convenience)
# ---------------------------------------------------------------------------
hwaccel_service = HwaccelService()
hwaccel = hwaccel_service
hw_accel_service = hwaccel_service
hw_accel = hwaccel_service
hwaccel_config_service = hwaccel_service
service = hwaccel_service
hwaccelservice = hwaccel_service
hwAccelService = hwaccel_service
HwAccel = HwaccelService
Hwaccel = HwaccelService
HWAccel = HwaccelService
HardwareAcceleration = HwaccelService

# Value aliases at import time.
video_hwaccel = get_video_hwaccel()
video_hwaccel_device = get_video_hwaccel_device()
ai_runtime = get_ai_runtime()
VIDEO_HWACCEL = video_hwaccel
VIDEO_HWACCEL_DEVICE = video_hwaccel_device
AI_RUNTIME = ai_runtime

video_encoder = get_video_encoder()
video_encoder_device = get_video_encoder_device()
VIDEO_ENCODER = video_encoder
VIDEO_ENCODER_DEVICE = video_encoder_device
HARDWARE_ENCODER = video_encoder
HARDWARE_ENCODER_DEVICE = video_encoder_device
ENCODER = video_encoder
ENCODER_DEVICE = video_encoder_device


def __getattr__(name: str) -> Any:
    low = name.lower()
    # Config field access.
    if name in ("VIDEO_HWACCEL", "video_hwaccel", "VIDEO_HW_ACCEL"):
        return get_video_hwaccel()
    if name in ("VIDEO_HWACCEL_DEVICE", "video_hwaccel_device", "VIDEO_HW_ACCEL_DEVICE"):
        return get_video_hwaccel_device()
    if name in ("AI_RUNTIME", "ai_runtime", "AI_RUNTIME_DEVICE"):
        return get_ai_runtime()
    # Function-ish access: try service method.
    svc = hwaccel_service
    if hasattr(svc, name):
        return getattr(svc, name)
    # Generic getter factory.
    if "encoder" in low or "codec" in low or "encoding" in low:
        if "device" in low:
            return get_video_encoder_device
        if "arg" in low or "ffmpeg" in low or "cmd" in low or "command" in low:
            return build_ffmpeg_encoder_args
        if "available" in low or "list" in low or "supported" in low:
            return available_video_encoders
        if "config" in low or "setting" in low:
            return get_hwaccel_config
        return get_video_encoder
    if "device" in low:
        return get_video_hwaccel_device
    if "runtime" in low or "ai" in low:
        return get_ai_runtime
    if "arg" in low or "ffmpeg" in low or "cmd" in low or "command" in low:
        return build_ffmpeg_hwaccel_args
    if "config" in low or "setting" in low:
        return get_hwaccel_config
    if "available" in low or "list" in low or "supported" in low:
        return available_hwaccels
    if "detect" in low:
        return detect_hwaccel
    # Default to video hwaccel value/getter.
    return get_video_hwaccel
