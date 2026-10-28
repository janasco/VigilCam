"""Per-camera inactivity (stillness / silence) alerting service for VigilCam.

Neither :mod:`app.services.motion_zoning` (motion/blob detection only) nor
:mod:`app.services.audio_detector` (mock detector that always returns
``detected=True``) fires alerts on PROLONGED STILLNESS or SILENCE. This module
fills that gap: it tracks the last motion and last sound timestamps per
camera and emits a throttled alert once a per-camera stillness (no-motion) or
silence (no-sound) timeout is exceeded.

Key concepts
------------
* **stillness**: ``now - last_motion_at >= stillness_timeout_seconds``.
* **silence**: ``now - last_sound_at >= silence_timeout_seconds``.
* **cooldown**: minimum seconds between two alerts for the same camera.
* **notifier**: optional injectable callback ``fn(camera_id, payload)``.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

DEFAULT_STILLNESS_TIMEOUT_SECONDS = 300.0
DEFAULT_SILENCE_TIMEOUT_SECONDS = 300.0
DEFAULT_COOLDOWN_SECONDS = 300.0
ENABLED_BY_DEFAULT = True

MIN_TIMEOUT_SECONDS = 0.0
MAX_TIMEOUT_SECONDS = 86400.0 * 30
MIN_COOLDOWN_SECONDS = 0.0
MAX_COOLDOWN_SECONDS = 86400.0 * 7

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

_lock = threading.RLock()

_CONFIG: Dict[str, Any] = {
    "enabled": bool(ENABLED_BY_DEFAULT),
    "auto_alert": True,
    "stillness_timeout_seconds": float(DEFAULT_STILLNESS_TIMEOUT_SECONDS),
    "silence_timeout_seconds": float(DEFAULT_SILENCE_TIMEOUT_SECONDS),
    "cooldown_seconds": float(DEFAULT_COOLDOWN_SECONDS),
}

# Per-camera timeout / cooldown overrides.
_STILLNESS_TIMEOUTS: Dict[int, float] = {}
_SILENCE_TIMEOUTS: Dict[int, float] = {}
_COOLDOWNS: Dict[int, float] = {}

_NOTIFIER: Optional[Callable[..., Any]] = None


def _now() -> datetime:
    return datetime.utcnow()


def _now_ts() -> float:
    return time.time()


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
        m = re.search(r"(\d+)$", s)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        return None


def _to_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        stamp = float(value)
        if abs(stamp) > 1e12:
            stamp = stamp / 1000.0
        try:
            return datetime.utcfromtimestamp(stamp)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            parsed = datetime.fromisoformat(s)
            if parsed.tzinfo is not None:
                parsed = parsed.replace(tzinfo=None)
            return parsed
        except ValueError:
            try:
                stamp = float(s)
                if abs(stamp) > 1e12:
                    stamp = stamp / 1000.0
                return datetime.utcfromtimestamp(stamp)
            except (ValueError, OSError, OverflowError):
                return None
    return None


CAMERA_KEYS = ("camera_id", "camera", "cam", "cam_id", "id", "device_id", "device", "source_id")
REASON_KEYS = ("reason", "cause", "trigger", "event", "event_type", "kind")
MESSAGE_KEYS = ("message", "msg", "text", "body", "detail", "description", "notes")
COOLDOWN_KEYS = ("cooldown", "cooldown_seconds", "cooldown_secs", "interval", "interval_seconds", "delay", "delay_seconds")
STILLNESS_KEYS = (
    "stillness_timeout", "stillness_timeout_seconds", "stillness_timeout_secs",
    "stillness", "no_motion_timeout", "no_motion_timeout_seconds", "motion_timeout",
    "motion_timeout_seconds", "inactivity_timeout", "inactivity_timeout_seconds",
)
SILENCE_KEYS = (
    "silence_timeout", "silence_timeout_seconds", "silence_timeout_secs",
    "silence", "no_sound_timeout", "no_sound_timeout_seconds", "sound_timeout",
    "sound_timeout_seconds", "audio_timeout", "audio_timeout_seconds",
)
TIMEOUT_KEYS = STILLNESS_KEYS + SILENCE_KEYS + ("timeout", "timeout_seconds", "threshold", "threshold_seconds")
NOW_KEYS = ("now", "at", "when", "timestamp", "ts", "time", "current_time")
FORCE_KEYS = ("force", "forced", "bypass", "bypass_throttle", "ignore_cooldown", "ignore_throttle")


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (int, float)) and "camera_id" not in data:
            data["camera_id"] = a
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if data.get("camera_id") is None and re.search(r"\d", s):
                coerced = _coerce_camera_id(s)
                if coerced is not None and len(s) < 16 and not any(
                    c.isalpha() and c.lower() not in ("x",) for c in s.replace(":", "").replace("-", "").replace("_", "")
                ):
                    data.setdefault("camera_id", coerced)
                elif data.get("reason") is None:
                    data["reason"] = s
                elif data.get("message") is None:
                    data["message"] = s
            elif data.get("reason") is None:
                data["reason"] = s
            elif data.get("message") is None:
                data["message"] = s
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request", "data", "body", "alert", "settings"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _resolve_camera_id(data: Dict[str, Any]) -> Optional[int]:
    for k in CAMERA_KEYS:
        if k in data and data.get(k) is not None:
            cid = _coerce_camera_id(data.get(k))
            if cid is not None:
                return cid
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in CAMERA_KEYS:
        if k.lower() in lower and lower[k.lower()] is not None:
            cid = _coerce_camera_id(lower[k.lower()])
            if cid is not None:
                return cid
    return None


def _resolve_reason(data: Dict[str, Any]) -> str:
    for k in REASON_KEYS:
        if k in data and data.get(k) is not None:
            s = str(data.get(k)).strip()
            if s:
                return s
    return "inactivity"


def _resolve_message(data: Dict[str, Any], camera_id: Any = None, reason: str = "inactivity") -> str:
    for k in MESSAGE_KEYS:
        if k in data and data.get(k) is not None:
            s = str(data.get(k)).strip()
            if s:
                return s
    if camera_id is not None:
        return f"Camera {camera_id} inactive ({reason})"
    return f"Camera inactive ({reason})"


def _resolve_now(data: Dict[str, Any]) -> datetime:
    for k in NOW_KEYS:
        if k in data and data.get(k) is not None:
            dt = _to_datetime(data.get(k))
            if dt is not None:
                return dt
    return _now()


def _resolve_force(data: Dict[str, Any]) -> bool:
    for k in FORCE_KEYS:
        if k in data and data.get(k) is not None:
            try:
                return bool(data.get(k))
            except Exception:  # noqa: BLE001
                continue
    return False


def _resolve_cooldown_override(data: Dict[str, Any]) -> Optional[float]:
    for k in COOLDOWN_KEYS:
        if k in data and data.get(k) is not None:
            try:
                return float(data.get(k))
            except (TypeError, ValueError):
                continue
    return None


def _resolve_stillness_override(data: Dict[str, Any]) -> Optional[float]:
    for k in STILLNESS_KEYS:
        if k in data and data.get(k) is not None:
            try:
                return float(data.get(k))
            except (TypeError, ValueError):
                continue
    return None


def _resolve_silence_override(data: Dict[str, Any]) -> Optional[float]:
    for k in SILENCE_KEYS:
        if k in data and data.get(k) is not None:
            try:
                return float(data.get(k))
            except (TypeError, ValueError):
                continue
    return None


def _clamp_timeout(value: Any, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(default)
    if v != v:  # NaN
        return float(default)
    if v < MIN_TIMEOUT_SECONDS:
        return float(MIN_TIMEOUT_SECONDS)
    if v > MAX_TIMEOUT_SECONDS:
        return float(MAX_TIMEOUT_SECONDS)
    return v


def _clamp_cooldown(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(DEFAULT_COOLDOWN_SECONDS)
    if v != v:  # NaN
        return float(DEFAULT_COOLDOWN_SECONDS)
    if v < MIN_COOLDOWN_SECONDS:
        return float(MIN_COOLDOWN_SECONDS)
    if v > MAX_COOLDOWN_SECONDS:
        return float(MAX_COOLDOWN_SECONDS)
    return v


def _effective_stillness_timeout(camera_id: Optional[int], override: Any = None) -> float:
    if override is not None:
        return _clamp_timeout(override, DEFAULT_STILLNESS_TIMEOUT_SECONDS)
    with _lock:
        if camera_id is not None and camera_id in _STILLNESS_TIMEOUTS:
            return float(_STILLNESS_TIMEOUTS[camera_id])
        return float(_CONFIG.get("stillness_timeout_seconds", DEFAULT_STILLNESS_TIMEOUT_SECONDS))


def _effective_silence_timeout(camera_id: Optional[int], override: Any = None) -> float:
    if override is not None:
        return _clamp_timeout(override, DEFAULT_SILENCE_TIMEOUT_SECONDS)
    with _lock:
        if camera_id is not None and camera_id in _SILENCE_TIMEOUTS:
            return float(_SILENCE_TIMEOUTS[camera_id])
        return float(_CONFIG.get("silence_timeout_seconds", DEFAULT_SILENCE_TIMEOUT_SECONDS))


def _effective_cooldown(camera_id: Optional[int], override: Any = None) -> float:
    if override is not None:
        return _clamp_cooldown(override)
    with _lock:
        if camera_id is not None and camera_id in _COOLDOWNS:
            return float(_COOLDOWNS[camera_id])
        return float(_CONFIG.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))


def _normalize_kind(value: Any) -> str:
    s = str(value if value is not None else "any").strip().lower()
    if s in ("still", "stillness", "no-motion", "no_motion", "nomotion", "motion", "video"):
        return "stillness"
    if s in ("silence", "silent", "no-sound", "no_sound", "nosound", "sound", "audio"):
        return "silence"
    if s in ("both", "all", "either", "any", "inactivity", "inactive"):
        return "any"
    return "any"


# ---------------------------------------------------------------------------
# Status model
# ---------------------------------------------------------------------------


@dataclass
class InactivityAlertStatus:
    """Per-camera inactivity tracking record."""

    camera_id: int = 0
    enabled: bool = True
    stillness_timeout_seconds: float = DEFAULT_STILLNESS_TIMEOUT_SECONDS
    silence_timeout_seconds: float = DEFAULT_SILENCE_TIMEOUT_SECONDS
    last_motion_at: Optional[datetime] = None
    last_sound_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    tracked_since: Optional[datetime] = None
    last_alert_at: Optional[datetime] = None
    last_check_at: Optional[datetime] = None
    last_reason: Optional[str] = None
    last_kind: Optional[str] = None
    stillness_elapsed: float = 0.0
    silence_elapsed: float = 0.0
    is_still: bool = False
    is_silent: bool = False
    inactive: bool = False
    alerted: bool = False
    alert_count: int = 0
    suppressed_count: int = 0
    updated_at: Optional[datetime] = None

    @property
    def alerts(self) -> int:
        return int(self.alert_count or 0)

    @property
    def suppressed(self) -> int:
        return int(self.suppressed_count or 0)

    @property
    def status(self) -> str:
        if self.inactive:
            return "inactive"
        return "active"

    @property
    def state(self) -> str:
        return self.status

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        aliases = {
            "id": "camera_id",
            "camera": "camera_id",
            "cam_id": "camera_id",
            "count": "alert_count",
            "alerts_sent": "alert_count",
            "total_alerts": "alert_count",
            "suppressed": "suppressed_count",
            "throttled": "suppressed_count",
            "stillness_timeout": "stillness_timeout_seconds",
            "silence_timeout": "silence_timeout_seconds",
            "timeout": "stillness_timeout_seconds",
        }
        if key in aliases:
            return getattr(self, aliases[key])
        return None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = self[key]
            if key in self.__dict__ or hasattr(self, key):
                return val
            return default if val is None else val
        except Exception:  # noqa: BLE001
            return default

    def to_dict(self) -> Dict[str, Any]:
        def _iso(v: Any) -> Any:
            return v.isoformat() if isinstance(v, datetime) else None

        return {
            "camera_id": self.camera_id,
            "id": self.camera_id,
            "enabled": bool(self.enabled),
            "stillness_timeout_seconds": float(self.stillness_timeout_seconds),
            "stillness_timeout": float(self.stillness_timeout_seconds),
            "silence_timeout_seconds": float(self.silence_timeout_seconds),
            "silence_timeout": float(self.silence_timeout_seconds),
            "last_motion_at": _iso(self.last_motion_at),
            "last_sound_at": _iso(self.last_sound_at),
            "last_activity_at": _iso(self.last_activity_at),
            "tracked_since": _iso(self.tracked_since),
            "last_alert_at": _iso(self.last_alert_at),
            "last_check_at": _iso(self.last_check_at),
            "last_reason": self.last_reason,
            "last_kind": self.last_kind,
            "stillness_elapsed": float(self.stillness_elapsed or 0.0),
            "silence_elapsed": float(self.silence_elapsed or 0.0),
            "is_still": bool(self.is_still),
            "is_silent": bool(self.is_silent),
            "inactive": bool(self.inactive),
            "status": self.status,
            "state": self.status,
            "alerted": bool(self.alerted),
            "alert_count": int(self.alert_count or 0),
            "alerts": int(self.alert_count or 0),
            "suppressed_count": int(self.suppressed_count or 0),
            "suppressed": int(self.suppressed_count or 0),
            "updated_at": _iso(self.updated_at),
            "ok": True,
            "success": True,
        }

    def dict(self) -> Dict[str, Any]:  # noqa: A003
        return self.to_dict()

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def __bool__(self) -> bool:
        return True


InactivityAlert = InactivityAlertStatus
CameraInactivityAlert = InactivityAlertStatus
InactivityAlertState = InactivityAlertStatus
CameraInactivityStatus = InactivityAlertStatus
InactivityStatus = InactivityAlertStatus

_ALERTS: Dict[int, InactivityAlertStatus] = {}


def _ensure(camera_id: Any, now: Optional[datetime] = None) -> InactivityAlertStatus:
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        cid = 0
    ts = now or _now()
    with _lock:
        if cid not in _ALERTS:
            _ALERTS[cid] = InactivityAlertStatus(
                camera_id=cid,
                stillness_timeout_seconds=float(
                    _STILLNESS_TIMEOUTS.get(cid, _CONFIG.get("stillness_timeout_seconds", DEFAULT_STILLNESS_TIMEOUT_SECONDS))
                ),
                silence_timeout_seconds=float(
                    _SILENCE_TIMEOUTS.get(cid, _CONFIG.get("silence_timeout_seconds", DEFAULT_SILENCE_TIMEOUT_SECONDS))
                ),
                last_motion_at=ts,
                last_sound_at=ts,
                last_activity_at=ts,
                tracked_since=ts,
                updated_at=ts,
            )
        st = _ALERTS[cid]
        if st.tracked_since is None:
            st.tracked_since = ts
        if st.last_motion_at is None:
            st.last_motion_at = st.tracked_since
        if st.last_sound_at is None:
            st.last_sound_at = st.tracked_since
        if st.last_activity_at is None:
            st.last_activity_at = st.tracked_since
        # Refresh effective timeouts on the record: per-camera override wins,
        # otherwise follow the current global config (so a later configure()
        # applies to already-tracked cameras too).
        if cid in _STILLNESS_TIMEOUTS:
            st.stillness_timeout_seconds = float(_STILLNESS_TIMEOUTS[cid])
        else:
            st.stillness_timeout_seconds = float(
                _CONFIG.get("stillness_timeout_seconds", DEFAULT_STILLNESS_TIMEOUT_SECONDS)
            )
        if cid in _SILENCE_TIMEOUTS:
            st.silence_timeout_seconds = float(_SILENCE_TIMEOUTS[cid])
        else:
            st.silence_timeout_seconds = float(
                _CONFIG.get("silence_timeout_seconds", DEFAULT_SILENCE_TIMEOUT_SECONDS)
            )
        return st


def _elapsed_seconds(since: Optional[datetime], now_dt: datetime) -> float:
    if since is None:
        return 0.0
    try:
        return max(0.0, (now_dt - since).total_seconds())
    except Exception:  # noqa: BLE001
        return 0.0


def _evaluate_locked(st: InactivityAlertStatus, now_dt: datetime) -> Dict[str, Any]:
    # NOTE: 0.0 is a valid timeout (alert immediately); only fall back to
    # global config when the record value is None.
    if st.stillness_timeout_seconds is None:
        still_to = float(_CONFIG.get("stillness_timeout_seconds", DEFAULT_STILLNESS_TIMEOUT_SECONDS))
    else:
        still_to = float(st.stillness_timeout_seconds)
    if st.silence_timeout_seconds is None:
        sil_to = float(_CONFIG.get("silence_timeout_seconds", DEFAULT_SILENCE_TIMEOUT_SECONDS))
    else:
        sil_to = float(st.silence_timeout_seconds)
    still_elapsed = _elapsed_seconds(st.last_motion_at, now_dt)
    sil_elapsed = _elapsed_seconds(st.last_sound_at, now_dt)
    is_still = still_elapsed >= still_to
    is_silent = sil_elapsed >= sil_to
    st.stillness_elapsed = float(still_elapsed)
    st.silence_elapsed = float(sil_elapsed)
    st.is_still = bool(is_still)
    st.is_silent = bool(is_silent)
    st.inactive = bool(is_still or is_silent)
    st.last_check_at = now_dt
    st.updated_at = now_dt
    return {
        "stillness_elapsed": float(still_elapsed),
        "silence_elapsed": float(sil_elapsed),
        "is_still": bool(is_still),
        "is_silent": bool(is_silent),
        "inactive": bool(is_still or is_silent),
    }


def _publish_event(camera_id: int, kind: str, reason: str, message: str) -> bool:
    try:
        from app.services.event_bus import event_bus as _bus

        if _bus is None:
            return False
        try:
            import asyncio

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            payload = {"camera_id": camera_id, "kind": kind, "reason": reason, "message": message}
            if loop is not None and loop.is_running():
                loop.create_task(_bus.publish("camera_inactivity", payload))
                return True
            return False
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False


def _run_notifier(camera_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        cb = _NOTIFIER
    if cb is None or not callable(cb):
        return {"notified": False, "reason": "no-notifier"}
    try:
        result = cb(camera_id, payload)
        if isinstance(result, dict):
            out = dict(result)
            out.setdefault("notified", bool(out.get("ok", True)))
            return out
        return {"notified": bool(result) if isinstance(result, bool) else True, "result": result}
    except Exception as exc:  # noqa: BLE001
        return {"notified": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def get_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    with _lock:
        cfg = dict(_CONFIG)
    cid = _resolve_camera_id(data)
    if cid is not None:
        with _lock:
            if cid in _STILLNESS_TIMEOUTS:
                cfg["stillness_timeout_seconds"] = float(_STILLNESS_TIMEOUTS[cid])
            if cid in _SILENCE_TIMEOUTS:
                cfg["silence_timeout_seconds"] = float(_SILENCE_TIMEOUTS[cid])
            if cid in _COOLDOWNS:
                cfg["cooldown_seconds"] = float(_COOLDOWNS[cid])
    cfg.setdefault("ok", True)
    cfg.setdefault("success", True)
    cfg.setdefault("stillness_timeout", cfg.get("stillness_timeout_seconds"))
    cfg.setdefault("silence_timeout", cfg.get("silence_timeout_seconds"))
    cfg.setdefault("timeout", cfg.get("stillness_timeout_seconds"))
    cfg.setdefault("cooldown", cfg.get("cooldown_seconds"))
    cfg.setdefault("interval", cfg.get("cooldown_seconds"))
    return cfg


def configure(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    with _lock:
        for key, cfg_key in (
            ("enabled", "enabled"),
            ("auto_alert", "auto_alert"),
            ("auto_notify", "auto_alert"),
            ("notify", "auto_alert"),
        ):
            if key in data and data[key] is not None:
                try:
                    _CONFIG[cfg_key] = bool(data[key])
                except Exception:  # noqa: BLE001
                    pass
        for key in ("cooldown_seconds", "cooldown", "interval", "interval_seconds", "delay_seconds"):
            if key in data and data[key] is not None:
                _CONFIG["cooldown_seconds"] = _clamp_cooldown(data[key])
                break
        for key in STILLNESS_KEYS:
            if key in data and data[key] is not None:
                _CONFIG["stillness_timeout_seconds"] = _clamp_timeout(data[key], DEFAULT_STILLNESS_TIMEOUT_SECONDS)
                break
        for key in SILENCE_KEYS:
            if key in data and data[key] is not None:
                _CONFIG["silence_timeout_seconds"] = _clamp_timeout(data[key], DEFAULT_SILENCE_TIMEOUT_SECONDS)
                break
        # Generic timeout applies to stillness when no specific key given.
        if "timeout" in data or "timeout_seconds" in data or "threshold" in data or "threshold_seconds" in data:
            has_specific = any(k in data and data[k] is not None for k in STILLNESS_KEYS + SILENCE_KEYS)
            if not has_specific:
                for key in ("timeout", "timeout_seconds", "threshold", "threshold_seconds"):
                    if key in data and data[key] is not None:
                        _CONFIG["stillness_timeout_seconds"] = _clamp_timeout(data[key], DEFAULT_STILLNESS_TIMEOUT_SECONDS)
                        break
        cid = _resolve_camera_id(data)
        if cid is not None and data.get("per_camera"):
            still_ov = _resolve_stillness_override(data)
            sil_ov = _resolve_silence_override(data)
            cd_ov = _resolve_cooldown_override(data)
            if still_ov is not None:
                _STILLNESS_TIMEOUTS[cid] = _clamp_timeout(still_ov, DEFAULT_STILLNESS_TIMEOUT_SECONDS)
            if sil_ov is not None:
                _SILENCE_TIMEOUTS[cid] = _clamp_timeout(sil_ov, DEFAULT_SILENCE_TIMEOUT_SECONDS)
            if cd_ov is not None:
                _COOLDOWNS[cid] = _clamp_cooldown(cd_ov)
    return get_config()


def set_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return configure(*args, **kwargs)


def update_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return configure(*args, **kwargs)


def enable(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        _CONFIG["enabled"] = True
    return get_config()


def disable(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        _CONFIG["enabled"] = False
    return get_config()


def is_enabled(*args: Any, **kwargs: Any) -> bool:
    with _lock:
        return bool(_CONFIG.get("enabled", True))


def set_cooldown(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    seconds_raw = _resolve_cooldown_override(data)
    if seconds_raw is None:
        vals = [a for a in args if isinstance(a, (int, float)) and not isinstance(a, bool)]
        seconds_raw = vals[0] if vals else DEFAULT_COOLDOWN_SECONDS
    seconds = _clamp_cooldown(seconds_raw)
    cid = _resolve_camera_id(data)
    with _lock:
        if cid is not None and data.get("per_camera"):
            _COOLDOWNS[cid] = float(seconds)
            return {"ok": True, "success": True, "camera_id": cid, "cooldown_seconds": float(seconds)}
        _CONFIG["cooldown_seconds"] = float(seconds)
    return {"ok": True, "success": True, "cooldown_seconds": float(seconds)}


def get_cooldown(*args: Any, **kwargs: Any) -> float:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    override = _resolve_cooldown_override(data)
    return float(_effective_cooldown(cid, override))


def set_stillness_timeout(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    raw = _resolve_stillness_override(data)
    if raw is None:
        vals = [a for a in args if isinstance(a, (int, float)) and not isinstance(a, bool)]
        raw = vals[0] if vals else DEFAULT_STILLNESS_TIMEOUT_SECONDS
    seconds = _clamp_timeout(raw, DEFAULT_STILLNESS_TIMEOUT_SECONDS)
    cid = _resolve_camera_id(data)
    with _lock:
        if cid is not None and (data.get("per_camera") or "camera_id" in data or "camera" in data or "id" in data):
            if data.get("per_camera", True):
                _STILLNESS_TIMEOUTS[cid] = float(seconds)
                if cid in _ALERTS:
                    _ALERTS[cid].stillness_timeout_seconds = float(seconds)
                return {"ok": True, "success": True, "camera_id": cid, "stillness_timeout_seconds": float(seconds)}
        _CONFIG["stillness_timeout_seconds"] = float(seconds)
    return {"ok": True, "success": True, "stillness_timeout_seconds": float(seconds)}


def set_silence_timeout(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    raw = _resolve_silence_override(data)
    if raw is None:
        vals = [a for a in args if isinstance(a, (int, float)) and not isinstance(a, bool)]
        raw = vals[0] if vals else DEFAULT_SILENCE_TIMEOUT_SECONDS
    seconds = _clamp_timeout(raw, DEFAULT_SILENCE_TIMEOUT_SECONDS)
    cid = _resolve_camera_id(data)
    with _lock:
        if cid is not None and (data.get("per_camera") or "camera_id" in data or "camera" in data or "id" in data):
            if data.get("per_camera", True):
                _SILENCE_TIMEOUTS[cid] = float(seconds)
                if cid in _ALERTS:
                    _ALERTS[cid].silence_timeout_seconds = float(seconds)
                return {"ok": True, "success": True, "camera_id": cid, "silence_timeout_seconds": float(seconds)}
        _CONFIG["silence_timeout_seconds"] = float(seconds)
    return {"ok": True, "success": True, "silence_timeout_seconds": float(seconds)}


def set_timeout(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Set the stillness (primary inactivity) timeout."""
    return set_stillness_timeout(*args, **kwargs)


