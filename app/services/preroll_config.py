"""Per-camera pre-roll duration configuration.

The global :mod:`app.services.preroll` ring buffer keeps a single
``max_seconds`` window (from ``RETENTION_PRE_ROLL_SECONDS``) for every
camera. This module adds the missing per-camera duration layer: each camera
may override the global default with its own pre-roll window in seconds.

State is kept in memory and persisted to
``DATA_DIR/preroll_config.json`` so overrides survive restarts. The default
falls back to ``settings.RETENTION_PRE_ROLL_SECONDS`` (30s) when no explicit
default has been stored.
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - import-safe fallback
    from app.core.config import DATA_DIR as _DATA_DIR
except Exception:  # noqa: BLE001
    _DATA_DIR = Path("data")

try:
    from app.core.config import settings as _settings
except Exception:  # noqa: BLE001
    _settings = None  # type: ignore[assignment]

DEFAULT_PREROLL_SECONDS: float = 30.0
MIN_PREROLL_SECONDS: float = 0.0
MAX_PREROLL_SECONDS: float = 600.0

_LOCK = threading.RLock()
_OVERRIDES: Dict[str, float] = {}
_DEFAULT_SECONDS: Optional[float] = None
_LOADED = False


def _config_file() -> Path:
    try:
        from app.core.config import DATA_DIR as _dd

        base = Path(str(_dd))
        if not base.is_absolute():
            base = Path(__file__).resolve().parents[2] / base
        return base / "preroll_config.json"
    except Exception:  # noqa: BLE001
        base = _DATA_DIR if isinstance(_DATA_DIR, Path) else Path(str(_DATA_DIR))
        return base / "preroll_config.json"


def _default_from_settings() -> float:
    if _DEFAULT_SECONDS is not None:
        return _DEFAULT_SECONDS
    try:
        if _settings is not None:
            raw = getattr(_settings, "RETENTION_PRE_ROLL_SECONDS", None)
            if raw is not None:
                return float(raw)
    except Exception:  # noqa: BLE001
        pass
    return float(DEFAULT_PREROLL_SECONDS)


def _coerce_seconds(value: Any) -> float:
    if value is None:
        raise ValueError("Missing pre-roll duration (provide 'seconds')")
    if isinstance(value, bool):
        raise ValueError(f"Invalid pre-roll duration: {value!r}")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid pre-roll duration: {value!r}") from exc
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN / inf
        raise ValueError(f"Invalid pre-roll duration: {value!r}")
    if seconds < MIN_PREROLL_SECONDS or seconds > MAX_PREROLL_SECONDS:
        raise ValueError(
            f"Pre-roll duration must be between {MIN_PREROLL_SECONDS:g} and "
            f"{MAX_PREROLL_SECONDS:g} seconds (got {seconds!r})"
        )
    return seconds


def _normalize_camera_key(camera: Any) -> str:
    if camera is None:
        raise ValueError("Missing camera (provide 'camera_name' or 'camera_id')")
    if isinstance(camera, dict):
        for key in ("camera_name", "camera", "cameraName", "cam", "name", "camera_id", "cameraId", "id", "slug"):
            value = camera.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        raise ValueError("Missing camera (provide 'camera_name' or 'camera_id')")
    key = str(camera).strip()
    if not key:
        raise ValueError("Missing camera (provide 'camera_name' or 'camera_id')")
    return key


def _extract_seconds(payload: Dict[str, Any]) -> Any:
    for key in (
        "seconds",
        "pre_roll_seconds",
        "preroll_seconds",
        "preRollSeconds",
        "pre_roll",
        "preroll",
        "preRoll",
        "duration",
        "duration_seconds",
        "window",
        "window_seconds",
        "windowBefore",
        "window_before",
        "value",
    ):
        if payload.get(key) is not None:
            return payload.get(key)
    return None


def _load_from_disk() -> None:
    global _LOADED, _DEFAULT_SECONDS
    path = _config_file()
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
            if isinstance(raw, dict):
                default = raw.get("default_seconds")
                if default is None:
                    for alias in ("default", "global_seconds", "global", "fallback_seconds"):
                        if raw.get(alias) is not None:
                            default = raw.get(alias)
                            break
                if default is not None:
                    try:
                        _DEFAULT_SECONDS = _coerce_seconds(default)
                    except ValueError:
                        pass
                cameras = raw.get("cameras")
                if cameras is None:
                    for alias in ("overrides", "per_camera", "per_camera_seconds", "durations", "configs"):
                        if isinstance(raw.get(alias), dict):
                            cameras = raw.get(alias)
                            break
                _OVERRIDES.clear()
                if isinstance(cameras, dict):
                    for cam, seconds in cameras.items():
                        try:
                            _OVERRIDES[str(cam)] = _coerce_seconds(seconds)
                        except ValueError:
                            continue
    except Exception:  # noqa: BLE001
        pass
    _LOADED = True


def _ensure_loaded() -> None:
    if not _LOADED:
        with _LOCK:
            if not _LOADED:
                _load_from_disk()


def _save_to_disk() -> None:
    path = _config_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "default_seconds": _default_from_settings(),
            "cameras": dict(sorted(_OVERRIDES.items())),
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def get_default_seconds() -> float:
    """Return the global default pre-roll window in seconds."""
    _ensure_loaded()
    with _LOCK:
        return float(_default_from_settings())


def set_default_seconds(seconds: Any) -> float:
    """Set the global default pre-roll window. Returns the stored value."""
    _ensure_loaded()
    coerced = _coerce_seconds(seconds)
    with _LOCK:
        global _DEFAULT_SECONDS
        _DEFAULT_SECONDS = coerced
        _save_to_disk()
        return float(coerced)


# ---------------------------------------------------------------------------
# Per-camera CRUD
# ---------------------------------------------------------------------------


def get_seconds_for_camera(camera: Any) -> float:
    """Return the effective pre-roll window for a camera (override or default)."""
    _ensure_loaded()
    key = _normalize_camera_key(camera)
    with _LOCK:
        if key in _OVERRIDES:
            return float(_OVERRIDES[key])
        return float(_default_from_settings())


def set_seconds_for_camera(camera: Any, seconds: Any) -> Dict[str, Any]:
    """Set a per-camera pre-roll override. Returns the stored entry dict."""
    _ensure_loaded()
    key = _normalize_camera_key(camera)
    coerced = _coerce_seconds(seconds)
    with _LOCK:
        _OVERRIDES[key] = coerced
        _save_to_disk()
        return {"camera_name": key, "seconds": float(coerced), "is_default": False}


def set_camera_preroll(camera: Any, seconds: Any) -> Dict[str, Any]:
    return set_seconds_for_camera(camera, seconds)


def get_camera_config(camera: Any) -> Dict[str, Any]:
    """Return the stored config entry for a camera (with default flag)."""
    _ensure_loaded()
    key = _normalize_camera_key(camera)
    with _LOCK:
        if key in _OVERRIDES:
            return {"camera_name": key, "seconds": float(_OVERRIDES[key]), "is_default": False}
        return {"camera_name": key, "seconds": float(_default_from_settings()), "is_default": True}


def delete_camera_config(camera: Any) -> bool:
    """Remove a per-camera override. Returns True when an entry was removed."""
    _ensure_loaded()
    key = _normalize_camera_key(camera)
    with _LOCK:
        if key in _OVERRIDES:
            del _OVERRIDES[key]
            _save_to_disk()
            return True
        return False


def clear_camera_config(camera: Any) -> bool:
    return delete_camera_config(camera)


def reset_camera_config(camera: Any) -> bool:
    return delete_camera_config(camera)


def list_configs() -> List[Dict[str, Any]]:
    """Return all per-camera overrides plus the default entry first."""
    _ensure_loaded()
    with _LOCK:
        default = float(_default_from_settings())
        items = [
            {"camera_name": cam, "seconds": float(seconds), "is_default": False}
            for cam, seconds in sorted(_OVERRIDES.items())
        ]
    return [{"camera_name": None, "seconds": default, "is_default": True, "default_seconds": default}] + items


def clear_all() -> int:
    """Remove all per-camera overrides. Returns number removed (test helper)."""
    _ensure_loaded()
    with _LOCK:
        count = len(_OVERRIDES)
        _OVERRIDES.clear()
        _save_to_disk()
        return count


def reload() -> None:
    """Force reload from disk (test helper)."""
    global _LOADED
    with _LOCK:
        _LOADED = False
    _ensure_loaded()


# ---------------------------------------------------------------------------
# Effective-window helpers + ring-buffer integration
# ---------------------------------------------------------------------------


def effective_seconds_for(camera: Any) -> float:
    return get_seconds_for_camera(camera)


def duration_for_camera(camera: Any) -> float:
    return get_seconds_for_camera(camera)


def window_for_camera(camera: Any) -> float:
    return get_seconds_for_camera(camera)


def get_window_for_camera(camera: Any) -> float:
    return get_seconds_for_camera(camera)


def get_preroll_seconds(camera: Any = None) -> float:
    if camera is None:
        return get_default_seconds()
    return get_seconds_for_camera(camera)


def recent_segments_for_camera(camera: Any, buffer: Any = None) -> List[Any]:
    """Return recent segments for a camera using its configured window.

    Falls back to the shared :mod:`app.services.preroll` buffer when no
    buffer is supplied.
    """
    key = _normalize_camera_key(camera)
    window = get_seconds_for_camera(key)
    buf = buffer
    if buf is None:
        try:
            from app.services import preroll as _preroll

            buf = _preroll.preroll_buffer
        except Exception:  # noqa: BLE001
            return []
    try:
        return list(buf.recent(seconds=window, camera_name=key))
    except Exception:  # noqa: BLE001
        try:
            return list(buf.get_segments(key))
        except Exception:  # noqa: BLE001
            return []


# ---------------------------------------------------------------------------
# Aliases for tolerant imports
# ---------------------------------------------------------------------------

get_default = get_default_seconds
set_default = set_default_seconds
get_seconds = get_seconds_for_camera
set_seconds = set_seconds_for_camera
get_camera_seconds = get_seconds_for_camera
set_camera_seconds = set_seconds_for_camera
get_duration = get_seconds_for_camera
set_duration = set_seconds_for_camera
get_preroll_duration = get_seconds_for_camera
set_preroll_duration = set_seconds_for_camera
get_config = get_camera_config
get_camera_preroll = get_seconds_for_camera
set_camera_preroll = set_seconds_for_camera
delete_camera_preroll = delete_camera_config
remove_camera_config = delete_camera_config
clear_camera = delete_camera_config
reset_camera = delete_camera_config
remove_camera = delete_camera_config
list_all = list_configs
list_all_configs = list_configs
get_all_configs = list_configs
get_all = list_configs
effective_seconds = effective_seconds_for
duration = duration_for_camera
window = window_for_camera

__all__ = [
    "DEFAULT_PREROLL_SECONDS",
    "MIN_PREROLL_SECONDS",
    "MAX_PREROLL_SECONDS",
    "get_default_seconds",
    "set_default_seconds",
    "get_seconds_for_camera",
    "set_seconds_for_camera",
    "set_camera_preroll",
    "get_camera_config",
    "delete_camera_config",
    "clear_camera_config",
    "reset_camera_config",
    "list_configs",
    "clear_all",
    "reload",
    "effective_seconds_for",
    "duration_for_camera",
    "window_for_camera",
    "get_window_for_camera",
    "get_preroll_seconds",
    "recent_segments_for_camera",
]
