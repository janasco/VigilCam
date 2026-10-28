"""Time-based motion zone sets for VigilCam.

Each camera keeps a single *static* ``motion_zones`` value
(``Camera.motion_zones`` — a list of polygon zone dicts with no time
component). This module adds *time-based zone sets* on top: named zone
collections (e.g. ``"day"`` / ``"night"``) bound to recurring daily time
windows (``start_time``/``end_time`` + optional ``days``). At any instant
exactly one set is *active* — evaluated by which window covers ``now`` —
and callers use :func:`resolve_zones` / :func:`active_zones` to pick the
zone list to feed to :class:`motion_zoning.MotionZoningEngine`.

How this differs from neighbouring services (do NOT merge):

* ``motion_zoning`` / ``Camera.motion_zones`` is **static per camera**: one
  zone list, no time component.
* ``quiet_hours`` is **alert suppression**: recurring time-of-day windows
  that mute alerts globally or per camera. It carries no zone geometry.
* ``rec_schedules`` controls **recording time profiles** (continuous /
  motion / off). It carries no zone geometry either.
* This module is **zone-set switching**: recurring time windows that select
  *which polygon zones are active*. It never suppresses alerts by itself
  and never starts/stops recording.

All state is in-memory, thread-safe, and dependency-free (zone geometry is
validated leniently; strict polygon parsing lives in ``motion_zoning``).
"""

from __future__ import annotations

import copy
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_SCHEDULES: Dict[str, Dict[str, Any]] = {}

_lock = threading.RLock()

CAMERA_KEYS = ("camera_id", "camera", "cam", "cam_id", "device_id", "source_id", "id", "name")
SET_KEYS = ("set_name", "set", "zone_set", "zones_name", "profile", "preset", "label", "title", "name")
ZONES_KEYS = ("zones", "zone_list", "polygon_zones", "polygons", "polygon", "zone", "motion_zones", "motionZones")
START_KEYS = ("start_time", "start", "from_time", "from", "begin", "begin_time", "window_start", "start_hour")
END_KEYS = ("end_time", "end", "to_time", "to", "until", "finish", "finish_time", "window_end", "end_hour", "stop", "stop_time")
DAYS_KEYS = ("days", "day", "days_of_week", "weekdays", "dow", "day_of_week", "active_days")
ENABLED_KEYS = ("enabled", "active", "is_enabled", "on")
ID_KEYS = ("id", "schedule_id", "zone_schedule_id", "rule_id", "zone_set_id", "uuid")
PRIORITY_KEYS = ("priority", "precedence", "order", "rank", "weight")
NOW_KEYS = ("now", "at", "when", "timestamp", "ts", "time", "datetime")
FALLBACK_KEYS = ("fallback", "fallback_zones", "default_zones", "default", "static_zones")

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
    m = re.match(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?\s*([ap]\.?m\.?)?$", s.lower())
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
                        out2.append(DAY_NAMES[prefix])
                    continue
            elif isinstance(item, (int, float)) and not isinstance(value, bool):
                out2.append(int(item) % 7)
        seen2: List[int] = []
        for d in out2:
            if d not in seen2:
                seen2.append(d)
        return seen2 or [0, 1, 2, 3, 4, 5, 6]
    return [0, 1, 2, 3, 4, 5, 6]


def _resolve_now(value: Any) -> datetime:
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
        if abs(v) > 1e12:
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
            if data.get("camera_id") is None:
                data["camera_id"] = s
            elif data.get("set_name") is None and data.get("name") is None:
                data["set_name"] = s
            elif data.get("start_time") is None:
                data["start_time"] = s
            elif data.get("end_time") is None:
                data["end_time"] = s
        elif isinstance(a, datetime):
            data["now"] = a
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            data.setdefault("now", a)
        elif isinstance(a, (list, tuple)):
            # Bare zone list positional.
            if data.get("zones") is None:
                data["zones"] = list(a)
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request", "data", "body", "zone_schedule", "schedule"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _normalize_camera(value: Any) -> str:
    if value is None:
        return "*"
    s = str(value).strip()
    if not s or s.lower() in ("*", "all", "global"):
        return "*"
    return s


def _camera_matches(rule_camera: str, query_camera: Any) -> bool:
    if rule_camera == "*":
        return True
    q = _normalize_camera(query_camera)
    if q == "*":
        return True
    return rule_camera.lower() == q.lower()


def _normalize_zones(value: Any) -> List[Dict[str, Any]]:
    """Coerce zone input to a list of plain zone dicts (lenient)."""
    if value is None:
        return []
    if isinstance(value, dict):
        # Single zone dict, or mapping name -> zone.
        if "points" in value or "coordinates" in value or "coords" in value or "polygon" in value:
            return [dict(value)]
        out: List[Dict[str, Any]] = []
        for k, v in value.items():
            if isinstance(v, dict):
                z = dict(v)
                z.setdefault("name", k)
                out.append(z)
            elif isinstance(v, (list, tuple)):
                out.append({"name": k, "points": list(v)})
        if out:
            return out
        return [dict(value)]
    if isinstance(value, (list, tuple)):
        out2: List[Dict[str, Any]] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, dict):
                out2.append(dict(item))
                continue
            if hasattr(item, "to_dict") and callable(getattr(item, "to_dict")):
                try:
                    out2.append(dict(item.to_dict()))
                    continue
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(item, (list, tuple)):
                # Raw points list -> single zone.
                try:
                    flat = [float(p) for p in item]
                    if len(flat) >= 6:
                        pts = [[flat[i], flat[i + 1]] for i in range(0, len(flat) - 1, 2)]
                        out2.append({"points": pts})
                        continue
                except (TypeError, ValueError):
                    pass
                # List of points.
                out2.append({"points": [list(p) if isinstance(p, (list, tuple)) else p for p in item]})
            else:
                try:
                    out2.append(dict(item))
                except Exception:  # noqa: BLE001
                    continue
        return out2
    return []


