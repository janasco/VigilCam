"""Alert cooldown / throttle service for VigilCam.

Prevents alert spam (email / SMS / webhook / push) by enforcing a per-key
cooldown interval and an optional sliding-window rate limit.

Key concepts
------------
* **key**: throttle bucket identifier (e.g. ``"motion:cam1"``,
  ``"email:front-door"``). Any string works; camera/event/channel parts may
  be passed separately and are joined into a canonical key.
* **cooldown**: minimum seconds between two allowed alerts for the same key.
* **throttle window**: optional ``(max_alerts, window_seconds)`` sliding-window
  cap applied on top of the cooldown.

All state is in-memory, thread-safe, and intentionally dependency-free so the
service can be called from any alert sender path.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_COOLDOWN_SECONDS = 300.0
DEFAULT_WINDOW_SECONDS = 3600.0
DEFAULT_MAX_PER_WINDOW: Optional[int] = None  # None == unlimited

MIN_COOLDOWN_SECONDS = 0.0
MAX_COOLDOWN_SECONDS = 86400.0 * 7  # one week sanity cap

# ---------------------------------------------------------------------------
# In-memory state (guarded by _lock)
# ---------------------------------------------------------------------------

_LAST_SENT: Dict[str, float] = {}
_HISTORY: Dict[str, List[float]] = {}
_COOLDOWNS: Dict[str, float] = {}
_WINDOWS: Dict[str, float] = {}
_MAX_PER_WINDOW: Dict[str, Optional[int]] = {}
_ALLOWED_COUNT: Dict[str, int] = {}
_SUPPRESSED_COUNT: Dict[str, int] = {}

_DEFAULT_COOLDOWN = float(DEFAULT_COOLDOWN_SECONDS)
_DEFAULT_WINDOW = float(DEFAULT_WINDOW_SECONDS)
_DEFAULT_MAX: Optional[int] = DEFAULT_MAX_PER_WINDOW

_lock = threading.RLock()

KEY_KEYS = ("key", "alert_key", "throttle_key", "cooldown_key", "id", "name", "bucket", "topic")
COOLDOWN_KEYS = ("cooldown", "cooldown_seconds", "cooldown_secs", "interval", "interval_seconds", "delay", "delay_seconds", "throttle_seconds", "min_interval")
WINDOW_KEYS = ("window", "window_seconds", "window_secs", "period", "period_seconds")
MAX_KEYS = ("max_alerts", "max", "max_per_window", "limit", "burst", "max_burst", "rate_limit", "count_limit")
CAMERA_KEYS = ("camera_id", "camera", "cam", "camera_name", "device_id", "source")
EVENT_KEYS = ("event_type", "event", "type", "kind", "alert_type", "category")
CHANNEL_KEYS = ("channel", "via", "medium", "service", "target", "destination")


def _now_ts() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default


def _clamp_cooldown(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(_DEFAULT_COOLDOWN)
    if v != v:  # NaN
        return float(_DEFAULT_COOLDOWN)
    if v < MIN_COOLDOWN_SECONDS:
        return float(MIN_COOLDOWN_SECONDS)
    if v > MAX_COOLDOWN_SECONDS:
        return float(MAX_COOLDOWN_SECONDS)
    return v


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        lk = str(k).lower()
        if lk in lower and lower[lk] is not None:
            return lower[lk]
    return None


def _flatten_nested(data: Dict[str, Any]) -> Dict[str, Any]:
    for nested_key in ("payload", "options", "config", "params", "request", "data", "alert", "throttle", "settings"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (bytes, bytearray)):
            try:
                data.setdefault("key", bytes(a).decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if data.get("key") is None:
                data["key"] = s
            elif data.get("event_type") is None:
                data["event_type"] = s
            else:
                data.setdefault("channel", s)
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    return _flatten_nested(data)


def _canonical_key(data: Dict[str, Any]) -> str:
    explicit = _first(data, KEY_KEYS)
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    parts: List[str] = []
    cam = _first(data, CAMERA_KEYS)
    evt = _first(data, EVENT_KEYS)
    ch = _first(data, CHANNEL_KEYS)
    if cam is not None and str(cam).strip():
        parts.append(str(cam).strip())
    if evt is not None and str(evt).strip():
        parts.append(str(evt).strip())
    if ch is not None and str(ch).strip():
        parts.append(str(ch).strip())
    if parts:
        return ":".join(parts)
    return "default"


def _resolve_params(data: Dict[str, Any]) -> tuple:
    key = _canonical_key(data)
    cd_raw = _first(data, COOLDOWN_KEYS)
    win_raw = _first(data, WINDOW_KEYS)
    max_raw = _first(data, MAX_KEYS)
    now = _coerce_float(data.get("now"), _now_ts()) if data.get("now") is not None else _now_ts()
    return key, cd_raw, win_raw, max_raw, now


def _effective_cooldown(key: str, override: Any) -> float:
    if override is not None:
        return _clamp_cooldown(override)
    with _lock:
        if key in _COOLDOWNS:
            return float(_COOLDOWNS[key])
        return float(_DEFAULT_COOLDOWN)


def _effective_window(key: str, override: Any) -> float:
    if override is not None:
        try:
            v = float(override)
            return v if v > 0 else float(_DEFAULT_WINDOW)
        except (TypeError, ValueError):
            pass
    with _lock:
        if key in _WINDOWS:
            return float(_WINDOWS[key])
        return float(_DEFAULT_WINDOW)


def _effective_max(key: str, override: Any) -> Optional[int]:
    if override is not None:
        try:
            if isinstance(override, str) and override.strip().lower() in ("none", "null", "unlimited", ""):
                return None
            v = int(float(override))  # type: ignore[arg-type]
            return v if v > 0 else None
        except (TypeError, ValueError):
            pass
    with _lock:
        if key in _MAX_PER_WINDOW:
            return _MAX_PER_WINDOW[key]
        return _DEFAULT_MAX


def _prune_history(key: str, now: float, window: float) -> List[float]:
    hist = _HISTORY.get(key, [])
    if not hist:
        return []
    cutoff = now - window
    pruned = [t for t in hist if t > cutoff]
    _HISTORY[key] = pruned
    return pruned


def _build_status(key: str, now: float) -> Dict[str, Any]:
    cooldown = _effective_cooldown(key, None)
    window = _effective_window(key, None)
    max_allowed = _effective_max(key, None)
    last = _LAST_SENT.get(key)
    elapsed = (now - last) if last is not None else None
    remaining = 0.0
    if last is not None:
        remaining = max(0.0, cooldown - (now - last))
    pruned = _prune_history(key, now, window) if window > 0 else list(_HISTORY.get(key, []))
    in_window = len(pruned)
    window_remaining: Optional[float] = None
    if max_allowed is not None and in_window >= max_allowed and pruned:
        oldest = min(pruned)
        window_remaining = max(0.0, (oldest + window) - now)
    throttled = bool(remaining > 0 or window_remaining)
    allowed = not throttled
    retry_after = max(remaining, window_remaining or 0.0)
    return {
        "key": key,
        "allowed": allowed,
        "throttled": throttled,
        "cooldown_active": bool(remaining > 0),
        "window_limited": bool(window_remaining),
        "cooldown_seconds": cooldown,
        "window_seconds": window,
        "max_per_window": max_allowed,
        "elapsed_seconds": elapsed,
        "remaining_seconds": remaining,
        "cooldown_remaining": remaining,
        "retry_after": retry_after,
        "retry_after_seconds": retry_after,
        "last_sent": datetime.fromtimestamp(last, tz=timezone.utc).isoformat() if last is not None else None,
        "last_sent_ts": last,
        "in_window": in_window,
        "window_remaining_seconds": window_remaining,
        "allowed_count": int(_ALLOWED_COUNT.get(key, 0)),
        "suppressed_count": int(_SUPPRESSED_COUNT.get(key, 0)),
        "checked_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Core public API
# ---------------------------------------------------------------------------

def check(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Check whether an alert for ``key`` may be sent (does not record)."""
    data = _normalize_inputs(*args, **kwargs)
    key, cd_raw, win_raw, max_raw, now = _resolve_params(data)
    with _lock:
        cooldown = _effective_cooldown(key, cd_raw)
        window = _effective_window(key, win_raw)
        max_allowed = _effective_max(key, max_raw)
        last = _LAST_SENT.get(key)
        remaining = 0.0
        if last is not None:
            remaining = max(0.0, cooldown - (now - last))
        pruned = _prune_history(key, now, window) if window > 0 else list(_HISTORY.get(key, []))
        window_remaining: Optional[float] = None
        if max_allowed is not None and len(pruned) >= max_allowed and pruned:
            window_remaining = max(0.0, (min(pruned) + window) - now)
        throttled = bool(remaining > 0 or window_remaining)
        retry_after = max(remaining, window_remaining or 0.0)
        result = _build_status(key, now)
        # honour per-call overrides in the returned view without persisting
        result["cooldown_seconds"] = cooldown
        result["window_seconds"] = window
        result["max_per_window"] = max_allowed
        result["remaining_seconds"] = remaining
        result["cooldown_remaining"] = remaining
        result["retry_after"] = retry_after
        result["retry_after_seconds"] = retry_after
        result["allowed"] = not throttled
        result["throttled"] = throttled
        result["ok"] = True
        result["success"] = True
        return result


