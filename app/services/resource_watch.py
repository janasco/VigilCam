"""Resource watcher (threshold alerting) service for VigilCam.

Complements :mod:`app.services.system_metrics` (which only reads current
CPU / memory / disk values and has NO threshold or alerting logic) with
configurable threshold evaluation, cooldown-throttled alerting, and
in-memory alert history.

Resources watched: ``cpu``, ``memory``, ``disk`` (percent-used values).
All state is in-memory and dependency-light so it can be called from health
checks, the watchdog, or API handlers.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Tunables / defaults
# ---------------------------------------------------------------------------

DEFAULT_CPU_THRESHOLD = 85.0
DEFAULT_MEMORY_THRESHOLD = 90.0
DEFAULT_DISK_THRESHOLD = 90.0
DEFAULT_COOLDOWN_SECONDS = 300.0
ENABLED_BY_DEFAULT = True

MIN_THRESHOLD = 0.0
MAX_THRESHOLD = 100.0
MIN_COOLDOWN_SECONDS = 0.0
MAX_COOLDOWN_SECONDS = 86400.0 * 7

MAX_HISTORY = 200

RESOURCES = ("cpu", "memory", "disk")

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

_lock = threading.RLock()

_CONFIG: Dict[str, Any] = {
    "enabled": bool(ENABLED_BY_DEFAULT),
    "cpu_threshold": float(DEFAULT_CPU_THRESHOLD),
    "memory_threshold": float(DEFAULT_MEMORY_THRESHOLD),
    "disk_threshold": float(DEFAULT_DISK_THRESHOLD),
    "cooldown_seconds": float(DEFAULT_COOLDOWN_SECONDS),
}

# resource -> datetime of last emitted alert
_LAST_ALERT_AT: Dict[str, datetime] = {}
# resource -> counts
_ALERT_COUNTS: Dict[str, int] = {r: 0 for r in RESOURCES}
_SUPPRESSED_COUNTS: Dict[str, int] = {r: 0 for r in RESOURCES}
# resource -> active alert record dict
_ACTIVE: Dict[str, Dict[str, Any]] = {}
# bounded history of emitted (non-throttled) alerts
_HISTORY: List[Dict[str, Any]] = []
# last evaluated values
_LAST_VALUES: Dict[str, Optional[float]] = {"cpu": None, "memory": None, "disk": None}
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


def _clamp_threshold(value: Any, fallback: float) -> float:
    v = _to_float(value)
    if v is None:
        return float(fallback)
    if v < MIN_THRESHOLD:
        return float(MIN_THRESHOLD)
    if v > MAX_THRESHOLD:
        return float(MAX_THRESHOLD)
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


CPU_KEYS = ("cpu", "cpu_percent", "cpu_usage", "cpu_usage_percent", "cpu_pct", "cpu_load_percent")
MEMORY_KEYS = ("memory", "memory_percent", "memory_usage", "memory_usage_percent", "mem",
               "mem_percent", "mem_usage", "memory_pct", "ram", "ram_percent")
DISK_KEYS = ("disk", "disk_percent", "disk_usage", "disk_usage_percent", "disk_pct",
             "storage", "storage_percent", "disk_percent_used")
CPU_THRESHOLD_KEYS = ("cpu_threshold", "cpu_percent_threshold", "cpu_limit", "cpu_max",
                      "cpu_alert_at", "cpu_alert_threshold", "cpu_threshold_percent")
MEMORY_THRESHOLD_KEYS = ("memory_threshold", "memory_percent_threshold", "mem_threshold",
                         "mem_percent_threshold", "memory_limit", "memory_max",
                         "memory_alert_at", "ram_threshold")
DISK_THRESHOLD_KEYS = ("disk_threshold", "disk_percent_threshold", "disk_limit", "disk_max",
                       "disk_alert_at", "storage_threshold", "disk_threshold_percent")
COOLDOWN_KEYS = ("cooldown", "cooldown_seconds", "cooldown_secs", "interval",
                 "interval_seconds", "delay", "delay_seconds")
FORCE_KEYS = ("force", "forced", "bypass", "bypass_throttle", "ignore_cooldown", "ignore_throttle")
NOW_KEYS = ("now", "at", "when", "timestamp", "ts", "time")


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
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
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request",
                       "data", "body", "alert", "settings", "thresholds", "values"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    # case-insensitive alias: also expose lowercased keys
    lower = {str(k).lower(): v for k, v in data.items()}
    for k, v in lower.items():
        data.setdefault(k, v)
    return data


def _resolve_resource_value(data: Dict[str, Any], keys: tuple) -> Optional[float]:
    for k in keys:
        if k in data and data.get(k) is not None:
            v = _to_float(data.get(k))
            if v is not None:
                return v
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            v = _to_float(lower[k.lower()])
            if v is not None:
                return v
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


def _thresholds_snapshot() -> Dict[str, float]:
    with _lock:
        return {
            "cpu": float(_CONFIG.get("cpu_threshold", DEFAULT_CPU_THRESHOLD)),
            "memory": float(_CONFIG.get("memory_threshold", DEFAULT_MEMORY_THRESHOLD)),
            "disk": float(_CONFIG.get("disk_threshold", DEFAULT_DISK_THRESHOLD)),
        }


def _values_from_metrics(metrics: Any) -> Dict[str, Optional[float]]:
    """Extract cpu/memory/disk percents from a system_metrics payload."""
    out: Dict[str, Optional[float]] = {"cpu": None, "memory": None, "disk": None}
    if not isinstance(metrics, dict):
        return out
    try:
        cpu = metrics.get("cpu")
        if isinstance(cpu, dict):
            for k in ("percent", "usage_percent", "cpu_percent"):
                if cpu.get(k) is not None:
                    v = _to_float(cpu.get(k))
                    if v is not None:
                        out["cpu"] = v
                        break
        if out["cpu"] is None:
            for k in ("cpu_percent", "cpu_usage", "cpu"):
                if metrics.get(k) is not None and not isinstance(metrics.get(k), dict):
                    v = _to_float(metrics.get(k))
                    if v is not None:
                        out["cpu"] = v
                        break
        mem = metrics.get("memory", metrics.get("mem"))
        if isinstance(mem, dict):
            for k in ("percent", "usage_percent", "percent_used"):
                if mem.get(k) is not None:
                    v = _to_float(mem.get(k))
                    if v is not None:
                        out["memory"] = v
                        break
        if out["memory"] is None:
            for k in ("memory_percent", "memory_usage", "mem_percent"):
                if metrics.get(k) is not None and not isinstance(metrics.get(k), dict):
                    v = _to_float(metrics.get(k))
                    if v is not None:
                        out["memory"] = v
                        break
        disk = metrics.get("disk")
        if isinstance(disk, dict):
            for k in ("percent", "usage_percent", "percent_used"):
                if disk.get(k) is not None:
                    v = _to_float(disk.get(k))
                    if v is not None:
                        out["disk"] = v
                        break
        if out["disk"] is None:
            for k in ("disk_percent", "disk_usage"):
                if metrics.get(k) is not None and not isinstance(metrics.get(k), dict):
                    v = _to_float(metrics.get(k))
                    if v is not None:
                        out["disk"] = v
                        break
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def get_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        cfg = dict(_CONFIG)
    out: Dict[str, Any] = {
        "ok": True,
        "success": True,
        "enabled": bool(cfg.get("enabled", True)),
        "cpu_threshold": float(cfg.get("cpu_threshold", DEFAULT_CPU_THRESHOLD)),
        "memory_threshold": float(cfg.get("memory_threshold", DEFAULT_MEMORY_THRESHOLD)),
        "disk_threshold": float(cfg.get("disk_threshold", DEFAULT_DISK_THRESHOLD)),
        "cooldown_seconds": float(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
    }
    # Aliases for flexible callers.
    out["cpu_percent_threshold"] = out["cpu_threshold"]
    out["memory_percent_threshold"] = out["memory_threshold"]
    out["mem_threshold"] = out["memory_threshold"]
    out["disk_percent_threshold"] = out["disk_threshold"]
    out["cooldown"] = out["cooldown_seconds"]
    out["interval_seconds"] = out["cooldown_seconds"]
    out["thresholds"] = {
        "cpu": out["cpu_threshold"],
        "memory": out["memory_threshold"],
        "disk": out["disk_threshold"],
    }
    return out


def get_thresholds(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    cfg = get_config(*args, **kwargs)
    return {
        "ok": True,
        "success": True,
        "cpu": cfg["cpu_threshold"],
        "memory": cfg["memory_threshold"],
        "disk": cfg["disk_threshold"],
        "cpu_threshold": cfg["cpu_threshold"],
        "memory_threshold": cfg["memory_threshold"],
        "disk_threshold": cfg["disk_threshold"],
        "thresholds": cfg["thresholds"],
    }


def configure(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    with _lock:
        if "enabled" in data and data["enabled"] is not None:
            try:
                _CONFIG["enabled"] = bool(data["enabled"])
            except Exception:  # noqa: BLE001
                pass
        for key in CPU_THRESHOLD_KEYS:
            if key in data and data[key] is not None:
                _CONFIG["cpu_threshold"] = _clamp_threshold(data[key], _CONFIG["cpu_threshold"])
                break
        for key in MEMORY_THRESHOLD_KEYS:
            if key in data and data[key] is not None:
                _CONFIG["memory_threshold"] = _clamp_threshold(data[key], _CONFIG["memory_threshold"])
                break
        for key in DISK_THRESHOLD_KEYS:
            if key in data and data[key] is not None:
                _CONFIG["disk_threshold"] = _clamp_threshold(data[key], _CONFIG["disk_threshold"])
                break
        # Generic "threshold" applies to all three when no specific key given.
        specific = any(k in data for k in (*CPU_THRESHOLD_KEYS, *MEMORY_THRESHOLD_KEYS, *DISK_THRESHOLD_KEYS))
        if not specific and "threshold" in data and data["threshold"] is not None:
            v = _clamp_threshold(data["threshold"], DEFAULT_CPU_THRESHOLD)
            _CONFIG["cpu_threshold"] = v
            _CONFIG["memory_threshold"] = v
            _CONFIG["disk_threshold"] = v
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


def _run_notifier(resource: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        cb = _NOTIFIER
    if cb is None or not callable(cb):
        return {"notified": False, "reason": "no-notifier"}
    try:
        result = cb(resource, payload)
        if isinstance(result, dict):
            out = dict(result)
            out.setdefault("notified", bool(out.get("ok", True)))
            return out
        return {"notified": bool(result) if isinstance(result, bool) else True, "result": result}
    except Exception as exc:  # noqa: BLE001
        return {"notified": False, "error": str(exc)}


def _publish_event(resource: str, value: Optional[float], threshold: float, message: str) -> bool:
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
            payload = {"resource": resource, "value": value,
                       "threshold": threshold, "message": message}
            if loop is not None and loop.is_running():
                loop.create_task(_bus.publish("resource_alert", payload))
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

    Accepts either explicit ``cpu`` / ``memory`` / ``disk`` percent values or a
    ``metrics`` dict shaped like :func:`collect_system_metrics` output.
    """
    data = _normalize_inputs(*args, **kwargs)
    metrics = data.get("metrics", data.get("system_metrics", data.get("system")))
    values: Dict[str, Optional[float]] = {"cpu": None, "memory": None, "disk": None}
    if isinstance(metrics, dict):
        values.update({k: v for k, v in _values_from_metrics(metrics).items() if v is not None})
    cpu_v = _resolve_resource_value(data, CPU_KEYS)
    if cpu_v is not None:
        values["cpu"] = cpu_v
    mem_v = _resolve_resource_value(data, MEMORY_KEYS)
    if mem_v is not None:
        values["memory"] = mem_v
    disk_v = _resolve_resource_value(data, DISK_KEYS)
    if disk_v is not None:
        values["disk"] = disk_v

    thresholds = _thresholds_snapshot()
    # Per-call threshold overrides (do not persist).
    for keys, res in ((CPU_THRESHOLD_KEYS, "cpu"), (MEMORY_THRESHOLD_KEYS, "memory"), (DISK_THRESHOLD_KEYS, "disk")):
        for k in keys:
            if k in data and data[k] is not None:
                thresholds[res] = _clamp_threshold(data[k], thresholds[res])
                break

    alerts: List[Dict[str, Any]] = []
    for res in RESOURCES:
        val = values.get(res)
        thr = thresholds.get(res)
        breached = val is not None and thr is not None and float(val) >= float(thr)
        alerts.append({
            "resource": res,
            "value": val,
            "threshold": thr,
            "breached": bool(breached),
            "severity": "critical" if breached else "ok",
            "message": (f"{res} usage {val}% >= threshold {thr}%" if breached
                        else f"{res} usage {val}% below threshold {thr}%") if val is not None
            else f"{res} value unknown (threshold {thr}%)",
        })
    breached_any = any(a["breached"] for a in alerts)
    return {
        "ok": True,
        "success": True,
        "breached": breached_any,
        "alerted": breached_any,
        "alerts": alerts,
        "breaches": [a for a in alerts if a["breached"]],
        "values": dict(values),
        "thresholds": dict(thresholds),
    }


