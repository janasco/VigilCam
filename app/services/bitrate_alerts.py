"""Stream bitrate alerting service for VigilCam.

Complements :mod:`app.services.bitrate` (which samples point-in-time
throughput only and has NO threshold or alerting logic) and
:mod:`app.services.resource_watch` (verified: watches ``cpu`` / ``memory`` /
``disk`` percent-used only -- ``RESOURCES = ("cpu", "memory", "disk")`` --
with no bitrate thresholds).

This module adds configurable low/high bitrate threshold evaluation,
cooldown-throttled alerting, and in-memory alert history keyed by stream
target. All state is in-memory and dependency-light so it can be called
from health checks, the watchdog, or API handlers.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Tunables / defaults
# ---------------------------------------------------------------------------

DEFAULT_MIN_BITRATE_BPS = 100_000.0
DEFAULT_MAX_BITRATE_BPS = 20_000_000.0
DEFAULT_COOLDOWN_SECONDS = 300.0
ENABLED_BY_DEFAULT = True

MIN_BITRATE_BPS = 0.0
MAX_BITRATE_BPS = 1_000_000_000.0
MIN_COOLDOWN_SECONDS = 0.0
MAX_COOLDOWN_SECONDS = 86400.0 * 7

MAX_HISTORY = 200

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

_lock = threading.RLock()

_CONFIG: Dict[str, Any] = {
    "enabled": bool(ENABLED_BY_DEFAULT),
    "min_bitrate_bps": float(DEFAULT_MIN_BITRATE_BPS),
    "max_bitrate_bps": float(DEFAULT_MAX_BITRATE_BPS),
    "cooldown_seconds": float(DEFAULT_COOLDOWN_SECONDS),
}

# target -> per-target threshold overrides {"min_bitrate_bps": ..., "max_bitrate_bps": ...}
_TARGET_CONFIG: Dict[str, Dict[str, float]] = {}

# target -> datetime of last emitted alert
_LAST_ALERT_AT: Dict[str, datetime] = {}
# target -> counts
_ALERT_COUNTS: Dict[str, int] = {}
_SUPPRESSED_COUNTS: Dict[str, int] = {}
# target -> active alert record dict
_ACTIVE: Dict[str, Dict[str, Any]] = {}
# bounded history of emitted (non-throttled) alerts
_HISTORY: List[Dict[str, Any]] = []
# target -> last evaluated bitrate bps
_LAST_VALUES: Dict[str, Optional[float]] = {}
_LAST_CHECK_AT: Optional[datetime] = None

_NOTIFIER: Optional[Callable[..., Any]] = None


def _now() -> datetime:
    return datetime.utcnow()


def _now_ts() -> float:
    return time.time()


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
                return datetime.utcfromtimestamp(float(s))
            except (ValueError, OSError, OverflowError):
                return None
    return None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _clamp_bitrate(value: Any, fallback: float) -> float:
    v = _to_float(value)
    if v is None:
        return float(fallback)
    if v < MIN_BITRATE_BPS:
        return float(MIN_BITRATE_BPS)
    if v > MAX_BITRATE_BPS:
        return float(MAX_BITRATE_BPS)
    return float(v)


def _clamp_cooldown(value: Any) -> float:
    v = _to_float(value)
    if v is None:
        return float(DEFAULT_COOLDOWN_SECONDS)
    if v < MIN_COOLDOWN_SECONDS:
        return float(MIN_COOLDOWN_SECONDS)
    if v > MAX_COOLDOWN_SECONDS:
        return float(MAX_COOLDOWN_SECONDS)
    return float(v)


TARGET_KEYS = ("target", "stream", "name", "label", "stream_url", "url",
               "source", "camera_id", "camera", "cam", "id", "device_id")
BITRATE_BPS_KEYS = ("bitrate_bps", "bps", "bitrate", "bits_per_second",
                    "bitrate_bps_measured", "measured_bps")
BITRATE_KBPS_KEYS = ("bitrate_kbps", "kbps", "kilobits_per_second")
BITRATE_MBPS_KEYS = ("bitrate_mbps", "mbps", "megabits_per_second")
MIN_THRESHOLD_KEYS = ("min_bitrate_bps", "min_bps", "min_bitrate", "low_threshold",
                      "low_bitrate_bps", "min_kbps", "min_mbps", "stall_threshold",
                      "stall_threshold_bps")
MAX_THRESHOLD_KEYS = ("max_bitrate_bps", "max_bps", "max_bitrate", "high_threshold",
                      "high_bitrate_bps", "max_kbps", "max_mbps")
COOLDOWN_KEYS = ("cooldown", "cooldown_seconds", "cooldown_secs", "interval",
                 "interval_seconds", "delay", "delay_seconds")
FORCE_KEYS = ("force", "forced", "bypass", "bypass_throttle", "ignore_cooldown", "ignore_throttle")
NOW_KEYS = ("now", "at", "when", "timestamp", "ts", "time")
BYTES_KEYS = ("bytes_received", "bytes", "total_bytes", "size_bytes", "size", "nbytes")
DURATION_KEYS = ("duration_seconds", "duration", "sample_seconds", "sample_duration",
                 "elapsed", "elapsed_seconds")


def _normalize_target(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"camera:{int(value)}"
    s = str(value).strip()
    return s or "unknown"


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            # Bare numeric positional arg: bitrate if bitrate-ish context else target.
            # Prefer target only when no target yet and value looks like an id.
            data.setdefault("target", a)
        elif isinstance(a, str):
            s = a.strip()
            if s and "target" not in data:
                data["target"] = s
            continue
        elif hasattr(a, "model_dump") and callable(getattr(a, "model_dump")):
            try:
                data.update(a.model_dump())
            except Exception:  # noqa: BLE001
                pass
        elif hasattr(a, "dict") and callable(getattr(a, "dict")):
            try:
                data.update(a.dict())
            except Exception:  # noqa: BLE001
                pass
        elif hasattr(a, "to_dict") and callable(getattr(a, "to_dict")):
            try:
                d = a.to_dict()
                if isinstance(d, dict):
                    data.update(d)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request",
                       "data", "body", "alert", "settings", "thresholds", "values",
                       "sample", "result", "bitrate"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k, v in lower.items():
        data.setdefault(k, v)
    return data


def _resolve_target(data: Dict[str, Any]) -> str:
    for k in TARGET_KEYS:
        if k in data and data.get(k) is not None:
            s = str(data.get(k)).strip()
            if s:
                # Numeric camera ids collapse to camera:N form for stability.
                if k in ("camera_id", "camera", "cam", "id", "device_id") and s.isdigit():
                    return f"camera:{int(s)}"
                return _normalize_target(s)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in TARGET_KEYS:
        if k.lower() in lower and lower[k.lower()] is not None:
            s = str(lower[k.lower()]).strip()
            if s:
                return _normalize_target(s)
    return "unknown"


def _resolve_bitrate_bps(data: Dict[str, Any]) -> Optional[float]:
    for k in BITRATE_BPS_KEYS:
        if k in data and data.get(k) is not None:
            v = _to_float(data.get(k))
            if v is not None:
                return v
    for k in BITRATE_KBPS_KEYS:
        if k in data and data.get(k) is not None:
            v = _to_float(data.get(k))
            if v is not None:
                return v * 1000.0
    for k in BITRATE_MBPS_KEYS:
        if k in data and data.get(k) is not None:
            v = _to_float(data.get(k))
            if v is not None:
                return v * 1_000_000.0
    # bytes + duration fallback: bps = bytes * 8 / duration.
    nbytes = None
    for k in BYTES_KEYS:
        if k in data and data.get(k) is not None:
            nbytes = _to_float(data.get(k))
            if nbytes is not None:
                break
    if nbytes is not None:
        duration = None
        for k in DURATION_KEYS:
            if k in data and data.get(k) is not None:
                duration = _to_float(data.get(k))
                if duration is not None and duration > 0:
                    break
        if duration is None or duration <= 0:
            duration = 2.0
        if nbytes < 0:
            nbytes = 0.0
        return (float(nbytes) * 8.0) / float(duration)
    # Nested sample/result dicts.
    for k in ("sample", "result", "bitrate", "metrics"):
        nested = data.get(k)
        if isinstance(nested, dict):
            v = _resolve_bitrate_bps(nested)
            if v is not None:
                return v
            # bitrate sampler BitrateResult shape.
            for kk in BITRATE_BPS_KEYS + BITRATE_KBPS_KEYS + BITRATE_MBPS_KEYS:
                if nested.get(kk) is not None:
                    nested_v = _resolve_bitrate_bps({kk: nested.get(kk)})
                    if nested_v is not None:
                        return nested_v
    return None


def _resolve_force(data: Dict[str, Any]) -> bool:
    for k in FORCE_KEYS:
        if k in data and data.get(k) is not None:
            try:
                if bool(data.get(k)):
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _resolve_now(data: Dict[str, Any]) -> datetime:
    for k in NOW_KEYS:
        if k in data and data.get(k) is not None:
            dt = _to_datetime(data.get(k))
            if dt is not None:
                return dt
    return _now()


def _thresholds_for(target: str) -> Dict[str, float]:
    with _lock:
        min_v = float(_CONFIG.get("min_bitrate_bps", DEFAULT_MIN_BITRATE_BPS))
        max_v = float(_CONFIG.get("max_bitrate_bps", DEFAULT_MAX_BITRATE_BPS))
        override = _TARGET_CONFIG.get(target)
        if override:
            if override.get("min_bitrate_bps") is not None:
                min_v = float(override["min_bitrate_bps"])
            if override.get("max_bitrate_bps") is not None:
                max_v = float(override["max_bitrate_bps"])
    return {"min_bitrate_bps": min_v, "max_bitrate_bps": max_v}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def get_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    target = _resolve_target(data) if data else "unknown"
    with _lock:
        cfg = dict(_CONFIG)
        tgt_override = dict(_TARGET_CONFIG.get(target, {})) if target != "unknown" else {}
    out: Dict[str, Any] = {
        "ok": True,
        "success": True,
        "enabled": bool(cfg.get("enabled", True)),
        "min_bitrate_bps": float(cfg.get("min_bitrate_bps", DEFAULT_MIN_BITRATE_BPS)),
        "max_bitrate_bps": float(cfg.get("max_bitrate_bps", DEFAULT_MAX_BITRATE_BPS)),
        "cooldown_seconds": float(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
    }
    out["min_bps"] = out["min_bitrate_bps"]
    out["max_bps"] = out["max_bitrate_bps"]
    out["min_kbps"] = round(out["min_bitrate_bps"] / 1000.0, 3)
    out["max_kbps"] = round(out["max_bitrate_bps"] / 1000.0, 3)
    out["min_mbps"] = round(out["min_bitrate_bps"] / 1_000_000.0, 6)
    out["max_mbps"] = round(out["max_bitrate_bps"] / 1_000_000.0, 6)
    out["cooldown"] = out["cooldown_seconds"]
    out["interval_seconds"] = out["cooldown_seconds"]
    out["thresholds"] = {
        "min_bitrate_bps": out["min_bitrate_bps"],
        "max_bitrate_bps": out["max_bitrate_bps"],
    }
    if tgt_override:
        out["target"] = target
        out["target_thresholds"] = dict(tgt_override)
    return out


def get_thresholds(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    target = _resolve_target(data)
    thr = _thresholds_for(target)
    return {
        "ok": True,
        "success": True,
        "target": target,
        "min_bitrate_bps": thr["min_bitrate_bps"],
        "max_bitrate_bps": thr["max_bitrate_bps"],
        "min_bps": thr["min_bitrate_bps"],
        "max_bps": thr["max_bitrate_bps"],
        "thresholds": dict(thr),
    }


def configure(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    # Per-target override when a concrete target is given alongside thresholds.
    has_target = any(k in data and data.get(k) is not None for k in TARGET_KEYS)
    target = _resolve_target(data) if has_target else None
    if target == "unknown":
        target = None
    with _lock:
        if "enabled" in data and data["enabled"] is not None:
            try:
                _CONFIG["enabled"] = bool(data["enabled"])
            except Exception:  # noqa: BLE001
                pass
        min_seen = False
        for key in MIN_THRESHOLD_KEYS:
            if key in data and data[key] is not None:
                raw = _to_float(data[key])
                if raw is None:
                    break
                # kbps/mbps-suffixed aliases arrive in native units.
                if key in ("min_kbps",):
                    raw = raw * 1000.0
                elif key in ("min_mbps",):
                    raw = raw * 1_000_000.0
                val = _clamp_bitrate(raw, _CONFIG["min_bitrate_bps"])
                if target is not None:
                    _TARGET_CONFIG.setdefault(target, {})["min_bitrate_bps"] = val
                else:
                    _CONFIG["min_bitrate_bps"] = val
                min_seen = True
                break
        for key in MAX_THRESHOLD_KEYS:
            if key in data and data[key] is not None:
                raw = _to_float(data[key])
                if raw is None:
                    break
                if key in ("max_kbps",):
                    raw = raw * 1000.0
                elif key in ("max_mbps",):
                    raw = raw * 1_000_000.0
                val = _clamp_bitrate(raw, _CONFIG["max_bitrate_bps"])
                if target is not None:
                    _TARGET_CONFIG.setdefault(target, {})["max_bitrate_bps"] = val
                else:
                    _CONFIG["max_bitrate_bps"] = val
                break
        # Generic "threshold"/"bitrate_threshold" applies to the minimum when
        # no specific min/max key was given.
        if not min_seen and "threshold" in data and data["threshold"] is not None:
            v = _clamp_bitrate(data["threshold"], _CONFIG["min_bitrate_bps"])
            if target is not None:
                _TARGET_CONFIG.setdefault(target, {})["min_bitrate_bps"] = v
            else:
                _CONFIG["min_bitrate_bps"] = v
        for key in COOLDOWN_KEYS:
            if key in data and data[key] is not None:
                _CONFIG["cooldown_seconds"] = _clamp_cooldown(data[key])
                break
    return get_config()


def set_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return configure(*args, **kwargs)


def update_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return configure(*args, **kwargs)


def set_thresholds(*args: Any, **kwargs: Any) -> Dict[str, Any]:
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


def _run_notifier(target: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        cb = _NOTIFIER
    if cb is None or not callable(cb):
        return {"notified": False, "reason": "no-notifier"}
    try:
        result = cb(target, payload)
        if isinstance(result, dict):
            out = dict(result)
            out.setdefault("notified", bool(out.get("ok", True)))
            return out
        return {"notified": bool(result) if isinstance(result, bool) else True, "result": result}
    except Exception as exc:  # noqa: BLE001
        return {"notified": False, "error": str(exc)}


def _publish_event(target: str, bitrate_bps: Optional[float], message: str) -> bool:
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
            payload = {"target": target, "bitrate_bps": bitrate_bps, "message": message}
            if loop is not None and loop.is_running():
                loop.create_task(_bus.publish("bitrate_alert", payload))
                return True
            return False
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def evaluate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Pure threshold evaluation (no state mutation, no cooldown).

    Accepts a ``target`` plus one of ``bitrate_bps`` / ``bps`` / ``kbps`` /
    ``mbps`` / ``bytes_received``+``duration_seconds``. Per-call ``min_*`` /
    ``max_*`` overrides do not persist.
    """
    data = _normalize_inputs(*args, **kwargs)
    target = _resolve_target(data)
    bitrate = _resolve_bitrate_bps(data)
    thresholds = _thresholds_for(target)
    # Per-call overrides (do not persist).
    for keys, res in ((MIN_THRESHOLD_KEYS, "min_bitrate_bps"), (MAX_THRESHOLD_KEYS, "max_bitrate_bps")):
        for k in keys:
            if k in data and data[k] is not None:
                raw = _to_float(data[k])
                if raw is None:
                    break
                if k in ("min_kbps", "max_kbps"):
                    raw = raw * 1000.0
                elif k in ("min_mbps", "max_mbps"):
                    raw = raw * 1_000_000.0
                thresholds[res] = _clamp_bitrate(raw, thresholds[res])
                break

    min_thr = thresholds["min_bitrate_bps"]
    max_thr = thresholds["max_bitrate_bps"]
    low = bitrate is not None and float(bitrate) < float(min_thr)
    high = bitrate is not None and float(bitrate) > float(max_thr)
    breached = bool(low or high)
    if bitrate is None:
        severity = "unknown"
        message = f"{target} bitrate unknown (range {min_thr}-{max_thr} bps)"
    elif low:
        severity = "critical"
        message = f"{target} bitrate {bitrate} bps below minimum {min_thr} bps"
    elif high:
        severity = "warning"
        message = f"{target} bitrate {bitrate} bps above maximum {max_thr} bps"
    else:
        severity = "ok"
        message = f"{target} bitrate {bitrate} bps within range {min_thr}-{max_thr} bps"
    return {
        "ok": True,
        "success": True,
        "breached": breached,
        "alerted": breached,
        "low": bool(low),
        "high": bool(high),
        "target": target,
        "bitrate_bps": bitrate,
        "value": bitrate,
        "thresholds": dict(thresholds),
        "min_bitrate_bps": min_thr,
        "max_bitrate_bps": max_thr,
        "severity": severity,
        "message": message,
    }


