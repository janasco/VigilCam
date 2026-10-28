"""Recurring daily quiet hours for VigilCam.

Suppresses alerts during a *recurring* time-of-day window (e.g. 22:00-06:00
every night), optionally restricted to certain days of the week.

How this differs from neighbouring services (do NOT merge):
* ``maintenance`` is **one-off calendar windows**: explicit ``[start, end]``
  date ranges (ISO datetime / epoch) bound to one camera (or global ``"*"``).
  Each window fires once and expires. It has no notion of daily recurrence,
  time-of-day, or days-of-week.
* ``alert_throttle`` is **rate-based**: cooldown seconds + sliding-window
  caps. It has no notion of calendar time at all.
* This module is **recurring quiet hours**: ``start_time``/``end_time`` are
  times-of-day (``"HH:MM"``) that repeat every day (or on selected ``days``).
  Rules never expire. ``is_quiet`` is the gate alert paths consult.

All state is in-memory, thread-safe, and dependency-free.
"""

from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_RULES: Dict[str, Dict[str, Any]] = {}

_lock = threading.RLock()

CAMERA_KEYS = ("camera_id", "camera", "cam", "cam_id", "device_id", "source_id", "id", "name")
START_KEYS = ("start_time", "start", "from_time", "from", "begin", "begin_time", "quiet_from", "start_hour")
END_KEYS = ("end_time", "end", "to_time", "to", "until", "finish", "finish_time", "quiet_to", "end_hour", "stop", "stop_time")
DAYS_KEYS = ("days", "day", "days_of_week", "weekdays", "dow", "day_of_week", "active_days")
REASON_KEYS = ("reason", "note", "notes", "description", "comment", "label", "title", "name")
ENABLED_KEYS = ("enabled", "active", "is_enabled", "on")
ID_KEYS = ("id", "rule_id", "quiet_hours_id", "quiet_id", "uuid")
NOW_KEYS = ("now", "at", "when", "timestamp", "ts", "time", "datetime")

DAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_time_to_minutes(value: Any) -> Optional[int]:
    """Parse a time-of-day to minutes since midnight (0-1439)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        iv = int(value)
        if 0 <= iv <= 23:
            return iv * 60
        if 0 <= iv <= 1439:
            return iv
        return iv % 1440
    s = str(value).strip()
    if not s:
        return None
    m = re.match(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?\s*([ap]\.?m?\.?)?$", s.lower())
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        ap = (m.group(4) or "").replace(".", "")
        if ap:
            if ap.startswith("p") and h < 12:
                h += 12
            if ap.startswith("a") and h == 12:
                h = 0
        return (h % 24) * 60 + (mi % 60)
    if re.match(r"^\d{3,4}$", s):
        iv = int(s)
        if len(s) <= 2:
            return (iv % 24) * 60
        h = iv // 100
        mi = iv % 100
        return (h % 24) * 60 + (mi % 60)
    try:
        return int(float(s)) % 1440
    except (TypeError, ValueError):
        return None


def _minutes_to_str(minutes: int) -> str:
    minutes = int(minutes) % 1440
    return "{:02d}:{:02d}".format(minutes // 60, minutes % 60)


def _parse_days(value: Any) -> List[int]:
    if value is None:
        return [0, 1, 2, 3, 4, 5, 6]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [int(value) % 7]
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("*", "all", "daily", "everyday", "every_day", "any", ""):
            return [0, 1, 2, 3, 4, 5, 6]
        if s in ("weekdays", "weekday", "workdays", "mon-fri", "mon_fri"):
            return [0, 1, 2, 3, 4]
        if s in ("weekends", "weekend", "sat-sun", "sat_sun"):
            return [5, 6]
        parts = re.split(r"[,\s;|/]+", s)
        out: List[int] = []
        for p in parts:
            p = p.strip().lower()
            if not p:
                continue
            if p in DAY_NAMES:
                out.append(DAY_NAMES[p])
            elif "-" in p and not p.startswith("-"):
                a, b = p.split("-", 1)
                a = a.strip()
                b = b.strip()
                try:
                    lo = DAY_NAMES[a] if a in DAY_NAMES else int(a) % 7
                    hi = DAY_NAMES[b] if b in DAY_NAMES else int(b) % 7
                    if lo <= hi:
                        out.extend(range(lo, hi + 1))
                    else:
                        out.extend(list(range(lo, 7)) + list(range(0, hi + 1)))
                except ValueError:
                    continue
            else:
                try:
                    out.append(int(p) % 7)
                    continue
                except ValueError:
                    pass
                prefix = p[:3]
                if prefix in DAY_NAMES:
                    out.append(DAY_NAMES[prefix])
        seen: List[int] = []
        for d in out:
            if d not in seen:
                seen.append(d)
        return seen or [0, 1, 2, 3, 4, 5, 6]
    if isinstance(value, (list, tuple, set)):
        out2: List[int] = []
        for item in value:
            if isinstance(item, str):
                key = item.strip().lower()
                if key in DAY_NAMES:
                    out2.append(DAY_NAMES[key])
                    continue
                try:
                    out2.append(int(key) % 7)
                    continue
                except ValueError:
                    prefix = key[:3]
                    if prefix in DAY_NAMES:
                        out2.append(prefix and DAY_NAMES[prefix])
                    continue
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                out2.append(int(item) % 7)
        seen2: List[int] = []
        for d in out2:
            if d not in seen2:
                seen2.append(d)
        return seen2 or [0, 1, 2, 3, 4, 5, 6]
    return [0, 1, 2, 3, 4, 5, 6]


def _resolve_now(value: Any) -> datetime:
    """Resolve a ``now`` reference to an aware datetime (UTC default)."""
    if value is None:
        return _now()
    if isinstance(value, bool):
        return _now()
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    if isinstance(value, (int, float)):
        v = float(value)
        if abs(v) > 1e12:  # millis
            v = v / 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return _now()
        try:
            v = float(s)
            if abs(v) > 1e12:
                v = v / 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
        mins = _parse_time_to_minutes(s)
        if mins is not None:
            base = _now()
            return base.replace(hour=mins // 60, minute=mins % 60, second=0, microsecond=0)
        return _now()
    return _now()


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
            # Time-of-day strings belong to start/end, not camera names.
            if _parse_time_to_minutes(s) is not None and (":" in s or re.match(r"^\d{3,4}$", s) or s.lower().endswith(("am", "pm"))):
                if data.get("start_time") is None and data.get("start") is None:
                    data.setdefault("start_time", s)
                elif data.get("end_time") is None and data.get("end") is None:
                    data.setdefault("end_time", s)
                elif data.get("camera_id") is None:
                    data.setdefault("camera_id", s)
                continue
            if data.get("camera_id") is None:
                data["camera_id"] = s
            elif data.get("reason") is None:
                data["reason"] = s
            elif data.get("start_time") is None:
                data["start_time"] = s
            elif data.get("end_time") is None:
                data["end_time"] = s
        elif isinstance(a, datetime):
            if data.get("now") is None and data.get("start_time") is not None and data.get("end_time") is not None:
                data["now"] = a
            elif data.get("start_time") is None:
                # datetime positional is a `now` reference only when times exist;
                # otherwise ignore (quiet hours take HH:MM, not dates).
                data["now"] = a
            elif data.get("end_time") is None:
                data["now"] = a
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            data.setdefault("now", a)
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request", "data", "body", "rule", "quiet_hours", "quiet"):
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


def _camera_matches(rule_camera: str, query_camera: Any) -> bool:
    if rule_camera == "*":
        return True
    q = _normalize_camera(query_camera)
    if q == "*":
        return True
    return rule_camera.lower() == q.lower()


def _rule_covers(rule: Dict[str, Any], moment: datetime) -> bool:
    """True when an enabled rule covers ``moment`` (recurring daily check)."""
    if not rule.get("enabled", True):
        return False
    days = rule.get("days", [0, 1, 2, 3, 4, 5, 6])
    start_min = int(rule.get("start_minutes", 0)) % 1440
    end_min = int(rule.get("end_minutes", 0)) % 1440
    now_min = moment.hour * 60 + moment.minute
    weekday = moment.weekday()  # Monday=0
    prev_day = (weekday - 1) % 7
    if start_min == end_min:
        return weekday in days  # full-day marker
    if start_min < end_min:
        return weekday in days and start_min <= now_min < end_min
    # Overnight window, e.g. 22:00-06:00. Days name the day the window starts.
    if now_min >= start_min:
        return weekday in days
    return now_min < end_min and prev_day in days  # early-morning tail belongs to previous day's window


def _rule_to_dict(r: Dict[str, Any]) -> Dict[str, Any]:
    return dict(r)


# ---------------------------------------------------------------------------
# Core public API
# ---------------------------------------------------------------------------

def create_rule(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create a recurring daily quiet-hours rule.

    Required: ``start_time`` + ``end_time`` as times-of-day (``"HH:MM"``,
    ``"22:00"``, ``"10pm"``...). Optional: ``camera_id`` (default global
    ``"*"``), ``days`` (default daily), ``enabled`` (default True),
    ``reason``. Overnight ranges (e.g. 22:00-06:00) are allowed; equal
    start/end means quiet all day.
    """
    data = _normalize_inputs(*args, **kwargs)
    camera = _normalize_camera(_first(data, CAMERA_KEYS))
    start_raw = _first(data, START_KEYS)
    end_raw = _first(data, END_KEYS)
    start_min = _parse_time_to_minutes(start_raw)
    end_min = _parse_time_to_minutes(end_raw)
    if start_min is None or end_min is None:
        return {
            "ok": False, "success": False,
            "error": "start_time and end_time are required (HH:MM time-of-day)",
            "camera_id": camera, "camera": camera,
        }
    days = _parse_days(_first(data, DAYS_KEYS))
    reason = _first(data, REASON_KEYS)
    reason = str(reason).strip() if reason is not None else ""
    enabled_raw = _first(data, ENABLED_KEYS)
    enabled = bool(enabled_raw) if enabled_raw is not None else True
    wid_raw = _first(data, ID_KEYS)
    wid = str(wid_raw).strip() if wid_raw is not None and not isinstance(wid_raw, bool) else None
    if wid is not None and (not wid or wid.lower() in ("*", "all")):
        wid = None
    if not wid:
        wid = str(uuid.uuid4())
    now_iso = _now_iso()
    rule = {
        "id": wid,
        "rule_id": wid,
        "quiet_hours_id": wid,
        "camera_id": camera,
        "camera": camera,
        "start_time": _minutes_to_str(start_min),
        "end_time": _minutes_to_str(end_min),
        "start_minutes": start_min,
        "end_minutes": end_min,
        "days": days,
        "days_of_week": days,
        "weekdays": days,
        "reason": reason,
        "note": reason,
        "description": reason,
        "enabled": bool(enabled),
        "active": bool(enabled),
        "recurring": True,
        "recurrence": "daily",
        "created_at": now_iso,
        "updated_at": now_iso,
        "ok": True,
        "success": True,
    }
    with _lock:
        _RULES[wid] = rule
    return _rule_to_dict(rule)