def get_stillness_timeout(*args: Any, **kwargs: Any) -> float:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    override = _resolve_stillness_override(data)
    return float(_effective_stillness_timeout(cid, override))


def get_silence_timeout(*args: Any, **kwargs: Any) -> float:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    override = _resolve_silence_override(data)
    return float(_effective_silence_timeout(cid, override))


def set_notifier(callback: Any = None, **kwargs: Any) -> Dict[str, Any]:
    global _NOTIFIER
    cb = callback
    if cb is None:
        for k in ("notifier", "callback", "handler", "on_alert", "on_notify"):
            if kwargs.get(k) is not None:
                cb = kwargs.get(k)
                break
    with _lock:
        _NOTIFIER = cb if callable(cb) else None
    return {"ok": True, "success": True, "has_notifier": bool(callable(cb))}


def get_notifier() -> Optional[Callable[..., Any]]:
    with _lock:
        return _NOTIFIER


# ---------------------------------------------------------------------------
# Activity reporting
# ---------------------------------------------------------------------------


def report_motion(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        return {"ok": False, "success": False, "error": "no camera_id provided", "detail": "no camera_id provided"}
    now_dt = _resolve_now(data)
    # Explicit timestamp overrides.
    for k in ("last_motion_at", "motion_at", "at_motion", "last_motion"):
        if data.get(k) is not None:
            parsed = _to_datetime(data.get(k))
            if parsed is not None:
                now_dt = parsed
                break
    st = _ensure(cid, now_dt)
    with _lock:
        st.last_motion_at = now_dt
        st.last_activity_at = now_dt
        st.is_still = False
        st.inactive = bool(st.is_silent)
        st.updated_at = now_dt
        if st.tracked_since is None:
            st.tracked_since = now_dt
    return {"ok": True, "success": True, "camera_id": cid, "last_motion_at": now_dt.isoformat()}


def report_sound(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        return {"ok": False, "success": False, "error": "no camera_id provided", "detail": "no camera_id provided"}
    now_dt = _resolve_now(data)
    for k in ("last_sound_at", "sound_at", "at_sound", "last_sound", "last_audio_at", "audio_at"):
        if data.get(k) is not None:
            parsed = _to_datetime(data.get(k))
            if parsed is not None:
                now_dt = parsed
                break
    st = _ensure(cid, now_dt)
    with _lock:
        st.last_sound_at = now_dt
        st.last_activity_at = now_dt
        st.is_silent = False
        st.inactive = bool(st.is_still)
        st.updated_at = now_dt
        if st.tracked_since is None:
            st.tracked_since = now_dt
    return {"ok": True, "success": True, "camera_id": cid, "last_sound_at": now_dt.isoformat()}


def report_activity(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        return {"ok": False, "success": False, "error": "no camera_id provided", "detail": "no camera_id provided"}
    kind_raw = data.get("kind", data.get("activity", data.get("type", "both")))
    now_dt = _resolve_now(data)
    st = _ensure(cid, now_dt)
    kind = _normalize_kind(kind_raw) if str(kind_raw).strip().lower() not in ("both", "all") else "both"
    if kind_raw is None or str(kind_raw).strip().lower() in ("both", "all", "activity", "any", "inactivity"):
        kind = "both"
    with _lock:
        if kind in ("stillness", "motion"):
            # report_activity with kind=motion means motion observed.
            st.last_motion_at = now_dt
        elif kind in ("silence", "sound"):
            st.last_sound_at = now_dt
        else:
            st.last_motion_at = now_dt
            st.last_sound_at = now_dt
        st.last_activity_at = now_dt
        st.updated_at = now_dt
        if st.tracked_since is None:
            st.tracked_since = now_dt
    return {"ok": True, "success": True, "camera_id": cid, "kind": kind, "at": now_dt.isoformat()}


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _kind_due(st: InactivityAlertStatus, kind: str) -> bool:
    if kind == "stillness":
        return bool(st.is_still)
    if kind == "silence":
        return bool(st.is_silent)
    return bool(st.is_still or st.is_silent)


def should_alert(*args: Any, **kwargs: Any) -> bool:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        return False
    force = _resolve_force(data)
    if force:
        return True
    with _lock:
        enabled = bool(_CONFIG.get("enabled", True))
    if not enabled and not data.get("ignore_enabled", False):
        return False
    kind = _normalize_kind(data.get("kind", data.get("reason", "any")))
    # A reason naming a specific kind narrows the check.
    reason_l = str(data.get("reason", "")).strip().lower()
    if reason_l in ("stillness", "no-motion", "no_motion", "motion", "still"):
        kind = "stillness"
    elif reason_l in ("silence", "no-sound", "no_sound", "sound", "audio", "silent"):
        kind = "silence"
    now_dt = _resolve_now(data)
    still_ov = _resolve_stillness_override(data)
    sil_ov = _resolve_silence_override(data)
    st = _ensure(cid, now_dt)
    with _lock:
        if still_ov is not None:
            st.stillness_timeout_seconds = _clamp_timeout(still_ov, DEFAULT_STILLNESS_TIMEOUT_SECONDS)
        if sil_ov is not None:
            st.silence_timeout_seconds = _clamp_timeout(sil_ov, DEFAULT_SILENCE_TIMEOUT_SECONDS)
        _evaluate_locked(st, now_dt)
        due = _kind_due(st, kind)
    if not due:
        return False
    override = _resolve_cooldown_override(data)
    cooldown = _effective_cooldown(cid, override)
    with _lock:
        last = st.last_alert_at
    if last is None:
        return True
    try:
        elapsed = (now_dt - last).total_seconds()
    except Exception:  # noqa: BLE001
        return True
    return elapsed >= cooldown


def is_throttled(*args: Any, **kwargs: Any) -> bool:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        return False
    now_dt = _resolve_now(data)
    st = _ensure(cid, now_dt)
    with _lock:
        _evaluate_locked(st, now_dt)
        due = _kind_due(st, _normalize_kind(data.get("kind", "any")))
    if not due:
        # Not due -> not throttled, just not alertable.
        return True
    return not should_alert(*args, **kwargs)


def cooldown_remaining(*args: Any, **kwargs: Any) -> float:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        return 0.0
    override = _resolve_cooldown_override(data)
    cooldown = _effective_cooldown(cid, override)
    now_dt = _resolve_now(data)
    st = _ensure(cid, now_dt)
    with _lock:
        last = st.last_alert_at
    if last is None:
        return 0.0
    try:
        elapsed = (now_dt - last).total_seconds()
    except Exception:  # noqa: BLE001
        return 0.0
    return max(0.0, cooldown - elapsed)


def check_camera(*args: Any, **kwargs: Any) -> InactivityAlertStatus:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        cid = 0
    now_dt = _resolve_now(data)
    st = _ensure(cid, now_dt)
    with _lock:
        still_ov = _resolve_stillness_override(data)
        sil_ov = _resolve_silence_override(data)
        if still_ov is not None:
            st.stillness_timeout_seconds = _clamp_timeout(still_ov, DEFAULT_STILLNESS_TIMEOUT_SECONDS)
        if sil_ov is not None:
            st.silence_timeout_seconds = _clamp_timeout(sil_ov, DEFAULT_SILENCE_TIMEOUT_SECONDS)
        _evaluate_locked(st, now_dt)
    auto = data.get("auto_alert")
    if auto is None:
        auto = data.get("auto_notify")
    if auto is None:
        auto = data.get("notify")
    if auto is None:
        with _lock:
            auto = bool(_CONFIG.get("auto_alert", True))
    if auto and st.inactive:
        try:
            send_inactivity_alert(cid, now=now_dt, reason=data.get("reason", "inactivity"))
        except Exception:  # noqa: BLE001
            pass
    return st


def check_all(*args: Any, **kwargs: Any) -> List[InactivityAlertStatus]:
    data = _normalize_inputs(*args, **kwargs)
    now_dt = _resolve_now(data)
    with _lock:
        ids = list(_ALERTS.keys())
    out: List[InactivityAlertStatus] = []
    for cid in sorted(set(ids)):
        try:
            out.append(check_camera(cid, now=now_dt, **{k: v for k, v in data.items() if k not in CAMERA_KEYS}))
        except Exception:  # noqa: BLE001
            continue
    return out


def check(*args: Any, **kwargs: Any) -> Any:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is not None or any(k in data for k in CAMERA_KEYS):
        return check_camera(*args, **kwargs)
    return check_all(*args, **kwargs)


def _record_alert_locked(st: InactivityAlertStatus, now_dt: datetime, kind: str, reason: str, message: str) -> None:
    st.alert_count = int(st.alert_count or 0) + 1
    st.alerted = True
    st.last_alert_at = now_dt
    st.last_reason = reason
    st.last_kind = kind
    st.updated_at = now_dt


def send_inactivity_alert(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        return {
            "ok": False, "success": False, "alerted": False, "sent": False,
            "throttled": False, "detail": "no camera_id provided",
            "message": "no camera_id provided", "error": "no camera_id provided",
        }
    reason = _resolve_reason(data)
    kind = _normalize_kind(data.get("kind", reason if reason in ("stillness", "silence") else "any"))
    if reason.strip().lower() in ("stillness", "no-motion", "no_motion", "still"):
        kind = "stillness"
    elif reason.strip().lower() in ("silence", "no-sound", "no_sound", "silent"):
        kind = "silence"
    message = _resolve_message(data, cid, reason)
    now_dt = _resolve_now(data)
    force = _resolve_force(data)
    still_ov = _resolve_stillness_override(data)
    sil_ov = _resolve_silence_override(data)
    cd_ov = _resolve_cooldown_override(data)
    cooldown = _effective_cooldown(cid, cd_ov)

    with _lock:
        enabled = bool(_CONFIG.get("enabled", True))
    if not enabled and not force:
        return {
            "ok": True, "success": True, "alerted": False, "sent": False,
            "throttled": False, "disabled": True, "camera_id": cid,
            "kind": kind, "reason": reason, "message": message,
            "detail": "inactivity alerts disabled",
        }

    st = _ensure(cid, now_dt)
    with _lock:
        if still_ov is not None:
            st.stillness_timeout_seconds = _clamp_timeout(still_ov, DEFAULT_STILLNESS_TIMEOUT_SECONDS)
        if sil_ov is not None:
            st.silence_timeout_seconds = _clamp_timeout(sil_ov, DEFAULT_SILENCE_TIMEOUT_SECONDS)
        ev = _evaluate_locked(st, now_dt)
        due_still = bool(ev["is_still"])
        due_sil = bool(ev["is_silent"])
        if kind == "stillness":
            due = due_still
        elif kind == "silence":
            due = due_sil
        else:
            due = bool(ev["inactive"])

    if not due and not force:
        return {
            "ok": True, "success": True, "alerted": False, "sent": False,
            "throttled": False, "suppressed": False, "not_due": True,
            "camera_id": cid, "kind": kind, "reason": reason, "message": message,
            "stillness_elapsed": float(st.stillness_elapsed or 0.0),
            "silence_elapsed": float(st.silence_elapsed or 0.0),
            "is_still": bool(st.is_still), "is_silent": bool(st.is_silent),
            "detail": "inactivity timeout not yet exceeded",
        }

    allowed = force or should_alert(cid, now=now_dt, kind=kind)
    if not allowed:
        remaining = cooldown_remaining(cid, now=now_dt)
        with _lock:
            st.suppressed_count = int(st.suppressed_count or 0) + 1
            st.updated_at = now_dt
        return {
            "ok": True, "success": True, "alerted": False, "sent": False,
            "throttled": True, "suppressed": True, "camera_id": cid,
            "kind": kind, "reason": reason, "message": message,
            "cooldown_seconds": float(cooldown),
            "remaining_seconds": float(remaining),
            "retry_after": float(remaining),
            "alert_count": int(st.alert_count or 0),
            "suppressed_count": int(st.suppressed_count or 0),
            "detail": f"throttled (retry in {remaining:.1f}s)",
        }

    with _lock:
        _record_alert_locked(st, now_dt, kind, reason, message)
        alert_count = int(st.alert_count or 0)
        suppressed = int(st.suppressed_count or 0)

    payload = {
        "camera_id": cid, "kind": kind, "reason": reason, "message": message,
        "alert_count": alert_count, "at": now_dt.isoformat(),
        "stillness_elapsed": float(st.stillness_elapsed or 0.0),
        "silence_elapsed": float(st.silence_elapsed or 0.0),
    }
    event_published = _publish_event(cid, kind, reason, message)
    notify_result = _run_notifier(cid, payload)

    return {
        "ok": True, "success": True, "alerted": True, "sent": True,
        "throttled": False, "suppressed": False, "camera_id": cid,
        "kind": kind, "reason": reason, "message": message,
        "alert_count": alert_count, "suppressed_count": suppressed,
        "cooldown_seconds": float(cooldown),
        "remaining_seconds": 0.0,
        "last_alert_at": now_dt.isoformat(),
        "stillness_elapsed": float(st.stillness_elapsed or 0.0),
        "silence_elapsed": float(st.silence_elapsed or 0.0),
        "event_published": bool(event_published),
        "notify": notify_result,
        "detail": f"inactivity alert recorded for camera {cid} ({kind})",
    }


def record_alert(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = dict(_normalize_inputs(*args, **kwargs))
    data["force"] = True
    return send_inactivity_alert(**data)


def get_status(*args: Any, **kwargs: Any) -> Optional[InactivityAlertStatus]:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        return None
    create = bool(data.get("create", False))
    with _lock:
        if cid in _ALERTS:
            return _ALERTS[cid]
    if create:
        return _ensure(cid, _resolve_now(data))
    return None


def get_camera_status(*args: Any, **kwargs: Any) -> Optional[InactivityAlertStatus]:
    return get_status(*args, **kwargs)


def list_status(*args: Any, **kwargs: Any) -> List[InactivityAlertStatus]:
    with _lock:
        return [_ALERTS[k] for k in sorted(_ALERTS.keys())]


def list_alerts(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return [s.to_dict() for s in list_status(*args, **kwargs)]


def get_all(*args: Any, **kwargs: Any) -> Dict[int, InactivityAlertStatus]:
    with _lock:
        return dict(_ALERTS)


def get_inactive_cameras(*args: Any, **kwargs: Any) -> List[InactivityAlertStatus]:
    try:
        check_all()
    except Exception:  # noqa: BLE001
        pass
    with _lock:
        return [s for s in _ALERTS.values() if s.inactive]


def get_alerted_cameras(*args: Any, **kwargs: Any) -> List[InactivityAlertStatus]:
    with _lock:
        return [s for s in _ALERTS.values() if s.alerted]


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        states = list(_ALERTS.values())
        cfg = dict(_CONFIG)
    total = len(states)
    inactive = sum(1 for s in states if s.inactive)
    active = total - inactive
    alerted = sum(1 for s in states if s.alerted)
    total_alerts = sum(int(s.alert_count or 0) for s in states)
    suppressed = sum(int(s.suppressed_count or 0) for s in states)
    return {
        "ok": True, "success": True,
        "total": total, "cameras": total, "tracked": total,
        "inactive": inactive, "active": active,
        "alerted": alerted, "alerted_cameras": alerted,
        "total_alerts": total_alerts, "alert_count": total_alerts, "alerts": total_alerts,
        "suppressed_count": suppressed, "suppressed": suppressed,
        "enabled": bool(cfg.get("enabled", True)),
        "stillness_timeout_seconds": float(cfg.get("stillness_timeout_seconds", DEFAULT_STILLNESS_TIMEOUT_SECONDS)),
        "silence_timeout_seconds": float(cfg.get("silence_timeout_seconds", DEFAULT_SILENCE_TIMEOUT_SECONDS)),
        "cooldown_seconds": float(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
    }


def resolve(*args: Any, **kwargs: Any) -> Optional[InactivityAlertStatus]:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        return None
    now_dt = _resolve_now(data)
    st = _ensure(cid, now_dt)
    with _lock:
        st.last_motion_at = now_dt
        st.last_sound_at = now_dt
        st.last_activity_at = now_dt
        st.is_still = False
        st.is_silent = False
        st.inactive = False
        st.last_check_at = now_dt
        st.updated_at = now_dt
    return st


def reset(*args: Any, **kwargs: Any) -> Any:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    now_dt = _resolve_now(data) if data.get("now") is not None else _now()
    with _lock:
        if cid is None:
            for st in _ALERTS.values():
                st.alert_count = 0
                st.suppressed_count = 0
                st.alerted = False
                st.last_alert_at = None
                st.updated_at = now_dt
            return get_summary()
        st = _ensure(cid, now_dt)
        st.alert_count = 0
        st.suppressed_count = 0
        st.alerted = False
        st.last_alert_at = None
        st.updated_at = now_dt
        return st


def remove(*args: Any, **kwargs: Any) -> bool:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        return False
    with _lock:
        return _ALERTS.pop(cid, None) is not None


def clear(*args: Any, **kwargs: Any) -> None:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    with _lock:
        if cid is None and not data:
            _ALERTS.clear()
            _COOLDOWNS.clear()
            _STILLNESS_TIMEOUTS.clear()
            _SILENCE_TIMEOUTS.clear()
            return
        if cid is None:
            _ALERTS.clear()
            _COOLDOWNS.clear()
            _STILLNESS_TIMEOUTS.clear()
            _SILENCE_TIMEOUTS.clear()
            return
        _ALERTS.pop(cid, None)
        _COOLDOWNS.pop(cid, None)
        _STILLNESS_TIMEOUTS.pop(cid, None)
        _SILENCE_TIMEOUTS.pop(cid, None)


def serialize(state: Any) -> Dict[str, Any]:
    if isinstance(state, InactivityAlertStatus):
        return state.to_dict()
    if isinstance(state, dict):
        return state
    cid = _coerce_camera_id(getattr(state, "camera_id", state))
    st = _ALERTS.get(cid) if cid is not None else None
    return st.to_dict() if st is not None else {}


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------


class InactivityAlertService:
    """Object-oriented facade over the module-level inactivity-alert state."""

    def get_config(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_config(*args, **kwargs)

    def configure(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return configure(*args, **kwargs)

    def set_config(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return configure(*args, **kwargs)

    def enable(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return enable(*args, **kwargs)

    def disable(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return disable(*args, **kwargs)

    def is_enabled(self, *args: Any, **kwargs: Any) -> bool:
        return is_enabled(*args, **kwargs)

    def should_alert(self, *args: Any, **kwargs: Any) -> bool:
        return should_alert(*args, **kwargs)

    def is_throttled(self, *args: Any, **kwargs: Any) -> bool:
        return is_throttled(*args, **kwargs)

    def cooldown_remaining(self, *args: Any, **kwargs: Any) -> float:
        return cooldown_remaining(*args, **kwargs)

    def check_camera(self, *args: Any, **kwargs: Any) -> InactivityAlertStatus:
        return check_camera(*args, **kwargs)

    def check_all(self, *args: Any, **kwargs: Any) -> List[InactivityAlertStatus]:
        return check_all(*args, **kwargs)

    def check(self, *args: Any, **kwargs: Any) -> Any:
        return check(*args, **kwargs)

    def send_inactivity_alert(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return send_inactivity_alert(*args, **kwargs)

    def send_alert(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return send_inactivity_alert(*args, **kwargs)

    def notify(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return send_inactivity_alert(*args, **kwargs)

    def alert(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return send_inactivity_alert(*args, **kwargs)

    def record_alert(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return record_alert(*args, **kwargs)

    def report_motion(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return report_motion(*args, **kwargs)

    def report_sound(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return report_sound(*args, **kwargs)

    def report_activity(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return report_activity(*args, **kwargs)

    def note_motion(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return report_motion(*args, **kwargs)

    def note_sound(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return report_sound(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Optional[InactivityAlertStatus]:
        return get_status(*args, **kwargs)

    def list_status(self, *args: Any, **kwargs: Any) -> List[InactivityAlertStatus]:
        return list_status(*args, **kwargs)

    def list_alerts(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_alerts(*args, **kwargs)

    def get_summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_summary(*args, **kwargs)

    def get_inactive_cameras(self, *args: Any, **kwargs: Any) -> List[InactivityAlertStatus]:
        return get_inactive_cameras(*args, **kwargs)

    def resolve(self, *args: Any, **kwargs: Any) -> Optional[InactivityAlertStatus]:
        return resolve(*args, **kwargs)

    def reset(self, *args: Any, **kwargs: Any) -> Any:
        return reset(*args, **kwargs)

    def remove(self, *args: Any, **kwargs: Any) -> bool:
        return remove(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> None:
        return clear(*args, **kwargs)

    def set_cooldown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_cooldown(*args, **kwargs)

    def get_cooldown(self, *args: Any, **kwargs: Any) -> float:
        return get_cooldown(*args, **kwargs)

    def set_stillness_timeout(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_stillness_timeout(*args, **kwargs)

    def set_silence_timeout(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_silence_timeout(*args, **kwargs)

    def set_notifier(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_notifier(*args, **kwargs)

    def serialize(self, state: Any) -> Dict[str, Any]:
        return serialize(state)


InactivityAlertsService = InactivityAlertService
CameraInactivityAlertService = InactivityAlertService
CameraInactivityAlertsService = InactivityAlertService
InactivityAlertManager = InactivityAlertService
InactivityAlertsManager = InactivityAlertService
AlertService = InactivityAlertService

inactivity_alert_service = InactivityAlertService()
inactivity_alerts = inactivity_alert_service
inactivity_service = inactivity_alert_service
service = inactivity_alert_service
manager = inactivity_alert_service
alert_service = inactivity_alert_service
default_service = inactivity_alert_service
monitor = inactivity_alert_service


def get_service() -> InactivityAlertService:
    return inactivity_alert_service


def create_service() -> InactivityAlertService:
    return InactivityAlertService()


# Module-level aliases for flexible callers.
notify_inactivity = send_inactivity_alert
alert_inactivity = send_inactivity_alert
trigger_alert = send_inactivity_alert
trigger_inactivity_alert = send_inactivity_alert
send_alert = send_inactivity_alert
send = send_inactivity_alert
notify = send_inactivity_alert
alert = send_inactivity_alert
dispatch_alert = send_inactivity_alert
fire_alert = send_inactivity_alert
emit_alert = send_inactivity_alert
push_alert = send_inactivity_alert
note_motion = report_motion
note_sound = report_sound
note_activity = report_activity
motion_seen = report_motion
sound_heard = report_sound

should_send = should_alert
can_alert = should_alert
is_alertable = should_alert
may_alert = should_alert
needs_alert = should_alert
check_inactivity = check_camera
get_inactivity_status = get_status
status_for = get_status
status = get_status
state = get_status
list_cameras = list_status
all_status = list_status
get_all_status = list_status
summary = get_summary
stats = get_summary
get_stats = get_summary
mark_recovered = resolve
mark_active = resolve
mark_resolved = resolve
acknowledge = resolve
ack = resolve
clear_alert = resolve
reset_alert = reset
clear_all = clear
reset_all = reset
delete = remove
drop = remove
unregister = remove
set_configuration = configure
get_configuration = get_config
config = get_config

__all__ = [
    "InactivityAlertStatus",
    "InactivityAlert",
    "InactivityAlertService",
    "InactivityAlertsService",
    "inactivity_alert_service",
    "inactivity_alerts",
    "service",
    "get_service",
    "create_service",
    "should_alert",
    "is_throttled",
    "cooldown_remaining",
    "check_camera",
    "check_all",
    "check",
    "send_inactivity_alert",
    "record_alert",
    "report_motion",
    "report_sound",
    "report_activity",
    "get_status",
    "list_status",
    "list_alerts",
    "get_all",
    "get_inactive_cameras",
    "get_summary",
    "get_config",
    "configure",
    "enable",
    "disable",
    "is_enabled",
    "set_cooldown",
    "get_cooldown",
    "set_stillness_timeout",
    "set_silence_timeout",
    "resolve",
    "reset",
    "remove",
    "clear",
    "serialize",
    "DEFAULT_STILLNESS_TIMEOUT_SECONDS",
    "DEFAULT_SILENCE_TIMEOUT_SECONDS",
    "DEFAULT_COOLDOWN_SECONDS",
]