def record(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Record that an alert was sent for ``key`` (resets its cooldown)."""
    data = _normalize_inputs(*args, **kwargs)
    key, _cd, _w, _m, now = _resolve_params(data)
    with _lock:
        _LAST_SENT[key] = now
        _HISTORY.setdefault(key, []).append(now)
        _ALLOWED_COUNT[key] = int(_ALLOWED_COUNT.get(key, 0)) + 1
        result = _build_status(key, now)
        result["recorded"] = True
        result["ok"] = True
        result["success"] = True
        return result


def check_and_record(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Atomically check the throttle and, if allowed, record the send.

    Returns a dict with ``allowed``. When throttled, ``allowed`` is False
    and the cooldown is left untouched (plus a suppressed counter bump).
    """
    data = _normalize_inputs(*args, **kwargs)
    key, cd_raw, win_raw, max_raw, now = _resolve_params(data)
    force = bool(data.get("force", False))
    with _lock:
        cooldown = _effective_cooldown(key, cd_raw)
        window = _effective_window(key, win_raw)
        max_allowed = _effective_max(key, max_raw)
        last = _LAST_SENT.get(key)
        remaining = 0.0
        if last is not None and not force:
            remaining = max(0.0, cooldown - (now - last))
        pruned = _prune_history(key, now, window) if window > 0 else list(_HISTORY.get(key, []))
        window_remaining: Optional[float] = None
        if not force and max_allowed is not None and len(pruned) >= max_allowed and pruned:
            window_remaining = max(0.0, (min(pruned) + window) - now)
        throttled = bool(remaining > 0 or window_remaining)
        if throttled:
            _SUPPRESSED_COUNT[key] = int(_SUPPRESSED_COUNT.get(key, 0)) + 1
            result = _build_status(key, now)
            result["allowed"] = False
            result["throttled"] = True
            result["recorded"] = False
            result["suppressed"] = True
            result["ok"] = True
            result["success"] = True
            return result
        _LAST_SENT[key] = now
        _HISTORY.setdefault(key, []).append(now)
        _ALLOWED_COUNT[key] = int(_ALLOWED_COUNT.get(key, 0)) + 1
        result = _build_status(key, now)
        result["allowed"] = True
        result["throttled"] = False
        result["recorded"] = True
        result["suppressed"] = False
        result["ok"] = True
        result["success"] = True
        return result


def should_send(*args: Any, **kwargs: Any) -> bool:
    return bool(check(*args, **kwargs).get("allowed", False))


def is_throttled(*args: Any, **kwargs: Any) -> bool:
    return bool(check(*args, **kwargs).get("throttled", False))


def cooldown_remaining(*args: Any, **kwargs: Any) -> float:
    return float(check(*args, **kwargs).get("remaining_seconds", 0.0) or 0.0)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    key = _canonical_key(data)
    now = _coerce_float(data.get("now"), _now_ts()) if data.get("now") is not None else _now_ts()
    with _lock:
        result = _build_status(key, now)
        result["ok"] = True
        result["success"] = True
        return result


def set_cooldown(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Set per-key (or default) cooldown seconds. ``key=None`` sets default."""
    global _DEFAULT_COOLDOWN
    data = _normalize_inputs(*args, **kwargs)
    seconds_raw = _first(data, COOLDOWN_KEYS)
    if seconds_raw is None:
        # allow positional (key, seconds) style
        vals = [a for a in args if isinstance(a, (int, float))]
        seconds_raw = vals[0] if vals else _DEFAULT_COOLDOWN
    seconds = _clamp_cooldown(seconds_raw)
    explicit = _first(data, KEY_KEYS)
    with _lock:
        if explicit is None or not str(explicit).strip():
            _DEFAULT_COOLDOWN = float(seconds)
            return {"ok": True, "success": True, "key": None, "default_cooldown": float(seconds), "cooldown_seconds": float(seconds)}
        key = str(explicit).strip()
        _COOLDOWNS[key] = float(seconds)
        return {"ok": True, "success": True, "key": key, "cooldown_seconds": float(seconds)}


def get_cooldown(*args: Any, **kwargs: Any) -> float:
    data = _normalize_inputs(*args, **kwargs)
    key = _canonical_key(data)
    override = _first(data, COOLDOWN_KEYS)
    return float(_effective_cooldown(key, override))


def set_throttle(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Configure sliding-window cap: ``max_per_window`` per ``window_seconds``."""
    global _DEFAULT_MAX, _DEFAULT_WINDOW
    data = _normalize_inputs(*args, **kwargs)
    max_raw = _first(data, MAX_KEYS)
    win_raw = _first(data, WINDOW_KEYS)
    explicit = _first(data, KEY_KEYS)
    with _lock:
        if explicit is None or not str(explicit).strip():
            if max_raw is not None:
                try:
                    _DEFAULT_MAX = int(float(max_raw))  # type: ignore[arg-type]
                    if _DEFAULT_MAX is not None and _DEFAULT_MAX <= 0:
                        _DEFAULT_MAX = None
                except (TypeError, ValueError):
                    pass
            if win_raw is not None:
                try:
                    _DEFAULT_WINDOW = float(win_raw)
                except (TypeError, ValueError):
                    pass
            return {"ok": True, "success": True, "key": None, "max_per_window": _DEFAULT_MAX, "window_seconds": _DEFAULT_WINDOW}
        key = str(explicit).strip()
        if max_raw is not None:
            try:
                v = int(float(max_raw))  # type: ignore[arg-type]
                _MAX_PER_WINDOW[key] = v if v > 0 else None
            except (TypeError, ValueError):
                pass
        if win_raw is not None:
            try:
                _WINDOWS[key] = float(win_raw)
            except (TypeError, ValueError):
                pass
        return {"ok": True, "success": True, "key": key, "max_per_window": _effective_max(key, None), "window_seconds": _effective_window(key, None)}


def get_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    explicit = _first(data, KEY_KEYS)
    with _lock:
        if explicit is not None and str(explicit).strip():
            key = str(explicit).strip()
            return {
                "ok": True,
                "success": True,
                "key": key,
                "cooldown_seconds": _effective_cooldown(key, None),
                "window_seconds": _effective_window(key, None),
                "max_per_window": _effective_max(key, None),
                "default_cooldown": float(_DEFAULT_COOLDOWN),
                "default_window": float(_DEFAULT_WINDOW),
                "default_max_per_window": _DEFAULT_MAX,
            }
        return {
            "ok": True,
            "success": True,
            "default_cooldown": float(_DEFAULT_COOLDOWN),
            "default_window": float(_DEFAULT_WINDOW),
            "default_max_per_window": _DEFAULT_MAX,
            "cooldown_seconds": float(_DEFAULT_COOLDOWN),
            "window_seconds": float(_DEFAULT_WINDOW),
            "max_per_window": _DEFAULT_MAX,
            "overrides": dict(_COOLDOWNS),
            "keys": sorted(set(list(_LAST_SENT.keys()) + list(_COOLDOWNS.keys()))),
            "tracked_keys": sorted(_LAST_SENT.keys()),
        }


def reset(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Clear cooldown state for one key, or everything when no key given."""
    data = _normalize_inputs(*args, **kwargs)
    explicit = _first(data, KEY_KEYS)
    cam = _first(data, CAMERA_KEYS)
    evt = _first(data, EVENT_KEYS)
    ch = _first(data, CHANNEL_KEYS)
    has_parts = any(v is not None for v in (cam, evt, ch))
    with _lock:
        if (explicit is None or not str(explicit).strip()) and not has_parts and not data:
            cleared = len(_LAST_SENT)
            _LAST_SENT.clear()
            _HISTORY.clear()
            _ALLOWED_COUNT.clear()
            _SUPPRESSED_COUNT.clear()
            return {"ok": True, "success": True, "reset": True, "cleared": cleared}
        key = _canonical_key(data) if (has_parts or (explicit is not None and str(explicit).strip())) else "default"
        if not data and (explicit is None):
            # reset() with no args -> clear everything
            cleared = len(_LAST_SENT)
            _LAST_SENT.clear()
            _HISTORY.clear()
            _ALLOWED_COUNT.clear()
            _SUPPRESSED_COUNT.clear()
            return {"ok": True, "success": True, "reset": True, "cleared": cleared}
        existed = key in _LAST_SENT or key in _HISTORY
        _LAST_SENT.pop(key, None)
        _HISTORY.pop(key, None)
        _ALLOWED_COUNT.pop(key, None)
        _SUPPRESSED_COUNT.pop(key, None)
        return {"ok": True, "success": True, "reset": True, "key": key, "existed": existed}


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    if not data:
        with _lock:
            n = len(_LAST_SENT)
            _LAST_SENT.clear()
            _HISTORY.clear()
            _ALLOWED_COUNT.clear()
            _SUPPRESSED_COUNT.clear()
            return {"ok": True, "success": True, "cleared": n, "reset": True}
    return reset(*args, **kwargs)


def list_keys() -> List[str]:
    with _lock:
        return sorted(_LAST_SENT.keys())


def list_status() -> List[Dict[str, Any]]:
    now = _now_ts()
    with _lock:
        return [_build_status(k, now) for k in sorted(_LAST_SENT.keys())]


# ---------------------------------------------------------------------------
# Service class (singleton mirrors module functions)
# ---------------------------------------------------------------------------

class AlertThrottleService:
    def check(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check(*args, **kwargs)

    def record(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return record(*args, **kwargs)

    def check_and_record(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check_and_record(*args, **kwargs)

    def should_send(self, *args: Any, **kwargs: Any) -> bool:
        return should_send(*args, **kwargs)

    def can_send(self, *args: Any, **kwargs: Any) -> bool:
        return should_send(*args, **kwargs)

    def is_allowed(self, *args: Any, **kwargs: Any) -> bool:
        return should_send(*args, **kwargs)

    def is_throttled(self, *args: Any, **kwargs: Any) -> bool:
        return is_throttled(*args, **kwargs)

    def cooldown_remaining(self, *args: Any, **kwargs: Any) -> float:
        return cooldown_remaining(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)

    def set_cooldown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_cooldown(*args, **kwargs)

    def get_cooldown(self, *args: Any, **kwargs: Any) -> float:
        return get_cooldown(*args, **kwargs)

    def set_throttle(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_throttle(*args, **kwargs)

    def get_config(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_config(*args, **kwargs)

    def reset(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return reset(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return clear(*args, **kwargs)

    # aliases
    def allow(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check_and_record(*args, **kwargs)

    def gate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check_and_record(*args, **kwargs)

    def attempt(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check_and_record(*args, **kwargs)

    def try_acquire(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check_and_record(*args, **kwargs)

    def acquire(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check_and_record(*args, **kwargs)

    def throttle(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check(*args, **kwargs)

    def mark_sent(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return record(*args, **kwargs)

    def note_sent(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return record(*args, **kwargs)


AlertThrottle = AlertThrottleService
ThrottleService = AlertThrottleService
CooldownService = AlertThrottleService

alert_throttle_service = AlertThrottleService()
throttle_service = alert_throttle_service
cooldown_service = alert_throttle_service
service = alert_throttle_service
manager = alert_throttle_service
alert_manager = alert_throttle_service
default_service = alert_throttle_service


def get_service() -> AlertThrottleService:
    return alert_throttle_service


def create_service() -> AlertThrottleService:
    return AlertThrottleService()


# Module-level aliases for flexible callers
can_send = should_send
is_allowed = should_send
allowed = should_send
throttle_check = check
throttle_status = get_status
check_throttle = check
is_cooldown_active = is_throttled
time_remaining = cooldown_remaining
retry_after = cooldown_remaining
remaining = cooldown_remaining
mark_sent = record
note_sent = record
log_send = record
allow = check_and_record
gate = check_and_record
attempt = check_and_record
try_acquire = check_and_record
acquire = check_and_record
throttle = check
status = get_status
state = get_status
describe = get_status
configure = set_throttle
update_config = set_throttle
reset_key = reset
clear_key = reset
reset_all = clear
clear_all = clear


__all__ = [
    "check",
    "record",
    "check_and_record",
    "should_send",
    "is_throttled",
    "cooldown_remaining",
    "get_status",
    "set_cooldown",
    "get_cooldown",
    "set_throttle",
    "get_config",
    "reset",
    "clear",
    "list_keys",
    "list_status",
    "AlertThrottleService",
    "AlertThrottle",
    "alert_throttle_service",
    "DEFAULT_COOLDOWN_SECONDS",
]