def should_alert(*args: Any, **kwargs: Any) -> bool:
    """True when a bitrate breach exists and cooldown allows a new alert."""
    data = _normalize_inputs(*args, **kwargs)
    target = _resolve_target(data)
    force = _resolve_force(data)
    if force:
        ev = evaluate(**data)
        return bool(ev.get("breached"))
    with _lock:
        enabled = bool(_CONFIG.get("enabled", True))
    if not enabled and not data.get("ignore_enabled", False):
        return False
    ev = evaluate(**data)
    if not ev.get("breached"):
        return False
    now_dt = _resolve_now(data)
    with _lock:
        cooldown = float(_CONFIG.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))
        last = _LAST_ALERT_AT.get(target)
        if last is None:
            return True
        try:
            return (now_dt - last).total_seconds() >= cooldown
        except Exception:  # noqa: BLE001
            return True


def is_throttled(*args: Any, **kwargs: Any) -> bool:
    data = _normalize_inputs(*args, **kwargs)
    ev = evaluate(**data)
    if not ev.get("breached"):
        return False
    return not should_alert(**data)


def cooldown_remaining(*args: Any, **kwargs: Any) -> float:
    data = _normalize_inputs(*args, **kwargs)
    target = _resolve_target(data)
    now_dt = _resolve_now(data)
    with _lock:
        cooldown = float(_CONFIG.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))
        last = _LAST_ALERT_AT.get(target)
        if last is None:
            return 0.0
        try:
            return max(0.0, cooldown - (now_dt - last).total_seconds())
        except Exception:  # noqa: BLE001
            return 0.0


