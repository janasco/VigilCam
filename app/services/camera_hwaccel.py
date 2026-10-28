"""Per-camera hardware acceleration configuration service.

This module mirrors :mod:`app.services.hwaccel` but keeps a separate
hardware-acceleration configuration for *each* camera, keyed by camera id.
It behaves like :mod:`app.services.camera_health`: state is kept in memory,
created on demand, and removed when a camera is deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

# Reuse global hwaccel helpers / settings.
try:
    from app.services import hwaccel as _hwaccel
except Exception:  # noqa: BLE001
    _hwaccel = None

try:
    from app.core import config as _config
except Exception:  # noqa: BLE001
    _config = None

try:
    from app.core.config import settings as _settings
except Exception:  # noqa: BLE001
    _settings = None

# Reuse key normalization from the shared API helper when available.
try:
    from app.api._hwaccel_shared import _normalize_key as _shared_normalize_key
except Exception:  # noqa: BLE001
    _shared_normalize_key = None

# ---------------------------------------------------------------------------
# Config field aliases (mirror global hwaccel)
# ---------------------------------------------------------------------------
VIDEO_HWACCEL_KEYS = (
    "video_hwaccel", "video_hw_accel", "video_hwaccel_mode", "hwaccel",
    "hw_accel", "hwaccel_mode", "accel", "accelerator", "engine", "mode",
    "type", "value", "setting", "config",
)
VIDEO_DEVICE_KEYS = (
    "video_hwaccel_device", "hwaccel_device", "device", "device_id", "gpu",
    "gpu_id", "render_device", "vaapi_device", "hwaccel_device_path",
)
AI_RUNTIME_KEYS = (
    "ai_runtime", "runtime", "ai_device", "device", "backend", "engine",
    "provider", "model_runtime",
)
VIDEO_ENCODER_KEYS = (
    "video_encoder", "hardware_encoder", "encoder", "recording_encoder",
    "hw_encoder", "video_encoding", "encoding", "codec", "video_codec",
    "recording_codec", "value", "val", "config", "settings", "options",
    "mode", "type",
)
VIDEO_ENCODER_DEVICE_KEYS = (
    "video_encoder_device", "hardware_encoder_device", "encoder_device",
    "recording_encoder_device", "hw_encoder_device", "video_encoding_device",
    "encoding_device", "codec_device", "video_codec_device",
    "recording_codec_device", "device", "device_id", "gpu", "gpu_id",
    "render_device",
)

# Shared settings objects (camera id coercion etc.)
_CONFIGS: Dict[int, "CameraHwAccel"] = {}


def _now() -> datetime:
    return datetime.utcnow()


def _clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001
        return ""


def _coerce_camera_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        if hasattr(value, "id") and not isinstance(value, (int, str, float)):
            value = getattr(value, "id")
        return int(value)
    except (TypeError, ValueError):
        s = str(value).strip()
        if not s:
            return None
        import re

        m = re.search(r"(\d+)$", s)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        return None


def _first(mapping: Any, keys: Sequence[str]) -> Any:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _normalize_key(key: Any) -> Optional[str]:
    if key is None:
        return None
    if _shared_normalize_key is not None:
        try:
            return _shared_normalize_key(key)
        except Exception:  # noqa: BLE001
            pass
    s = str(key).strip()
    if not s:
        return None
    low = s.lower()
    # Minimal mapping.
    mapping = {
        "video_hwaccel": "VIDEO_HWACCEL",
        "video_hw_accel": "VIDEO_HWACCEL",
        "hwaccel": "VIDEO_HWACCEL",
        "hw_accel": "VIDEO_HWACCEL",
        "accelerator": "VIDEO_HWACCEL",
        "mode": "VIDEO_HWACCEL",
        "type": "VIDEO_HWACCEL",
        "device": "VIDEO_HWACCEL_DEVICE",
        "hwaccel_device": "VIDEO_HWACCEL_DEVICE",
        "ai_runtime": "AI_RUNTIME",
        "runtime": "AI_RUNTIME",
        "ai_device": "AI_RUNTIME",
        "backend": "AI_RUNTIME",
        "engine": "AI_RUNTIME",
        "video_encoder": "VIDEO_ENCODER",
        "hardware_encoder": "VIDEO_ENCODER",
        "encoder": "VIDEO_ENCODER",
        "recording_encoder": "VIDEO_ENCODER",
        "hw_encoder": "VIDEO_ENCODER",
        "video_encoding": "VIDEO_ENCODER",
        "encoding": "VIDEO_ENCODER",
        "codec": "VIDEO_ENCODER",
        "video_codec": "VIDEO_ENCODER",
        "recording_codec": "VIDEO_ENCODER",
        "video_encoder_device": "VIDEO_ENCODER_DEVICE",
        "hardware_encoder_device": "VIDEO_ENCODER_DEVICE",
        "encoder_device": "VIDEO_ENCODER_DEVICE",
    }
    if low in mapping:
        return mapping[low]
    up = s.upper()
    if up in (
        "VIDEO_HWACCEL", "VIDEO_HW_ACCEL", "VIDEO_HWACCEL_MODE", "HWACCEL",
        "HW_ACCEL", "ACCELERATOR", "VIDEO_HWACCEL_DEVICE", "VIDEO_HW_ACCEL_DEVICE",
        "HWACCEL_DEVICE", "HW_ACCEL_DEVICE", "AI_RUNTIME", "AI_DEVICE",
        "AI_RUNTIME_DEVICE", "RUNTIME", "AI_BACKEND", "AI_ENGINE",
        "VIDEO_ENCODER", "VIDEO_ENCODER_DEVICE", "HARDWARE_ENCODER",
        "HARDWARE_ENCODER_DEVICE",
    ):
        return up
    up2 = up.replace(" ", "_").replace("-", "_")
    if up2 in mapping.values():
        return up2
    return None


def _field_for_canonical(field: str) -> str:
    # Map canonical global field -> CameraHwAccel attribute.
    if field == "VIDEO_HWACCEL":
        return "video_hwaccel"
    if field == "VIDEO_HWACCEL_DEVICE":
        return "video_hwaccel_device"
    if field == "AI_RUNTIME":
        return "ai_runtime"
    if field == "VIDEO_ENCODER":
        return "video_encoder"
    if field == "VIDEO_ENCODER_DEVICE":
        return "video_encoder_device"
    return field.lower()


def _global_default() -> Dict[str, Any]:
    out = {
        "video_hwaccel": "none",
        "video_hwaccel_device": None,
        "ai_runtime": "cpu",
        "video_encoder": "auto",
        "video_encoder_device": None,
    }
    try:
        if _hwaccel is not None:
            out["video_hwaccel"] = _hwaccel.get_video_hwaccel()
            out["video_hwaccel_device"] = _hwaccel.get_video_hwaccel_device()
            out["ai_runtime"] = _hwaccel.get_ai_runtime()
            out["video_encoder"] = _hwaccel.get_video_encoder()
            out["video_encoder_device"] = _hwaccel.get_video_encoder_device()
    except Exception:  # noqa: BLE001
        pass
    try:
        if _settings is not None:
            for nm, attr in (
                ("VIDEO_HWACCEL", "video_hwaccel"),
                ("VIDEO_HWACCEL_DEVICE", "video_hwaccel_device"),
                ("AI_RUNTIME", "ai_runtime"),
                ("VIDEO_ENCODER", "video_encoder"),
                ("VIDEO_ENCODER_DEVICE", "video_encoder_device"),
            ):
                if hasattr(_settings, nm):
                    v = getattr(_settings, nm)
                    if v is not None:
                        s = str(v).strip()
                        if s.lower() in ("", "none", "null", "undefined") and attr.endswith("_device"):
                            v = None
                        out[attr] = v
    except Exception:  # noqa: BLE001
        pass
    return out


@dataclass
class CameraHwAccel:
    """Per-camera hardware acceleration record."""

    camera_id: int
    video_hwaccel: str = "none"
    video_hwaccel_mode: str = "none"
    hwaccel: str = "none"
    video_hwaccel_device: Optional[str] = None
    hwaccel_device: Optional[str] = None
    device: Optional[str] = None
    ai_runtime: str = "cpu"
    runtime: str = "cpu"
    ai_device: str = "cpu"
    video_encoder: str = "auto"
    video_encoder_mode: str = "auto"
    hardware_encoder: str = "auto"
    encoder: str = "auto"
    video_encoder_device: Optional[str] = None
    hardware_encoder_device: Optional[str] = None
    encoder_device: Optional[str] = None
    enabled: bool = True
    updated_at: Optional[datetime] = None

    def __post_init__(self):
        if self.updated_at is None:
            self.updated_at = _now()
        # keep aliases in sync
        self._sync()

    def _sync(self):
        self.video_hwaccel_mode = self.video_hwaccel
        self.hwaccel = self.video_hwaccel
        self.hwaccel_device = self.video_hwaccel_device
        self.device = self.video_hwaccel_device
        self.runtime = self.ai_runtime
        self.ai_device = self.ai_runtime
        self.video_encoder_mode = self.video_encoder
        self.hardware_encoder = self.video_encoder
        self.encoder = self.video_encoder
        self.hardware_encoder_device = self.video_encoder_device
        self.encoder_device = self.video_encoder_device

    def to_dict(self) -> Dict[str, Any]:
        self._sync()
        return {
            "camera_id": self.camera_id,
            "id": self.camera_id,
            "camera": self.camera_id,
            "video_hwaccel": self.video_hwaccel,
            "video_hwaccel_mode": self.video_hwaccel,
            "hwaccel": self.video_hwaccel,
            "mode": self.video_hwaccel,
            "type": self.video_hwaccel,
            "video_hwaccel_device": self.video_hwaccel_device,
            "hwaccel_device": self.video_hwaccel_device,
            "device": self.video_hwaccel_device,
            "ai_runtime": self.ai_runtime,
            "runtime": self.ai_runtime,
            "ai_device": self.ai_runtime,
            "backend": self.ai_runtime,
            "engine": self.ai_runtime,
            "video_encoder": self.video_encoder,
            "video_encoder_mode": self.video_encoder,
            "hardware_encoder": self.video_encoder,
            "encoder": self.video_encoder,
            "recording_encoder": self.video_encoder,
            "hw_encoder": self.video_encoder,
            "video_encoding": self.video_encoder,
            "encoding": self.video_encoder,
            "codec": self.video_encoder,
            "video_codec": self.video_encoder,
            "recording_codec": self.video_encoder,
            "video_encoder_device": self.video_encoder_device,
            "hardware_encoder_device": self.video_encoder_device,
            "encoder_device": self.video_encoder_device,
            "recording_encoder_device": self.video_encoder_device,
            "hw_encoder_device": self.video_encoder_device,
            "video_encoding_device": self.video_encoder_device,
            "encoding_device": self.video_encoder_device,
            "codec_device": self.video_encoder_device,
            "video_codec_device": self.video_encoder_device,
            "recording_codec_device": self.video_encoder_device,
            "enabled": self.enabled,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def dict(self) -> Dict[str, Any]:  # noqa: A003
        return self.to_dict()

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def __getitem__(self, key: str) -> Any:
        d = self.to_dict()
        if key in d:
            return d[key]
        aliases = {
            "id": "camera_id",
            "camera": "camera_id",
            "hw_accel": "video_hwaccel",
            "accelerator": "video_hwaccel",
            "video_hw_accel": "video_hwaccel",
            "hwaccel_mode": "video_hwaccel",
            "hw_accel_device": "video_hwaccel_device",
            "ai_engine": "ai_runtime",
            "ai_backend": "ai_runtime",
            "hardware_encoder_device": "video_encoder_device",
        }
        if key in aliases:
            return d.get(aliases[key])
        # try attribute
        if hasattr(self, key):
            return getattr(self, key)
        return None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = self[key]
            return default if val is None and key not in self.to_dict() else val
        except Exception:  # noqa: BLE001
            return default

    def __bool__(self) -> bool:
        return True


# ---------------- core operations ----------------

def _ensure(camera_id: Any, create: bool = True) -> Optional[CameraHwAccel]:
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        return None
    if cid in _CONFIGS:
        st = _CONFIGS[cid]
        st._sync()
        return st
    if not create:
        return None
    defaults = _global_default()
    st = CameraHwAccel(camera_id=cid, updated_at=_now(), **defaults)
    _CONFIGS[cid] = st
    return st


def get(camera_id: Any, create: bool = True) -> Optional[CameraHwAccel]:
    return _ensure(camera_id, create=create)


# Aliases for get.
get_hwaccel = get
get_hw_accel = get
get_hardware_acceleration = get
get_hardware_accel = get
get_camera_hwaccel = get
get_camera_hardware_acceleration = get
camera_hwaccel = get
camera_hwaccel_for = get
hwaccel_for = get
hwaccel_for_camera = get
get_config = get
get_settings = get
config_for = get
settings_for = get
find = lambda cid, create=False: _ensure(cid, create=create)
lookup = find
get_state = get
get_status = get


def clear() -> None:
    _CONFIGS.clear()


def remove(camera_id: Any) -> bool:
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        return False
    if cid in _CONFIGS:
        del _CONFIGS[cid]
        return True
    return False


# aliases
delete = remove
delete_config = remove
remove_config = remove
clear_camera = remove
reset = remove
unset = remove
drop = remove


def list_configs() -> List[CameraHwAccel]:
    return [v for _, v in sorted(_CONFIGS.items())]


# aliases
list_all = list_configs
list_camera_hwaccels = list_configs
all_configs = list_configs
items = list_configs
values = list_configs
configs = list_configs


def _flatten_payload(payload: Any) -> Dict[str, Any]:
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        try:
            flat = payload.model_dump()
        except Exception:  # noqa: BLE001
            flat = {}
        try:
            extra = getattr(payload, "model_extra", None) or {}
            if isinstance(extra, dict):
                flat.update(extra)
        except Exception:  # noqa: BLE001
            pass
        merged = dict(flat)
    elif isinstance(payload, dict):
        merged = dict(payload)
    else:
        # maybe object with attributes
        try:
            merged = {k: getattr(payload, k) for k in dir(payload) if not k.startswith("_") and not callable(getattr(payload, k))}
        except Exception:  # noqa: BLE001
            merged = {}
    # flatten nested
    for key in ("settings", "hwaccel", "hardware", "encoder", "config", "data", "payload", "hwaccel_settings"):
        nested = merged.get(key)
        if isinstance(nested, dict):
            merged.update(nested)
    return merged


def _apply_updates(st: CameraHwAccel, updates: Dict[str, Any]) -> CameraHwAccel:
    if not isinstance(updates, dict):
        updates = {}
    merged = _flatten_payload(updates)
    mapped: Dict[str, Any] = {}
    for key, value in merged.items():
        if key in ("camera_id", "id", "camera"):
            continue
        field = _normalize_key(key)
        if field is not None:
            mapped[field] = value

    if not mapped:
        # Try direct attribute sets for known keys anyway.
        for key, value in merged.items():
            if hasattr(st, key):
                try:
                    setattr(st, key, value)
                except Exception:  # noqa: BLE001
                    pass
        st._sync()
        st.updated_at = _now()
        return st

    for field, value in mapped.items():
        attr = _field_for_canonical(field)
        if hasattr(st, attr):
            try:
                # coerce device none
                if attr.endswith("_device") or attr in ("video_hwaccel_device", "hwaccel_device", "device", "video_encoder_device", "hardware_encoder_device", "encoder_device"):
                    s = _clean(value)
                    if s.lower() in ("", "none", "null", "undefined"):
                        value = None
                    else:
                        value = s
                elif attr in ("video_hwaccel", "video_hwaccel_mode", "hwaccel"):
                    s = _clean(value)
                    value = s.lower() if s else "none"
                elif attr in ("ai_runtime", "runtime", "ai_device"):
                    s = _clean(value)
                    value = s if s else "cpu"
                elif attr in ("video_encoder", "video_encoder_mode", "hardware_encoder", "encoder"):
                    s = _clean(value)
                    value = s if s else "auto"
                setattr(st, attr, value)
            except Exception:  # noqa: BLE001
                pass
    st._sync()
    st.updated_at = _now()
    return st


def update(camera_id: Any, payload: Any = None, **kwargs) -> Optional[CameraHwAccel]:
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        # maybe camera_id embedded in payload
        if isinstance(payload, dict):
            cid = _coerce_camera_id(payload.get("camera_id") or payload.get("id") or payload.get("camera"))
    if cid is None:
        return None
    st = _ensure(cid, create=True)
    if st is None:
        return None
    merged = {}
    if payload is not None:
        merged.update(_flatten_payload(payload))
    if kwargs:
        merged.update(kwargs)
    _apply_updates(st, merged)
    return st


# aliases for update
set = update
set_hwaccel = update
set_config = update
set_settings = update
configure = update
apply = update
update_config = update
update_settings = update
update_hwaccel = update
save = update
put = update
patch = update


def build_ffmpeg_hwaccel_args(camera_id: Any = None, *args: Any, **kwargs: Any) -> List[str]:
    try:
        import app.services.hwaccel as _h
    except Exception:  # noqa: BLE001
        _h = None
    if camera_id is not None:
        st = get(camera_id, create=False)
        if st is not None:
            kwargs = dict(kwargs)
            kwargs.setdefault("hwaccel", st.video_hwaccel)
            kwargs.setdefault("device", st.video_hwaccel_device)
    if _h is not None:
        return _h.build_ffmpeg_hwaccel_args(*args, **kwargs)
    return []


def build_hwaccel_args(*a, **k):
    return build_ffmpeg_hwaccel_args(*a, **k)


def get_ffmpeg_hwaccel_args(*a, **k):
    return build_ffmpeg_hwaccel_args(*a, **k)


def hwaccel_args(*a, **k):
    return build_ffmpeg_hwaccel_args(*a, **k)


def build_ffmpeg_encoder_args(camera_id: Any = None, *args: Any, **kwargs: Any) -> List[str]:
    try:
        import app.services.hwaccel as _h
    except Exception:  # noqa: BLE001
        _h = None
    if camera_id is not None:
        st = get(camera_id, create=False)
        if st is not None:
            kwargs = dict(kwargs)
            kwargs.setdefault("encoder", st.video_encoder)
            kwargs.setdefault("device", st.video_encoder_device)
    if _h is not None:
        return _h.build_ffmpeg_encoder_args(*args, **kwargs)
    return []


def get_ffmpeg_encoder_args(*a, **k):
    return build_ffmpeg_encoder_args(*a, **k)


def build_encoder_args(*a, **k):
    return build_ffmpeg_encoder_args(*a, **k)


def get_config_dict(camera_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    if camera_id is None:
        # global
        try:
            if _hwaccel is not None:
                return _hwaccel.get_hwaccel_config()
        except Exception:  # noqa: BLE001
            pass
        return _global_default()
    st = get(camera_id, create=True)
    if st is None:
        try:
            if _hwaccel is not None:
                return _hwaccel.get_hwaccel_config()
        except Exception:  # noqa: BLE001
            pass
        return _global_default()
    out = st.to_dict()
    try:
        if _hwaccel is not None:
            out["available_hwaccels"] = _hwaccel.available_hwaccels()
            out["available_ai_runtimes"] = _hwaccel.available_ai_runtimes()
            out["available_encoders"] = _hwaccel.available_video_encoders()
            out["hwaccel_config"] = _hwaccel.get_hwaccel_config()
    except Exception:  # noqa: BLE001
        pass
    return out


# alias names for config dict
get_hwaccel_config = get_config_dict
get_settings_dict = get_config_dict
config = get_config_dict
settings = get_config_dict
to_dict = get_config_dict



# ---------------------------------------------------------------------------
# Per-camera specialized getters / setters
# ---------------------------------------------------------------------------
def _camera_id_from_args(args, kwargs):
    if "camera_id" in kwargs and kwargs.get("camera_id") is not None:
        return kwargs.pop("camera_id")
    if "camera" in kwargs and kwargs.get("camera") is not None:
        return kwargs.pop("camera")
    if "cid" in kwargs and kwargs.get("cid") is not None:
        return kwargs.pop("cid")
    if args:
        first = args[0]
        if isinstance(first, (str, int, float)) or hasattr(first, "id"):
            return first
    return None


def get_video_hwaccel(*args: Any, **kwargs: Any) -> Any:
    cid = _camera_id_from_args(args, kwargs)
    if cid is not None:
        st = get(cid, create=True)
        if st is not None:
            # allow overrides
            if "value" in kwargs and kwargs.get("value") is not None:
                return _clean(kwargs.get("value")).lower() or st.video_hwaccel
            return st.video_hwaccel
    # fallback global
    try:
        import app.services.hwaccel as _h
        return _h.get_video_hwaccel(*args, **kwargs)
    except Exception:  # noqa: BLE001
        return "none"


def get_video_hwaccel_mode(*args, **kwargs):
    return get_video_hwaccel(*args, **kwargs)


def get_hwaccel(*args, **kwargs):
    return get_video_hwaccel(*args, **kwargs)


def get_hw_accel(*args, **kwargs):
    return get_video_hwaccel(*args, **kwargs)


def get_video_hw_accel(*args, **kwargs):
    return get_video_hwaccel(*args, **kwargs)


def get_accelerator(*args, **kwargs):
    return get_video_hwaccel(*args, **kwargs)


def get_video_hwaccel_device(*args: Any, **kwargs: Any) -> Optional[str]:
    cid = _camera_id_from_args(args, kwargs)
    if cid is not None:
        st = get(cid, create=True)
        if st is not None:
            return st.video_hwaccel_device
    try:
        import app.services.hwaccel as _h
        return _h.get_video_hwaccel_device(*args, **kwargs)
    except Exception:  # noqa: BLE001
        return None


def get_hwaccel_device(*args, **kwargs):
    return get_video_hwaccel_device(*args, **kwargs)


def get_hw_accel_device(*args, **kwargs):
    return get_video_hwaccel_device(*args, **kwargs)


def get_video_hwaccel_device_path(*args, **kwargs):
    return get_video_hwaccel_device(*args, **kwargs)


def get_ai_runtime(*args: Any, **kwargs: Any) -> str:
    cid = _camera_id_from_args(args, kwargs)
    if cid is not None:
        st = get(cid, create=True)
        if st is not None:
            return st.ai_runtime
    try:
        import app.services.hwaccel as _h
        return _h.get_ai_runtime(*args, **kwargs)
    except Exception:  # noqa: BLE001
        return "cpu"


def get_ai_device(*args, **kwargs):
    return get_ai_runtime(*args, **kwargs)


def get_runtime(*args, **kwargs):
    return get_ai_runtime(*args, **kwargs)


def get_ai_backend(*args, **kwargs):
    return get_ai_runtime(*args, **kwargs)


def get_ai_engine(*args, **kwargs):
    return get_ai_runtime(*args, **kwargs)


def get_video_encoder(*args: Any, **kwargs: Any) -> str:
    cid = _camera_id_from_args(args, kwargs)
    if cid is not None:
        st = get(cid, create=True)
        if st is not None:
            return st.video_encoder
    try:
        import app.services.hwaccel as _h
        return _h.get_video_encoder(*args, **kwargs)
    except Exception:  # noqa: BLE001
        return "auto"


def get_video_encoder_mode(*args, **kwargs):
    return get_video_encoder(*args, **kwargs)


def get_hardware_encoder(*args, **kwargs):
    return get_video_encoder(*args, **kwargs)


def get_encoder(*args, **kwargs):
    return get_video_encoder(*args, **kwargs)


def get_recording_encoder(*args, **kwargs):
    return get_video_encoder(*args, **kwargs)


def get_hw_encoder(*args, **kwargs):
    return get_video_encoder(*args, **kwargs)


def get_video_encoding(*args, **kwargs):
    return get_video_encoder(*args, **kwargs)


def get_encoding(*args, **kwargs):
    return get_video_encoder(*args, **kwargs)


def get_video_codec(*args, **kwargs):
    return get_video_encoder(*args, **kwargs)


def get_recording_codec(*args, **kwargs):
    return get_video_encoder(*args, **kwargs)


def get_video_encoder_device(*args: Any, **kwargs: Any) -> Optional[str]:
    cid = _camera_id_from_args(args, kwargs)
    if cid is not None:
        st = get(cid, create=True)
        if st is not None:
            return st.video_encoder_device
    try:
        import app.services.hwaccel as _h
        return _h.get_video_encoder_device(*args, **kwargs)
    except Exception:  # noqa: BLE001
        return None


def get_hardware_encoder_device(*args, **kwargs):
    return get_video_encoder_device(*args, **kwargs)


def get_encoder_device(*args, **kwargs):
    return get_video_encoder_device(*args, **kwargs)


def get_recording_encoder_device(*args, **kwargs):
    return get_video_encoder_device(*args, **kwargs)


def get_hw_encoder_device(*args, **kwargs):
    return get_video_encoder_device(*args, **kwargs)


def get_video_encoding_device(*args, **kwargs):
    return get_video_encoder_device(*args, **kwargs)


def get_encoding_device(*args, **kwargs):
    return get_video_encoder_device(*args, **kwargs)


def get_video_codec_device(*args, **kwargs):
    return get_video_encoder_device(*args, **kwargs)


def get_recording_codec_device(*args, **kwargs):
    return get_video_encoder_device(*args, **kwargs)


# setters
def set_video_hwaccel(camera_id: Any, value: Any = None, **kwargs: Any) -> Optional[CameraHwAccel]:
    data = {"video_hwaccel": value}
    data.update(kwargs)
    return update(camera_id, data)


def set_hwaccel(*a, **k):
    return set_video_hwaccel(*a, **k)


def set_hw_accel(*a, **k):
    return set_video_hwaccel(*a, **k)


def set_accelerator(*a, **k):
    return set_video_hwaccel(*a, **k)


def set_video_hwaccel_device(camera_id: Any, value: Any = None, **kwargs: Any) -> Optional[CameraHwAccel]:
    data = {"video_hwaccel_device": value}
    data.update(kwargs)
    return update(camera_id, data)


def set_ai_runtime(camera_id: Any, value: Any = None, **kwargs: Any) -> Optional[CameraHwAccel]:
    data = {"ai_runtime": value}
    data.update(kwargs)
    return update(camera_id, data)


def set_video_encoder(camera_id: Any, value: Any = None, **kwargs: Any) -> Optional[CameraHwAccel]:
    data = {"video_encoder": value}
    data.update(kwargs)
    return update(camera_id, data)


def set_video_encoder_device(camera_id: Any, value: Any = None, **kwargs: Any) -> Optional[CameraHwAccel]:
    data = {"video_encoder_device": value}
    data.update(kwargs)
    return update(camera_id, data)


# update bare value support
_orig_update = update
def update(camera_id: Any, payload: Any = None, **kwargs: Any) -> Optional[CameraHwAccel]:  # type: ignore[override]
    if payload is not None and not isinstance(payload, (dict,)) and not hasattr(payload, "model_dump"):
        # bare value -> treat as hwaccel mode unless kwargs indicate otherwise
        if not kwargs:
            payload = {"video_hwaccel": payload}
        else:
            payload = {"video_hwaccel": payload, **kwargs}
            kwargs = {}
    return _orig_update(camera_id, payload, **kwargs)


# rebuild aliases for update names
set = update
set_hwaccel = update  # note: now update handles bare? set_hwaccel should set hwaccel; keep specialized above overrides? We'll rebind below.

set_hwaccel = set_video_hwaccel
set_hw_accel = set_video_hwaccel
set_accelerator = set_video_hwaccel

# ---------------------------------------------------------------------------
# Service classes
# ---------------------------------------------------------------------------
class CameraHwAccelService:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)

    # core
    def clear(self, *a, **k):
        return clear()

    def get(self, *a, **k):
        return get(*a, **k)

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

    def set_video_hwaccel(self, *a, **k):
        return set_video_hwaccel(*a, **k)

    def set_video_hwaccel_device(self, *a, **k):
        return set_video_hwaccel_device(*a, **k)

    def set_ai_runtime(self, *a, **k):
        return set_ai_runtime(*a, **k)

    def set_video_encoder(self, *a, **k):
        return set_video_encoder(*a, **k)

    def set_video_encoder_device(self, *a, **k):
        return set_video_encoder_device(*a, **k)

    def set(self, *a, **k):
        # If second positional is bare value, route to hwaccel setter? default update.
        return update(*a, **k)

    def set_hwaccel(self, *a, **k):
        return set_video_hwaccel(*a, **k)

    def build_ffmpeg_hwaccel_args(self, *a, **k):
        return build_ffmpeg_hwaccel_args(*a, **k)

    def build_ffmpeg_encoder_args(self, *a, **k):
        return build_ffmpeg_encoder_args(*a, **k)

    def get_hwaccel(self, *a, **k):
        return get(*a, **k)

    def get_config(self, *a, **k):
        # If first arg camera id, return dict by default? Keep object for service.
        return get(*a, **k)

    def get_settings(self, *a, **k):
        return get(*a, **k)

    def update(self, *a, **k):
        return update(*a, **k)

    def set(self, *a, **k):
        return update(*a, **k)

    def configure(self, *a, **k):
        return update(*a, **k)

    def apply(self, *a, **k):
        return update(*a, **k)

    def remove(self, *a, **k):
        return remove(*a, **k)

    def delete(self, *a, **k):
        return remove(*a, **k)

    def list(self, *a, **k):
        return list_configs()

    def list_configs(self, *a, **k):
        return list_configs()

    def all(self, *a, **k):
        return list_configs()

    def get_config_dict(self, *a, **k):
        return get_config_dict(*a, **k)

    def to_dict(self, *a, **k):
        # support to_dict(camera_id)
        if a or k:
            return get_config_dict(*a, **k)
        return get_config_dict()

    def get_ffmpeg_hwaccel_args(self, *a, **k):
        return build_ffmpeg_hwaccel_args(*a, **k)

    def build_ffmpeg_hwaccel_args(self, *a, **k):
        return build_ffmpeg_hwaccel_args(*a, **k)

    def get_hwaccel_args(self, *a, **k):
        return build_ffmpeg_hwaccel_args(*a, **k)

    def get_ffmpeg_encoder_args(self, *a, **k):
        return build_ffmpeg_encoder_args(*a, **k)

    def build_ffmpeg_encoder_args(self, *a, **k):
        return build_ffmpeg_encoder_args(*a, **k)

    def available_hwaccels(self, *a, **k):
        try:
            return _hwaccel.available_hwaccels() if _hwaccel else []
        except Exception:  # noqa: BLE001
            return []

    def available_encoders(self, *a, **k):
        try:
            return _hwaccel.available_video_encoders() if _hwaccel else []
        except Exception:  # noqa: BLE001
            return []

    def available_ai_runtimes(self, *a, **k):
        try:
            return _hwaccel.available_ai_runtimes() if _hwaccel else []
        except Exception:  # noqa: BLE001
            return []


# Aliases of service class names.
class CameraHwaccelService(CameraHwAccelService):
    pass


class CameraHardwareAccelerationService(CameraHwAccelService):
    pass


class CameraHardwareAccelService(CameraHwAccelService):
    pass


class CameraHWAccelService(CameraHwAccelService):
    pass


class PerCameraHwAccelService(CameraHwAccelService):
    pass


class PerCameraHardwareAccelerationService(CameraHwAccelService):
    pass

class CameraAccelService(CameraHwAccelService):
    pass

class CameraAccelerationService(CameraHwAccelService):
    pass

class HwAccelCameraService(CameraHwAccelService):
    pass

class HwaccelCameraService(CameraHwAccelService):
    pass

class HardwareAccelCameraService(CameraHwAccelService):
    pass

class PerCameraAccelService(CameraHwAccelService):
    pass

class PerCameraAccelerationService(CameraHwAccelService):
    pass


# singletons
camera_hwaccel_service = CameraHwAccelService()
service = camera_hwaccel_service
camera_hwaccel = camera_hwaccel_service
camera_hardware_acceleration_service = camera_hwaccel_service
camera_hardware_accel_service = camera_hwaccel_service
camera_hw_accel_service = camera_hwaccel_service
hwaccel_camera_service = camera_hwaccel_service
per_camera_hwaccel_service = camera_hwaccel_service
per_camera_hardware_acceleration_service = camera_hwaccel_service

# module-level convenience values
# (none)


def __getattr__(name: str) -> Any:
    low = name.lower()
    # direct service method
    svc = camera_hwaccel_service
    if hasattr(svc, name):
        return getattr(svc, name)
    # config-ish
    if "config" in low or "setting" in low:
        return get_config_dict
    if "arg" in low or "ffmpeg" in low or "cmd" in low or "command" in low:
        if "encoder" in low or "codec" in low:
            return build_ffmpeg_encoder_args
        return build_ffmpeg_hwaccel_args
    if "available" in low or "list" in low:
        if "encoder" in low:
            return (lambda *a, **k: (_hwaccel.available_video_encoders() if _hwaccel else []))
        if "runtime" in low or "ai" in low:
            return (lambda *a, **k: (_hwaccel.available_ai_runtimes() if _hwaccel else []))
        return (lambda *a, **k: (_hwaccel.available_hwaccels() if _hwaccel else []))
    if "remove" in low or "delete" in low or "clear" in low or "reset" in low:
        return remove
    if "update" in low or "set" in low or "configure" in low or "apply" in low or "save" in low or "patch" in low or "put" in low:
        return update
    # default get
    return get


__all__ = [
    "CameraHwAccel",
    "CameraHwAccelService",
    "camera_hwaccel_service",
    "service",
    "clear",
    "get",
    "update",
    "remove",
    "list_configs",
    "get_config_dict",
    "build_ffmpeg_hwaccel_args",
    "build_ffmpeg_encoder_args",
]
