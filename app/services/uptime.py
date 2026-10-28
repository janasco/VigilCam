"""Camera uptime stats for VigilCam.

Tracks per-camera online/offline transitions in-memory and computes
windowed uptime statistics (online seconds + historical % online).

This is intentionally separate from ``app.services.activity`` which is
recent-window only (``active_camera_ids`` / ``get_active_cameras`` return
cameras active within the last N seconds with no historical percentage).

No ``app/main.py`` edit is required: ``app.api.uptime`` is auto-discovered
by the router loader in ``app.main``.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# Default window for uptime stats (24h).
DEFAULT_WINDOW_SECONDS = 86400.0

# In-memory transition log: camera_id -> [(timestamp, online)] sorted.
_TRANSITIONS: Dict[int, List[Tuple[datetime, bool]]] = {}
_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.utcnow()


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


def _to_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
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


def _to_window(value: Any, default: float = DEFAULT_WINDOW_SECONDS) -> float:
    if value is None:
        return float(default)
    if isinstance(value, timedelta):
        return float(value.total_seconds())
    try:
        w = float(value)
    except (TypeError, ValueError):
        return float(default)
    if w < 0:
        return 0.0
    return w


def _current_online_from_health(camera_id: int) -> Optional[bool]:
    try:
        from app.services import camera_health as _ch

        st = _ch.get_health(camera_id, create=False)
        if st is None:
            return None
        try:
            _ch.evaluate(st)
        except Exception:  # noqa: BLE001
            pass
        return bool(getattr(st, "online", False))
    except Exception:  # noqa: BLE001
        return None


# ---------------- recording ----------------

def record_transition(camera_id: Any = None, online: Any = False, when: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Append an online/offline transition for *camera_id*."""
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("id") or kwargs.get("cam_id")
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        raise ValueError("camera_id is required")
    if "is_online" in kwargs and "online" not in kwargs:
        online = kwargs.get("is_online")
    if "status" in kwargs and "online" not in kwargs and "is_online" not in kwargs:
        online = str(kwargs.get("status")).strip().lower() in ("online", "healthy", "up", "true", "1", "yes")
    flag = bool(online) if not isinstance(online, str) else online.strip().lower() in (
        "online", "healthy", "up", "true", "1", "yes", "on",
    )
    dt = _to_datetime(when) if when is not None else None
    if dt is None:
        for key in ("ts", "timestamp", "at", "time"):
            if kwargs.get(key) is not None:
                dt = _to_datetime(kwargs.get(key))
                if dt is not None:
                    break
    if dt is None:
        dt = _now()
    with _LOCK:
        log = _TRANSITIONS.setdefault(cid, [])
        # Avoid duplicate consecutive identical states at same-ish time? Keep all,
        # but collapse exact duplicate consecutive flags to keep log small.
        if log and log[-1][1] == flag:
            # Still update timestamp so "last seen" advances; replace if newer.
            if dt >= log[-1][0]:
                log[-1] = (dt, flag)
            else:
                log.append((dt, flag))
                log.sort(key=lambda e: e[0])
        else:
            log.append((dt, flag))
            log.sort(key=lambda e: e[0])
        # Cap log length to avoid unbounded growth (keep last 10k).
        if len(log) > 10000:
            _TRANSITIONS[cid] = log[-10000:]
    return {
        "camera_id": cid,
        "online": flag,
        "status": "online" if flag else "offline",
        "at": dt.isoformat(),
    }