def should_alert(*args: Any, **kwargs: Any) -> bool:
    """True when a threshold breach exists and cooldown allows a new alert."""
    data = _normalize_inputs(*args, **kwargs)
    resource = str(data.get("resource", data.get("name", "any"))).strip().lower() or "any"
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
        targets = [resource] if resource in RESOURCES else [a["resource"] for a in ev["breaches"]]
        for res in targets:
            last = _LAST_ALERT_AT.get(res)
            if last is None:
                return True
            try:
                if (now_dt - last).total_seconds() >= cooldown:
                    return True
            except Exception:  # noqa: BLE001
                return True
        return False


def is_throttled(*args: Any, **kwargs: Any) -> bool:
    data = _normalize_inputs(*args, **kwargs)
    ev = evaluate(**data)
    if not ev.get("breached"):
        return False
    return not should_alert(**data)


def cooldown_remaining(*args: Any, **kwargs: Any) -> float:
    data = _normalize_inputs(*args, **kwargs)
    resource = str(data.get("resource", data.get("name", ""))).strip().lower()
    now_dt = _resolve_now(data)
    with _lock:
        cooldown = float(_CONFIG.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))
        if resource in RESOURCES:
            last = _LAST_ALERT_AT.get(resource)
            if last is None:
                return 0.0
            try:
                return max(0.0, cooldown - (now_dt - last).total_seconds())
            except Exception:  # noqa: BLE001
                return 0.0
        # "any": max remaining across breached resources
        remaining = 0.0
        for res in RESOURCES:
            last = _LAST_ALERT_AT.get(res)
            if last is None:
                continue
            try:
                remaining = max(remaining, cooldown - (now_dt - last).total_seconds())
            except Exception:  # noqa: BLE001
                continue
        return max(0.0, remaining)


