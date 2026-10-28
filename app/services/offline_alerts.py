"""Camera offline alerting service for VigilCam.

Tracks per-camera offline state (via :mod:`app.services.camera_health`) and
emits throttled offline alerts. All state is in-memory and dependency-light so
the service can be called from health checks, the watchdog, or API handlers.

Key concepts
------------
* **offline**: camera has no recent frame / health reports offline.
* **cooldown**: minimum seconds between two alerts for the same camera.
* **notifier**: optional injectable callback ``fn(camera_id, payload)`` used to
  fan out to email / SMS / webhook / push. Defaults to a best-effort
  ``event_bus`` publish.
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

DEFAULT_COOLDOWN_SECONDS = 300.0
DEFAULT_OFFLINE_THRESHOLD_SECONDS = 60.0
ENABLED_BY_DEFAULT = True

MIN_COOLDOWN_SECONDS = 0.0
MAX_COOLDOWN_SECONDS = 86400.0 * 7

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

_lock = threading.RLock()

_CONFIG: Dict[str, Any] = {
    "enabled": bool(ENABLED_BY_DEFAULT),
    "auto_alert": True,
    "cooldown_seconds": float(DEFAULT_COOLDOWN_SECONDS),
    "offline_threshold_seconds": float(DEFAULT_OFFLINE_THRESHOLD_SECONDS),
}

# Per-camera cooldown overrides.
_COOLDOWNS: Dict[int, float] = {}

# Optional notifier callback.
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
THRESHOLD_KEYS = ("offline_threshold", "offline_threshold_seconds", "threshold", "threshold_seconds", "offline_timeout", "timeout", "timeout_seconds")
NOW_KEYS = ("now", "at", "when", "timestamp", "ts", "time")
FORCE_KEYS = ("force", "forced", "bypass", "bypass_throttle", "ignore_cooldown", "ignore_throttle")


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (int, float)) and "camera_id" not in data:
            # Bare numeric positional arg is most likely a camera id.
            data["camera_id"] = a
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if data.get("camera_id") is None and re.search(r"\d", s):
                # Heuristic: strings with digits and short length look like ids.
                coerced = _coerce_camera_id(s)
                if coerced is not None and data.get("reason") is not None:
                    data.setdefault("camera_id", coerced)
                elif coerced is not None and len(s) < 16 and not any(c.isalpha() and c.lower() not in ("x",) for c in s.replace(":", "").replace("-", "").replace("_", "")):
                    # Ambiguous; prefer camera_id if no reason yet, else reason.
                    if data.get("camera_id") is None:
                        data["camera_id"] = coerced
                    elif data.get("reason") is None:
                        data["reason"] = s
                    elif data.get("message") is None:
                        data["message"] = s
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
    return "offline"


def _resolve_message(data: Dict[str, Any], camera_id: Any = None, reason: str = "offline") -> str:
    for k in MESSAGE_KEYS:
        if k in data and data.get(k) is not None:
            s = str(data.get(k)).strip()
            if s:
                return s
    if camera_id is not None:
        return f"Camera {camera_id} is offline ({reason})"
    return f"Camera is offline ({reason})"


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


def _effective_cooldown(camera_id: Optional[int], override: Any = None) -> float:
    if override is not None:
        return _clamp_cooldown(override)
    with _lock:
        if camera_id is not None and camera_id in _COOLDOWNS:
            return float(_COOLDOWNS[camera_id])
        return float(_CONFIG.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))


def _effective_threshold(override: Any = None) -> float:
    if override is not None:
        try:
            return max(0.0, float(override))
        except (TypeError, ValueError):
            pass
    with _lock:
        try:
            return max(0.0, float(_CONFIG.get("offline_threshold_seconds", DEFAULT_OFFLINE_THRESHOLD_SECONDS)))
        except (TypeError, ValueError):
            return float(DEFAULT_OFFLINE_THRESHOLD_SECONDS)


# ---------------------------------------------------------------------------
# Status model
# ---------------------------------------------------------------------------


@dataclass
class OfflineAlertStatus:
    """Per-camera offline-alert record."""

    camera_id: int
    offline: bool = False
    online: bool = True
    status: str = "online"
    enabled: bool = True
    alerted: bool = False
    alert_count: int = 0
    suppressed_count: int = 0
    consecutive_offline_checks: int = 0
    offline_since: Optional[datetime] = None
    last_offline_at: Optional[datetime] = None
    last_online_at: Optional[datetime] = None
    last_alert_at: Optional[datetime] = None
    last_check_at: Optional[datetime] = None
    last_reason: Optional[str] = None
    updated_at: Optional[datetime] = None

    @property
    def alerts(self) -> int:
        return int(self.alert_count or 0)

    @property
    def suppressed(self) -> int:
        return int(self.suppressed_count or 0)

    @property
    def is_offline(self) -> bool:
        return bool(self.offline)

    @property
    def is_online(self) -> bool:
        return bool(self.online)

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
            "offline_status": "status",
            "health": "online",
        }
        if key in aliases:
            return getattr(self, aliases[key])
        return None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = self[key]
            if val is None and key not in self.__dict__ and key not in (
                "id", "camera", "cam_id", "count", "alerts_sent", "total_alerts",
                "suppressed", "throttled", "offline_status", "health",
            ):
                return default
            # Return default only when key truly missing; Nones are meaningful.
            if key in self.__dict__ or hasattr(self, key):
                return val
            return default if val is None else val
        except Exception:  # noqa: BLE001
            return default

    def to_dict(self) -> Dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "id": self.camera_id,
            "offline": bool(self.offline),
            "online": bool(self.online),
            "status": self.status,
            "state": self.status,
            "enabled": bool(self.enabled),
            "alerted": bool(self.alerted),
            "alert_count": int(self.alert_count or 0),
            "alerts": int(self.alert_count or 0),
            "suppressed_count": int(self.suppressed_count or 0),
            "suppressed": int(self.suppressed_count or 0),
            "consecutive_offline_checks": int(self.consecutive_offline_checks or 0),
            "offline_since": self.offline_since.isoformat() if self.offline_since else None,
            "last_offline_at": self.last_offline_at.isoformat() if self.last_offline_at else None,
            "last_online_at": self.last_online_at.isoformat() if self.last_online_at else None,
            "last_alert_at": self.last_alert_at.isoformat() if self.last_alert_at else None,
            "last_check_at": self.last_check_at.isoformat() if self.last_check_at else None,
            "last_reason": self.last_reason,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def dict(self) -> Dict[str, Any]:  # noqa: A003
        return self.to_dict()

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def __bool__(self) -> bool:
        return True


OfflineAlert = OfflineAlertStatus
CameraOfflineAlert = OfflineAlertStatus
OfflineAlertState = OfflineAlertStatus
CameraOfflineStatus = OfflineAlertStatus
OfflineStatus = OfflineAlertStatus

_ALERTS: Dict[int, OfflineAlertStatus] = {}


def _ensure(camera_id: Any) -> OfflineAlertStatus:
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        cid = 0
    with _lock:
        if cid not in _ALERTS:
            _ALERTS[cid] = OfflineAlertStatus(camera_id=cid, updated_at=_now())
        return _ALERTS[cid]


def _health_snapshot(camera_id: int) -> Dict[str, Any]:
    try:
        from app.services import camera_health as _ch

        st = _ch.get_health(camera_id, create=False)
        if st is None:
            return {"known": False, "online": False, "last_frame": None, "age_seconds": None}
        try:
            _ch.evaluate(st)
        except Exception:  # noqa: BLE001
            pass
        online = bool(getattr(st, "online", False))
        last_frame = getattr(st, "last_frame", None)
        age: Optional[float] = None
        if last_frame is not None:
            try:
                age = (_now() - last_frame).total_seconds()
            except Exception:  # noqa: BLE001
                age = None
        return {"known": True, "online": online, "last_frame": last_frame, "age_seconds": age}
    except Exception:  # noqa: BLE001
        return {"known": False, "online": False, "last_frame": None, "age_seconds": None}


def _publish_offline_event(camera_id: int, reason: str, message: str) -> bool:
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
            payload = {"camera_id": camera_id, "reason": reason, "message": message}
            if loop is not None and loop.is_running():
                loop.create_task(_bus.publish("camera_offline", payload))
                return True
            # No running loop: try synchronous fire-and-forget via subscribers is skipped.
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
    # Per-camera override view when a camera is specified.
    cid = _resolve_camera_id(data)
    if cid is not None:
        with _lock:
            if cid in _COOLDOWNS:
                cfg["cooldown_seconds"] = float(_COOLDOWNS[cid])
    cfg.setdefault("ok", True)
    cfg.setdefault("success", True)
    # Aliases for flexible callers.
    cfg.setdefault("cooldown", cfg.get("cooldown_seconds"))
    cfg.setdefault("interval", cfg.get("cooldown_seconds"))
    cfg.setdefault("offline_threshold", cfg.get("offline_threshold_seconds"))
    cfg.setdefault("threshold", cfg.get("offline_threshold_seconds"))
    cfg.setdefault("auto_restart", cfg.get("auto_alert"))
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
        for key in ("offline_threshold_seconds", "offline_threshold", "threshold", "threshold_seconds", "offline_timeout", "timeout_seconds"):
            if key in data and data[key] is not None:
                try:
                    _CONFIG["offline_threshold_seconds"] = max(0.0, float(data[key]))
                except (TypeError, ValueError):
                    pass
                break
        # Per-camera cooldown override.
        cid = _resolve_camera_id(data)
        cd_override = _resolve_cooldown_override(data)
        if cid is not None and cd_override is not None and len(data) <= 3:
            # Heuristic: when only camera + cooldown given, treat as per-camera override.
            # Otherwise treat as global default change.
            pass
        if cid is not None and cd_override is not None and data.get("per_camera", False):
            _COOLDOWNS[cid] = _clamp_cooldown(cd_override)
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
        if cid is not None and (data.get("per_camera") or "camera_id" in data or "camera" in data or "id" in data):
            # When a camera is explicitly given, store a per-camera override
            # but also keep behaviour predictable for callers expecting a global change
            # when no per_camera flag: update global default.
            if data.get("per_camera"):
                _COOLDOWNS[cid] = float(seconds)
                return {"ok": True, "success": True, "camera_id": cid, "cooldown_seconds": float(seconds)}
        _CONFIG["cooldown_seconds"] = float(seconds)
    return {"ok": True, "success": True, "cooldown_seconds": float(seconds)}


def get_cooldown(*args: Any, **kwargs: Any) -> float:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    override = _resolve_cooldown_override(data)
    return float(_effective_cooldown(cid, override))


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
# Core logic
# ---------------------------------------------------------------------------


def should_alert(*args: Any, **kwargs: Any) -> bool:
    """Return True when an offline alert may be sent for the camera."""
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
    st = _ensure(cid)
    now_dt = _resolve_now(data)
    # Refresh offline flag from health when available.
    snap = _health_snapshot(cid)
    if snap.get("known"):
        is_offline = not bool(snap.get("online", False))
    else:
        # Unknown health: rely on internal tracked state; default to offline
        # only if we have previously seen it offline, else not alertable to
        # avoid spamming for never-seen cameras unless forced/explicit.
        if st.offline:
            is_offline = True
        elif data.get("offline") is not None:
            try:
                is_offline = bool(data.get("offline"))
            except Exception:  # noqa: BLE001
                is_offline = False
        else:
            is_offline = bool(st.offline)
            # If never tracked and caller explicitly asks via send path, allow;
            # pure should_alert checks for unknown cameras return False.
            if not st.alerted and st.alert_count == 0 and st.last_offline_at is None:
                # Allow alert when the caller passes explicit offline=True or reason.
                if str(data.get("reason", "")).strip().lower() not in ("", "offline", "check"):
                    is_offline = True
                else:
                    # Unknown camera with no history: do not alert on bare check.
                    return False
    if not is_offline:
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
    # Online cameras are never "throttled" in the alert sense.
    snap = _health_snapshot(cid)
    if snap.get("known") and snap.get("online"):
        return False
    return not should_alert(*args, **kwargs)


def cooldown_remaining(*args: Any, **kwargs: Any) -> float:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        return 0.0
    override = _resolve_cooldown_override(data)
    cooldown = _effective_cooldown(cid, override)
    now_dt = _resolve_now(data)
    st = _ensure(cid)
    with _lock:
        last = st.last_alert_at
    if last is None:
        return 0.0
    try:
        elapsed = (now_dt - last).total_seconds()
    except Exception:  # noqa: BLE001
        return 0.0
    return max(0.0, cooldown - elapsed)


def check_camera(*args: Any, **kwargs: Any) -> OfflineAlertStatus:
    """Refresh tracked offline state for one camera; optionally auto-alert."""
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        cid = 0
    st = _ensure(cid)
    now_dt = _resolve_now(data)
    snap = _health_snapshot(cid)
    with _lock:
        st.last_check_at = now_dt
        st.updated_at = now_dt
        if snap.get("known"):
            if snap.get("online"):
                st.offline = False
                st.online = True
                st.status = "online"
                st.consecutive_offline_checks = 0
                st.last_online_at = now_dt
            else:
                st.offline = True
                st.online = False
                st.status = "offline"
                st.consecutive_offline_checks = int(st.consecutive_offline_checks or 0) + 1
                st.last_offline_at = st.last_offline_at or now_dt
                if st.offline_since is None:
                    st.offline_since = now_dt
        else:
            # Unknown health: honour explicit offline/online hints.
            if data.get("offline") is not None:
                off = bool(data.get("offline"))
                st.offline = off
                st.online = not off
                st.status = "offline" if off else "online"
                if off:
                    st.consecutive_offline_checks = int(st.consecutive_offline_checks or 0) + 1
                    st.last_offline_at = st.last_offline_at or now_dt
                    if st.offline_since is None:
                        st.offline_since = now_dt
                else:
                    st.consecutive_offline_checks = 0
                    st.last_online_at = now_dt
                    st.offline_since = None
    auto = data.get("auto_alert")
    if auto is None:
        auto = data.get("auto_notify")
    if auto is None:
        auto = data.get("notify")
    if auto is None:
        with _lock:
            auto = bool(_CONFIG.get("auto_alert", True))
    if auto and st.offline:
        try:
            # Best-effort auto alert; respects cooldown unless forced.
            send_offline_alert(cid, now=now_dt, reason=data.get("reason", "offline"))
        except Exception:  # noqa: BLE001
            pass
    return st


def check_all(*args: Any, **kwargs: Any) -> List[OfflineAlertStatus]:
    data = _normalize_inputs(*args, **kwargs)
    now_dt = _resolve_now(data)
    ids: List[int] = []
    with _lock:
        ids.extend(list(_ALERTS.keys()))
    try:
        from app.services import camera_health as _ch

        for h in _ch.list_health():
            try:
                cid = _coerce_camera_id(getattr(h, "camera_id", None))
            except Exception:  # noqa: BLE001
                cid = None
            if cid is not None and cid not in ids:
                ids.append(cid)
    except Exception:  # noqa: BLE001
        pass
    out: List[OfflineAlertStatus] = []
    for cid in sorted(set(ids)):
        try:
            out.append(check_camera(cid, now=now_dt, **{k: v for k, v in data.items() if k not in ("camera_id", "camera", "id")}))
        except Exception:  # noqa: BLE001
            continue
    return out


def check(*args: Any, **kwargs: Any) -> Any:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is not None or "camera_id" in data or "camera" in data or "id" in data:
        return check_camera(*args, **kwargs)
    return check_all(*args, **kwargs)


def _record_alert_locked(st: OfflineAlertStatus, now_dt: datetime, reason: str, message: str) -> None:
    st.alert_count = int(st.alert_count or 0) + 1
    st.alerted = True
    st.last_alert_at = now_dt
    st.last_reason = reason
    st.updated_at = now_dt
    if st.offline_since is None:
        st.offline_since = st.last_offline_at or now_dt


def send_offline_alert(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Send (record) an offline alert for a camera, respecting cooldown."""
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        return {
            "ok": False, "success": False, "alerted": False, "sent": False,
            "throttled": False, "detail": "no camera_id provided",
            "message": "no camera_id provided", "error": "no camera_id provided",
        }
    reason = _resolve_reason(data)
    message = _resolve_message(data, cid, reason)
    now_dt = _resolve_now(data)
    force = _resolve_force(data)
    override = _resolve_cooldown_override(data)
    cooldown = _effective_cooldown(cid, override)

    with _lock:
        enabled = bool(_CONFIG.get("enabled", True))
    if not enabled and not force:
        return {
            "ok": True, "success": True, "alerted": False, "sent": False,
            "throttled": False, "disabled": True, "camera_id": cid,
            "reason": reason, "message": message,
            "detail": "offline alerts disabled",
        }

    st = _ensure(cid)
    # Sync offline flag from health if known.
    snap = _health_snapshot(cid)
    with _lock:
        if snap.get("known"):
            if snap.get("online"):
                st.offline = False
                st.online = True
                st.status = "online"
                st.last_online_at = now_dt
                st.updated_at = now_dt
                st.offline_since = None
            else:
                st.offline = True
                st.online = False
                st.status = "offline"
                if st.offline_since is None:
                    st.offline_since = now_dt
                st.last_offline_at = st.last_offline_at or now_dt
                st.updated_at = now_dt
        else:
            # Unknown health: mark offline for an explicit send.
            if not st.offline:
                st.offline = True
                st.online = False
                st.status = "offline"
                if st.offline_since is None:
                    st.offline_since = now_dt
                st.last_offline_at = st.last_offline_at or now_dt
                st.updated_at = now_dt

    allowed = force or should_alert(cid, now=now_dt, reason=reason)
    if not allowed:
        remaining = cooldown_remaining(cid, now=now_dt)
        with _lock:
            st.suppressed_count = int(st.suppressed_count or 0) + 1
            st.updated_at = now_dt
        return {
            "ok": True, "success": True, "alerted": False, "sent": False,
            "throttled": True, "suppressed": True, "camera_id": cid,
            "reason": reason, "message": message,
            "cooldown_seconds": float(cooldown),
            "remaining_seconds": float(remaining),
            "retry_after": float(remaining),
            "alert_count": int(st.alert_count or 0),
            "suppressed_count": int(st.suppressed_count or 0),
            "detail": f"throttled (retry in {remaining:.1f}s)",
        }

    with _lock:
        _record_alert_locked(st, now_dt, reason, message)
        alert_count = int(st.alert_count or 0)
        suppressed = int(st.suppressed_count or 0)

    payload = {
        "camera_id": cid, "reason": reason, "message": message,
        "alert_count": alert_count, "at": now_dt.isoformat(),
    }
    event_published = _publish_offline_event(cid, reason, message)
    notify_result = _run_notifier(cid, payload)

    return {
        "ok": True, "success": True, "alerted": True, "sent": True,
        "throttled": False, "suppressed": False, "camera_id": cid,
        "reason": reason, "message": message,
        "alert_count": alert_count, "suppressed_count": suppressed,
        "cooldown_seconds": float(cooldown),
        "remaining_seconds": 0.0,
        "last_alert_at": now_dt.isoformat(),
        "event_published": bool(event_published),
        "notify": notify_result,
        "detail": f"offline alert recorded for camera {cid}",
    }