def check(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Evaluate bitrate thresholds, apply cooldown, and record alerts."""
    data = _normalize_inputs(*args, **kwargs)
    now_dt = _resolve_now(data)
    force = _resolve_force(data)
    target = _resolve_target(data)
    with _lock:
        enabled = bool(_CONFIG.get("enabled", True))
        cooldown = float(_CONFIG.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))

    ev = evaluate(**data)
    bitrate = ev["bitrate_bps"]
    thresholds = ev["thresholds"]

    with _lock:
        global _LAST_CHECK_AT
        _LAST_VALUES[target] = bitrate
        _LAST_CHECK_AT = now_dt

    if not enabled and not force:
        return {
            "ok": True, "success": True,
            "breached": bool(ev["breached"]),
            "alerted": False, "sent": False,
            "throttled": False, "disabled": True,
            "target": target,
            "bitrate_bps": bitrate,
            "value": bitrate,
            "thresholds": thresholds,
            "at": now_dt.isoformat(),
            "detail": "bitrate alerts disabled",
        }

    if not ev.get("breached"):
        return {
            "ok": True, "success": True,
            "breached": False,
            "alerted": False, "sent": False,
            "throttled": False,
            "target": target,
            "bitrate_bps": bitrate,
            "value": bitrate,
            "thresholds": thresholds,
            "low": ev.get("low", False),
            "high": ev.get("high", False),
            "severity": ev.get("severity", "ok"),
            "message": ev.get("message", ""),
            "at": now_dt.isoformat(),
            "cooldown_seconds": float(cooldown),
        }

    with _lock:
        last = _LAST_ALERT_AT.get(target)
        elapsed = None
        if last is not None:
            try:
                elapsed = (now_dt - last).total_seconds()
            except Exception:  # noqa: BLE001
                elapsed = None
        allowed = force or last is None or (elapsed is not None and elapsed >= cooldown)
        if not allowed:
            _SUPPRESSED_COUNTS[target] = int(_SUPPRESSED_COUNTS.get(target, 0) or 0) + 1
            remaining = max(0.0, cooldown - (elapsed or 0.0))
            suppressed_record = {
                "target": target, "bitrate_bps": bitrate,
                "thresholds": dict(thresholds), "remaining_seconds": remaining,
            }
            return {
                "ok": True, "success": True,
                "breached": True,
                "alerted": False, "sent": False,
                "throttled": True, "suppressed": suppressed_record,
                "target": target,
                "bitrate_bps": bitrate, "value": bitrate,
                "thresholds": thresholds,
                "low": ev.get("low", False), "high": ev.get("high", False),
                "severity": ev.get("severity", "critical"),
                "message": ev.get("message", ""),
                "at": now_dt.isoformat(),
                "cooldown_seconds": float(cooldown),
                "remaining_seconds": float(remaining),
            }
        _LAST_ALERT_AT[target] = now_dt
        _ALERT_COUNTS[target] = int(_ALERT_COUNTS.get(target, 0) or 0) + 1
        record = {
            "target": target,
            "bitrate_bps": bitrate,
            "value": bitrate,
            "thresholds": dict(thresholds),
            "low": bool(ev.get("low", False)),
            "high": bool(ev.get("high", False)),
            "severity": ev.get("severity", "critical"),
            "message": ev.get("message", ""),
            "at": now_dt.isoformat(),
            "alert_count": int(_ALERT_COUNTS.get(target, 0)),
        }
        _ACTIVE[target] = dict(record)
        _HISTORY.append(dict(record))
        while len(_HISTORY) > MAX_HISTORY:
            _HISTORY.pop(0)
    message = str(ev.get("message", ""))
    event_published = _publish_event(target, bitrate, message)
    notify_result = _run_notifier(target, {"target": target, "bitrate_bps": bitrate,
                                           "thresholds": thresholds, "message": message,
                                           "at": now_dt.isoformat()})
    record_out = dict(_ACTIVE.get(target, {}))
    record_out["event_published"] = bool(event_published)
    record_out["notify"] = notify_result
    return {
        "ok": True, "success": True,
        "breached": True,
        "alerted": True, "sent": True,
        "throttled": False,
        "target": target,
        "bitrate_bps": bitrate, "value": bitrate,
        "thresholds": thresholds,
        "low": ev.get("low", False), "high": ev.get("high", False),
        "severity": ev.get("severity", "critical"),
        "message": message,
        "new_alert": record_out,
        "emitted": [record_out],
        "active": [dict(_ACTIVE[k]) for k in sorted(_ACTIVE.keys())],
        "at": now_dt.isoformat(),
        "cooldown_seconds": float(cooldown),
        "remaining_seconds": 0.0,
    }


async def check_async(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Async check: samples live bitrate unless an explicit value was given."""
    data = _normalize_inputs(*args, **kwargs)
    has_explicit = _resolve_bitrate_bps(data) is not None
    if has_explicit:
        return check(**data)
    target = _resolve_target(data)
    try:
        from app.services.bitrate import sample_camera as _sample_camera
        from app.services.bitrate import sample_url as _sample_url
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "success": False, "breached": False,
                "alerted": False, "error": str(exc), "target": target,
                "thresholds": _thresholds_for(target)}
    try:
        url = data.get("url", data.get("stream_url", data.get("source")))
        sampler = data.get("sampler")
        if url:
            result = _sample_url(url, target=target, sampler=sampler)
        elif target != "unknown":
            if target.startswith("camera:"):
                try:
                    cid = int(target.split(":", 1)[1])
                except (ValueError, IndexError):
                    cid = target
                result = _sample_camera(cid, url=url, sampler=sampler)
            else:
                result = _sample_url(target, target=target, sampler=sampler)
        else:
            return {"ok": False, "success": False, "breached": False,
                    "alerted": False, "error": "no target or url provided",
                    "target": target, "thresholds": _thresholds_for(target)}
        try:
            payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        except Exception:  # noqa: BLE001
            payload = {}
        merged = dict(data)
        for k in ("bitrate_bps", "bps", "bitrate_kbps", "kbps",
                  "bitrate_mbps", "mbps", "bytes_received", "duration_seconds"):
            if payload.get(k) is not None and merged.get(k) is None:
                merged[k] = payload.get(k)
        return check(**merged)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "success": False, "breached": False,
                "alerted": False, "error": str(exc), "target": target,
                "thresholds": _thresholds_for(target)}


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Current watcher status: last values, thresholds, active alerts."""
    data = _normalize_inputs(*args, **kwargs)
    has_target = any(k in data and data.get(k) is not None for k in TARGET_KEYS)
    target = _resolve_target(data) if has_target else None
    with _lock:
        active = [dict(_ACTIVE[k]) for k in sorted(_ACTIVE.keys())]
        values = dict(_LAST_VALUES)
        last_check = _LAST_CHECK_AT.isoformat() if _LAST_CHECK_AT else None
        cfg = dict(_CONFIG)
        counts = {k: int(v) for k, v in _ALERT_COUNTS.items()}
        suppressed = {k: int(v) for k, v in _SUPPRESSED_COUNTS.items()}
    out: Dict[str, Any] = {
        "ok": True, "success": True,
        "enabled": bool(cfg.get("enabled", True)),
        "values": values,
        "thresholds": {"min_bitrate_bps": float(cfg.get("min_bitrate_bps", DEFAULT_MIN_BITRATE_BPS)),
                        "max_bitrate_bps": float(cfg.get("max_bitrate_bps", DEFAULT_MAX_BITRATE_BPS))},
        "active": active,
        "alerts": active,
        "alert_counts": counts,
        "suppressed_counts": suppressed,
        "last_check_at": last_check,
    }
    if target is not None:
        thr = _thresholds_for(target)
        out["target"] = target
        out["target_alert"] = dict(_ACTIVE.get(target)) if target in _ACTIVE else None
        out["alert_count"] = counts.get(target, 0)
        out["bitrate_bps"] = values.get(target)
        out["thresholds"] = dict(thr)
    return out


def list_alerts(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    with _lock:
        return [dict(_ACTIVE[k]) for k in sorted(_ACTIVE.keys())]


def get_alerts(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_alerts(*args, **kwargs)


def get_history(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    data = _normalize_inputs(*args, **kwargs)
    limit = _to_float(data.get("limit", data.get("n", data.get("count"))))
    target = _resolve_target(data) if any(k in data for k in TARGET_KEYS) else None
    with _lock:
        hist = list(_HISTORY)
    if target is not None and target != "unknown":
        hist = [h for h in hist if h.get("target") == target]
    hist = list(reversed(hist))
    if limit is not None and limit >= 0:
        hist = hist[: int(limit)]
    return hist


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        cfg = dict(_CONFIG)
        active = [dict(_ACTIVE[k]) for k in sorted(_ACTIVE.keys())]
        total_alerts = sum(int(v or 0) for v in _ALERT_COUNTS.values())
        total_suppressed = sum(int(v or 0) for v in _SUPPRESSED_COUNTS.values())
        values = dict(_LAST_VALUES)
        last_check = _LAST_CHECK_AT.isoformat() if _LAST_CHECK_AT else None
        counts = {k: int(v) for k, v in _ALERT_COUNTS.items()}
    return {
        "ok": True, "success": True,
        "enabled": bool(cfg.get("enabled", True)),
        "service": "bitrate_alerts",
        "active_count": len(active),
        "active": active,
        "alerts": active,
        "total_alerts": total_alerts,
        "alert_count": total_alerts,
        "suppressed_count": total_suppressed,
        "suppressed": total_suppressed,
        "alert_counts": counts,
        "thresholds": {"min_bitrate_bps": float(cfg.get("min_bitrate_bps", DEFAULT_MIN_BITRATE_BPS)),
                        "max_bitrate_bps": float(cfg.get("max_bitrate_bps", DEFAULT_MAX_BITRATE_BPS))},
        "cooldown_seconds": float(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
        "values": values,
        "last_check_at": last_check,
    }


def resolve(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Clear active alert(s). No target = clear all."""
    data = _normalize_inputs(*args, **kwargs)
    has_target = any(k in data and data.get(k) is not None for k in TARGET_KEYS)
    target = _resolve_target(data) if has_target else None
    with _lock:
        if target is not None and target != "unknown":
            removed = _ACTIVE.pop(target, None)
            return {"ok": True, "success": True, "resolved": removed is not None,
                    "target": target, "active": [dict(_ACTIVE[k]) for k in sorted(_ACTIVE.keys())]}
        _ACTIVE.clear()
        return {"ok": True, "success": True, "resolved": True,
                "active": [], "detail": "all bitrate alerts resolved"}