def mark_online(camera_id: Any = None, when: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return record_transition(camera_id, True, when, **kwargs)


def mark_offline(camera_id: Any = None, when: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return record_transition(camera_id, False, when, **kwargs)


def mark_frame_received(camera_id: Any = None, when: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """A received frame implies the camera is online."""
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("id") or kwargs.get("stream_id")
    return record_transition(camera_id, True, when, **kwargs)


def record_online(*a: Any, **kw: Any) -> Dict[str, Any]:
    return mark_online(*a, **kw)


def record_offline(*a: Any, **kw: Any) -> Dict[str, Any]:
    return mark_offline(*a, **kw)


def record_state(*a: Any, **kw: Any) -> Dict[str, Any]:
    return record_transition(*a, **kw)


def set_online(*a: Any, **kw: Any) -> Dict[str, Any]:
    return mark_online(*a, **kw)


def set_offline(*a: Any, **kw: Any) -> Dict[str, Any]:
    return mark_offline(*a, **kw)


# ---------------- computation ----------------

def get_uptime(camera_id: Any = None, window_seconds: Any = None, now: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Compute windowed uptime stats for one camera.

    Returns dict with ``online_seconds``, ``offline_seconds``,
    ``uptime_percent`` (historical % online over the window), current
    ``online`` flag, transition count and last timestamps.
    """
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("id")
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        raise ValueError("camera_id is required")
    # Resolve window aliases.
    window = window_seconds
    if window is None:
        for key in ("window", "seconds", "within", "within_seconds", "duration", "period", "period_seconds"):
            if kwargs.get(key) is not None:
                window = kwargs.get(key)
                break
    window_f = _to_window(window)
    now_dt = _to_datetime(now) if now is not None else _now()
    if now_dt is None:
        now_dt = _now()
    cutoff = now_dt - timedelta(seconds=window_f) if window_f > 0 else now_dt

    with _LOCK:
        log = list(_TRANSITIONS.get(cid, []))
    log = sorted(log, key=lambda e: e[0])

    # State at cutoff: last transition at/before cutoff, else offline.
    current: Optional[bool] = None
    if log:
        current = log[-1][1]
    else:
        current = _current_online_from_health(cid)
    if current is None:
        current = False

    prev_online = False
    for ts, flag in log:
        if ts <= cutoff:
            prev_online = bool(flag)
        else:
            break

    online_seconds = 0.0
    prev_time = cutoff
    in_window = [(ts, flag) for ts, flag in log if ts > cutoff and ts <= now_dt]
    # Transitions in the future (clock skew) are ignored for accumulation
    # but still count toward "current" state.
    for ts, flag in in_window:
        try:
            delta = (ts - prev_time).total_seconds()
        except Exception:  # noqa: BLE001
            delta = 0.0
        if delta < 0:
            delta = 0.0
        if prev_online:
            online_seconds += delta
        prev_time = ts
        prev_online = bool(flag)
    try:
        tail = (now_dt - prev_time).total_seconds()
    except Exception:  # noqa: BLE001
        tail = 0.0
    if tail < 0:
        tail = 0.0
    if prev_online:
        online_seconds += tail

    total = float(window_f)
    if total > 0:
        online_seconds = max(0.0, min(online_seconds, total))
        offline_seconds = max(0.0, total - online_seconds)
        uptime_percent = round(online_seconds / total * 100.0, 2)
    else:
        offline_seconds = 0.0
        uptime_percent = 100.0 if current else 0.0

    last_online_at: Optional[datetime] = None
    last_offline_at: Optional[datetime] = None
    for ts, flag in log:
        if flag:
            last_online_at = ts if last_online_at is None or ts > last_online_at else last_online_at
        else:
            last_offline_at = ts if last_offline_at is None or ts > last_offline_at else last_offline_at

    # If no local transitions, fall back to health timestamps for last-seen.
    last_seen: Optional[datetime] = None
    if log:
        last_seen = log[-1][0]
    else:
        try:
            from app.services import camera_health as _ch

            st = _ch.get_health(cid, create=False)
            if st is not None:
                last_seen = getattr(st, "last_frame", None) or getattr(st, "last_seen", None)
        except Exception:  # noqa: BLE001
            pass

    def _iso(v: Any) -> Any:
        return v.isoformat() if isinstance(v, datetime) else v

    return {
        "camera_id": cid,
        "id": cid,
        "window_seconds": float(window_f),
        "window": float(window_f),
        "online": bool(current),
        "status": "online" if current else "offline",
        "healthy": bool(current),
        "online_seconds": round(float(online_seconds), 2),
        "offline_seconds": round(float(offline_seconds), 2),
        "uptime_seconds": round(float(online_seconds), 2),
        "downtime_seconds": round(float(offline_seconds), 2),
        "uptime_percent": float(uptime_percent),
        "uptime_pct": float(uptime_percent),
        "percent_online": float(uptime_percent),
        "availability_percent": float(uptime_percent),
        "transitions": len(log),
        "transition_count": len(log),
        "last_online_at": _iso(last_online_at),
        "last_offline_at": _iso(last_offline_at),
        "last_seen": _iso(last_seen),
        "last_seen_at": _iso(last_seen),
        "window_start": cutoff.isoformat() if isinstance(cutoff, datetime) else None,
        "window_end": now_dt.isoformat() if isinstance(now_dt, datetime) else None,
    }


def list_uptime(window_seconds: Any = None, now: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    """Uptime stats for all known cameras (transitions + health registry)."""
    window = window_seconds
    if window is None:
        for key in ("window", "seconds", "within", "duration", "period"):
            if kwargs.get(key) is not None:
                window = kwargs.get(key)
                break
    window_f = _to_window(window)
    with _LOCK:
        ids = set(_TRANSITIONS.keys())
    try:
        from app.services import camera_health as _ch

        for st in _ch.list_health():
            try:
                cid = _coerce_camera_id(getattr(st, "camera_id", None))
            except Exception:  # noqa: BLE001
                cid = None
            if cid is not None:
                ids.add(cid)
    except Exception:  # noqa: BLE001
        pass
    return [get_uptime(cid, window_seconds=window_f, now=now) for cid in sorted(ids)]


def get_summary(window_seconds: Any = None, now: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Aggregate uptime summary across known cameras."""
    window = window_seconds
    if window is None:
        for key in ("window", "seconds", "within", "duration", "period"):
            if kwargs.get(key) is not None:
                window = kwargs.get(key)
                break
    window_f = _to_window(window)
    rows = list_uptime(window_seconds=window_f, now=now)
    total = len(rows)
    online = sum(1 for r in rows if r.get("online"))
    offline = total - online
    if rows:
        avg = round(sum(float(r.get("uptime_percent", 0.0)) for r in rows) / len(rows), 2)
        lo = min(float(r.get("uptime_percent", 0.0)) for r in rows)
        hi = max(float(r.get("uptime_percent", 0.0)) for r in rows)
    else:
        avg, lo, hi = 0.0, 0.0, 0.0
    return {
        "total": total,
        "cameras": total,
        "online": online,
        "offline": offline,
        "healthy": online,
        "window_seconds": float(window_f),
        "window": float(window_f),
        "avg_uptime_percent": float(avg),
        "average_uptime_percent": float(avg),
        "uptime_percent_avg": float(avg),
        "min_uptime_percent": float(lo),
        "max_uptime_percent": float(hi),
    }


def get_config() -> Dict[str, Any]:
    return {"default_window_seconds": float(DEFAULT_WINDOW_SECONDS)}


def remove(camera_id: Any = None, **kwargs: Any) -> bool:
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("id")
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        return False
    with _LOCK:
        return _TRANSITIONS.pop(cid, None) is not None


def clear(camera_id: Any = None, **kwargs: Any) -> None:
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("id")
    with _LOCK:
        if camera_id is None:
            _TRANSITIONS.clear()
            return
        cid = _coerce_camera_id(camera_id)
        if cid is None:
            _TRANSITIONS.clear()
            return
        _TRANSITIONS.pop(cid, None)


# Compatibility aliases.
get_camera_uptime = get_uptime
camera_uptime = get_uptime
uptime_for = get_uptime
uptime_for_camera = get_uptime
get_camera_uptime_stats = get_uptime
all_uptime = list_uptime
list_all = list_uptime
summary = get_summary
stats = get_summary
config = get_config
delete = remove
drop = remove
reset = clear

__all__ = [
    "DEFAULT_WINDOW_SECONDS",
    "record_transition",
    "mark_online",
    "mark_offline",
    "mark_frame_received",
    "get_uptime",
    "list_uptime",
    "get_summary",
    "get_config",
    "remove",
    "clear",
]