def check(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Evaluate thresholds, apply cooldown, and record alerts.

    Accepts explicit values (``cpu`` / ``memory`` / ``disk``), a ``metrics``
    dict, plus optional ``force`` and ``now``. Returns a summary dict.
    """
    data = _normalize_inputs(*args, **kwargs)
    now_dt = _resolve_now(data)
    force = _resolve_force(data)
    with _lock:
        enabled = bool(_CONFIG.get("enabled", True))
        cooldown = float(_CONFIG.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))

    ev = evaluate(**data)
    values = ev["values"]
    thresholds = ev["thresholds"]

    with _lock:
        global _LAST_CHECK_AT
        _LAST_VALUES.update({k: values.get(k) for k in RESOURCES})
        _LAST_CHECK_AT = now_dt

    if not enabled and not force:
        return {
            "ok": True, "success": True,
            "breached": bool(ev["breached"]),
            "alerted": False, "sent": False,
            "throttled": False, "disabled": True,
            "alerts": ev["alerts"],
            "values": values, "thresholds": thresholds,
            "at": now_dt.isoformat(),
            "detail": "resource watcher disabled",
        }

    new_alerts: List[Dict[str, Any]] = []
    throttled: List[Dict[str, Any]] = []
    for breach in ev.get("breaches", []):
        res = breach["resource"]
        val = breach["value"]
        thr = breach["threshold"]
        with _lock:
            last = _LAST_ALERT_AT.get(res)
            elapsed = None
            if last is not None:
                try:
                    elapsed = (now_dt - last).total_seconds()
                except Exception:  # noqa: BLE001
                    elapsed = None
            allowed = force or last is None or (elapsed is not None and elapsed >= cooldown)
            if not allowed:
                _SUPPRESSED_COUNTS[res] = int(_SUPPRESSED_COUNTS.get(res, 0) or 0) + 1
                remaining = max(0.0, cooldown - (elapsed or 0.0))
                throttled.append({"resource": res, "value": val, "threshold": thr,
                                  "remaining_seconds": remaining})
                continue
            _LAST_ALERT_AT[res] = now_dt
            _ALERT_COUNTS[res] = int(_ALERT_COUNTS.get(res, 0) or 0) + 1
            record = {
                "resource": res,
                "value": val,
                "threshold": thr,
                "at": now_dt.isoformat(),
                "alert_count": int(_ALERT_COUNTS.get(res, 0)),
                "message": f"{res} usage {val}% >= threshold {thr}%",
            }
            _ACTIVE[res] = dict(record)
            _HISTORY.append(dict(record))
            while len(_HISTORY) > MAX_HISTORY:
                _HISTORY.pop(0)
        if res in (_ACTIVE if 'res' in dir() else {}):
            pass
        # publish + notifier outside lock
        message = f"{res} usage {val}% >= threshold {thr}%"
        event_published = _publish_event(res, val, thr, message)
        notify_result = _run_notifier(res, {"resource": res, "value": val,
                                           "threshold": thr, "message": message,
                                           "at": now_dt.isoformat()})
        record_out = dict(_ACTIVE.get(res, {}))
        record_out["event_published"] = bool(event_published)
        record_out["notify"] = notify_result
        new_alerts.append(record_out)

    alerted = len(new_alerts) > 0
    return {
        "ok": True, "success": True,
        "breached": bool(ev["breached"]),
        "alerted": alerted,
        "sent": alerted,
        "throttled": len(throttled) > 0,
        "alerts": ev["alerts"],
        "new_alerts": new_alerts,
        "emitted": new_alerts,
        "suppressed": throttled,
        "throttled_alerts": throttled,
        "active": [dict(_ACTIVE[k]) for k in sorted(_ACTIVE.keys())],
        "values": values,
        "thresholds": thresholds,
        "at": now_dt.isoformat(),
        "cooldown_seconds": float(cooldown),
        "remaining_seconds": max([float(t.get("remaining_seconds", 0.0)) for t in throttled], default=0.0),
    }


async def check_async(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Async check: collects live metrics unless explicit values provided."""
    data = _normalize_inputs(*args, **kwargs)
    has_explicit = any(
        _resolve_resource_value(data, keys) is not None
        for keys in (CPU_KEYS, MEMORY_KEYS, DISK_KEYS)
    ) or isinstance(data.get("metrics"), dict)
    if has_explicit:
        return check(**data)
    try:
        from app.services.system_metrics import collect_system_metrics

        metrics = await collect_system_metrics()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "success": False, "breached": False,
                "alerted": False, "error": str(exc), "values": {},
                "thresholds": _thresholds_snapshot()}
    merged = dict(data)
    merged["metrics"] = metrics
    return check(**merged)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Current watcher status: last values, thresholds, active alerts."""
    data = _normalize_inputs(*args, **kwargs)
    resource = str(data.get("resource", data.get("name", ""))).strip().lower() or None
    with _lock:
        active = [dict(_ACTIVE[k]) for k in sorted(_ACTIVE.keys())]
        values = dict(_LAST_VALUES)
        last_check = _LAST_CHECK_AT.isoformat() if _LAST_CHECK_AT else None
        cfg = dict(_CONFIG)
        counts = {r: int(_ALERT_COUNTS.get(r, 0)) for r in RESOURCES}
        suppressed = {r: int(_SUPPRESSED_COUNTS.get(r, 0)) for r in RESOURCES}
    ev = evaluate(memory=values.get("memory"), cpu=values.get("cpu"), disk=values.get("disk"))
    out: Dict[str, Any] = {
        "ok": True, "success": True,
        "enabled": bool(cfg.get("enabled", True)),
        "values": values,
        "thresholds": {"cpu": float(cfg.get("cpu_threshold", DEFAULT_CPU_THRESHOLD)),
                       "memory": float(cfg.get("memory_threshold", DEFAULT_MEMORY_THRESHOLD)),
                       "disk": float(cfg.get("disk_threshold", DEFAULT_DISK_THRESHOLD))},
        "active": active,
        "alerts": active,
        "alert_counts": counts,
        "suppressed_counts": suppressed,
        "breached": bool(ev.get("breached")) if any(v is not None for v in values.values()) else any(a["resource"] in _ACTIVE for a in active),
        "last_check_at": last_check,
    }
    if resource in RESOURCES:
        out["resource"] = resource
        out["resource_alert"] = dict(_ACTIVE.get(resource)) if resource in _ACTIVE else None
        out["alert_count"] = counts.get(resource, 0)
    return out


def list_alerts(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    with _lock:
        return [dict(_ACTIVE[k]) for k in sorted(_ACTIVE.keys())]


def get_alerts(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_alerts(*args, **kwargs)


def get_history(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    data = _normalize_inputs(*args, **kwargs)
    limit = _to_float(data.get("limit", data.get("n", data.get("count"))))
    with _lock:
        hist = list(_HISTORY)
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
    return {
        "ok": True, "success": True,
        "enabled": bool(cfg.get("enabled", True)),
        "active_count": len(active),
        "active": active,
        "alerts": active,
        "total_alerts": total_alerts,
        "alert_count": total_alerts,
        "suppressed_count": total_suppressed,
        "suppressed": total_suppressed,
        "alert_counts": {r: int(_ALERT_COUNTS.get(r, 0)) for r in RESOURCES},
        "thresholds": {"cpu": float(cfg.get("cpu_threshold", DEFAULT_CPU_THRESHOLD)),
                       "memory": float(cfg.get("memory_threshold", DEFAULT_MEMORY_THRESHOLD)),
                       "disk": float(cfg.get("disk_threshold", DEFAULT_DISK_THRESHOLD))},
        "cooldown_seconds": float(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
        "values": values,
        "last_check_at": last_check,
    }


def resolve(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Clear active alert(s). No resource = clear all."""
    data = _normalize_inputs(*args, **kwargs)
    resource = str(data.get("resource", data.get("name", ""))).strip().lower() or None
    with _lock:
        if resource in RESOURCES:
            removed = _ACTIVE.pop(resource, None)
            return {"ok": True, "success": True, "resolved": removed is not None,
                    "resource": resource, "active": [dict(_ACTIVE[k]) for k in sorted(_ACTIVE.keys())]}
        _ACTIVE.clear()
        return {"ok": True, "success": True, "resolved": True,
                "active": [], "detail": "all resource alerts resolved"}