def get_rule(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return one rule by id, or None."""
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
        r = _RULES.get(str(wid))
        return _rule_to_dict(r) if r is not None else None


def list_rules(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """List rules, optionally filtered by camera."""
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    active_only = bool(data.get("active_only", data.get("activeOnly", False)))
    now_raw = _first(data, NOW_KEYS)
    moment = _resolve_now(now_raw) if now_raw is not None else None
    with _lock:
        items = list(_RULES.values())
    out: List[Dict[str, Any]] = []
    for r in items:
        if cam_raw is not None and str(cam_raw).strip() not in ("", "*", "all"):
            if not _camera_matches(str(r.get("camera_id", "*")), cam_raw):
                if str(r.get("camera_id", "*")) != "*":
                    continue
        if active_only:
            if moment is None:
                moment = _now()
            if not _rule_covers(r, moment):
                continue
        out.append(_rule_to_dict(r))
    out.sort(key=lambda d: (str(d.get("camera_id", "")), int(d.get("start_minutes", 0))))
    return out


def delete_rule(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Delete one rule by id."""
    data = _normalize_inputs(*args, **kwargs)
    wid = _first(data, ID_KEYS)
    if wid is None and args:
        for a in args:
            if isinstance(a, str) and a.strip():
                wid = a.strip()
                break
    if wid is None or not str(wid).strip():
        return {"ok": False, "success": False, "error": "rule id is required", "deleted": False}
    key = str(wid).strip()
    with _lock:
        existed = _RULES.pop(key, None) is not None
    return {"ok": True, "success": True, "deleted": bool(existed), "existed": bool(existed), "id": key}


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Clear rules: all, or only those matching a camera when given."""
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    with _lock:
        if cam_raw is None or (isinstance(cam_raw, str) and not cam_raw.strip()):
            n = len(_RULES)
            _RULES.clear()
            return {"ok": True, "success": True, "cleared": n, "cleared_count": n}
        target = _normalize_camera(cam_raw)
        doomed = [k for k, w in _RULES.items() if str(w.get("camera_id", "*")).lower() == str(target).lower()]
        for k in doomed:
            _RULES.pop(k, None)
        return {"ok": True, "success": True, "cleared": len(doomed), "cleared_count": len(doomed), "camera_id": target}


def set_enabled(rule_id: Any, enabled: bool = True, **kwargs: Any) -> Dict[str, Any]:
    """Enable/disable one rule by id."""
    data = _normalize_inputs({"id": rule_id, "enabled": enabled, **kwargs})
    wid = _first(data, ID_KEYS)
    if wid is None:
        return {"ok": False, "success": False, "error": "rule id is required"}
    key = str(wid).strip()
    with _lock:
        r = _RULES.get(key)
        if r is None:
            return {"ok": False, "success": False, "error": "rule not found", "id": key}
        r["enabled"] = bool(enabled)
        r["active"] = bool(enabled)
        r["updated_at"] = _now_iso()
        return _rule_to_dict(r)


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
    """Rules quieting ``camera_id`` at ``now`` (default: now)."""
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    now_raw = _first(data, NOW_KEYS)
    moment = _resolve_now(now_raw)
    with _lock:
        items = list(_RULES.values())
    out = []
    for r in items:
        if not r.get("enabled", True):
            continue
        if cam_raw is not None and not _camera_matches(str(r.get("camera_id", "*")), cam_raw):
            continue
        if _rule_covers(r, moment):
            out.append(_rule_to_dict(r))
    out.sort(key=lambda d: int(d.get("start_minutes", 0)))
    return out


def is_quiet(*args: Any, **kwargs: Any) -> bool:
    """Return True when ``camera_id`` is inside recurring quiet hours now.

    A camera is quiet when at least one **enabled** rule bound to it
    (or a global ``"*"`` rule) covers the reference time ``now``
    (defaults to the current time). Times are times-of-day that repeat
    daily, so unlike one-off maintenance windows the same wall-clock time
    matches on any date. Accepts flexible spellings:
    ``is_quiet("cam1")``, ``is_quiet(camera_id="cam1")``,
    ``is_quiet(camera_id="cam1", now=<iso/epoch/datetime>)``.
    """
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    if cam_raw is None:
        cam_raw = "*"
    now_raw = _first(data, NOW_KEYS)
    moment = _resolve_now(now_raw)
    with _lock:
        items = list(_RULES.values())
    for r in items:
        if not r.get("enabled", True):
            continue
        if not _camera_matches(str(r.get("camera_id", "*")), cam_raw):
            continue
        if _rule_covers(r, moment):
            return True
    return False


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Quiet-hours status for one camera (or overall when no camera given)."""
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    now_raw = _first(data, NOW_KEYS)
    moment = _resolve_now(now_raw)
    if cam_raw is None:
        with _lock:
            total = len(_RULES)
            enabled = sum(1 for r in _RULES.values() if r.get("enabled", True))
        active = get_active(now=moment)
        return {
            "ok": True, "success": True,
            "camera_id": None, "camera": None,
            "quiet": bool(active),
            "is_quiet": bool(active),
            "suppressed": bool(active),
            "is_suppressed": bool(active),
            "active_rules": active,
            "active_count": len(active),
            "total_rules": total,
            "enabled_rules": enabled,
            "checked_at": moment.isoformat(),
        }
    camera = _normalize_camera(cam_raw)
    active = get_active(camera_id=camera, now=moment)
    return {
        "ok": True, "success": True,
        "camera_id": camera, "camera": camera,
        "quiet": bool(active),
        "is_quiet": bool(active),
        "suppressed": bool(active),
        "is_suppressed": bool(active),
        "active_rules": active,
        "active_windows": active,
        "active_count": len(active),
        "checked_at": moment.isoformat(),
    }


# ---------------------------------------------------------------------------
# Service class (singleton mirrors module functions)
# ---------------------------------------------------------------------------

class QuietHoursService:
    def create_rule(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_rule(*args, **kwargs)

    def get_rule(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_rule(*args, **kwargs)

    def list_rules(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_rules(*args, **kwargs)

    def delete_rule(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return delete_rule(*args, **kwargs)

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

    def is_quiet(self, *args: Any, **kwargs: Any) -> bool:
        return is_quiet(*args, **kwargs)

    def is_suppressed(self, *args: Any, **kwargs: Any) -> bool:
        return is_quiet(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)

    # aliases
    def check(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)

    def is_active(self, *args: Any, **kwargs: Any) -> bool:
        return is_quiet(*args, **kwargs)

    def should_suppress(self, *args: Any, **kwargs: Any) -> bool:
        return is_quiet(*args, **kwargs)

    def suppressed(self, *args: Any, **kwargs: Any) -> bool:
        return is_quiet(*args, **kwargs)


QuietHours = QuietHoursService
QuietHoursManager = QuietHoursService

quiet_hours_service = QuietHoursService()
service = quiet_hours_service
manager = quiet_hours_service
default_service = quiet_hours_service


def get_service() -> QuietHoursService:
    return quiet_hours_service


def create_service() -> QuietHoursService:
    return QuietHoursService()


# Module-level aliases for flexible callers
create = create_rule
add_rule = create_rule
add = create_rule
schedule = create_rule
schedule_rule = create_rule
new_rule = create_rule
register = create_rule
set_quiet_hours = create_rule
set_rule = create_rule
get = get_rule
fetch = get_rule
list_all = list_rules
all_rules = list_rules
list_all_rules = list_rules
delete = delete_rule
remove = delete_rule
remove_rule = delete_rule
clear_all = clear
reset = clear
is_active = is_quiet
should_suppress = is_quiet
suppressed = is_quiet
is_suppressed = is_quiet
check_quiet = is_quiet
in_quiet_hours = is_quiet
status = get_status


__all__ = [
    "create_rule",
    "get_rule",
    "list_rules",
    "delete_rule",
    "clear",
    "enable",
    "disable",
    "set_enabled",
    "get_active",
    "is_quiet",
    "is_suppressed",
    "get_status",
    "QuietHoursService",
    "quiet_hours_service",
    "get_service",
    "create_service",
]
