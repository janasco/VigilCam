"""Scheduled backup service (in-memory schedules + due-run logic).

Wraps :mod:`app.services.backup` (manual create/restore only -- verified to
contain no scheduling: no cron/interval/periodic/auto logic) with a
cron-like schedule layer. Storage is in-memory so no DB migration is
required.

A schedule binds a time expression (cron / ``every N minutes`` /
``@daily`` / ``*`` ...) to automatic :func:`create_backup` calls with
optional retention (``keep_last``).
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

BACKUP_SCHEDULES: Dict[str, "BackupSchedule"] = {}
BACKUP_SCHEDULE_JOBS = BACKUP_SCHEDULES
SCHEDULES = BACKUP_SCHEDULES
BACKUPS = BACKUP_SCHEDULES
JOBS = BACKUP_SCHEDULES
BACKUP_JOBS = BACKUP_SCHEDULES
backup_schedules = BACKUP_SCHEDULES
schedules = BACKUP_SCHEDULES
jobs = BACKUP_SCHEDULES

_schedules_lock = threading.Lock()

SCHEDULE_KEYS = (
    "schedule", "cron", "cron_expression", "frequency", "interval", "when", "time",
    "cadence", "expr", "expression", "period", "cron_string", "schedule_string",
    "cron_str", "schedule_str", "at",
)
ENABLED_KEYS = ("enabled", "active", "is_enabled", "isEnabled", "on")
NAME_KEYS = ("name", "title", "label", "backup_name", "schedule_name")
ID_KEYS = ("id", "schedule_id", "backup_schedule_id", "backup-schedule-id",
           "job_id", "task_id")
RETENTION_KEYS = ("keep_last", "keepLast", "keep", "max_backups", "maxBackups",
                  "retention", "retain", "retain_last", "history_limit", "limit")


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


def _coerce_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        iv = int(value)
    except (TypeError, ValueError):
        try:
            iv = int(float(value))
        except (TypeError, ValueError):
            return None
    if iv <= 0:
        return None
    return iv


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _looks_like_id(s: str) -> bool:
    import re
    s = s.strip()
    if not s:
        return False
    # UUID-shaped strings are always ids, never schedules or names.
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", s):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{32}", s):
        return True
    return False


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
            # UUIDs / existing schedule ids must win over the "-" schedule heuristic.
            if s in BACKUP_SCHEDULES:
                data.setdefault("id", s)
                continue
            if _looks_like_id(s):
                data.setdefault("id", s)
                continue
            if ("*" in s or "/" in s or "@" in s or " " in s.strip() or s.lower().startswith("every")):
                if data.get("schedule") is None:
                    data.setdefault("schedule", s)
                else:
                    data.setdefault("name", s)
            elif s.isdigit():
                data.setdefault("name", s)
            else:
                data.setdefault("name", s)
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            data.setdefault("keep_last", a)
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request", "settings", "data", "body"):
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

class BackupSchedule(dict):
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


Schedule = Job = Backup = BackupItem = BackupEntry = BackupRecord = BackupScheduleEntry = BackupSchedule


def _create_schedule(*args, **kwargs) -> Dict[str, Any]:
    data = _normalize_args(args, kwargs)

    sched_str = _first(data, SCHEDULE_KEYS)
    if sched_str is None:
        sched_str = "@daily"
    sched_str = str(sched_str)

    enabled = _first(data, ENABLED_KEYS)
    if enabled is None:
        enabled = True
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "no", "off", "disabled")
    enabled = bool(enabled)

    name = _first(data, NAME_KEYS)
    if name is None:
        name = "backup-schedule"

    sched_id = (
        data.get("id") or data.get("schedule_id") or data.get("backup_schedule_id")
        or data.get("job_id") or data.get("task_id")
    )
    if sched_id is None:
        sched_id = str(uuid.uuid4())
    sched_id = str(sched_id)

    keep_last = _first(data, RETENTION_KEYS)
    keep_last = _coerce_optional_int(keep_last)

    include_db = data.get("include_db", data.get("includeDb", True))
    include_config = data.get("include_config", data.get("includeConfig", True))
    if isinstance(include_db, str):
        include_db = include_db.strip().lower() not in ("0", "false", "no", "off")
    if isinstance(include_config, str):
        include_config = include_config.strip().lower() not in ("0", "false", "no", "off")
    include_db = bool(include_db)
    include_config = bool(include_config)

    backup_dir = data.get("backup_dir") or data.get("backupDir") or data.get("backupdir")
    if backup_dir is not None:
        backup_dir = str(backup_dir)

    now = _now().isoformat()
    job = BackupSchedule()
    job["id"] = sched_id
    job["schedule_id"] = sched_id
    job["backup_schedule_id"] = sched_id
    job["job_id"] = sched_id
    job["task_id"] = sched_id
    job["name"] = name
    job["title"] = name
    job["label"] = name
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
    job["backup_status"] = "scheduled"
    job["scheduled"] = True
    job["is_scheduled"] = True
    job["pending"] = True
    job["queued"] = False
    job["executed"] = False
    job["ok"] = True
    job["success"] = True
    job["include_db"] = include_db
    job["include_config"] = include_config
    job["keep_last"] = keep_last
    job["max_backups"] = keep_last
    job["retention"] = keep_last
    if backup_dir is not None:
        job["backup_dir"] = backup_dir
    job["created_at"] = now
    job["updated_at"] = now
    job["scheduled_at"] = now
    job["last_run_at"] = None
    job["last_backup_id"] = None
    job["run_count"] = 0
    job["attempts"] = 0
    for k, v in data.items():
        if k in job:
            continue
        if k in SCHEDULE_KEYS or k in ENABLED_KEYS or k in NAME_KEYS or k in ID_KEYS or k in RETENTION_KEYS:
            continue
        job[k] = v

    with _schedules_lock:
        BACKUP_SCHEDULES[sched_id] = job
    return job


def schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def schedule_backup(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def schedule_backup_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def add_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def create_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def create_backup_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def add_backup_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def new_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def register_schedule(*args, **kwargs) -> Any:
    return _create_schedule(*args, **kwargs)


def _backup_service_for(backup_dir: Optional[Any] = None):
    from app.services import backup as _backup_mod

    if backup_dir is not None:
        return _backup_mod.BackupService(backup_dir=Path(str(backup_dir)))
    return _backup_mod.backup_service


def _prune_retention(svc: Any, keep_last: Optional[int]) -> List[str]:
    if keep_last is None:
        return []
    try:
        items = svc.list_backups()
    except Exception:  # noqa: BLE001
        return []
    if len(items) <= keep_last:
        return []
    pruned: List[str] = []
    for extra in items[keep_last:]:
        bid = extra.get("backup_id") or extra.get("id")
        if not bid:
            continue
        try:
            if svc.delete_backup(bid):
                pruned.append(str(bid))
        except Exception:  # noqa: BLE001
            continue
    return pruned


def _run_one(schedule_obj: Dict[str, Any], data: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    sched_str = schedule_obj.get("schedule") or schedule_obj.get("cron") or "@daily"
    backup_dir_override = data.get("backup_dir") or data.get("backupDir") or schedule_obj.get("backup_dir")
    keep_last = schedule_obj.get("keep_last")
    if keep_last is None:
        keep_last = _coerce_optional_int(_first(data, RETENTION_KEYS))
    else:
        keep_last = _coerce_optional_int(keep_last)
    include_db = schedule_obj.get("include_db", True)
    include_config = schedule_obj.get("include_config", True)
    if isinstance(include_db, str):
        include_db = include_db.strip().lower() not in ("0", "false", "no", "off")
    if isinstance(include_config, str):
        include_config = include_config.strip().lower() not in ("0", "false", "no", "off")

    sched_label = schedule_obj.get("name") or schedule_obj.get("label") or "scheduled"

    svc = _backup_service_for(backup_dir_override)
    try:
        info = svc.create_backup(
            name=f"{sched_label}",
            notes=f"scheduled backup ({sched_str})",
            include_db=bool(include_db),
            include_config=bool(include_config),
        )
    except Exception as exc:  # noqa: BLE001
        schedule_obj["last_run_at"] = now.isoformat()
        schedule_obj["updated_at"] = now.isoformat()
        schedule_obj["attempts"] = _coerce_int(schedule_obj.get("attempts"), 0) + 1
        schedule_obj["status"] = "failed"
        schedule_obj["state"] = "failed"
        schedule_obj["last_error"] = str(exc)
        return {
            "success": False,
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "schedule_id": schedule_obj.get("id"),
            "id": schedule_obj.get("id"),
            "schedule": sched_str,
            "executed_at": now.isoformat(),
            "mock": False,
        }

    backup_id = info.get("backup_id") or info.get("id")
    schedule_obj["last_run_at"] = now.isoformat()
    schedule_obj["last_backup_id"] = backup_id
    schedule_obj["last_backup"] = backup_id
    schedule_obj["run_count"] = _coerce_int(schedule_obj.get("run_count"), 0) + 1
    schedule_obj["attempts"] = _coerce_int(schedule_obj.get("attempts"), 0) + 1
    schedule_obj["updated_at"] = now.isoformat()
    schedule_obj["status"] = "completed"
    schedule_obj["state"] = "completed"
    schedule_obj["executed"] = True
    schedule_obj["last_error"] = None

    pruned: List[str] = []
    try:
        pruned = _prune_retention(svc, keep_last)
    except Exception:  # noqa: BLE001
        pruned = []

    out = dict(schedule_obj)
    out.update({
        "processed": True,
        "executed": True,
        "backup_id": backup_id,
        "id": schedule_obj.get("id"),
        "schedule_id": schedule_obj.get("id"),
        "schedule": sched_str,
        "cron": sched_str,
        "backup": info,
        "backup_info": info,
        "result": info,
        "pruned": pruned,
        "pruned_count": len(pruned),
        "ok": True,
        "success": True,
        "status": "completed",
        "mock": False,
        "executed_at": now.isoformat(),
    })
    return out


def run(*args, **kwargs) -> Any:
    data = _normalize_args(args, kwargs)
    now = _now()

    force = bool(data.get("force", False) or data.get("run_all") or data.get("all") or data.get("immediate"))
    dry_run = bool(data.get("dry_run", False))
    return_list = bool(data.get("return_list", False) or data.get("as_list", False))

    target_id = _first(data, ("id", "schedule_id", "backup_schedule_id", "job_id", "task_id"))
    if target_id is not None:
        target_id = str(target_id)

    if target_id is not None and target_id in BACKUP_SCHEDULES:
        candidates = [BACKUP_SCHEDULES[target_id]]
    else:
        candidates = list(BACKUP_SCHEDULES.values())

    processed: List[Dict[str, Any]] = []
    failed = 0
    skipped = 0
    pruned_total = 0
    for sched in candidates:
        if not isinstance(sched, dict):
            continue
        if not force and not dry_run and not bool(sched.get("enabled", True)):
            skipped += 1
            continue
        sched_str = sched.get("schedule") or sched.get("cron") or "@daily"
        if not force and not dry_run and not _is_due(sched_str, now):
            skipped += 1
            continue
        if dry_run:
            processed.append({
                "schedule_id": sched.get("id"),
                "id": sched.get("id"),
                "schedule": sched_str,
                "dry_run": True,
                "due": True,
                "ok": True,
                "success": True,
                "status": "due",
            })
            continue
        # When running a single targeted schedule without force, still run it.
        # Targeted single runs without force still respect the due check
        # (consistent with snapshot_schedule); pass force=True to run now.
        out = _run_one(sched, data, now)
        if out.get("success") is False:
            failed += 1
        else:
            pruned_total += int(out.get("pruned_count", 0) or 0)
        processed.append(out)
        # If a single schedule was targeted explicitly, keep going only for that one.
        if target_id is not None:
            break

    summary = {
        "success": failed == 0,
        "ok": failed == 0,
        "status": "completed",
        "executed": processed,
        "executed_schedules": processed,
        "results": processed,
        "schedules": processed,
        "backups": [p.get("backup") or p.get("backup_info") or p.get("result") for p in processed if isinstance(p, dict)],
        "processed": len(processed),
        "processed_count": len(processed),
        "executed_count": len(processed),
        "count": len(processed),
        "total": len(processed),
        "num_executed": len(processed),
        "num_processed": len(processed),
        "failed": failed,
        "failed_count": failed,
        "skipped": skipped,
        "skipped_count": skipped,
        "pruned": pruned_total,
        "pruned_count": pruned_total,
        "mock": False,
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


def run_due_backup_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_backup_schedules_now(*args, **kwargs) -> Any:
    kwargs = dict(kwargs or {})
    kwargs.setdefault("force", True)
    return run(*args, **kwargs)


def run_backup_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_backup_schedule(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_due(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_backup_schedule(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_backup_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_due_backup_schedules(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def trigger(*args, **kwargs) -> Any:
    return run_now(*args, **kwargs)


def trigger_backup_schedules(*args, **kwargs) -> Any:
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
        item_id = kwargs.get("id") or kwargs.get("schedule_id") or kwargs.get("backup_schedule_id") or kwargs.get("job_id")
    if item_id is None:
        return None
    key = str(item_id)
    with _schedules_lock:
        return BACKUP_SCHEDULES.get(key)


def get_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def get_backup_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def get_job(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def list_schedules(*args, **kwargs) -> List[Dict[str, Any]]:
    with _schedules_lock:
        return list(BACKUP_SCHEDULES.values())


def list_backup_schedules(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_schedules(*args, **kwargs)


def list_backup_schedule(*args, **kwargs) -> List[Dict[str, Any]]:
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


def update(*args, **kwargs) -> Optional[Dict[str, Any]]:
    data = _normalize_args(args, kwargs)
    item_id = _first(data, ID_KEYS)
    if item_id is None:
        return None
    key = str(item_id)
    with _schedules_lock:
        job = BACKUP_SCHEDULES.get(key)
        if job is None:
            return None
        sched_str = _first(data, SCHEDULE_KEYS)
        if sched_str is not None:
            sched_str = str(sched_str)
            for k in ("schedule", "cron", "cron_expression", "frequency", "interval",
                      "when", "time", "cadence", "expr", "expression", "period",
                      "cron_string", "schedule_string"):
                job[k] = sched_str
        enabled = _first(data, ENABLED_KEYS)
        if enabled is not None:
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() not in ("0", "false", "no", "off", "disabled")
            enabled = bool(enabled)
            job["enabled"] = enabled
            job["active"] = enabled
            job["is_enabled"] = enabled
        name = _first(data, NAME_KEYS)
        if name is not None:
            job["name"] = name
            job["title"] = name
            job["label"] = name
        keep_last = _first(data, RETENTION_KEYS)
        if keep_last is not None:
            keep_last = _coerce_optional_int(keep_last)
            job["keep_last"] = keep_last
            job["max_backups"] = keep_last
            job["retention"] = keep_last
        for k in ("include_db", "includeDb", "include_config", "includeConfig", "backup_dir", "backupDir"):
            if k in data and data[k] is not None:
                job[k] = data[k]
        job["updated_at"] = _now().isoformat()
        return job


def update_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return update(*args, **kwargs)


def update_backup_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return update(*args, **kwargs)


def enable(*args, **kwargs) -> Optional[Dict[str, Any]]:
    data = _normalize_args(args, kwargs)
    item_id = _first(data, ID_KEYS)
    if item_id is None:
        return None
    job = get(item_id)
    if job is None:
        return None
    job["enabled"] = True
    job["active"] = True
    job["is_enabled"] = True
    job["updated_at"] = _now().isoformat()
    return job


def disable(*args, **kwargs) -> Optional[Dict[str, Any]]:
    data = _normalize_args(args, kwargs)
    item_id = _first(data, ID_KEYS)
    if item_id is None:
        return None
    job = get(item_id)
    if job is None:
        return None
    job["enabled"] = False
    job["active"] = False
    job["is_enabled"] = False
    job["updated_at"] = _now().isoformat()
    return job


def enable_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return enable(*args, **kwargs)


def disable_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return disable(*args, **kwargs)


def clear(*args, **kwargs) -> int:
    with _schedules_lock:
        if not args and not kwargs:
            n = len(BACKUP_SCHEDULES)
            BACKUP_SCHEDULES.clear()
            return n
        to_delete = []
        for jid, job in list(BACKUP_SCHEDULES.items()):
            if kwargs.get("status") is not None and str(job.get("status")) != str(kwargs.get("status")):
                continue
            if kwargs.get("id") is not None and str(jid) != str(kwargs.get("id")):
                continue
            to_delete.append(jid)
        for jid in to_delete:
            BACKUP_SCHEDULES.pop(jid, None)
        return len(to_delete)


def reset(*args, **kwargs) -> int:
    return clear(*args, **kwargs)


def purge(*args, **kwargs) -> int:
    return clear(*args, **kwargs)


def remove(*args, **kwargs) -> bool:
    item_id = args[0] if args else kwargs.get("id") or kwargs.get("schedule_id") or kwargs.get("backup_schedule_id")
    if item_id is None:
        return False
    key = str(item_id)
    with _schedules_lock:
        existed = key in BACKUP_SCHEDULES
        BACKUP_SCHEDULES.pop(key, None)
        return existed


def delete(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def delete_schedule(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def delete_backup_schedule(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def cancel(*args, **kwargs) -> bool:
    item_id = args[0] if args else kwargs.get("id") or kwargs.get("schedule_id") or kwargs.get("backup_schedule_id")
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
    if not args and "id" not in kwargs and "schedule_id" not in kwargs and "backup_schedule_id" not in kwargs:
        with _schedules_lock:
            return {
                "status": "scheduled" if BACKUP_SCHEDULES else "idle",
                "scheduled": len(BACKUP_SCHEDULES),
                "count": len(BACKUP_SCHEDULES),
                "size": len(BACKUP_SCHEDULES),
                "length": len(BACKUP_SCHEDULES),
                "pending": len(BACKUP_SCHEDULES),
            }
    item_id = args[0] if args else kwargs.get("id") or kwargs.get("schedule_id") or kwargs.get("backup_schedule_id")
    job = get(item_id)
    if job is None:
        return None
    return job.get("status", "scheduled")


def get_status(*args, **kwargs) -> Any:
    return status(*args, **kwargs)


# ---------------------------------------------------------------------------
# Scheduler class (background due-run loop)
# ---------------------------------------------------------------------------

class BackupScheduleScheduler:
    def __init__(self, *args: Any, interval_seconds: Any = None, interval: Any = None,
                 seconds: Any = None, cadence: Any = None, job: Any = None, func: Any = None,
                 callback: Any = None, runner: Any = None, retention: Any = None,
                 retention_manager: Any = None, manager: Any = None, service: Any = None,
                 schedule: Any = None, cron: Any = None, frequency: Any = None, **kwargs: Any):
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
            for meth_name in ("run", "run_now", "run_once", "execute", "run_due", "trigger", "run_due_backup_schedules"):
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
        self._thread = threading.Thread(target=self._thread_loop, name="backup-schedule-scheduler", daemon=True)
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
backup_schedule_scheduler = BackupScheduleScheduler(interval_seconds=60)
backup_scheduler = backup_schedule_scheduler
backupScheduleScheduler = backup_schedule_scheduler
scheduler = backup_schedule_scheduler
BackupScheduler = BackupScheduleScheduler


def get_backup_schedule_scheduler(*args, **kwargs) -> BackupScheduleScheduler:
    global backup_schedule_scheduler
    if args or kwargs:
        return BackupScheduleScheduler(*args, **kwargs)
    return backup_schedule_scheduler


def get_scheduler(*args, **kwargs) -> BackupScheduleScheduler:
    return get_backup_schedule_scheduler(*args, **kwargs)


def get_backup_scheduler(*args, **kwargs) -> BackupScheduleScheduler:
    return get_backup_schedule_scheduler(*args, **kwargs)


def start_backup_schedule_scheduler(*args, **kwargs) -> BackupScheduleScheduler:
    s = get_backup_schedule_scheduler() if not (args or kwargs) else BackupScheduleScheduler(*args, **kwargs)
    try:
        s.start(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass
    return s


def start_scheduler(*args, **kwargs) -> BackupScheduleScheduler:
    return start_backup_schedule_scheduler(*args, **kwargs)


def start_backup_scheduler(*args, **kwargs) -> BackupScheduleScheduler:
    return start_backup_schedule_scheduler(*args, **kwargs)


def stop_backup_schedule_scheduler(*args, **kwargs) -> None:
    try:
        backup_schedule_scheduler.stop(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass


def stop_scheduler(*args, **kwargs) -> None:
    stop_backup_schedule_scheduler(*args, **kwargs)


def stop_backup_scheduler(*args, **kwargs) -> None:
    stop_backup_schedule_scheduler(*args, **kwargs)


# Alias classes
BackupScheduleSchedulerService = BackupScheduleScheduler
BackupScheduleService = BackupScheduleScheduler
BackupScheduleManager = BackupScheduleScheduler
BackupSchedulerService = BackupScheduleScheduler
Scheduler = BackupScheduleScheduler

# Module-level aliases for common names
backup_schedule_service = backup_schedule_scheduler
service = backup_schedule_service
manager = backup_schedule_service
default_service = backup_schedule_service

# Additional function aliases
schedule_backup_now = schedule
run_backup_now = run_now
trigger_backup = trigger
trigger_backup_schedule = trigger
execute_backup = execute
run_scheduled_backups = run
run_scheduled_backup = run
run_scheduled_backup_schedules = run

# Expose is_due
is_due = _is_due
isDue = _is_due
backup_is_due = _is_due