def reset(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    resource = str(data.get("resource", data.get("name", ""))).strip().lower() or None
    with _lock:
        if resource in RESOURCES:
            _ALERT_COUNTS[resource] = 0
            _SUPPRESSED_COUNTS[resource] = 0
            _LAST_ALERT_AT.pop(resource, None)
            _ACTIVE.pop(resource, None)
        else:
            for r in RESOURCES:
                _ALERT_COUNTS[r] = 0
                _SUPPRESSED_COUNTS[r] = 0
            _LAST_ALERT_AT.clear()
            _ACTIVE.clear()
    return get_summary()


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Clear active alerts, history, and counts (keeps config)."""
    with _lock:
        _ACTIVE.clear()
        _HISTORY.clear()
        _LAST_ALERT_AT.clear()
        for r in RESOURCES:
            _ALERT_COUNTS[r] = 0
            _SUPPRESSED_COUNTS[r] = 0
        _LAST_VALUES.update({"cpu": None, "memory": None, "disk": None})
        global _LAST_CHECK_AT
        _LAST_CHECK_AT = None
    return {"ok": True, "success": True, "cleared": True}


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------


class ResourceWatchService:
    """Object-oriented facade over the module-level resource-watch state."""

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


ResourceWatcherService = ResourceWatchService
ResourceWatchManager = ResourceWatchService
ResourceMonitorService = ResourceWatchService

resource_watch_service = ResourceWatchService()
resource_watcher = resource_watch_service
service = resource_watch_service
manager = resource_watch_service
watcher = resource_watch_service


def get_service() -> ResourceWatchService:
    return resource_watch_service


def create_service() -> ResourceWatchService:
    return ResourceWatchService()


# Aliases for flexible callers.
evaluate_thresholds = evaluate
evaluate_resources = evaluate
check_resources = check
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
    "ResourceWatchService",
    "resource_watch_service",
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
    "DEFAULT_CPU_THRESHOLD",
    "DEFAULT_MEMORY_THRESHOLD",
    "DEFAULT_DISK_THRESHOLD",
    "DEFAULT_COOLDOWN_SECONDS",
]