def record_alert(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Force-record an alert, bypassing cooldown."""
    data = dict(_normalize_inputs(*args, **kwargs))
    data["force"] = True
    return send_offline_alert(**data)


def get_status(*args: Any, **kwargs: Any) -> Optional[OfflineAlertStatus]:
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        # No camera given: return None (use list_status for all).
        return None
    create = bool(data.get("create", False))
    with _lock:
        if cid in _ALERTS:
            return _ALERTS[cid]
    if create:
        return _ensure(cid)
    return None


def get_camera_status(*args: Any, **kwargs: Any) -> Optional[OfflineAlertStatus]:
    return get_status(*args, **kwargs)


def list_status(*args: Any, **kwargs: Any) -> List[OfflineAlertStatus]:
    with _lock:
        return [_ALERTS[k] for k in sorted(_ALERTS.keys())]


def list_alerts(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return [s.to_dict() for s in list_status(*args, **kwargs)]


def get_all(*args: Any, **kwargs: Any) -> Dict[int, OfflineAlertStatus]:
    with _lock:
        return dict(_ALERTS)


def get_offline_cameras(*args: Any, **kwargs: Any) -> List[OfflineAlertStatus]:
    # Refresh from health first for accuracy.
    try:
        check_all()
    except Exception:  # noqa: BLE001
        pass
    with _lock:
        return [s for s in _ALERTS.values() if s.offline]


def get_alerted_cameras(*args: Any, **kwargs: Any) -> List[OfflineAlertStatus]:
    with _lock:
        return [s for s in _ALERTS.values() if s.alerted]


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        states = list(_ALERTS.values())
        cfg = dict(_CONFIG)
    total = len(states)
    offline = sum(1 for s in states if s.offline)
    online = total - offline
    alerted = sum(1 for s in states if s.alerted)
    total_alerts = sum(int(s.alert_count or 0) for s in states)
    suppressed = sum(int(s.suppressed_count or 0) for s in states)
    return {
        "ok": True, "success": True,
        "total": total, "cameras": total, "tracked": total,
        "offline": offline, "online": online, "healthy": online,
        "alerted": alerted, "alerted_cameras": alerted,
        "total_alerts": total_alerts, "alert_count": total_alerts, "alerts": total_alerts,
        "suppressed_count": suppressed, "suppressed": suppressed,
        "enabled": bool(cfg.get("enabled", True)),
        "cooldown_seconds": float(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
        "offline_threshold_seconds": float(cfg.get("offline_threshold_seconds", DEFAULT_OFFLINE_THRESHOLD_SECONDS)),
    }


def resolve(*args: Any, **kwargs: Any) -> Optional[OfflineAlertStatus]:
    """Mark a camera recovered/online and clear its offline latch."""
    data = _normalize_inputs(*args, **kwargs)
    cid = _resolve_camera_id(data)
    if cid is None:
        return None
    st = _ensure(cid)
    now_dt = _resolve_now(data)
    with _lock:
        st.offline = False
        st.online = True
        st.status = "online"
        st.offline_since = None
        st.consecutive_offline_checks = 0
        st.last_online_at = now_dt
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
                st.consecutive_offline_checks = 0
                st.updated_at = now_dt
            return get_summary()
        st = _ensure(cid)
        st.alert_count = 0
        st.suppressed_count = 0
        st.alerted = False
        st.last_alert_at = None
        st.consecutive_offline_checks = 0
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
            return
        if cid is None:
            _ALERTS.clear()
            _COOLDOWNS.clear()
            return
        _ALERTS.pop(cid, None)
        _COOLDOWNS.pop(cid, None)


def serialize(state: Any) -> Dict[str, Any]:
    if isinstance(state, OfflineAlertStatus):
        return state.to_dict()
    if isinstance(state, dict):
        return state
    cid = _coerce_camera_id(getattr(state, "camera_id", state))
    st = _ALERTS.get(cid) if cid is not None else None
    return st.to_dict() if st is not None else {}


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------


class OfflineAlertService:
    """Object-oriented facade over the module-level offline-alert state."""

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

    def check_camera(self, *args: Any, **kwargs: Any) -> OfflineAlertStatus:
        return check_camera(*args, **kwargs)

    def check_all(self, *args: Any, **kwargs: Any) -> List[OfflineAlertStatus]:
        return check_all(*args, **kwargs)

    def check(self, *args: Any, **kwargs: Any) -> Any:
        return check(*args, **kwargs)

    def send_offline_alert(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return send_offline_alert(*args, **kwargs)

    def send_alert(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return send_offline_alert(*args, **kwargs)

    def notify(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return send_offline_alert(*args, **kwargs)

    def alert(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return send_offline_alert(*args, **kwargs)

    def notify_offline(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return send_offline_alert(*args, **kwargs)

    def record_alert(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return record_alert(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Optional[OfflineAlertStatus]:
        return get_status(*args, **kwargs)

    def list_status(self, *args: Any, **kwargs: Any) -> List[OfflineAlertStatus]:
        return list_status(*args, **kwargs)

    def list_alerts(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_alerts(*args, **kwargs)

    def get_summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_summary(*args, **kwargs)

    def get_offline_cameras(self, *args: Any, **kwargs: Any) -> List[OfflineAlertStatus]:
        return get_offline_cameras(*args, **kwargs)

    def resolve(self, *args: Any, **kwargs: Any) -> Optional[OfflineAlertStatus]:
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

    def set_notifier(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_notifier(*args, **kwargs)

    def serialize(self, state: Any) -> Dict[str, Any]:
        return serialize(state)


OfflineAlertsService = OfflineAlertService
CameraOfflineAlertService = OfflineAlertService
CameraOfflineAlertsService = OfflineAlertService
OfflineAlertManager = OfflineAlertService
OfflineAlertsManager = OfflineAlertService
AlertService = OfflineAlertService

offline_alert_service = OfflineAlertService()
offline_alerts = offline_alert_service
offline_service = offline_alert_service
service = offline_alert_service
manager = offline_alert_service
alert_service = offline_alert_service
default_service = offline_alert_service
monitor = offline_alert_service


def get_service() -> OfflineAlertService:
    return offline_alert_service


def create_service() -> OfflineAlertService:
    return OfflineAlertService()


# Module-level aliases for flexible callers.
notify_offline = send_offline_alert
alert_offline = send_offline_alert
trigger_alert = send_offline_alert
trigger_offline_alert = send_offline_alert
send_alert = send_offline_alert
send = send_offline_alert
notify = send_offline_alert
alert = send_offline_alert
dispatch_alert = send_offline_alert
fire_alert = send_offline_alert
emit_alert = send_offline_alert
push_alert = send_offline_alert
camera_offline_alert = send_offline_alert
camera_offline_notify = send_offline_alert
offline_notify = send_offline_alert
offline_alert = send_offline_alert
notify_camera_offline = send_offline_alert
alert_camera_offline = send_offline_alert

should_send = should_alert
can_alert = should_alert
is_alertable = should_alert
may_alert = should_alert
needs_alert = should_alert
check_offline = check_camera
get_offline_status = get_status
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
mark_online = resolve
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
    "OfflineAlertStatus",
    "OfflineAlert",
    "OfflineAlertService",
    "OfflineAlertsService",
    "offline_alert_service",
    "offline_alerts",
    "service",
    "get_service",
    "create_service",
    "should_alert",
    "is_throttled",
    "cooldown_remaining",
    "check_camera",
    "check_all",
    "check",
    "send_offline_alert",
    "record_alert",
    "get_status",
    "list_status",
    "list_alerts",
    "get_all",
    "get_offline_cameras",
    "get_summary",
    "get_config",
    "configure",
    "enable",
    "disable",
    "is_enabled",
    "set_cooldown",
    "get_cooldown",
    "resolve",
    "reset",
    "remove",
    "clear",
    "serialize",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_OFFLINE_THRESHOLD_SECONDS",
]