def reset(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    has_target = any(k in data and data.get(k) is not None for k in TARGET_KEYS)
    target = _resolve_target(data) if has_target else None
    with _lock:
        if target is not None and target != "unknown":
            _ALERT_COUNTS.pop(target, None)
            _SUPPRESSED_COUNTS.pop(target, None)
            _LAST_ALERT_AT.pop(target, None)
            _ACTIVE.pop(target, None)
            _LAST_VALUES.pop(target, None)
        else:
            _ALERT_COUNTS.clear()
            _SUPPRESSED_COUNTS.clear()
            _LAST_ALERT_AT.clear()
            _ACTIVE.clear()
            _TARGET_CONFIG.clear()
    return get_summary()


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Clear active alerts, history, and counts (keeps config)."""
    with _lock:
        _ACTIVE.clear()
        _HISTORY.clear()
        _LAST_ALERT_AT.clear()
        _ALERT_COUNTS.clear()
        _SUPPRESSED_COUNTS.clear()
        _LAST_VALUES.clear()
        global _LAST_CHECK_AT
        _LAST_CHECK_AT = None
    return {"ok": True, "success": True, "cleared": True}


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------


class BitrateAlertsService:
    """Object-oriented facade over the module-level bitrate-alert state."""

    def get_config(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_config(*a, **k)

    def configure(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return configure(*a, **k)

    def set_config(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return configure(*a, **k)

    def update_config(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return configure(*a, **k)

    def get_thresholds(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_thresholds(*a, **k)

    def set_thresholds(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return configure(*a, **k)

    def enable(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return enable(*a, **k)

    def disable(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return disable(*a, **k)

    def is_enabled(self, *a: Any, **k: Any) -> bool:
        return is_enabled(*a, **k)

    def evaluate(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return evaluate(*a, **k)

    def should_alert(self, *a: Any, **k: Any) -> bool:
        return should_alert(*a, **k)

    def is_throttled(self, *a: Any, **k: Any) -> bool:
        return is_throttled(*a, **k)

    def cooldown_remaining(self, *a: Any, **k: Any) -> float:
        return cooldown_remaining(*a, **k)

    def check(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return check(*a, **k)

    async def check_async(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return await check_async(*a, **k)

    def get_status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_status(*a, **k)

    def list_alerts(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_alerts(*a, **k)

    def get_alerts(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_alerts(*a, **k)

    def get_history(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return get_history(*a, **k)

    def get_summary(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_summary(*a, **k)

    def resolve(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return resolve(*a, **k)

    def reset(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return reset(*a, **k)

    def clear(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return clear(*a, **k)

    def set_notifier(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return set_notifier(*a, **k)


BitrateAlertService = BitrateAlertsService
BitrateWatchService = BitrateAlertsService
BitrateMonitorService = BitrateAlertsService
StreamBitrateAlertService = BitrateAlertsService

bitrate_alerts_service = BitrateAlertsService()
bitrate_alert_service = bitrate_alerts_service
service = bitrate_alerts_service
manager = bitrate_alerts_service
watcher = bitrate_alerts_service


def get_service() -> BitrateAlertsService:
    return bitrate_alerts_service


def create_service() -> BitrateAlertsService:
    return BitrateAlertsService()


# Aliases for flexible callers.
evaluate_thresholds = evaluate
check_thresholds = check
status = get_status
state = get_status
summary = get_summary
stats = get_summary
get_stats = get_summary
history = get_history
alerts = list_alerts
active_alerts = list_alerts
config = get_config
get_configuration = get_config
set_configuration = configure
mark_resolved = resolve
acknowledge = resolve
ack = resolve
reset_all = reset
clear_all = clear

__all__ = [
    "BitrateAlertsService",
    "bitrate_alerts_service",
    "service",
    "get_service",
    "create_service",
    "get_config",
    "configure",
    "enable",
    "disable",
    "is_enabled",
    "get_thresholds",
    "set_thresholds",
    "evaluate",
    "should_alert",
    "is_throttled",
    "cooldown_remaining",
    "check",
    "check_async",
    "get_status",
    "list_alerts",
    "get_history",
    "get_summary",
    "resolve",
    "reset",
    "clear",
    "DEFAULT_MIN_BITRATE_BPS",
    "DEFAULT_MAX_BITRATE_BPS",
    "DEFAULT_COOLDOWN_SECONDS",
]
