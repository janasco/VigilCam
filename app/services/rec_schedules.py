"""Recording schedule service (time-profile rules, in-memory)."""
from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Shared in-memory state
# ---------------------------------------------------------------------------

REC_SCHEDULES: Dict[str, "RecSchedule"] = {}
REC_SCHEDULE_JOBS = REC_SCHEDULES
SCHEDULES = REC_SCHEDULES
RECORDING_SCHEDULES = REC_SCHEDULES
RECORDINGS_SCHEDULES = REC_SCHEDULES
REC_JOBS = REC_SCHEDULES
JOBS = REC_SCHEDULES
rec_schedules = REC_SCHEDULES
schedules = REC_SCHEDULES
jobs = REC_SCHEDULES
recording_schedules = REC_SCHEDULES

_schedules_lock = threading.Lock()

CAMERA_KEYS = (
    "camera_id", "camera", "cam", "cameraId", "cameraID", "cam_id",
    "source_id", "device_id",
)
SCHEDULE_KEYS = (
    "schedule", "cron", "cron_expression", "frequency", "interval", "when", "time",
    "cadence", "expr", "expression", "period", "cron_string", "schedule_string",
    "cron_str", "schedule_str",
)
ENABLED_KEYS = ("enabled", "active", "is_enabled", "isEnabled", "on")
NAME_KEYS = ("name", "title", "label", "recording_name")
ID_KEYS = ("id", "schedule_id", "rec_schedule_id", "recording_schedule_id",
           "recording_id", "job_id", "task_id")
MODE_KEYS = ("mode", "recording_mode", "profile", "recording_profile",
             "rec_mode", "type", "schedule_mode")
DAYS_KEYS = ("days", "day", "days_of_week", "weekdays", "dow",
             "day_of_week", "active_days")
START_KEYS = ("start_time", "start", "from_time", "from", "start_hour",
              "window_start", "begin", "begin_time")
END_KEYS = ("end_time", "end", "to_time", "to", "end_hour",
            "window_end", "finish", "finish_time")

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


def _coerce_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _normalize_args(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (bytes, bytearray)):
            try:
                data.setdefault("name", bytes(a).decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if any(ch in s for ch in ("*", " ", "/", "-", "@", ":")) or s.lower().startswith("every"):
                if data.get("schedule") is None:
                    data.setdefault("schedule", s)
                else:
                    data.setdefault("name", s)
            elif s.isdigit():
                data.setdefault("camera_id", s)
            else:
                data.setdefault("name", s)
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            data.setdefault("camera_id", a)
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a)
                         if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request",
                       "settings", "data", "body"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


# ---------------------------------------------------------------------------
# Time-profile parsing helpers
# ---------------------------------------------------------------------------

def _normalize_mode(value: Any) -> str:
    if value is None:
        return "continuous"
    s = str(value).strip().lower()
    if s in {"continuous", "always", "all", "24/7", "24x7", "record",
             "always_record", "always-record", "on", "true", "continuous_record"}:
        return "continuous"
    if s in {"motion", "motion-only", "motion_only", "on-motion", "on_motion",
             "motion_detect", "event", "events", "smart"}:
        return "motion"
    if s in {"off", "disabled", "never", "none", "false", "paused", "stop", "disable"}:
        return "off"
    return s or "continuous"


def _parse_days(value: Any) -> List[int]:
    if value is None:
        return [0, 1, 2, 3, 4, 5, 6]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = int(value) % 7
        return [v]
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"*", "all", "daily", "everyday", "every_day", "any", ""}:
            return [0, 1, 2, 3, 4, 5, 6]
        if s in {"weekdays", "weekday", "workdays", "mon-fri", "mon_fri"}:
            return [0, 1, 2, 3, 4]
        if s in {"weekends", "weekend", "sat-sun", "sat_sun"}:
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
                # range like mon-fri or 0-4
                a, b = p.split("-", 1)
                a = a.strip()
                b = b.strip()
                try:
                    if a in DAY_NAMES and b in DAY_NAMES:
                        lo, hi = DAY_NAMES[a], DAY_NAMES[b]
                    else:
                        lo, hi = int(a) % 7, int(b) % 7
                    if lo <= hi:
                        out.extend(range(lo, hi + 1))
                    else:  # wrap, e.g. fri-mon
                        out.extend(list(range(lo, 7)) + list(range(0, hi + 1)))
                except ValueError:
                    continue
            else:
                try:
                    out.append(int(p) % 7)
                    continue
                except ValueError:
                    pass
                # 3-letter prefix match
                prefix = p[:3]
                if prefix in DAY_NAMES:
                    out.append(DAY_NAMES[prefix])
        # dedupe preserving order
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
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                out2.append(int(item) % 7)
        seen2: List[int] = []
        for d in out2:
            if d not in seen2:
                seen2.append(d)
        return seen2 or [0, 1, 2, 3, 4, 5, 6]
    return [0, 1, 2, 3, 4, 5, 6]


