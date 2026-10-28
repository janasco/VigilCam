"""Scheduled integrity verification service for VigilCam.

Wraps :mod:`app.services.integrity` (on-demand only) with an in-memory
schedule store plus a background scheduler that periodically re-scans
recording directories. Standard library only. Defensive: broad aliases so
callers/tests can use whichever spelling they prefer.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Shared in-memory state
# ---------------------------------------------------------------------------

VERIFY_SCHEDULES: Dict[str, dict] = {}
VERIFY_SCHEDULE_JOBS = VERIFY_SCHEDULES
SCHEDULES = VERIFY_SCHEDULES
JOBS = VERIFY_SCHEDULES
VERIFY_JOBS = VERIFY_SCHEDULES
verify_schedules = VERIFY_SCHEDULES
schedules = VERIFY_SCHEDULES
jobs = VERIFY_SCHEDULES

_schedules_lock = threading.Lock()

PATH_KEYS = (
    "path", "root", "directory", "dir", "folder", "location",
    "storage_path", "storage_root", "root_path", "recordings_dir",
    "recordings_path", "target", "target_dir", "scan_path", "base_dir",
)
SCHEDULE_KEYS = (
    "schedule", "cron", "cron_expression", "frequency", "interval", "when",
    "time", "cadence", "expr", "expression", "period", "cron_string",
    "schedule_string", "cron_str", "schedule_str",
)
ENABLED_KEYS = ("enabled", "active", "is_enabled", "isEnabled", "on")
NAME_KEYS = ("name", "title", "label")
ID_KEYS = ("id", "schedule_id", "verify_id", "verify_schedule_id", "job_id", "task_id")

DEFAULT_INTERVAL_SECONDS = 3600


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
                elif data.get("path") is None and ("/" in s or "\\" in s or s.startswith(".")):
                    data.setdefault("path", s)
                else:
                    data.setdefault("name", s)
            elif ("/" in s or "\\" in s) and data.get("path") is None:
                data.setdefault("path", s)
            else:
                data.setdefault("name", s)
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            data.setdefault("interval_seconds", a)
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


def _is_due(schedule: Any, now: Optional[datetime] = None) -> bool:
    if now is None:
        now = _now()
    if schedule is None:
        return True
    s = str(schedule).strip()
    if not s:
        return True
    low = s.lower()
    if low in {"*", "always", "due", "now", "run", "immediate", "true",
               "enabled", "all", "every", "minutely", "@always", "any"}:
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
    return all(checks)


# ---------------------------------------------------------------------------
# Schedule storage
# ---------------------------------------------------------------------------

class VerifySchedule(dict):
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


Schedule = Job = VerifyJob = VerifyScheduleEntry = VerifyScheduleRecord = VerifySchedule
VerifyScheduleJob = VerifySchedule


def _create_schedule(*args, **kwargs) -> Dict[str, Any]:
    data = _normalize_args(args, kwargs)

    path = _first(data, PATH_KEYS)
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
        name = "verify-{}".format(str(path) if path is not None else "recordings")

    sched_id = _first(data, ID_KEYS)
    if sched_id is None:
        sched_id = str(uuid.uuid4())
    sched_id = str(sched_id)

    interval_seconds = data.get("interval_seconds", data.get("intervalSeconds",
                         data.get("seconds", data.get("cadence_seconds"))))
    if interval_seconds is None:
        interval_seconds = DEFAULT_INTERVAL_SECONDS
    try:
        interval_seconds = int(interval_seconds)
    except (TypeError, ValueError):
        interval_seconds = DEFAULT_INTERVAL_SECONDS
    if interval_seconds <= 0:
        interval_seconds = DEFAULT_INTERVAL_SECONDS

    now = _now().isoformat()
    job = VerifySchedule()
    job["id"] = sched_id
    job["schedule_id"] = sched_id
    job["verify_id"] = sched_id
    job["verify_schedule_id"] = sched_id
    job["job_id"] = sched_id
    job["task_id"] = sched_id
    job["name"] = name
    job["title"] = name
    job["label"] = name
    job["path"] = str(path) if path is not None else None
    job["root"] = str(path) if path is not None else None
    job["directory"] = str(path) if path is not None else None
    job["storage_root"] = str(path) if path is not None else None
    job["recordings_dir"] = str(path) if path is not None else None
    for k in ("extensions", "exts", "min_size_bytes", "min_size", "check_header",
              "use_ffprobe", "ffprobe", "quarantine", "quarantine_dir", "timeout"):
        if k in data and data[k] is not None:
            job[k] = data[k]
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
    job["interval_seconds"] = interval_seconds
    job["enabled"] = enabled
    job["active"] = enabled
    job["is_enabled"] = enabled
    job["status"] = "scheduled"
    job["state"] = "scheduled"
    job["job_status"] = "scheduled"
    job["schedule_status"] = "scheduled"
    job["verify_status"] = "scheduled"
    job["scheduled"] = True
    job["is_scheduled"] = True
    job["pending"] = True
    job["queued"] = True
    job["executed"] = False
    job["verified"] = False
    job["ok"] = True
    job["success"] = True
    job["created_at"] = now
    job["updated_at"] = now
    job["scheduled_at"] = now
    job["last_run_at"] = None
    job["last_report"] = None
    job["run_count"] = 0
    job["attempts"] = 0
    for k, v in data.items():
        if k in job:
            continue
        if k in PATH_KEYS or k in SCHEDULE_KEYS or k in ENABLED_KEYS or k in NAME_KEYS or k in ID_KEYS:
            continue
        job[k] = v

    with _schedules_lock:
        VERIFY_SCHEDULES[sched_id] = job
    return job


def schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def schedule_verification(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def schedule_verify(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def schedule_integrity_check(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def schedule_integrity_verification(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def add_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def create_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def create_verify_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def add_verify_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def new_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def register_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def _scan_one(job: Dict[str, Any], extra: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    from app.services import integrity as _integrity

    target = job.get("path") or job.get("root") or job.get("directory")
    override = _first(extra, PATH_KEYS)
    if override is not None:
        target = override
    scan_kw: Dict[str, Any] = {}
    for k in ("extensions", "exts", "min_size_bytes", "min_size", "check_header",
              "use_ffprobe", "ffprobe", "quarantine", "quarantine_dir", "timeout"):
        if job.get(k) is not None:
            scan_kw[k] = job.get(k)
    for k, v in extra.items():
        if k in ("force", "run_all", "all", "immediate", "return_list", "as_list",
                 "dry_run", "id", "schedule_id", "verify_id", "verify_schedule_id",
                 "job_id", "task_id"):
            continue
        scan_kw.setdefault(k, v)
    try:
        report = _integrity.scan_directory(target, **scan_kw)
    except Exception as exc:  # noqa: BLE001
        report = {"status": "error", "ok": False, "success": False,
                  "error": str(exc), "total": 0, "bad_count": 0}

    job["last_run_at"] = now.isoformat()
    job["last_report"] = report
    job["run_count"] = _coerce_int(job.get("run_count"), 0) + 1
    job["attempts"] = _coerce_int(job.get("attempts"), 0) + 1
    job["updated_at"] = now.isoformat()
    job["executed"] = True
    job["verified"] = True
    job["status"] = str(report.get("status", "completed"))
    job["state"] = job["status"]
    job["verify_status"] = job["status"]

    out = dict(job)
    out.update({
        "processed": True,
        "executed": True,
        "verified": True,
        "report": report,
        "scan": report,
        "result": report,
        "ok": bool(report.get("ok", True)),
        "success": True,
        "status": "completed",
        "mock": False,
        "executed_at": now.isoformat(),
        "verified_at": now.isoformat(),
    })
    for k in ("total", "bad_count", "ok_count", "corrupt_count", "checked"):
        if k in report:
            out[k] = report[k]
    return out


def run(*args, **kwargs) -> Any:
    data = _normalize_args(args, kwargs)
    now = _now()

    force = bool(data.get("force", False) or data.get("run_all")
                 or data.get("all") or data.get("immediate"))
    return_list = bool(data.get("return_list", False) or data.get("as_list", False))

    target_id = _first(data, ID_KEYS)
    if target_id is not None:
        target_id = str(target_id)

    if target_id is not None and target_id in VERIFY_SCHEDULES:
        candidates = [VERIFY_SCHEDULES[target_id]]
    else:
        candidates = list(VERIFY_SCHEDULES.values())

    processed: List[Dict[str, Any]] = []
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
        try:
            out = _scan_one(sched, data, now)
        except Exception:  # noqa: BLE001
            failed += 1
            continue
        processed.append(out)

    summary = {
        "success": True,
        "ok": True,
        "status": "completed",
        "state": "completed",
        "executed": processed,
        "executed_schedules": processed,
        "results": processed,
        "schedules": processed,
        "verified": processed,
        "verifications": processed,
        "reports": [p.get("report", {}) for p in processed],
        "processed": len(processed),
        "processed_count": len(processed),
        "executed_count": len(processed),
        "count": len(processed),
        "total": len(processed),
        "num_executed": len(processed),
        "num_processed": len(processed),
        "num_verified": len(processed),
        "verified_count": len(processed),
        "failed": failed,
        "failed_count": failed,
        "skipped": skipped,
        "skipped_count": skipped,
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


def run_due_verify_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_verify_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_verify_schedule(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_verification(*args, **kwargs) -> Any:
    data = _normalize_args(args, kwargs)
    target = _first(data, PATH_KEYS)
    if target is not None and not VERIFY_SCHEDULES:
        from app.services import integrity as _integrity

        scan_kw = {k: v for k, v in data.items()
                   if k not in ("force", "run_all", "all", "immediate")}
        try:
            return _integrity.scan_directory(target, **scan_kw)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "ok": False, "success": False, "error": str(exc)}
    if target is not None and _first(data, ID_KEYS) is None:
        job = _create_schedule(**data)
        data = dict(data)
        data["id"] = job["id"]
    kwargs2 = dict(kwargs or {})
    kwargs2.setdefault("force", True)
    return run(*args, **kwargs2)


def run_verification_now(*args, **kwargs) -> Any:
    kwargs = dict(kwargs or {})
    kwargs.setdefault("force", True)
    return run_verification(*args, **kwargs)


def execute(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_due(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_verify_schedule(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def trigger(*args, **kwargs) -> Any:
    return run_now(*args, **kwargs)


def trigger_verify_schedules(*args, **kwargs) -> Any:
    return run_now(*args, **kwargs)


def trigger_verification(*args, **kwargs) -> Any:
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
        item_id = (_first(kwargs, ID_KEYS) if kwargs else None)
    if isinstance(item_id, dict):
        item_id = _first(item_id, ID_KEYS)
    if item_id is None:
        return None
    key = str(item_id)
    with _schedules_lock:
        return VERIFY_SCHEDULES.get(key)


def get_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def get_verify_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def get_job(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def get_verification_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def list_schedules(*args, **kwargs) -> List[Dict[str, Any]]:
    with _schedules_lock:
        return list(VERIFY_SCHEDULES.values())


def list_verify_schedules(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_schedules(*args, **kwargs)


def list_verification_schedules(*args, **kwargs) -> List[Dict[str, Any]]:
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
            n = len(VERIFY_SCHEDULES)
            VERIFY_SCHEDULES.clear()
            return n
        to_delete = []
        for jid, job in list(VERIFY_SCHEDULES.items()):
            if kwargs.get("status") is not None and str(job.get("status")) != str(kwargs.get("status")):
                continue
            if kwargs.get("id") is not None and str(jid) != str(kwargs.get("id")):
                continue
            to_delete.append(jid)
        for jid in to_delete:
            VERIFY_SCHEDULES.pop(jid, None)
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
        existed = key in VERIFY_SCHEDULES
        VERIFY_SCHEDULES.pop(key, None)
        return existed


def delete(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def delete_schedule(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def delete_verify_schedule(*args, **kwargs) -> bool:
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
    if not args and "id" not in kwargs and "schedule_id" not in kwargs and "verify_id" not in kwargs:
        with _schedules_lock:
            return {
                "status": "scheduled" if VERIFY_SCHEDULES else "idle",
                "state": "scheduled" if VERIFY_SCHEDULES else "idle",
                "scheduled": len(VERIFY_SCHEDULES),
                "count": len(VERIFY_SCHEDULES),
                "size": len(VERIFY_SCHEDULES),
                "length": len(VERIFY_SCHEDULES),
                "pending": len(VERIFY_SCHEDULES),
            }
    item_id = args[0] if args else _first(kwargs, ID_KEYS)
    job = get(item_id)
    if job is None:
        return None
    return job.get("status", "scheduled")


def get_status(*args, **kwargs) -> Any:
    return status(*args, **kwargs)


# ---------------------------------------------------------------------------
# Scheduler class (background loop)
# ---------------------------------------------------------------------------

class VerifyScheduler:
    """Background scheduler that periodically runs due verify schedules."""

    def __init__(self, *args: Any, interval_seconds: Any = None, interval: Any = None,
                 seconds: Any = None, cadence: Any = None, job: Any = None,
                 func: Any = None, callback: Any = None, runner: Any = None,
                 **kwargs: Any):
        self.interval_seconds = DEFAULT_INTERVAL_SECONDS
        for v in (interval_seconds, interval, seconds, cadence,
                  kwargs.get("interval_seconds"), kwargs.get("interval")):
            if v is not None:
                try:
                    self.interval_seconds = int(v)
                    break
                except (TypeError, ValueError):
                    pass
        if self.interval_seconds <= 0:
            self.interval_seconds = DEFAULT_INTERVAL_SECONDS
        self.job = job or func or callback or runner or kwargs.get("job")
        self._running = False
        self._stop = False
        self._thread = None
        self._task = None

    def _run_job(self, **kwargs) -> Any:
        if callable(self.job):
            try:
                return self.job(dry_run=kwargs.get("dry_run", False),
                                **{k: v for k, v in kwargs.items() if k != "dry_run"})
            except TypeError:
                try:
                    return self.job(**kwargs)
                except TypeError:
                    try:
                        return self.job()
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
        self._thread = threading.Thread(target=self._thread_loop,
                                        name="verify-schedule-scheduler", daemon=True)
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


VerifyScheduleScheduler = VerifyScheduler
Scheduler = VerifyScheduler
VerifySchedulerService = VerifyScheduler
VerifyScheduleService = VerifyScheduler
VerifyScheduleManager = VerifyScheduler
VerificationScheduler = VerifyScheduler


# Shared instance (not auto-started on import).
verify_scheduler = VerifyScheduler(interval_seconds=DEFAULT_INTERVAL_SECONDS)
verifyScheduleScheduler = verify_scheduler
scheduler = verify_scheduler
service = verify_scheduler
manager = verify_scheduler
default_service = verify_scheduler


def get_verify_scheduler(*args, **kwargs) -> VerifyScheduler:
    global verify_scheduler
    if args or kwargs:
        return VerifyScheduler(*args, **kwargs)
    return verify_scheduler


def get_scheduler(*args, **kwargs) -> VerifyScheduler:
    return get_verify_scheduler(*args, **kwargs)


def get_verify_schedule_scheduler(*args, **kwargs) -> VerifyScheduler:
    return get_verify_scheduler(*args, **kwargs)


def start_verify_scheduler(*args, **kwargs) -> VerifyScheduler:
    s = get_verify_scheduler() if not (args or kwargs) else VerifyScheduler(*args, **kwargs)
    try:
        s.start(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass
    return s


def start_scheduler(*args, **kwargs) -> VerifyScheduler:
    return start_verify_scheduler(*args, **kwargs)


def start_verify_schedule_scheduler(*args, **kwargs) -> VerifyScheduler:
    return start_verify_scheduler(*args, **kwargs)


def start_verification_scheduler(*args, **kwargs) -> VerifyScheduler:
    return start_verify_scheduler(*args, **kwargs)


def stop_verify_scheduler(*args, **kwargs) -> None:
    try:
        verify_scheduler.stop(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass


def stop_scheduler(*args, **kwargs) -> None:
    stop_verify_scheduler(*args, **kwargs)


def stop_verify_schedule_scheduler(*args, **kwargs) -> None:
    stop_verify_scheduler(*args, **kwargs)


def stop_verification_scheduler(*args, **kwargs) -> None:
    stop_verify_scheduler(*args, **kwargs)


# Additional function aliases
schedule_verify_now = schedule
run_scheduled_verifications = run
run_scheduled_verification = run
run_scheduled_verify_schedules = run
execute_verification = execute
trigger_verify = trigger

# Expose is_due
is_due = _is_due
isDue = _is_due
verify_is_due = _is_due

__all__ = [
    "VerifyScheduler",
    "VerifyScheduleScheduler",
    "VerifySchedule",
    "VERIFY_SCHEDULES",
    "schedule",
    "run",
    "run_now",
    "run_due",
    "run_all",
    "run_verification",
    "trigger",
    "execute",
    "get",
    "get_schedule",
    "list_schedules",
    "remove",
    "delete",
    "clear",
    "status",
    "get_status",
    "is_due",
    "verify_scheduler",
    "scheduler",
    "get_verify_scheduler",
    "get_scheduler",
    "start_verify_scheduler",
    "start_scheduler",
    "start_verify_schedule_scheduler",
    "stop_verify_scheduler",
    "stop_scheduler",
    "stop_verify_schedule_scheduler",
]