def _covers(rule: Dict[str, Any], moment: datetime) -> bool:
    if not rule.get("enabled", True):
        return False
    days = rule.get("days", [0, 1, 2, 3, 4, 5, 6])
    start_min = int(rule.get("start_minutes", 0)) % 1440
    end_min = int(rule.get("end_minutes", 0)) % 1440
    now_min = moment.hour * 60 + moment.minute
    weekday = moment.weekday()
    prev_day = (weekday - 1) % 7
    if start_min == end_min:
        return weekday in days
    if start_min < end_min:
        return weekday in days and start_min <= now_min < end_min
    if now_min >= start_min:
        return weekday in days
    return now_min < end_min and prev_day in days


def _rule_to_dict(r: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(r)


# ---------------------------------------------------------------------------
# Core public API
# ---------------------------------------------------------------------------

def create_schedule(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create one time-based zone set for a camera.

    Required: ``zones`` (list of polygon zone dicts). Optional: ``camera_id``
    (default ``"*"``), ``set_name``/``name`` (default ``"day"``/``"night"``
    auto or ``"zones-N"``), ``start_time``/``end_time`` times-of-day
    (default full-day ``"00:00"``-``"00:00"``), ``days`` (default daily),
    ``enabled`` (default True), ``priority`` (default 0; higher wins when
    several windows overlap).
    """
    data = _normalize_inputs(*args, **kwargs)
    camera = _normalize_camera(_first(data, CAMERA_KEYS))
    raw_name = _first(data, SET_KEYS)
    set_name = str(raw_name).strip() if raw_name is not None else ""
    if not set_name:
        with _lock:
            n = sum(1 for r in _SCHEDULES.values() if str(r.get("camera_id")) == camera) + 1
        set_name = "zones-%d" % n
    zones = _normalize_zones(_first(data, ZONES_KEYS))
    if not zones:
        return {
            "ok": False, "success": False,
            "error": "zones are required (list of polygon zone dicts)",
            "camera_id": camera, "camera": camera,
        }
    start_raw = _first(data, START_KEYS)
    end_raw = _first(data, END_KEYS)
    start_min = _parse_time_to_minutes(start_raw) if start_raw is not None else 0
    end_min = _parse_time_to_minutes(end_raw) if end_raw is not None else 0
    if start_min is None:
        start_min = 0
    if end_min is None:
        end_min = 0
    days = _parse_days(_first(data, DAYS_KEYS))
    enabled_raw = _first(data, ENABLED_KEYS)
    enabled = bool(enabled_raw) if enabled_raw is not None else True
    prio_raw = _first(data, PRIORITY_KEYS)
    try:
        priority = int(prio_raw) if prio_raw is not None else 0
    except (TypeError, ValueError):
        priority = 0
    wid_raw = _first(data, ID_KEYS)
    # NOTE: set names are not ids; only accept explicit id-looking keys when
    # they do not collide with the set name. A bare ``name`` key is the set
    # name, not the row id.
    wid = None
    if isinstance(data, dict):
        for k in ("id", "schedule_id", "zone_schedule_id", "rule_id", "zone_set_id", "uuid"):
            if data.get(k) is not None:
                wid = str(data.get(k)).strip()
                break
    if wid is not None and (not wid or wid.lower() in ("*", "all")):
        wid = None
    if not wid:
        wid = str(uuid.uuid4())
    now_iso = _now_iso()
    rule = {
        "id": wid,
        "schedule_id": wid,
        "zone_schedule_id": wid,
        "rule_id": wid,
        "camera_id": camera,
        "camera": camera,
        "set_name": set_name,
        "name": set_name,
        "zone_set": set_name,
        "zones": zones,
        "zone_list": zones,
        "polygon_zones": zones,
        "start_time": _minutes_to_str(start_min),
        "end_time": _minutes_to_str(end_min),
        "start_minutes": start_min,
        "end_minutes": end_min,
        "days": days,
        "days_of_week": days,
        "weekdays": days,
        "enabled": bool(enabled),
        "active": bool(enabled),
        "priority": priority,
        "recurring": True,
        "recurrence": "daily",
        "created_at": now_iso,
        "updated_at": now_iso,
        "ok": True,
        "success": True,
    }
    with _lock:
        _SCHEDULES[wid] = rule
    return _rule_to_dict(rule)


def set_day_night(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Convenience: install complementary ``day`` + ``night`` zone sets.

    Args: ``camera_id``, ``day_zones`` (+ aliases ``day``/``zones_day``),
    ``night_zones`` (+ aliases ``night``/``zones_night``),
    ``day_start`` (default ``"06:00"``), ``day_end`` (default ``"18:00"``),
    ``night_start``/``night_end`` default to the complement, ``days``,
    ``priority``. Existing ``day``/``night`` sets for the camera are
    replaced (same set_name rows are removed first).
    """
    data = _normalize_inputs(*args, **kwargs)
    camera = _normalize_camera(_first(data, CAMERA_KEYS))
    day_zones = None
    night_zones = None
    for k in ("day_zones", "day", "zones_day", "dayZones", "day_windows_zones"):
        if data.get(k) is not None:
            day_zones = data.get(k)
            break
    for k in ("night_zones", "night", "zones_night", "nightZones"):
        if data.get(k) is not None:
            night_zones = data.get(k)
            break
    if day_zones is None:
        day_zones = _first(data, ZONES_KEYS)
    if day_zones is None or night_zones is None:
        return {"ok": False, "success": False, "error": "day_zones and night_zones are required", "camera_id": camera}
    day_start = data.get("day_start", data.get("dayStart", "06:00"))
    day_end = data.get("day_end", data.get("dayEnd", "18:00"))
    night_start = data.get("night_start", data.get("nightStart", day_end))
    night_end = data.get("night_end", data.get("nightEnd", day_start))
    days = _first(data, DAYS_KEYS)
    priority = _first(data, PRIORITY_KEYS)
    with _lock:
        doomed = [k for k, r in _SCHEDULES.items()
                  if str(r.get("camera_id", "*")).lower() == str(camera).lower()
                  and str(r.get("set_name", "")).lower() in ("day", "night")]
        for k in doomed:
            _SCHEDULES.pop(k, None)
    day_rule = create_schedule(camera_id=camera, set_name="day", zones=_normalize_zones(day_zones),
                               start_time=day_start, end_time=day_end,
                               days=days if days is not None else [0, 1, 2, 3, 4, 5, 6],
                               priority=priority if priority is not None else 0)
    night_rule = create_schedule(camera_id=camera, set_name="night", zones=_normalize_zones(night_zones),
                                 start_time=night_start, end_time=night_end,
                                 days=days if days is not None else [0, 1, 2, 3, 4, 5, 6],
                                 priority=priority if priority is not None else 0)
    if not day_rule.get("ok") or not night_rule.get("ok"):
        return {"ok": False, "success": False, "error": "failed to create day/night sets",
                "day": day_rule, "night": night_rule, "camera_id": camera}
    return {"ok": True, "success": True, "camera_id": camera, "camera": camera,
            "day": day_rule, "night": night_rule,
            "schedules": [day_rule, night_rule], "count": 2}


def get_schedule(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    data = _normalize_inputs(*args, **kwargs)
    wid = _first(data, ID_KEYS)
    # A bare set name is not a row id; fall back to set_name lookup only when
    # no explicit id key was given. Prefer explicit id keys.
    explicit = None
    for k in ("id", "schedule_id", "zone_schedule_id", "rule_id", "zone_set_id", "uuid"):
        if data.get(k) is not None:
            explicit = str(data.get(k)).strip()
            break
    lookup = explicit or (str(wid).strip() if wid is not None else None)
    if lookup is None and args:
        for a in args:
            if isinstance(a, str) and a.strip():
                lookup = a.strip()
                break
    if lookup is None:
        return None
    with _lock:
        r = _SCHEDULES.get(str(lookup))
        if r is not None:
            return _rule_to_dict(r)
        # Fall back: match set_name for convenience.
        for v in _SCHEDULES.values():
            if str(v.get("set_name", "")).lower() == str(lookup).lower():
                cam = _normalize_camera(_first(data, CAMERA_KEYS))
                if cam == "*" or _camera_matches(str(v.get("camera_id", "*")), cam):
                    return _rule_to_dict(v)
        return None


def list_schedules(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    set_raw = _first(data, SET_KEYS)
    # NOTE: _first(SET_KEYS) also matches "name"/"label"/"title" which may be
    # a camera name positional; only treat as set filter when an explicit
    # set-ish key is present.
    has_set_key = any(
        data.get(k) is not None
        for k in ("set_name", "set", "zone_set", "zones_name", "profile", "preset")
    )
    active_only = bool(data.get("active_only", data.get("activeOnly", False)))
    now_raw = _first(data, NOW_KEYS)
    moment = _resolve_now(now_raw) if now_raw is not None else None
    with _lock:
        items = list(_SCHEDULES.values())
    out: List[Dict[str, Any]] = []
    for r in items:
        if cam_raw is not None and str(cam_raw).strip() not in ("", "*", "all"):
            if not _camera_matches(str(r.get("camera_id", "*")), cam_raw):
                if str(r.get("camera_id", "*")) != "*":
                    continue
        if has_set_key and set_raw is not None:
            if str(r.get("set_name", "")).lower() != str(set_raw).strip().lower():
                continue
        if active_only:
            if moment is None:
                moment = _now()
            if not _covers(r, moment):
                continue
        out.append(_rule_to_dict(r))
    out.sort(key=lambda d: (-int(d.get("priority", 0)), int(d.get("start_minutes", 0))))
    return out


def update_schedule(schedule_id: Any, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    data = _normalize_inputs(*args, **kwargs)
    key = str(schedule_id).strip()
    with _lock:
        rule = _SCHEDULES.get(key)
        if rule is None:
            # Try set_name match.
            for v in _SCHEDULES.values():
                if str(v.get("set_name", "")).lower() == key.lower():
                    cam = _normalize_camera(_first(data, CAMERA_KEYS))
                    if cam == "*" or _camera_matches(str(v.get("camera_id", "*")), cam):
                        rule = v
                        key = str(v.get("id"))
                        break
        if rule is None:
            return None
        if _first(data, ZONES_KEYS) is not None:
            zones = _normalize_zones(_first(data, ZONES_KEYS))
            if zones:
                rule["zones"] = zones
                rule["zone_list"] = zones
                rule["polygon_zones"] = zones
        if _first(data, SET_KEYS) is not None and not any(
            data.get(k) is not None for k in ("id", "schedule_id", "zone_schedule_id", "rule_id", "zone_set_id", "uuid")
        ):
            # Only rename when caller did not pass an explicit id key (else
            # the "name" is the lookup key, not a rename).
            raw = _first(data, SET_KEYS)
            if raw is not None and str(raw).strip():
                rule["set_name"] = str(raw).strip()
                rule["name"] = str(raw).strip()
                rule["zone_set"] = str(raw).strip()
        if _first(data, START_KEYS) is not None:
            sm = _parse_time_to_minutes(_first(data, START_KEYS))
            if sm is not None:
                rule["start_minutes"] = sm
                rule["start_time"] = _minutes_to_str(sm)
        if _first(data, END_KEYS) is not None:
            em = _parse_time_to_minutes(_first(data, END_KEYS))
            if em is not None:
                rule["end_minutes"] = em
                rule["end_time"] = _minutes_to_str(em)
        if _first(data, DAYS_KEYS) is not None:
            days = _parse_days(_first(data, DAYS_KEYS))
            rule["days"] = days
            rule["days_of_week"] = days
            rule["weekdays"] = days
        if _first(data, CAMERA_KEYS) is not None:
            cam = _normalize_camera(_first(data, CAMERA_KEYS))
            rule["camera_id"] = cam
            rule["camera"] = cam
        if _first(data, ENABLED_KEYS) is not None:
            en = bool(_first(data, ENABLED_KEYS))
            rule["enabled"] = en
            rule["active"] = en
        if _first(data, PRIORITY_KEYS) is not None:
            try:
                rule["priority"] = int(_first(data, PRIORITY_KEYS))
            except (TypeError, ValueError):
                pass
        rule["updated_at"] = _now_iso()
        return _rule_to_dict(rule)


def delete_schedule(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    wid = None
    for k in ("id", "schedule_id", "zone_schedule_id", "rule_id", "zone_set_id", "uuid"):
        if data.get(k) is not None:
            wid = str(data.get(k)).strip()
            break
    if wid is None and args:
        for a in args:
            if isinstance(a, str) and a.strip():
                wid = a.strip()
                break
    if wid is None or not str(wid).strip():
        return {"ok": False, "success": False, "error": "schedule id is required", "deleted": False}
    key = str(wid).strip()
    with _lock:
        existed = _SCHEDULES.pop(key, None) is not None
        if not existed:
            # Try set_name delete scoped to camera when given.
            cam = _normalize_camera(_first(data, CAMERA_KEYS))
            for k, v in list(_SCHEDULES.items()):
                if str(v.get("set_name", "")).lower() == key.lower():
                    if cam == "*" or _camera_matches(str(v.get("camera_id", "*")), cam):
                        _SCHEDULES.pop(k, None)
                        existed = True
                        key = k
                        break
    return {"ok": True, "success": True, "deleted": bool(existed), "existed": bool(existed), "id": key}


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    with _lock:
        if cam_raw is None or (isinstance(cam_raw, str) and not cam_raw.strip()):
            n = len(_SCHEDULES)
            _SCHEDULES.clear()
            return {"ok": True, "success": True, "cleared": n, "cleared_count": n}
        target = _normalize_camera(cam_raw)
        doomed = [k for k, w in _SCHEDULES.items() if str(w.get("camera_id", "*")).lower() == str(target).lower()]
        for k in doomed:
            _SCHEDULES.pop(k, None)
        return {"ok": True, "success": True, "cleared": len(doomed), "cleared_count": len(doomed), "camera_id": target}


def set_enabled(schedule_id: Any, enabled: bool = True, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs({"id": schedule_id, "enabled": enabled, **kwargs})
    wid = _first(data, ID_KEYS)
    if wid is None:
        return {"ok": False, "success": False, "error": "schedule id is required"}
    key = str(wid).strip()
    with _lock:
        r = _SCHEDULES.get(key)
        if r is None:
            return {"ok": False, "success": False, "error": "schedule not found", "id": key}
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
    """Schedules whose time window covers ``camera_id`` at ``now``."""
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    now_raw = _first(data, NOW_KEYS)
    moment = _resolve_now(now_raw)
    with _lock:
        items = list(_SCHEDULES.values())
    out = []
    for r in items:
        if not r.get("enabled", True):
            continue
        if cam_raw is not None and not _camera_matches(str(r.get("camera_id", "*")), cam_raw):
            continue
        if _covers(r, moment):
            out.append(_rule_to_dict(r))
    out.sort(key=lambda d: (-int(d.get("priority", 0)), int(d.get("start_minutes", 0))))
    return out


def active_zones(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Zone list of the highest-priority active set (or ``fallback``)."""
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    if cam_raw is None:
        cam_raw = "*"
    now_raw = _first(data, NOW_KEYS)
    moment = _resolve_now(now_raw)
    fallback = _normalize_zones(_first(data, FALLBACK_KEYS))
    active = get_active(camera_id=cam_raw, now=moment)
    if not active:
        return list(fallback)
    top = active[0]
    return copy.deepcopy(top.get("zones", []))


def resolve_zones(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Resolve which zone set applies to ``camera_id`` at ``now``.

    Returns ``{"set_name", "zones", "schedule_id", "fallback_used"}``. When
    no window covers ``now``, returns the ``fallback`` zones (default ``[]``)
    with ``fallback_used=True`` and ``set_name=None``.
    """
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    camera = _normalize_camera(cam_raw)
    now_raw = _first(data, NOW_KEYS)
    moment = _resolve_now(now_raw)
    fallback = _normalize_zones(_first(data, FALLBACK_KEYS))
    active = get_active(camera_id=cam_raw if cam_raw is not None else "*", now=moment)
    if not active:
        return {
            "ok": True, "success": True,
            "camera_id": camera, "camera": camera,
            "set_name": None, "name": None,
            "schedule_id": None, "id": None,
            "zones": list(fallback),
            "fallback_used": True,
            "active_count": 0,
            "checked_at": moment.isoformat(),
        }
    top = active[0]
    return {
        "ok": True, "success": True,
        "camera_id": camera, "camera": camera,
        "set_name": top.get("set_name"), "name": top.get("set_name"),
        "schedule_id": top.get("id"), "id": top.get("id"),
        "zones": copy.deepcopy(top.get("zones", [])),
        "fallback_used": False,
        "active_count": len(active),
        "active_sets": [a.get("set_name") for a in active],
        "checked_at": moment.isoformat(),
    }


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    now_raw = _first(data, NOW_KEYS)
    moment = _resolve_now(now_raw)
    if cam_raw is None:
        with _lock:
            total = len(_SCHEDULES)
            enabled = sum(1 for r in _SCHEDULES.values() if r.get("enabled", True))
        active = get_active(now=moment)
        return {
            "ok": True, "success": True,
            "camera_id": None, "camera": None,
            "total_schedules": total, "enabled_schedules": enabled,
            "active_count": len(active),
            "active_sets": [a.get("set_name") for a in active],
            "checked_at": moment.isoformat(),
        }
    camera = _normalize_camera(cam_raw)
    active = get_active(camera_id=camera, now=moment)
    resolved = resolve_zones(camera_id=camera, now=moment,
                             fallback=_first(data, FALLBACK_KEYS))
    with _lock:
        total = sum(1 for r in _SCHEDULES.values()
                    if _camera_matches(str(r.get("camera_id", "*")), camera))
    return {
        "ok": True, "success": True,
        "camera_id": camera, "camera": camera,
        "set_name": resolved.get("set_name"),
        "zones": resolved.get("zones", []),
        "fallback_used": resolved.get("fallback_used", True),
        "active": active,
        "active_count": len(active),
        "total_schedules": total,
        "checked_at": moment.isoformat(),
    }


def build_engine(*args: Any, **kwargs: Any) -> Any:
    """Build a ``MotionZoningEngine`` with the currently-active zones.

    Kwargs: ``camera_id``, ``now``, ``fallback``/``fallback_zones``,
    ``frame_width``/``width``, ``frame_height``/``height`` plus any
    ``MotionZoningEngine`` options (threshold, grid_*...). Lazy-imports
    ``motion_zoning`` so this module stays dependency-free.
    """
    from app.services.motion_zoning import MotionZoningEngine

    data = _normalize_inputs(*args, **kwargs)
    cam_raw = _first(data, CAMERA_KEYS)
    camera = _normalize_camera(cam_raw)
    now_raw = _first(data, NOW_KEYS)
    moment = _resolve_now(now_raw)
    fallback = _normalize_zones(_first(data, FALLBACK_KEYS))
    resolved = resolve_zones(camera_id=camera, now=moment, fallback=fallback)
    zones = resolved.get("zones", [])
    width = data.get("frame_width", data.get("width", data.get("w", 640)))
    height = data.get("frame_height", data.get("height", data.get("h", 480)))
    try:
        width = int(width)
    except (TypeError, ValueError):
        width = 640
    try:
        height = int(height)
    except (TypeError, ValueError):
        height = 480
    engine_kwargs: Dict[str, Any] = {}
    for k in ("threshold_percentage", "threshold", "grid_rows", "grid_cols",
              "grid_size", "grid", "rows", "cols", "cell_size",
              "min_blob_area", "occupancy_threshold"):
        if data.get(k) is not None:
            engine_kwargs[k] = data.get(k)
    engine = MotionZoningEngine(width, height, zones=zones, **engine_kwargs)
    return engine


# ---------------------------------------------------------------------------
# Service class (singleton mirrors module functions)
# ---------------------------------------------------------------------------

class ZoneSchedulesService:
    def create_schedule(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_schedule(*args, **kwargs)

    def set_day_night(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_day_night(*args, **kwargs)

    def get_schedule(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_schedule(*args, **kwargs)

    def list_schedules(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_schedules(*args, **kwargs)

    def update_schedule(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return update_schedule(*args, **kwargs)

    def delete_schedule(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return delete_schedule(*args, **kwargs)

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

    def active_zones(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return active_zones(*args, **kwargs)

    def resolve_zones(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return resolve_zones(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)

    def build_engine(self, *args: Any, **kwargs: Any) -> Any:
        return build_engine(*args, **kwargs)

    # aliases
    def get_active_zones(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return active_zones(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)

    def check(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)


ZoneSchedules = ZoneSchedulesService
ZoneScheduleService = ZoneSchedulesService
ZoneScheduleManager = ZoneSchedulesService

zone_schedules_service = ZoneSchedulesService()
service = zone_schedules_service
manager = zone_schedules_service
default_service = zone_schedules_service


def get_service() -> ZoneSchedulesService:
    return zone_schedules_service


def create_service() -> ZoneSchedulesService:
    return ZoneSchedulesService()


# Module-level aliases for flexible callers.
create = create_schedule
add_schedule = create_schedule
create_rule = create_schedule
add_rule = create_schedule
add = create_schedule
new_schedule = create_schedule
register_schedule = create_schedule
schedule = create_schedule
get = get_schedule
get_rule = get_schedule
fetch = get_schedule
list_all = list_schedules
all_schedules = list_schedules
list_rules = list_schedules
list_all_schedules = list_schedules
update = update_schedule
patch_schedule = update_schedule
delete = delete_schedule
delete_rule = delete_schedule
remove = delete_schedule
remove_schedule = delete_schedule
clear_all = clear
reset = clear
get_active_zones = active_zones
resolve = resolve_zones
status = get_status

__all__ = [
    "create_schedule",
    "set_day_night",
    "get_schedule",
    "list_schedules",
    "update_schedule",
    "delete_schedule",
    "clear",
    "enable",
    "disable",
    "set_enabled",
    "get_active",
    "active_zones",
    "resolve_zones",
    "get_status",
    "build_engine",
    "ZoneSchedulesService",
    "zone_schedules_service",
    "get_service",
    "create_service",
]
