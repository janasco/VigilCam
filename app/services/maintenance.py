"""Maintenance windows service for VigilCam.

Scheduled downtime periods during which alerts are intentionally suppressed
(e.g. cleaning crew on site, firmware upgrade, construction work).

How this differs from neighbouring services (do NOT merge):
* ``alert_throttle`` is **rate-based**: cooldown seconds + sliding-window
  caps that limit alert frequency. It has no notion of calendar time.
* ``retention`` **schedules recordings**: max-age / max-size pruning of
  stored MP4 segments. It never suppresses alerts.
* This module is **calendar-based alert suppression**: explicit
  ``[start, end]`` time windows bound to one camera (or global ``"*"``)
  during which alert senders should stay quiet. ``is_suppressed`` is the
  single gate alert paths consult.

All state is in-memory, thread-safe, and dependency-free.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


_WINDOWS: Dict[str, Dict[str, Any]] = {}

_lock = threading.RLock()

CAMERA_KEYS = ("camera_id", "camera", "cam", "cam_id", "device_id", "source_id", "id", "name")
START_KEYS = ("start", "start_time", "start_at", "from", "from_time", "begin", "begin_at", "since")
END_KEYS = ("end", "end_time", "end_at", "to", "until", "finish", "finish_at", "stop", "stop_at")
REASON_KEYS = ("reason", "note", "notes", "description", "comment", "label", "title")
ENABLED_KEYS = ("enabled", "active", "is_enabled", "on")
ID_KEYS = ("id", "window_id", "maintenance_id", "uuid")
NOW_KEYS = ("now", "at", "when", "timestamp", "ts", "time")
DURATION_KEYS = ("duration", "duration_seconds", "duration_secs", "seconds", "length_seconds")


def _now_ts() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_ts(value: Any) -> Optional[float]:
    """Parse datetime / epoch seconds-or-ms / ISO string to epoch seconds."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, (int, float)):
        v = float(value)
        if abs(v) > 1e12:  # millis
            v = v / 1000.0
        elif abs(v) > 1e10:  # micros? treat as millis-safe fallback
            v = v / 1000.0 if abs(v) > 1e11 else v
        return v
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # numeric string -> epoch
        try:
            v = float(s)
            if abs(v) > 1e12:
                v = v / 1000.0
            return v
        except ValueError:
            pass
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _to_iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


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


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (bytes, bytearray)):
            try:
                data.setdefault("camera_id", bytes(a).decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            # Heuristic: ISO datetime / epoch-ish strings are time bounds, not cameras.
            if _to_ts(s) is not None and ("-" in s or ":" in s or "T" in s or s.replace(".", "").isdigit()):
                if data.get("start") is None:
                    # Only treat as time if it parses AND looks like a date/number.
                    # Bare camera names like "cam1" fail _to_ts, so no clash.
                    data.setdefault("start", s)
                elif data.get("end") is None:
                    data.setdefault("end", s)
                elif data.get("camera_id") is None:
                    data.setdefault("camera_id", s)
                continue
            if data.get("camera_id") is None:
                data["camera_id"] = s
            elif data.get("reason") is None:
                data["reason"] = s
            elif data.get("start") is None:
                data["start"] = s
            elif data.get("end") is None:
                data["end"] = s
        elif isinstance(a, datetime):
            if data.get("start") is None:
                data["start"] = a
            elif data.get("end") is None:
                data["end"] = a
            elif data.get("now") is None:
                data["now"] = a
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            # Bare number: ambiguous epoch vs camera id; prefer time bound first.
            if data.get("start") is None and data.get("camera_id") is not None:
                data["start"] = a
            elif data.get("end") is None and data.get("start") is not None:
                data["end"] = a
            elif data.get("camera_id") is None:
                data["camera_id"] = a
            elif data.get("start") is None:
                data["start"] = a
            elif data.get("end") is None:
                data["end"] = a
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request", "data", "body", "window", "maintenance"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _normalize_camera(value: Any) -> str:
    if value is None:
        return "*"
    s = str(value).strip()
    if not s or s in ("*", "all", "ALL", "global"):
        return "*"
    return s


def _camera_matches(window_camera: str, query_camera: Any) -> bool:
    if window_camera == "*":
        return True
    q = _normalize_camera(query_camera)
    if q == "*":
        return True
    return window_camera.lower() == q.lower()


def _window_to_dict(w: Dict[str, Any]) -> Dict[str, Any]:
    return dict(w)


# ---------------------------------------------------------------------------
# Core public API
# ---------------------------------------------------------------------------

def create_window(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create a maintenance window that suppresses alerts for a camera.

    Required: camera (or global "*") + start + end (ISO string, datetime,
    or epoch seconds). Optional: reason, enabled (default True).
    A ``duration_seconds`` may substitute for ``end`` when ``start`` given.
    """
    data = _normalize_inputs(*args, **kwargs)
    camera = _normalize_camera(_first(data, CAMERA_KEYS))
    # Default camera when nothing given: global window (explicit, documented).
    start_raw = _first(data, START_KEYS)
    end_raw = _first(data, END_KEYS)
    if end_raw is None:
        dur_raw = _first(data, DURATION_KEYS)
        if dur_raw is not None and start_raw is not None:
            try:
                start_ts_tmp = _to_ts(start_raw)
                if start_ts_tmp is not None:
                    end_raw = float(start_ts_tmp) + float(dur_raw)
            except (TypeError, ValueError):
                end_raw = None
    start_ts = _to_ts(start_raw)
    end_ts = _to_ts(end_raw)
    if start_ts is None or end_ts is None:
        return {
            "ok": False, "success": False,
            "error": "start and end are required (ISO datetime or epoch seconds)",
            "camera_id": camera, "camera": camera,
        }
    if end_ts <= start_ts:
        return {
            "ok": False, "success": False,
            "error": "end must be after start",
            "camera_id": camera, "camera": camera,
            "start": _to_iso(start_ts), "end": _to_iso(end_ts),
        }
    reason = _first(data, REASON_KEYS)
    reason = str(reason).strip() if reason is not None else ""
    enabled_raw = _first(data, ENABLED_KEYS)
    enabled = bool(enabled_raw) if enabled_raw is not None else True
    wid_raw = _first(data, ID_KEYS)
    # Only honour an explicit id if it looks like an id (not a camera int).
    wid = str(wid_raw).strip() if wid_raw is not None and not isinstance(wid_raw, bool) else None
    if wid is not None and (not wid or wid.lower() in ("*", "all")):
        wid = None
    # Guard: a bare numeric positional camera id may have been consumed as id.
    # _normalize_inputs prefers camera_id, so wid from ID_KEYS with camera present
    # is a genuine explicit id; keep it, else mint a uuid.
    if not wid:
        wid = str(uuid.uuid4())
    now_iso = _now_iso()
    window = {
        "id": wid,
        "window_id": wid,
        "maintenance_id": wid,
        "camera_id": camera,
        "camera": camera,
        "start_ts": float(start_ts),
        "end_ts": float(end_ts),
        "start": _to_iso(start_ts),
        "start_time": _to_iso(start_ts),
        "end": _to_iso(end_ts),
        "end_time": _to_iso(end_ts),
        "reason": reason,
        "note": reason,
        "description": reason,
        "enabled": bool(enabled),
        "active": bool(enabled),
        "created_at": now_iso,
        "updated_at": now_iso,
        "ok": True,
        "success": True,
    }
    with _lock:
        _WINDOWS[wid] = window
    return _window_to_dict(window)


def get_window(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return one window by id, or None."""
    data = _normalize_inputs(*args, **kwargs)
    wid = _first(data, ID_KEYS)
    if wid is None and args:
        for a in args:
            if isinstance(a, str) and a.strip():
                wid = a.strip()
                break
    if wid is None:
        return None
    with _lock:
        w = _WINDOWS.get(str(wid))
        return _window_to_dict(w) if w is not None else None


def list_windows(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """List windows, optionally filtered by camera and/or active_only."""
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    active_only = bool(data.get("active_only", data.get("activeOnly", False)))
    now_raw = _first(data, NOW_KEYS)
    now_ts = _to_ts(now_raw) if now_raw is not None else _now_ts()
    with _lock:
        items = list(_WINDOWS.values())
    out: List[Dict[str, Any]] = []
    for w in items:
        if cam_raw is not None and str(cam_raw).strip() not in ("", "*", "all"):
            if not _camera_matches(str(w.get("camera_id", "*")), cam_raw):
                # Also include global windows when filtering by camera.
                if str(w.get("camera_id", "*")) != "*":
                    continue
        if active_only:
            if not w.get("enabled", True):
                continue
            if not (float(w["start_ts"]) <= float(now_ts) <= float(w["end_ts"])):
                continue
        out.append(_window_to_dict(w))
    out.sort(key=lambda d: (str(d.get("camera_id", "")), float(d.get("start_ts", 0))))
    return out


def delete_window(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Delete one window by id."""
    data = _normalize_inputs(*args, **kwargs)
    wid = _first(data, ID_KEYS)
    if wid is None and args:
        for a in args:
            if isinstance(a, str) and a.strip():
                wid = a.strip()
                break
            if isinstance(a, (int, float)) and not isinstance(a, bool):
                continue
    if wid is None or not str(wid).strip():
        return {"ok": False, "success": False, "error": "window id is required", "deleted": False}
    key = str(wid).strip()
    with _lock:
        existed = _WINDOWS.pop(key, None) is not None
    return {"ok": True, "success": True, "deleted": bool(existed), "existed": bool(existed), "id": key}


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Clear windows: all, or only those matching a camera when given."""
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    with _lock:
        if cam_raw is None or (isinstance(cam_raw, str) and not cam_raw.strip()):
            n = len(_WINDOWS)
            _WINDOWS.clear()
            return {"ok": True, "success": True, "cleared": n, "cleared_count": n}
        target = _normalize_camera(cam_raw)
        doomed = [k for k, w in _WINDOWS.items() if str(w.get("camera_id", "*")).lower() == str(target).lower()]
        for k in doomed:
            _WINDOWS.pop(k, None)
        return {"ok": True, "success": True, "cleared": len(doomed), "cleared_count": len(doomed), "camera_id": target}


def set_enabled(window_id: Any, enabled: bool = True, **kwargs: Any) -> Dict[str, Any]:
    """Enable/disable one window by id."""
    data = _normalize_inputs({"id": window_id, "enabled": enabled, **kwargs})
    wid = _first(data, ID_KEYS)
    if wid is None:
        return {"ok": False, "success": False, "error": "window id is required"}
    key = str(wid).strip()
    with _lock:
        w = _WINDOWS.get(key)
        if w is None:
            return {"ok": False, "success": False, "error": "window not found", "id": key}
        w["enabled"] = bool(enabled)
        w["active"] = bool(enabled)
        w["updated_at"] = _now_iso()
        return _window_to_dict(w)


def enable(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    wid = _first(data, ID_KEYS)
    if wid is None and args and isinstance(args[0], str):
        wid = args[0]
    return set_enabled(wid, True)


def disable(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    wid = _first(data, ID_KEYS)
    if wid is None and args and isinstance(args[0], str):
        wid = args[0]
    return set_enabled(wid, False)


def get_active(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Windows currently suppressing ``camera_id`` at ``now`` (default: now)."""
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    now_raw = _first(data, NOW_KEYS)
    now_ts = _to_ts(now_raw) if now_raw is not None else _now_ts()
    with _lock:
        items = list(_WINDOWS.values())
    out = []
    for w in items:
        if not w.get("enabled", True):
            continue
        if cam_raw is not None and not _camera_matches(str(w.get("camera_id", "*")), cam_raw):
            continue
        if float(w["start_ts"]) <= float(now_ts) <= float(w["end_ts"]):
            out.append(_window_to_dict(w))
    out.sort(key=lambda d: float(d.get("start_ts", 0)))
    return out


def is_suppressed(*args: Any, **kwargs: Any) -> bool:
    """Return True when an alert for ``camera_id`` must be suppressed now.

    A camera is suppressed when at least one **enabled** window bound to it
    (or a global ``"*"`` window) covers the reference time ``now``
    (defaults to the current time). Accepts flexible spellings:
    ``is_suppressed("cam1")``, ``is_suppressed(camera_id="cam1")``,
    ``is_suppressed(camera_id="cam1", now=<iso/epoch/datetime>)``.
    """
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    # Bare positional camera string lands in camera_id via _normalize_inputs.
    if cam_raw is None:
        # No camera specified: suppressed only if a global window is active.
        cam_raw = "*"
    now_raw = _first(data, NOW_KEYS)
    now_ts = _to_ts(now_raw) if now_raw is not None else _now_ts()
    if now_ts is None:
        now_ts = _now_ts()
    with _lock:
        items = list(_WINDOWS.values())
    for w in items:
        if not w.get("enabled", True):
            continue
        if not _camera_matches(str(w.get("camera_id", "*")), cam_raw):
            continue
        if float(w["start_ts"]) <= float(now_ts) <= float(w["end_ts"]):
            return True
    return False


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Suppression status for one camera (or overall when no camera given)."""
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    now_raw = _first(data, NOW_KEYS)
    now_ts = _to_ts(now_raw) if now_raw is not None else _now_ts()
    if cam_raw is None:
        with _lock:
            total = len(_WINDOWS)
            enabled = sum(1 for w in _WINDOWS.values() if w.get("enabled", True))
        active = get_active(now=now_ts)
        return {
            "ok": True, "success": True,
            "camera_id": None, "camera": None,
            "suppressed": bool(active),
            "is_suppressed": bool(active),
            "active_windows": active,
            "active_count": len(active),
            "total_windows": total,
            "enabled_windows": enabled,
            "checked_at": _to_iso(now_ts),
        }
    camera = _normalize_camera(cam_raw)
    active = get_active(camera_id=camera, now=now_ts)
    return {
        "ok": True, "success": True,
        "camera_id": camera, "camera": camera,
        "suppressed": bool(active),
        "is_suppressed": bool(active),
        "active_windows": active,
        "active_count": len(active),
        "checked_at": _to_iso(now_ts),
    }


# ---------------------------------------------------------------------------
# Service class (singleton mirrors module functions)
# ---------------------------------------------------------------------------

class MaintenanceService:
    def create_window(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_window(*args, **kwargs)

    def get_window(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_window(*args, **kwargs)

    def list_windows(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_windows(*args, **kwargs)

    def delete_window(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return delete_window(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return clear(*args, **kwargs)

    def enable(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return enable(*args, **kwargs)

    def disable(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return disable(*args, **kwargs)

    def set_enabled(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_enabled(*args, **kwargs)

    def get_active(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return get_active(*args, **kwargs)

    def is_suppressed(self, *args: Any, **kwargs: Any) -> bool:
        return is_suppressed(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)

    # aliases
    def check(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)

    def is_active(self, *args: Any, **kwargs: Any) -> bool:
        return is_suppressed(*args, **kwargs)

    def should_suppress(self, *args: Any, **kwargs: Any) -> bool:
        return is_suppressed(*args, **kwargs)

    def suppressed(self, *args: Any, **kwargs: Any) -> bool:
        return is_suppressed(*args, **kwargs)


Maintenance = MaintenanceService
MaintenanceWindowsService = MaintenanceService
MaintenanceWindowService = MaintenanceService

maintenance_service = MaintenanceService()
service = maintenance_service
manager = maintenance_service
default_service = maintenance_service


def get_service() -> MaintenanceService:
    return maintenance_service


def create_service() -> MaintenanceService:
    return MaintenanceService()


# Module-level aliases for flexible callers
create = create_window
add_window = create_window
add = create_window
schedule = create_window
schedule_window = create_window
open_window = create_window
new_window = create_window
register = create_window
get = get_window
fetch = get_window
list_all = list_windows
all_windows = list_windows
list_all_windows = list_windows
delete = delete_window
remove = delete_window
remove_window = delete_window
delete_maintenance = delete_window
clear_all = clear
reset = clear
is_active = is_suppressed
should_suppress = is_suppressed
suppressed = is_suppressed
check_suppressed = is_suppressed
in_maintenance = is_suppressed
under_maintenance = is_suppressed
status = get_status


__all__ = [
    "create_window",
    "get_window",
    "list_windows",
    "delete_window",
    "clear",
    "enable",
    "disable",
    "set_enabled",
    "get_active",
    "is_suppressed",
    "get_status",
    "MaintenanceService",
    "maintenance_service",
    "get_service",
    "create_service",
]
