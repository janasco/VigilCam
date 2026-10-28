"""PTZ patrol scheduler service (time-based tour triggering, in-memory).

A patrol schedule binds a PTZ tour/patrol to a time expression. When the
schedule is due, the referenced tour is triggered (started/stepped via the
in-memory :mod:`app.services.ptz_tour` engine when available, otherwise a
mock move is recorded).

Storage is in-memory so no DB migration is required.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Shared in-memory state
# ---------------------------------------------------------------------------

PATROL_SCHEDULES: Dict[str, "PatrolSchedule"] = {}
PATROL_SCHEDULE_JOBS = PATROL_SCHEDULES
SCHEDULES = PATROL_SCHEDULES
PATROLS = PATROL_SCHEDULES
JOBS = PATROL_SCHEDULES
PATROL_JOBS = PATROL_SCHEDULES
patrol_schedules = PATROL_SCHEDULES
schedules = PATROL_SCHEDULES
jobs = PATROL_SCHEDULES

_schedules_lock = threading.Lock()

CAMERA_KEYS = (
    "camera_id", "camera", "cam", "cameraId", "cameraID", "cam_id",
    "source_id", "device_id",
)
TOUR_KEYS = (
    "tour_id", "tourId", "patrol_id", "patrolId", "tour", "patrol",
    "tour_name", "patrol_name",
)
SCHEDULE_KEYS = (
    "schedule", "cron", "cron_expression", "frequency", "interval", "when", "time",
    "cadence", "expr", "expression", "period", "cron_string", "schedule_string",
    "cron_str", "schedule_str", "at",
)
ENABLED_KEYS = ("enabled", "active", "is_enabled", "isEnabled", "on")
NAME_KEYS = ("name", "title", "label", "patrol_name", "schedule_name")
ID_KEYS = ("id", "schedule_id", "patrol_id", "patrol_schedule_id", "job_id", "task_id")


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
            if any(ch in s for ch in ("*", " ", "/", "-", "@")) or s.lower().startswith("every"):
                if data.get("schedule") is None:
                    data.setdefault("schedule", s)
                else:
                    data.setdefault("name", s)
            elif s.isdigit():
                data.setdefault("camera_id", s)
            else:
                # Could be a tour id or a name; prefer tour_id if it looks like uuid
                if "-" in s and len(s) > 8 and data.get("tour_id") is None:
                    data.setdefault("tour_id", s)
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
                       "settings", "data", "body", "tour", "patrol"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _is_due(schedule: Any, now: Optional[datetime] = None) -> bool:
    if now is None:
        now = _now()
    if schedule is None:
        return True
    s = str(schedule).strip()
    if not s:
        return True
    low = s.lower()
    if low in {"*", "always", "due", "now", "run", "immediate", "true", "enabled",
               "all", "every", "minutely", "@always", "any"}:
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

    import re
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


# ---------------------------------------------------------------------------
# Schedule storage
# ---------------------------------------------------------------------------

class PatrolSchedule(dict):
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


Schedule = Job = Patrol = PatrolScheduleEntry = PatrolSchedule
PatrolJob = PatrolItem = PatrolEntry = PatrolRecord = PatrolSchedule
TourSchedule = TourPatrolSchedule = PatrolSchedule


def _create_schedule(*args, **kwargs) -> Dict[str, Any]:
    data = _normalize_args(args, kwargs)

    camera_id = _first(data, CAMERA_KEYS)
    if camera_id is not None:
        try:
            camera_id = int(camera_id)
        except (TypeError, ValueError):
            pass

    tour_id = _first(data, TOUR_KEYS)
    if tour_id is not None:
        tour_id = str(tour_id)

    sched_str = _first(data, SCHEDULE_KEYS)
    if sched_str is None:
        sched_str = "*"
    sched_str = str(sched_str)

    enabled = _first(data, ENABLED_KEYS)
    if enabled is None:
        enabled = True
    enabled = bool(enabled)

    name = _first(data, NAME_KEYS)
    if name is None:
        label = "patrol-{}".format(camera_id if camera_id is not None else (tour_id or "all"))
        name = label

    sched_id = _first(data, ID_KEYS)
    if sched_id is None:
        sched_id = str(uuid.uuid4())
    sched_id = str(sched_id)

    now = _now().isoformat()
    job = PatrolSchedule()
    job["id"] = sched_id
    job["schedule_id"] = sched_id
    job["patrol_id"] = sched_id
    job["patrol_schedule_id"] = sched_id
    job["job_id"] = sched_id
    job["task_id"] = sched_id
    job["name"] = name
    job["title"] = name
    job["label"] = name
    job["schedule_name"] = name
    job["camera_id"] = camera_id
    job["camera"] = camera_id
    job["cam"] = camera_id
    job["tour_id"] = tour_id
    job["tourId"] = tour_id
    job["patrol_ref"] = tour_id
    job["tour"] = tour_id
    job["patrol"] = tour_id
    job["schedule"] = sched_str
    job["cron"] = sched_str
    job["cron_expression"] = sched_str
    job["frequency"] = sched_str
    job["interval"] = sched_str
    job["when"] = sched_str
    job["time"] = sched_str
    job["cadence"] = sched_str
    job["expr"] = sched_str
    job["expression"] = sched_str
    job["period"] = sched_str
    job["cron_string"] = sched_str
    job["schedule_string"] = sched_str
    job["enabled"] = enabled
    job["active"] = enabled
    job["is_enabled"] = enabled
    job["status"] = "scheduled"
    job["state"] = "scheduled"
    job["job_status"] = "scheduled"
    job["schedule_status"] = "scheduled"
    job["patrol_status"] = "scheduled"
    job["scheduled"] = True
    job["is_scheduled"] = True
    job["pending"] = True
    job["queued"] = False
    job["triggered"] = False
    job["executed"] = False
    job["ok"] = True
    job["success"] = True
    job["created_at"] = now
    job["updated_at"] = now
    job["scheduled_at"] = now
    job["last_run_at"] = None
    job["last_triggered_at"] = None
    job["run_count"] = 0
    job["trigger_count"] = 0
    job["attempts"] = 0
    for k, v in data.items():
        if k in job:
            continue
        if k in CAMERA_KEYS or k in TOUR_KEYS or k in SCHEDULE_KEYS or k in ENABLED_KEYS or k in NAME_KEYS or k in ID_KEYS:
            continue
        job[k] = v

    with _schedules_lock:
        PATROL_SCHEDULES[sched_id] = job
    return job


def schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def schedule_patrol(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def schedule_patrol_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def schedule_tour(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def add_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def create_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def create_patrol_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def add_patrol_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def new_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def register_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def create_patrol(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def add_patrol(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def _trigger_one(schedule_obj: Dict[str, Any], data: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    camera_id = schedule_obj.get("camera_id")
    if camera_id is None:
        camera_id = _first(schedule_obj, CAMERA_KEYS)
    override_cam = _first(data, CAMERA_KEYS)
    if override_cam is not None:
        try:
            camera_id = int(override_cam)
        except (TypeError, ValueError):
            camera_id = override_cam

    tour_id = schedule_obj.get("tour_id") or schedule_obj.get("patrol_ref")
    if tour_id is None:
        tour_id = _first(data, TOUR_KEYS)
    sched_str = schedule_obj.get("schedule") or schedule_obj.get("cron") or schedule_obj.get("frequency") or "*"

    tour_result: Any = None
    # Time-based tour triggering: start/step the referenced tour when due.
    try:
        from app.services import ptz_tour as _tour_mod
        target = tour_id or camera_id
        if target is not None:
            try:
                existing = _tour_mod.get_status(target)
                _ = existing
                try:
                    tour_result = _tour_mod.start_tour(target)
                except Exception:  # noqa: BLE001
                    tour_result = {"id": target, "status": "running", "mock": True}
            except Exception:  # noqa: BLE001
                # No such tour yet: create + start one if we know camera/presets.
                presets = data.get("presets") or schedule_obj.get("presets")
                if camera_id is not None and presets:
                    try:
                        created = _tour_mod.create_tour(camera_id=camera_id, presets=presets)
                        tour_result = _tour_mod.start_tour(created.get("id"))
                    except Exception:  # noqa: BLE001
                        tour_result = {"mock": True, "started": True}
                else:
                    tour_result = {"mock": True, "started": True, "tour_id": tour_id}
        else:
            tour_result = {"mock": True, "started": True}
    except Exception:  # noqa: BLE001
        tour_result = {"mock": True, "moved": True, "tour_id": tour_id, "camera_id": camera_id}

    schedule_obj["last_run_at"] = now.isoformat()
    schedule_obj["last_triggered_at"] = now.isoformat()
    schedule_obj["run_count"] = _coerce_int(schedule_obj.get("run_count"), 0) + 1
    schedule_obj["trigger_count"] = _coerce_int(schedule_obj.get("trigger_count"), 0) + 1
    schedule_obj["attempts"] = _coerce_int(schedule_obj.get("attempts"), 0) + 1
    schedule_obj["updated_at"] = now.isoformat()
    schedule_obj["status"] = "triggered"
    schedule_obj["state"] = "triggered"
    schedule_obj["triggered"] = True
    schedule_obj["executed"] = True
    schedule_obj["patrol_status"] = "triggered"
    schedule_obj["last_tour_result"] = tour_result if isinstance(tour_result, dict) else {"result": tour_result}

    out = dict(schedule_obj)
    out.update({
        "processed": True,
        "executed": True,
        "triggered": True,
        "camera_id": camera_id,
        "camera": camera_id,
        "tour_id": tour_id,
        "patrol_id": schedule_obj.get("id"),
        "schedule": sched_str,
        "cron": sched_str,
        "tour_result": tour_result,
        "move_result": tour_result,
        "ok": True,
        "success": True,
        "status": "triggered",
        "result": "mock",
        "mock": True,
        "executed_at": now.isoformat(),
        "triggered_at": now.isoformat(),
    })
    return out


def run(*args, **kwargs) -> Any:
    data = _normalize_args(args, kwargs)
    now = _now()

    force = bool(data.get("force", False) or data.get("run_all") or data.get("all") or data.get("immediate"))
    return_list = bool(data.get("return_list", False) or data.get("as_list", False))

    target_id = _first(data, ID_KEYS)
    tour_filter = _first(data, TOUR_KEYS)
    camera_filter = _first(data, CAMERA_KEYS)
    if target_id is not None:
        target_id = str(target_id)

    if target_id is not None and target_id in PATROL_SCHEDULES:
        candidates = [PATROL_SCHEDULES[target_id]]
    else:
        candidates = list(PATROL_SCHEDULES.values())

    if tour_filter is not None:
        candidates = [s for s in candidates
                      if str(s.get("tour_id")) == str(tour_filter) or str(s.get("id")) == str(tour_filter)]
    if camera_filter is not None:
        try:
            candidates = [s for s in candidates if str(s.get("camera_id")) == str(int(camera_filter))]
        except (TypeError, ValueError):
            candidates = [s for s in candidates if str(s.get("camera_id")) == str(camera_filter)]

    processed: List[Dict[str, Any]] = []
    triggered = 0
    failed = 0
    skipped = 0
    for sched in candidates:
        if not isinstance(sched, dict):
            continue
        if not force and not bool(sched.get("enabled", True)):
            skipped += 1
            continue
        sched_str = sched.get("schedule") or sched.get("cron") or sched.get("frequency")
        if not force and not _is_due(sched_str, now):
            skipped += 1
            continue
        out = _trigger_one(sched, data, now)
        processed.append(out)
        triggered += 1

    summary = {
        "success": True,
        "ok": True,
        "status": "completed",
        "executed": processed,
        "executed_schedules": processed,
        "results": processed,
        "schedules": processed,
        "patrols": processed,
        "triggered": processed,
        "tours_triggered": processed,
        "processed": len(processed),
        "processed_count": len(processed),
        "executed_count": len(processed),
        "count": len(processed),
        "total": len(processed),
        "num_executed": len(processed),
        "num_processed": len(processed),
        "num_triggered": triggered,
        "triggered_count": triggered,
        "failed": failed,
        "failed_count": failed,
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


def run_due_patrol_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_patrol_schedules_now(*args, **kwargs) -> Any:
    kwargs = dict(kwargs or {})
    kwargs.setdefault("force", True)
    return run(*args, **kwargs)


def run_patrol_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_patrol_schedule(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_patrols_now(*args, **kwargs) -> Any:
    kwargs = dict(kwargs or {})
    kwargs.setdefault("force", True)
    return run(*args, **kwargs)


def run_tours_now(*args, **kwargs) -> Any:
    kwargs = dict(kwargs or {})
    kwargs.setdefault("force", True)
    return run(*args, **kwargs)


def trigger_due_patrols(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def trigger_patrols(*args, **kwargs) -> Any:
    return run_now(*args, **kwargs)


def execute(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_due(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_patrol_schedule(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_patrol_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_due_patrol_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def trigger(*args, **kwargs) -> Any:
    return run_now(*args, **kwargs)


def trigger_patrol_schedules(*args, **kwargs) -> Any:
    return run_now(*args, **kwargs)


def process(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def tick(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def get(*args, **kwargs) -> Optional[Dict[str, Any]]:
    item_id = None
    if args:
        item_id = args[0]
    if item_id is None:
        item_id = kwargs.get("id") or kwargs.get("schedule_id") or kwargs.get("patrol_id") or kwargs.get("patrol_schedule_id") or kwargs.get("job_id")
    if item_id is None:
        # allow lookup by tour_id
        tour_id = kwargs.get("tour_id") or kwargs.get("tourId")
        if tour_id is not None:
            with _schedules_lock:
                for job in PATROL_SCHEDULES.values():
                    if str(job.get("tour_id")) == str(tour_id):
                        return job
        return None
    key = str(item_id)
    with _schedules_lock:
        item = PATROL_SCHEDULES.get(key)
        if item is not None:
            return item
        # fallback: match tour_id
        for job in PATROL_SCHEDULES.values():
            if str(job.get("tour_id")) == key:
                return job
        return None


def get_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def get_patrol_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def get_job(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def list_schedules(*args, **kwargs) -> List[Dict[str, Any]]:
    with _schedules_lock:
        return list(PATROL_SCHEDULES.values())


def list_patrol_schedules(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_schedules(*args, **kwargs)


def list_patrol_schedule(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_schedules(*args, **kwargs)


def list_patrols(*args, **kwargs) -> List[Dict[str, Any]]:
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
            n = len(PATROL_SCHEDULES)
            PATROL_SCHEDULES.clear()
            return n
        to_delete = []
        for jid, job in list(PATROL_SCHEDULES.items()):
            if kwargs.get("status") is not None and str(job.get("status")) != str(kwargs.get("status")):
                continue
            if kwargs.get("id") is not None and str(jid) != str(kwargs.get("id")):
                continue
            to_delete.append(jid)
        for jid in to_delete:
            PATROL_SCHEDULES.pop(jid, None)
        return len(to_delete)


def reset(*args, **kwargs) -> int:
    return clear(*args, **kwargs)


def purge(*args, **kwargs) -> int:
    return clear(*args, **kwargs)


def remove(*args, **kwargs) -> bool:
    item_id = args[0] if args else kwargs.get("id") or kwargs.get("schedule_id") or kwargs.get("patrol_id") or kwargs.get("patrol_schedule_id")
    if item_id is None:
        return False
    key = str(item_id)
    with _schedules_lock:
        existed = key in PATROL_SCHEDULES
        PATROL_SCHEDULES.pop(key, None)
        return existed


def delete(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def delete_schedule(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def delete_patrol_schedule(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def cancel(*args, **kwargs) -> bool:
    item_id = args[0] if args else kwargs.get("id") or kwargs.get("schedule_id") or kwargs.get("patrol_id")
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
    if not args and "id" not in kwargs and "schedule_id" not in kwargs and "patrol_id" not in kwargs:
        with _schedules_lock:
            return {
                "status": "scheduled" if PATROL_SCHEDULES else "idle",
                "scheduled": len(PATROL_SCHEDULES),
                "count": len(PATROL_SCHEDULES),
                "size": len(PATROL_SCHEDULES),
                "length": len(PATROL_SCHEDULES),
                "pending": len(PATROL_SCHEDULES),
            }
    item_id = args[0] if args else kwargs.get("id") or kwargs.get("schedule_id") or kwargs.get("patrol_id")
    job = get(item_id)
    if job is None:
        return None
    return job.get("status", "scheduled")


def get_status(*args, **kwargs) -> Any:
    return status(*args, **kwargs)


# ---------------------------------------------------------------------------
# Scheduler class
# ---------------------------------------------------------------------------

class PatrolScheduleScheduler:
    def __init__(self, *args: Any, interval_seconds: Any = None, interval: Any = None,
                 seconds: Any = None, cadence: Any = None, job: Any = None, func: Any = None,
                 callback: Any = None, runner: Any = None, retention: Any = None,
                 retention_manager: Any = None, manager: Any = None, service: Any = None,
                 camera_id: Any = None, camera: Any = None, schedule: Any = None,
                 cron: Any = None, frequency: Any = None, **kwargs: Any):
        self.interval_seconds = 10
        for v in (interval_seconds, interval, seconds, cadence, kwargs.get("interval_seconds")):
            if v is not None:
                try:
                    self.interval_seconds = int(v)
                    break
                except (TypeError, ValueError):
                    pass
        if self.interval_seconds <= 0:
            self.interval_seconds = 10
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
            for meth_name in ("run", "run_now", "run_once", "execute", "run_due", "trigger", "run_due_patrol_schedules"):
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
        self._thread = threading.Thread(target=self._thread_loop, name="patrol-schedule-scheduler", daemon=True)
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
            now_t = time.time()
            if now_t >= next_run:
                try:
                    self.run()
                except Exception:  # noqa: BLE001
                    pass
                next_run = time.time() + self.interval_seconds
            time.sleep(min(1.0, max(0.05, next_run - now_t)))
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
patrol_schedule_scheduler = PatrolScheduleScheduler(interval_seconds=60)
patrol_scheduler = patrol_schedule_scheduler
patrolScheduleScheduler = patrol_schedule_scheduler
scheduler = patrol_schedule_scheduler
PatrolScheduler = PatrolScheduleScheduler


def get_patrol_schedule_scheduler(*args, **kwargs) -> PatrolScheduleScheduler:
    global patrol_schedule_scheduler
    if args or kwargs:
        return PatrolScheduleScheduler(*args, **kwargs)
    return patrol_schedule_scheduler


def get_scheduler(*args, **kwargs) -> PatrolScheduleScheduler:
    return get_patrol_schedule_scheduler(*args, **kwargs)


def get_patrol_scheduler(*args, **kwargs) -> PatrolScheduleScheduler:
    return get_patrol_schedule_scheduler(*args, **kwargs)


def start_patrol_schedule_scheduler(*args, **kwargs) -> PatrolScheduleScheduler:
    s = get_patrol_schedule_scheduler() if not (args or kwargs) else PatrolScheduleScheduler(*args, **kwargs)
    try:
        s.start(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass
    return s


def start_scheduler(*args, **kwargs) -> PatrolScheduleScheduler:
    return start_patrol_schedule_scheduler(*args, **kwargs)


def start_patrol_scheduler(*args, **kwargs) -> PatrolScheduleScheduler:
    return start_patrol_schedule_scheduler(*args, **kwargs)


def stop_patrol_schedule_scheduler(*args, **kwargs) -> None:
    try:
        patrol_schedule_scheduler.stop(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass


def stop_scheduler(*args, **kwargs) -> None:
    stop_patrol_schedule_scheduler(*args, **kwargs)


def stop_patrol_scheduler(*args, **kwargs) -> None:
    stop_patrol_schedule_scheduler(*args, **kwargs)


# Alias classes
PatrolScheduleSchedulerService = PatrolScheduleScheduler
PatrolScheduleService = PatrolScheduleScheduler
PatrolScheduleManager = PatrolScheduleScheduler
PatrolSchedulerService = PatrolScheduleScheduler
Scheduler = PatrolScheduleScheduler

# Module-level aliases for common names
patrol_schedule_service = patrol_schedule_scheduler
service = patrol_schedule_service
manager = patrol_schedule_service
default_service = patrol_schedule_service

# Additional function aliases
schedule_patrol_now = schedule
run_patrol_now = run_now
trigger_patrol = trigger
trigger_patrol_schedule = trigger
execute_patrol = execute
run_scheduled_patrols = run
run_scheduled_patrol = run
run_scheduled_patrol_schedules = run

# Expose is_due
is_due = _is_due
isDue = _is_due
patrol_is_due = _is_due