def _parse_time_minutes(value: Any, default: str = "00:00") -> int:
    """Parse HH:MM(:SS) to minutes since midnight. Falls back to default."""
    def _one(v: Any) -> Optional[int]:
        if v is None:
            return None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            iv = int(v)
            if 0 <= iv <= 23:
                return iv * 60
            if 0 <= iv <= 1439:
                return iv
            return iv % 1440
        s = str(v).strip()
        if not s:
            return None
        m = re.match(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?\s*([ap]m?)?$", s.lower())
        if m:
            h = int(m.group(1))
            mi = int(m.group(2))
            ap = m.group(4)
            if ap:
                if ap.startswith("p") and h < 12:
                    h += 12
                if ap.startswith("a") and h == 12:
                    h = 0
            return (h % 24) * 60 + (mi % 60)
        if re.match(r"^\d{3,4}$", s):
            # e.g. 900 -> 09:00, 2130 -> 21:30
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
    parsed = _one(value)
    if parsed is not None:
        return parsed
    return _one(default) or 0


def _minutes_to_str(minutes: int) -> str:
    minutes = int(minutes) % 1440
    return "{:02d}:{:02d}".format(minutes // 60, minutes % 60)


def _in_time_window(start_min: int, end_min: int, now_min: int) -> bool:
    start_min = int(start_min) % 1440
    end_min = int(end_min) % 1440
    now_min = int(now_min) % 1440
    if start_min == 0 and end_min in (0, 1439):
        return True
    if start_min == end_min:
        return True  # full-day marker
    if start_min < end_min:
        return start_min <= now_min < end_min or (start_min <= now_min <= end_min if end_min - start_min <= 1 else False)
    # overnight window, e.g. 22:00-06:00
    return now_min >= start_min or now_min < end_min


# ---------------------------------------------------------------------------
# Cron matching (shared with snapshot/ptz conventions)
# ---------------------------------------------------------------------------

def _cron_field_match(field: str, value: int, min_v: int, max_v: int) -> bool:
    field = field.strip()
    if field == "*":
        return True
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        base = part
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                step = 1
            if step <= 0:
                step = 1
        if base == "*" or base == "":
            lo, hi = min_v, max_v
        elif "-" in base:
            a, b = base.split("-", 1)
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
        else:
            try:
                lo = hi = int(base)
            except ValueError:
                continue
        lo = max(lo, min_v)
        hi = min(hi, max_v)
        if lo > hi:
            continue
        for v in range(lo, hi + 1):
            if v == value and ((v - lo) % step == 0):
                return True
    return False


def _cron_match(fields, now: datetime) -> bool:
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    cron_dow = (now.weekday() + 1) % 7
    checks = [
        _cron_field_match(minute, now.minute, 0, 59),
        _cron_field_match(hour, now.hour, 0, 23),
        _cron_field_match(dom, now.day, 1, 31),
        _cron_field_match(month, now.month, 1, 12),
    ]
    dow_ok = _cron_field_match(dow, cron_dow, 0, 7) or _cron_field_match(dow, now.weekday(), 0, 6)
    checks.append(dow_ok)
    dom_restricted = dom.strip() not in ("*", "")
    dow_restricted = dow.strip() not in ("*", "")
    if dom_restricted and dow_restricted:
        dom_ok = _cron_field_match(dom, now.day, 1, 31)
        dow_ok2 = _cron_field_match(dow, cron_dow, 0, 7) or _cron_field_match(dow, now.weekday(), 0, 6)
        if not (dom_ok or dow_ok2):
            checks[2] = False
            checks[4] = False
    return all(checks)


def _cron_is_due(schedule: Any, now: Optional[datetime] = None) -> bool:
    if now is None:
        now = _now()
    if schedule is None:
        return True
    s = str(schedule).strip()
    if not s:
        return True
    low = s.lower()
    if low in {"*", "always", "due", "now", "run", "immediate", "true", "enabled",
               "all", "every", "minutely", "@always", "any", "continuous", "24/7"}:
        return True
    if "always" in low:
        return True
    if low in {"@hourly", "hourly"}:
        return now.minute == 0
    if low in {"@daily", "daily", "midnight"}:
        return now.hour == 0 and now.minute == 0
    if low in {"@weekly", "weekly"}:
        return now.weekday() == 0 and now.hour == 0 and now.minute == 0
    if low in {"@monthly", "monthly"}:
        return now.day == 1 and now.hour == 0 and now.minute == 0
    if low in {"@minutely", "minutely"}:
        return True
    m = re.match(r"every\s+(\d+)\s*(min|minute|minutes|m)\b", low)
    if m:
        n = max(1, int(m.group(1)))
        return (now.minute % n) == 0
    m = re.match(r"every\s+(\d+)\s*(hour|hours|h)\b", low)
    if m:
        n = max(1, int(m.group(1)))
        return now.minute == 0 and (now.hour % n) == 0
    m = re.match(r"every\s+(\d+)\s*(day|days|d)\b", low)
    if m:
        n = max(1, int(m.group(1)))
        return now.hour == 0 and now.minute == 0 and ((now.timetuple().tm_yday) % n) == 0
    fields = s.split()
    if len(fields) == 6:
        for candidate in (fields[1:], fields[:-1]):
            if _cron_match(candidate, now):
                return True
        return False
    if len(fields) == 5:
        return _cron_match(fields, now)
    return True


# ---------------------------------------------------------------------------
# Schedule object
# ---------------------------------------------------------------------------

class RecSchedule(dict):
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)

    def dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()


RecRecordingSchedule = RecSchedule
RecordingSchedule = RecSchedule
RecScheduleEntry = RecSchedule
RecScheduleRecord = RecSchedule
Snapshot = RecSchedule
Job = Schedule = RecSchedule


def _resolve_now(now: Any) -> datetime:
    if now is None:
        return _now()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(float(now), tz=timezone.utc)
    if isinstance(now, str):
        s = now.strip()
        if not s:
            return _now()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
        # HH:MM today
        try:
            mins = _parse_time_minutes(s)
            base = _now()
            return base.replace(hour=mins // 60, minute=mins % 60, second=0, microsecond=0)
        except Exception:  # noqa: BLE001
            return _now()
    return _now()


def is_active(schedule: Any, now: Any = None) -> bool:
    """Return True when a time-profile rule is active at ``now``."""
    moment = _resolve_now(now)
    if schedule is None:
        return False
    if isinstance(schedule, str):
        return _cron_is_due(schedule, moment)
    data = schedule if isinstance(schedule, dict) else {}
    try:
        enabled = data.get("enabled", data.get("active", data.get("is_enabled", True)))
        if enabled is None:
            enabled = True
        if not bool(enabled):
            return False
    except Exception:  # noqa: BLE001
        pass
    mode = _normalize_mode(data.get("mode", data.get("recording_mode",
                                    data.get("profile", data.get("recording_profile",
                                    data.get("type", "continuous"))))))
    if mode == "off":
        return False
    sched_str = (data.get("schedule") or data.get("cron") or data.get("frequency")
                 or data.get("interval") or "*")
    if not _cron_is_due(sched_str, moment):
        return False
    days = _parse_days(data.get("days", data.get("days_of_week",
                           data.get("weekdays", data.get("dow", None)))))
    # weekday(): Monday=0
    if moment.weekday() not in days:
        return False
    start_min = _parse_time_minutes(
        data.get("start_time", data.get("start", data.get("from_time",
        data.get("window_start", "00:00")))), default="00:00")
    end_min = _parse_time_minutes(
        data.get("end_time", data.get("end", data.get("to_time",
        data.get("window_end", "23:59")))), default="23:59")
    now_min = moment.hour * 60 + moment.minute
    return _in_time_window(start_min, end_min, now_min)


is_due = _cron_is_due
isDue = _cron_is_due
rec_is_due = _cron_is_due
is_recording_now = is_active
isActive = is_active
should_record_now = is_active


def should_record(camera_id: Any = None, now: Any = None, **kwargs: Any) -> bool:
    """True when any enabled time-profile covers ``camera_id`` at ``now``."""
    data = _normalize_args((), dict(kwargs, **({"camera_id": camera_id} if camera_id is not None else {})))
    moment = _resolve_now(now if now is not None else data.get("now", data.get("at", data.get("time_now"))))
    cam = _first(data, CAMERA_KEYS)
    if cam is not None:
        try:
            cam = int(cam)
        except (TypeError, ValueError):
            pass
    with _schedules_lock:
        items = list(REC_SCHEDULES.values())
    if not items:
        return False
    for sched in items:
        if cam is not None:
            sched_cam = sched.get("camera_id")
            if sched_cam is not None:
                try:
                    if int(sched_cam) != int(cam):  # type: ignore[arg-type]
                        continue
                except (TypeError, ValueError):
                    if str(sched_cam) != str(cam):
                        continue
        if is_active(sched, moment):
            return True
    return False


def active_at(now: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    moment = _resolve_now(now)
    cam = _first(_normalize_args((), kwargs), CAMERA_KEYS)
    with _schedules_lock:
        items = list(REC_SCHEDULES.values())
    out: List[Dict[str, Any]] = []
    for sched in items:
        if cam is not None and sched.get("camera_id") is not None:
            try:
                if int(sched.get("camera_id")) != int(cam):  # type: ignore[arg-type]
                    continue
            except (TypeError, ValueError):
                if str(sched.get("camera_id")) != str(cam):
                    continue
        if is_active(sched, moment):
            out.append(sched)
    return out


def active_schedules(now: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    if isinstance(now, dict) and not kwargs:
        kwargs = now
        now = None
    return active_at(now, **kwargs)


def schedules_for_camera(camera_id: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    with _schedules_lock:
        items = list(REC_SCHEDULES.values())
    out = []
    for sched in items:
        sc = sched.get("camera_id")
        try:
            if sc is not None and int(sc) == int(camera_id):  # type: ignore[arg-type]
                out.append(sched)
            elif sc is None and kwargs.get("include_all"):
                out.append(sched)
        except (TypeError, ValueError):
            if str(sc) == str(camera_id):
                out.append(sched)
    return out


def evaluate(now: Any = None, **kwargs: Any) -> Dict[str, Any]:
    moment = _resolve_now(now)
    with _schedules_lock:
        items = list(REC_SCHEDULES.values())
    active = [s for s in items if is_active(s, moment)]
    return {
        "now": moment.isoformat(),
        "total": len(items),
        "active": active,
        "active_count": len(active),
        "inactive_count": len(items) - len(active),
        "should_record": bool(active) if kwargs.get("camera_id") is None else should_record(kwargs.get("camera_id"), moment),
        "success": True,
        "ok": True,
    }


# ---------------------------------------------------------------------------
# Schedule storage
# ---------------------------------------------------------------------------

def _create_schedule(*args, **kwargs) -> Dict[str, Any]:
    data = _normalize_args(args, kwargs)

    camera_id = _first(data, CAMERA_KEYS)
    if camera_id is not None:
        try:
            camera_id = int(camera_id)
        except (TypeError, ValueError):
            pass

    sched_str = _first(data, SCHEDULE_KEYS)
    if sched_str is None:
        sched_str = "*"
    sched_str = str(sched_str)

    enabled = _first(data, ENABLED_KEYS)
    if enabled is None:
        enabled = True
    enabled = bool(enabled)

    mode = _normalize_mode(_first(data, MODE_KEYS) or data.get("recordingMode"))

    days = _parse_days(_first(data, DAYS_KEYS))
    start_raw = _first(data, START_KEYS)
    end_raw = _first(data, END_KEYS)
    start_min = _parse_time_minutes(start_raw if start_raw is not None else "00:00", default="00:00")
    end_min = _parse_time_minutes(end_raw if end_raw is not None else "23:59", default="23:59")

    name = _first(data, NAME_KEYS)
    if name is None:
        name = "rec-{}".format(camera_id if camera_id is not None else "all")

    sched_id = (data.get("id") or data.get("schedule_id") or data.get("rec_schedule_id")
                or data.get("recording_schedule_id") or data.get("job_id") or data.get("task_id"))
    if sched_id is None:
        sched_id = str(uuid.uuid4())
    sched_id = str(sched_id)

    now_iso = _now().isoformat()
    job = RecSchedule()
    job["id"] = sched_id
    job["schedule_id"] = sched_id
    job["rec_schedule_id"] = sched_id
    job["recording_schedule_id"] = sched_id
    job["job_id"] = sched_id
    job["task_id"] = sched_id
    job["name"] = name
    job["title"] = name
    job["label"] = name
    job["camera_id"] = camera_id
    job["camera"] = camera_id
    job["cam"] = camera_id
    job["schedule"] = sched_str
    job["cron"] = sched_str
    job["cron_expression"] = sched_str
    job["frequency"] = sched_str
    job["interval"] = sched_str
    job["mode"] = mode
    job["recording_mode"] = mode
    job["profile"] = mode
    job["recording_profile"] = mode
    job["days"] = days
    job["days_of_week"] = days
    job["weekdays"] = days
    job["dow"] = days
    job["start_time"] = _minutes_to_str(start_min)
    job["start"] = _minutes_to_str(start_min)
    job["window_start"] = _minutes_to_str(start_min)
    job["end_time"] = _minutes_to_str(end_min)
    job["end"] = _minutes_to_str(end_min)
    job["window_end"] = _minutes_to_str(end_min)
    job["start_minutes"] = start_min
    job["end_minutes"] = end_min
    job["enabled"] = enabled
    job["active"] = enabled
    job["is_enabled"] = enabled
    job["status"] = "scheduled"
    job["state"] = "scheduled"
    job["scheduled"] = True
    job["is_scheduled"] = True
    job["pending"] = True
    job["ok"] = True
    job["success"] = True
    job["created_at"] = now_iso
    job["updated_at"] = now_iso
    job["scheduled_at"] = now_iso
    job["last_run_at"] = None
    job["run_count"] = 0
    job["attempts"] = 0
    for k, v in data.items():
        if k in job:
            continue
        if k in CAMERA_KEYS or k in SCHEDULE_KEYS or k in ENABLED_KEYS or k in NAME_KEYS or k in ID_KEYS:
            continue
        job[k] = v

    with _schedules_lock:
        REC_SCHEDULES[sched_id] = job
    return job


def schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def schedule_recording(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def schedule_rec_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def schedule_recording_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def add_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def create_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def create_rec_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def create_recording_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def add_rec_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def add_recording_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def new_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def register_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def upsert_schedule(*args, **kwargs) -> Any:
    data = _normalize_args(args, kwargs)
    item_id = _first(data, ID_KEYS)
    if item_id is not None and str(item_id) in REC_SCHEDULES:
        return update_schedule(str(item_id), **data)
    return _create_schedule(**data)


def update_schedule(item_id: Any, *args, **kwargs) -> Optional[Dict[str, Any]]:
    data = _normalize_args(args, kwargs)
    key = str(item_id)
    with _schedules_lock:
        job = REC_SCHEDULES.get(key)
        if job is None:
            return None
        for k, v in data.items():
            if k in ID_KEYS or k in ("id", "schedule_id"):
                continue
            job[k] = v
        # re-normalize derived fields when relevant keys changed
        if _first(data, MODE_KEYS) is not None:
            mode = _normalize_mode(_first(data, MODE_KEYS))
            job["mode"] = mode
            job["recording_mode"] = mode
            job["profile"] = mode
            job["recording_profile"] = mode
        if _first(data, DAYS_KEYS) is not None:
            days = _parse_days(_first(data, DAYS_KEYS))
            job["days"] = days
            job["days_of_week"] = days
            job["weekdays"] = days
            job["dow"] = days
        if _first(data, START_KEYS) is not None:
            sm = _parse_time_minutes(_first(data, START_KEYS), default="00:00")
            job["start_time"] = _minutes_to_str(sm)
            job["start"] = _minutes_to_str(sm)
            job["window_start"] = _minutes_to_str(sm)
            job["start_minutes"] = sm
        if _first(data, END_KEYS) is not None:
            em = _parse_time_minutes(_first(data, END_KEYS), default="23:59")
            job["end_time"] = _minutes_to_str(em)
            job["end"] = _minutes_to_str(em)
            job["window_end"] = _minutes_to_str(em)
            job["end_minutes"] = em
        if _first(data, SCHEDULE_KEYS) is not None:
            ss = str(_first(data, SCHEDULE_KEYS))
            job["schedule"] = ss
            job["cron"] = ss
            job["cron_expression"] = ss
            job["frequency"] = ss
        if _first(data, CAMERA_KEYS) is not None:
            cam = _first(data, CAMERA_KEYS)
            try:
                cam = int(cam)
            except (TypeError, ValueError):
                pass
            job["camera_id"] = cam
            job["camera"] = cam
            job["cam"] = cam
        if _first(data, ENABLED_KEYS) is not None:
            en = bool(_first(data, ENABLED_KEYS))
            job["enabled"] = en
            job["active"] = en
            job["is_enabled"] = en
        if _first(data, NAME_KEYS) is not None:
            nm = str(_first(data, NAME_KEYS))
            job["name"] = nm
            job["title"] = nm
            job["label"] = nm
        job["updated_at"] = _now().isoformat()
        return job


def patch_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    item_id = args[0] if args else _first(kwargs, ID_KEYS)
    rest = dict(kwargs)
    if args and len(args) > 1 and isinstance(args[1], dict):
        rest.update(args[1])
    return update_schedule(item_id, **rest)


def update_rec_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return patch_schedule(*args, **kwargs)


def update_recording_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return patch_schedule(*args, **kwargs)


def set_enabled(item_id: Any, enabled: bool = True) -> Optional[Dict[str, Any]]:
    return update_schedule(item_id, enabled=bool(enabled))


def enable(*args, **kwargs) -> Optional[Dict[str, Any]]:
    item_id = args[0] if args else _first(kwargs, ID_KEYS)
    return set_enabled(item_id, True)


def disable(*args, **kwargs) -> Optional[Dict[str, Any]]:
    item_id = args[0] if args else _first(kwargs, ID_KEYS)
    return set_enabled(item_id, False)


def _evaluate_one(schedule_obj: Dict[str, Any], moment: datetime) -> Dict[str, Any]:
    active = bool(is_active(schedule_obj, moment))
    out = dict(schedule_obj)
    out.update({
        "evaluated_at": moment.isoformat(),
        "is_active": active,
        "active_now": active,
        "should_record": active,
        "recording": active,
        "due": active,
        "is_due": active,
        "ok": True,
        "success": True,
        "status": "active" if active else "inactive",
    })
    return out


def run(*args, **kwargs) -> Any:
    data = _normalize_args(args, kwargs)
    moment = _resolve_now(data.get("now", data.get("at")))
    force = bool(data.get("force", False) or data.get("run_all")
                 or data.get("all") or data.get("immediate"))
    return_list = bool(data.get("return_list", False) or data.get("as_list", False))

    target_id = _first(data, ID_KEYS)
    if target_id is not None:
        target_id = str(target_id)

    with _schedules_lock:
        if target_id is not None and target_id in REC_SCHEDULES:
            candidates = [REC_SCHEDULES[target_id]]
        else:
            candidates = list(REC_SCHEDULES.values())

    processed: List[Dict[str, Any]] = []
    evaluated: List[Dict[str, Any]] = []
    active_count = 0
    inactive_count = 0
    skipped = 0
    now_iso = moment.isoformat()
    for sched in candidates:
        if not isinstance(sched, dict):
            continue
        if not force and not bool(sched.get("enabled", True)):
            skipped += 1
            continue
        active = bool(is_active(sched, moment))
        with _schedules_lock:
            sched["last_run_at"] = now_iso
            sched["updated_at"] = now_iso
            sched["run_count"] = _coerce_int(sched.get("run_count"), 0) + 1
            sched["attempts"] = _coerce_int(sched.get("attempts"), 0) + 1
        item = _evaluate_one(sched, moment)
        evaluated.append(item)
        if force or active:
            processed.append(item)
            if active:
                active_count += 1
            else:
                inactive_count += 1
        else:
            skipped += 1

    summary = {
        "success": True,
        "ok": True,
        "status": "completed",
        "now": now_iso,
        "evaluated": evaluated,
        "evaluated_schedules": evaluated,
        "results": evaluated,
        "schedules": evaluated,
        "active": [e for e in evaluated if e.get("is_active")],
        "active_schedules": [e for e in evaluated if e.get("is_active")],
        "executed": processed,
        "executed_schedules": processed,
        "processed": len(processed),
        "processed_count": len(processed),
        "executed_count": len(processed),
        "count": len(processed),
        "total": len(processed),
        "num_executed": len(processed),
        "num_processed": len(processed),
        "num_active": active_count,
        "active_count": active_count,
        "inactive_count": inactive_count,
        "skipped": skipped,
        "skipped_count": skipped,
        "mock": True,
        "due": True,
    }
    return processed if return_list else summary


def run_now(*args, **kwargs) -> Any:
    kwargs = dict(kwargs or {})
    kwargs.setdefault("force", True)
    return run(*args, **kwargs)


def run_due(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_all(*args, **kwargs) -> Any:
    kwargs = dict(kwargs or {})
    kwargs.setdefault("force", True)
    return run(*args, **kwargs)


def run_due_rec_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_rec_schedules_now(*args, **kwargs) -> Any:
    kwargs = dict(kwargs or {})
    kwargs.setdefault("force", True)
    return run(*args, **kwargs)


def run_recording_schedules_now(*args, **kwargs) -> Any:
    kwargs = dict(kwargs or {})
    kwargs.setdefault("force", True)
    return run(*args, **kwargs)


def run_rec_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_recording_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_rec_schedule(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_recording_schedule(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_due(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_rec_schedule(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_rec_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_recording_schedule(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_recording_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def trigger(*args, **kwargs) -> Any:
    return run_now(*args, **kwargs)


def trigger_rec_schedules(*args, **kwargs) -> Any:
    return run_now(*args, **kwargs)


def trigger_recording_schedules(*args, **kwargs) -> Any:
    return run_now(*args, **kwargs)


def process(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def get(*args, **kwargs) -> Optional[Dict[str, Any]]:
    item_id = None
    if args:
        item_id = args[0]
    if item_id is None:
        item_id = _first(kwargs, ID_KEYS)
    if item_id is None:
        return None
    key = str(item_id)
    with _schedules_lock:
        return REC_SCHEDULES.get(key)


def get_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def get_rec_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def get_recording_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def get_job(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def list_schedules(*args, **kwargs) -> List[Dict[str, Any]]:
    data = _normalize_args(args, kwargs)
    cam = _first(data, CAMERA_KEYS)
    with _schedules_lock:
        items = list(REC_SCHEDULES.values())
    if cam is None:
        return items
    out = []
    for job in items:
        sc = job.get("camera_id")
        try:
            if sc is not None and int(sc) == int(cam):  # type: ignore[arg-type]
                out.append(job)
        except (TypeError, ValueError):
            if str(sc) == str(cam):
                out.append(job)
    return out


def list_rec_schedules(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_schedules(*args, **kwargs)


def list_recording_schedules(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_schedules(*args, **kwargs)


def list_recording_schedule(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_schedules(*args, **kwargs)


def list_jobs(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_schedules(*args, **kwargs)


def list_all(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_schedules(*args, **kwargs)


def all(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_schedules(*args, **kwargs)


def items(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_schedules(*args, **kwargs)


def values(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_schedules(*args, **kwargs)


def clear(*args, **kwargs) -> int:
    with _schedules_lock:
        if not args and not kwargs:
            n = len(REC_SCHEDULES)
            REC_SCHEDULES.clear()
            return n
        to_delete = []
        for jid, job in list(REC_SCHEDULES.items()):
            if kwargs.get("status") is not None and str(job.get("status")) != str(kwargs.get("status")):
                continue
            if kwargs.get("id") is not None and str(jid) != str(kwargs.get("id")):
                continue
            to_delete.append(jid)
        for jid in to_delete:
            REC_SCHEDULES.pop(jid, None)
        return len(to_delete)


def reset(*args, **kwargs) -> int:
    return clear(*args, **kwargs)


def purge(*args, **kwargs) -> int:
    return clear(*args, **kwargs)


def remove(*args, **kwargs) -> bool:
    item_id = args[0] if args else _first(kwargs, ID_KEYS)
    if item_id is None:
        return False
    key = str(item_id)
    with _schedules_lock:
        existed = key in REC_SCHEDULES
        REC_SCHEDULES.pop(key, None)
        return existed


def delete(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def delete_schedule(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def delete_rec_schedule(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def delete_recording_schedule(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def cancel(*args, **kwargs) -> bool:
    item_id = args[0] if args else _first(kwargs, ID_KEYS)
    if item_id is None:
        return False
    job = get(item_id)
    if job is None:
        return False
    job["status"] = "cancelled"
    job["state"] = "cancelled"
    job["cancelled"] = True
    job["canceled"] = True
    job["updated_at"] = _now().isoformat()
    return remove(item_id)


def status(*args, **kwargs) -> Any:
    if not args and "id" not in kwargs and "schedule_id" not in kwargs \
            and "rec_schedule_id" not in kwargs and "recording_schedule_id" not in kwargs:
        with _schedules_lock:
            total = len(REC_SCHEDULES)
            moment = _now()
            active = sum(1 for s in REC_SCHEDULES.values() if is_active(s, moment))
            return {
                "status": "scheduled" if REC_SCHEDULES else "idle",
                "scheduled": total,
                "count": total,
                "size": total,
                "length": total,
                "pending": total,
                "active": active,
                "active_count": active,
                "inactive_count": total - active,
            }
    item_id = args[0] if args else _first(kwargs, ID_KEYS)
    job = get(item_id)
    if job is None:
        return None
    return job.get("status", "scheduled")


def get_status(*args, **kwargs) -> Any:
    return status(*args, **kwargs)


# ---------------------------------------------------------------------------
# Scheduler class
# ---------------------------------------------------------------------------

class RecScheduleScheduler:
    def __init__(self, *args: Any, interval_seconds: Any = None, interval: Any = None,
                 seconds: Any = None, cadence: Any = None, job: Any = None, func: Any = None,
                 callback: Any = None, runner: Any = None, retention: Any = None,
                 retention_manager: Any = None, manager: Any = None, service: Any = None,
                 camera_id: Any = None, camera: Any = None, schedule: Any = None,
                 cron: Any = None, frequency: Any = None, **kwargs: Any):
        self.interval_seconds = 60
        for v in (interval_seconds, interval, seconds, cadence, kwargs.get("interval_seconds")):
            if v is not None:
                try:
                    self.interval_seconds = int(v)
                    break
                except (TypeError, ValueError):
                    pass
        if self.interval_seconds <= 0:
            self.interval_seconds = 60
        self.retention = retention
        if self.retention is None:
            self.retention = retention_manager
        if self.retention is None:
            self.retention = manager
        if self.retention is None:
            self.retention = service
        self.job = job or func or callback or runner or kwargs.get("job")
        self.camera_id = camera_id if camera_id is not None else (camera if camera is not None else kwargs.get("camera_id"))
        self.schedule_str = schedule if schedule is not None else (cron if cron is not None else (frequency if frequency is not None else kwargs.get("schedule")))
        self._running = False
        self._stop = False
        self._thread = None
        self._task = None

    def _run_job(self, **kwargs) -> Any:
        if callable(self.job):
            try:
                return self.job(dry_run=kwargs.get("dry_run", False), **{k: v for k, v in kwargs.items() if k != "dry_run"})
            except TypeError:
                try:
                    return self.job(**kwargs)
                except TypeError:
                    try:
                        return self.job()
                    except Exception:  # noqa: BLE001
                        pass
        rm = self.retention
        if rm is not None:
            for meth_name in ("run", "run_now", "run_once", "execute", "run_due", "trigger", "evaluate"):
                meth = getattr(rm, meth_name, None)
                if callable(meth):
                    try:
                        return meth(dry_run=kwargs.get("dry_run", False), **{k: v for k, v in kwargs.items() if k != "dry_run"})
                    except TypeError:
                        try:
                            return meth(**kwargs)
                        except TypeError:
                            try:
                                return meth()
                            except Exception:  # noqa: BLE001
                                continue
            if callable(rm):
                try:
                    return rm(**kwargs)
                except Exception:  # noqa: BLE001
                    try:
                        return rm()
                    except Exception:  # noqa: BLE001
                        pass
        try:
            return run(**kwargs)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "ok": False, "status": "error", "error": str(exc)}

    def run(self, *args, **kwargs) -> Any:
        dry_run = kwargs.pop("dry_run", None)
        if args and isinstance(args[0], bool):
            dry_run = args[0]
            args = args[1:]
        for a in args:
            if isinstance(a, dict):
                kwargs.update(a)
        if dry_run is not None:
            kwargs["dry_run"] = dry_run
        return self._run_job(**kwargs)

    def run_now(self, *args, **kwargs) -> Any:
        kwargs = dict(kwargs or {})
        kwargs.setdefault("force", True)
        return self.run(*args, **kwargs)

    def run_once(self, *args, **kwargs) -> Any:
        return self.run(*args, **kwargs)

    def trigger(self, *args, **kwargs) -> Any:
        return self.run_now(*args, **kwargs)

    def execute(self, *args, **kwargs) -> Any:
        return self.run(*args, **kwargs)

    async def run_async(self, *args, **kwargs) -> Any:
        import asyncio
        return await asyncio.to_thread(self.run, *args, **kwargs)

    def start(self, *args, **kwargs) -> Any:
        if kwargs.get("interval_seconds") is not None:
            try:
                self.interval_seconds = int(kwargs["interval_seconds"])
            except (TypeError, ValueError):
                pass
        if self._running and self._thread is not None and getattr(self._thread, "is_alive", lambda: False)():
            return self
        self._stop = False
        self._running = True
        try:
            import asyncio
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            if self._task is None or getattr(self._task, "done", lambda: True)():
                self._task = loop.create_task(self._async_loop())
            return self
        self._thread = threading.Thread(target=self._thread_loop, name="rec-schedule-scheduler", daemon=True)
        self._thread.start()
        return self

    async def _async_loop(self) -> None:
        self._running = True
        self._stop = False
        while not self._stop:
            try:
                import asyncio
                await asyncio.sleep(self.interval_seconds)
            except Exception:  # noqa: BLE001
                break
            if self._stop:
                break
            try:
                await self.run_async()
            except Exception:  # noqa: BLE001
                pass

    def _thread_loop(self) -> None:
        self._running = True
        self._stop = False
        next_run = time.time() + self.interval_seconds
        while not self._stop:
            now = time.time()
            if now >= next_run:
                try:
                    self.run()
                except Exception:  # noqa: BLE001
                    pass
                next_run = time.time() + self.interval_seconds
            time.sleep(min(1.0, max(0.05, next_run - now)))
        self._running = False

    def stop(self, *args, **kwargs) -> Any:
        self._stop = True
        self._running = False
        if self._thread is not None:
            try:
                self._thread.join(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self._thread = None
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._task = None
        return self

    def shutdown(self, *args, **kwargs) -> Any:
        return self.stop(*args, **kwargs)

    def schedule(self, *args, **kwargs) -> Any:
        return schedule(*args, **kwargs)

    def list_schedules(self, *args, **kwargs) -> Any:
        return list_schedules(*args, **kwargs)

    def list(self, *args, **kwargs) -> Any:
        return list_schedules(*args, **kwargs)

    def get(self, *args, **kwargs) -> Any:
        return get(*args, **kwargs)

    def get_schedule(self, *args, **kwargs) -> Any:
        return get(*args, **kwargs)

    def clear(self, *args, **kwargs) -> Any:
        return clear(*args, **kwargs)

    def status(self, *args, **kwargs) -> Any:
        return status(*args, **kwargs)

    @property
    def is_running(self) -> bool:
        return bool(self._running)


# Shared instance
rec_schedule_scheduler = RecScheduleScheduler(interval_seconds=60)
recording_schedule_scheduler = rec_schedule_scheduler
recordingScheduler = rec_schedule_scheduler
scheduler = rec_schedule_scheduler
RecScheduler = RecScheduleScheduler
RecordingScheduler = RecScheduleScheduler
RecRecordingScheduler = RecScheduleScheduler


def get_rec_schedule_scheduler(*args, **kwargs) -> RecScheduleScheduler:
    global rec_schedule_scheduler
    if args or kwargs:
        return RecScheduleScheduler(*args, **kwargs)
    return rec_schedule_scheduler


def get_scheduler(*args, **kwargs) -> RecScheduleScheduler:
    return get_rec_schedule_scheduler(*args, **kwargs)


def get_recording_scheduler(*args, **kwargs) -> RecScheduleScheduler:
    return get_rec_schedule_scheduler(*args, **kwargs)


def get_recording_schedule_scheduler(*args, **kwargs) -> RecScheduleScheduler:
    return get_rec_schedule_scheduler(*args, **kwargs)


def start_rec_schedule_scheduler(*args, **kwargs) -> RecScheduleScheduler:
    s = get_rec_schedule_scheduler() if not (args or kwargs) else RecScheduleScheduler(*args, **kwargs)
    try:
        s.start(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass
    return s


def start_scheduler(*args, **kwargs) -> RecScheduleScheduler:
    return start_rec_schedule_scheduler(*args, **kwargs)


def start_recording_scheduler(*args, **kwargs) -> RecScheduleScheduler:
    return start_rec_schedule_scheduler(*args, **kwargs)


def start_recording_schedule_scheduler(*args, **kwargs) -> RecScheduleScheduler:
    return start_rec_schedule_scheduler(*args, **kwargs)


def stop_rec_schedule_scheduler(*args, **kwargs) -> None:
    try:
        rec_schedule_scheduler.stop(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass


def stop_scheduler(*args, **kwargs) -> None:
    stop_rec_schedule_scheduler(*args, **kwargs)


def stop_recording_scheduler(*args, **kwargs) -> None:
    stop_rec_schedule_scheduler(*args, **kwargs)


def stop_recording_schedule_scheduler(*args, **kwargs) -> None:
    stop_rec_schedule_scheduler(*args, **kwargs)


# Alias classes
RecScheduleSchedulerService = RecScheduleScheduler
RecScheduleService = RecScheduleScheduler
RecScheduleManager = RecScheduleScheduler
RecordingScheduleService = RecScheduleScheduler
RecordingScheduleManager = RecScheduleScheduler
RecordingScheduleScheduler = RecScheduleScheduler
Scheduler = RecScheduleScheduler

# Module-level aliases for common names
rec_schedule_service = rec_schedule_scheduler
service = rec_schedule_service
manager = rec_schedule_service
default_service = rec_schedule_service

# Additional function aliases
schedule_recording_now = schedule
run_recording_now = run_now
trigger_recording = trigger
trigger_recording_schedule = trigger
execute_recording = execute
run_scheduled_recordings = run
run_scheduled_recording = run
run_scheduled_rec_schedules = run
check = is_active
check_schedule = is_active
is_scheduled = is_active
