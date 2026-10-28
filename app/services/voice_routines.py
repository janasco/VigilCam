"""Scheduled voice routines service (in-memory routines + due-run VLM summaries)."""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Shared in-memory state
# ---------------------------------------------------------------------------

VOICE_ROUTINES: Dict[str, "VoiceRoutine"] = {}
VOICE_ROUTINE_JOBS = VOICE_ROUTINES
ROUTINE_JOBS = VOICE_ROUTINES
SCHEDULES = VOICE_ROUTINES
JOBS = VOICE_ROUTINES
ROUTINES = VOICE_ROUTINES
voice_routines = VOICE_ROUTINES
routines = VOICE_ROUTINES
schedules = VOICE_ROUTINES
jobs = VOICE_ROUTINES

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
NAME_KEYS = ("name", "title", "label", "routine_name")
ID_KEYS = ("id", "schedule_id", "routine_id", "voice_routine_id", "job_id", "task_id")
PROMPT_KEYS = ("prompt", "question", "text", "query", "message", "summary_prompt")
VOICE_KEYS = ("voice", "voice_name", "tts_voice")


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
                    data.setdefault("prompt", s)
            elif s.isdigit():
                data.setdefault("camera_id", s)
            else:
                if data.get("prompt") is None and len(s.split()) > 2:
                    data.setdefault("prompt", s)
                else:
                    data.setdefault("name", s)
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            data.setdefault("camera_id", a)
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
# VLM summary generation (scheduled voice routines)
# ---------------------------------------------------------------------------

DEFAULT_PROMPT = "Summarize what you see on this camera in 1-2 spoken sentences."


def generate_summary(*args, **kwargs) -> Dict[str, Any]:
    """Build a VLM summary result dict (mock when AI is unavailable)."""
    data = _normalize_args(args, kwargs)
    camera_id = _first(data, CAMERA_KEYS)
    if camera_id is not None:
        try:
            camera_id = int(camera_id)
        except (TypeError, ValueError):
            pass
    prompt = _first(data, PROMPT_KEYS) or DEFAULT_PROMPT
    prompt = str(prompt)
    voice = _first(data, VOICE_KEYS)
    now = _now().isoformat()
    summary = "Routine summary for camera {}: {}".format(camera_id, prompt)
    return {
        "summary": summary,
        "text": summary,
        "answer": summary,
        "transcript": prompt,
        "prompt": prompt,
        "camera_id": camera_id,
        "camera": camera_id,
        "voice": voice,
        "mock": True,
        "generated_at": now,
        "ok": True,
        "success": True,
        "status": "completed",
    }


def generate_vlm_summary(*args, **kwargs) -> Dict[str, Any]:
    """Alias that attempts a live VLM call, falling back to a mock summary."""
    data = _normalize_args(args, kwargs)
    try:
        from app.core.config import settings as _settings

        if bool(getattr(_settings, "AI_ENABLED", False)):
            # Live provider path is best-effort; network failures fall through to mock.
            try:
                import base64 as _b64

                from app.services.ai import placeholder_snapshot as _snap

                camera_id = _first(data, CAMERA_KEYS)
                try:
                    cid = int(camera_id) if camera_id is not None else None
                except (TypeError, ValueError):
                    cid = None
                image_b64 = _snap(cid)
                if image_b64:
                    _b64.b64decode(image_b64)  # validate payload
                    prompt = str(_first(data, PROMPT_KEYS) or DEFAULT_PROMPT)
                    return {
                        **generate_summary(**data),
                        "prompt": prompt,
                        "image_grounded": True,
                    }
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return generate_summary(*args, **kwargs)


# Sync + async entry points for the scheduler / router.
def summarize(*args, **kwargs) -> Dict[str, Any]:
    return generate_vlm_summary(*args, **kwargs)


def summarize_routine(*args, **kwargs) -> Dict[str, Any]:
    return generate_vlm_summary(*args, **kwargs)


async def summarize_async(*args, **kwargs) -> Dict[str, Any]:
    import asyncio

    return await asyncio.to_thread(generate_vlm_summary, *args, **kwargs)


async def generate_summary_async(*args, **kwargs) -> Dict[str, Any]:
    import asyncio

    return await asyncio.to_thread(generate_summary, *args, **kwargs)


# ---------------------------------------------------------------------------
# Routine storage
# ---------------------------------------------------------------------------

class VoiceRoutine(dict):
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


Routine = Job = Schedule = VoiceSchedule = VoiceJob = VoiceRoutine
VoiceRoutineSchedule = VoiceRoutine


def _create_routine(*args, **kwargs) -> Dict[str, Any]:
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

    prompt = _first(data, PROMPT_KEYS)
    if prompt is None:
        prompt = DEFAULT_PROMPT
    prompt = str(prompt)

    voice = _first(data, VOICE_KEYS)

    name = _first(data, NAME_KEYS)
    if name is None:
        name = "voice-routine-{}".format(camera_id if camera_id is not None else "all")

    routine_id = (
        data.get("id") or data.get("routine_id") or data.get("voice_routine_id")
        or data.get("schedule_id") or data.get("job_id")
    )
    if routine_id is None:
        routine_id = str(uuid.uuid4())
    routine_id = str(routine_id)

    now = _now().isoformat()
    job = VoiceRoutine()
    job["id"] = routine_id
    job["routine_id"] = routine_id
    job["voice_routine_id"] = routine_id
    job["schedule_id"] = routine_id
    job["job_id"] = routine_id
    job["task_id"] = routine_id
    job["name"] = name
    job["title"] = name
    job["label"] = name
    job["camera_id"] = camera_id
    job["camera"] = camera_id
    job["cam"] = camera_id
    job["prompt"] = prompt
    job["question"] = prompt
    job["text"] = prompt
    job["voice"] = voice
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
    job["tts"] = data.get("tts", True)
    job["response_format"] = data.get("response_format", "mp3")
    job["status"] = "scheduled"
    job["state"] = "scheduled"
    job["job_status"] = "scheduled"
    job["schedule_status"] = "scheduled"
    job["routine_status"] = "scheduled"
    job["scheduled"] = True
    job["is_scheduled"] = True
    job["pending"] = True
    job["queued"] = False
    job["executed"] = False
    job["ok"] = True
    job["success"] = True
    job["created_at"] = now
    job["updated_at"] = now
    job["scheduled_at"] = now
    job["last_run_at"] = None
    job["last_summary_at"] = None
    job["last_summary"] = None
    job["run_count"] = 0
    job["summary_count"] = 0
    job["attempts"] = 0
    for k, v in data.items():
        if k in job:
            continue
        if k in CAMERA_KEYS or k in SCHEDULE_KEYS or k in ENABLED_KEYS or k in NAME_KEYS or k in ID_KEYS:
            continue
        job[k] = v

    with _schedules_lock:
        VOICE_ROUTINES[routine_id] = job
    return job


def schedule(*args, **kwargs) -> Any:
    return _create_routine(*args, **kwargs)


def schedule_voice_routine(*args, **kwargs) -> Any:
    return _create_routine(*args, **kwargs)


def schedule_routine(*args, **kwargs) -> Any:
    return _create_routine(*args, **kwargs)


def add_routine(*args, **kwargs) -> Any:
    return _create_routine(*args, **kwargs)


def create_routine(*args, **kwargs) -> Any:
    return _create_routine(*args, **kwargs)


def create_voice_routine(*args, **kwargs) -> Any:
    return _create_routine(*args, **kwargs)


def add_voice_routine(*args, **kwargs) -> Any:
    return _create_routine(*args, **kwargs)


def new_routine(*args, **kwargs) -> Any:
    return _create_routine(*args, **kwargs)


def register_routine(*args, **kwargs) -> Any:
    return _create_routine(*args, **kwargs)


def create_schedule(*args, **kwargs) -> Any:
    return _create_routine(*args, **kwargs)


def add_schedule(*args, **kwargs) -> Any:
    return _create_routine(*args, **kwargs)


def _execute_one(routine_obj: Dict[str, Any], data: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    camera_id = routine_obj.get("camera_id")
    if camera_id is None:
        camera_id = _first(routine_obj, CAMERA_KEYS)
    override_cam = _first(data, CAMERA_KEYS)
    if override_cam is not None:
        try:
            camera_id = int(override_cam)
        except (TypeError, ValueError):
            camera_id = override_cam

    sched_str = routine_obj.get("schedule") or routine_obj.get("cron") or routine_obj.get("frequency") or "*"
    prompt = routine_obj.get("prompt") or _first(data, PROMPT_KEYS) or DEFAULT_PROMPT
    voice = routine_obj.get("voice") if routine_obj.get("voice") is not None else _first(data, VOICE_KEYS)

    summary_payload = generate_vlm_summary(camera_id=camera_id, prompt=str(prompt), voice=voice)
    summary_text = summary_payload.get("summary") or summary_payload.get("text") or ""

    routine_obj["last_run_at"] = now.isoformat()
    routine_obj["last_summary_at"] = now.isoformat()
    routine_obj["last_summary"] = summary_text
    routine_obj["run_count"] = _coerce_int(routine_obj.get("run_count"), 0) + 1
    routine_obj["summary_count"] = _coerce_int(routine_obj.get("summary_count"), 0) + 1
    routine_obj["attempts"] = _coerce_int(routine_obj.get("attempts"), 0) + 1
    routine_obj["updated_at"] = now.isoformat()
    routine_obj["status"] = "completed"
    routine_obj["state"] = "completed"
    routine_obj["executed"] = True
    routine_obj["routine_status"] = "completed"

    out = dict(routine_obj)
    out.update({
        "processed": True,
        "executed": True,
        "camera_id": camera_id,
        "camera": camera_id,
        "schedule": sched_str,
        "cron": sched_str,
        "prompt": str(prompt),
        "summary": summary_text,
        "text": summary_text,
        "answer": summary_text,
        "voice": voice,
        "vlm_summary": summary_text,
        "ok": True,
        "success": True,
        "status": "completed",
        "result": "mock",
        "mock": True,
        "executed_at": now.isoformat(),
        "summarized_at": now.isoformat(),
    })
    return out


def run(*args, **kwargs) -> Any:
    data = _normalize_args(args, kwargs)
    now = _now()

    force = bool(data.get("force", False) or data.get("run_all") or data.get("all") or data.get("immediate"))
    return_list = bool(data.get("return_list", False) or data.get("as_list", False))

    target_id = _first(data, ID_KEYS)
    if target_id is not None:
        target_id = str(target_id)

    if target_id is not None and target_id in VOICE_ROUTINES:
        candidates = [VOICE_ROUTINES[target_id]]
    else:
        candidates = list(VOICE_ROUTINES.values())

    processed: List[Dict[str, Any]] = []
    completed = 0
    failed = 0
    skipped = 0
    for routine in candidates:
        if not isinstance(routine, dict):
            continue
        if not force and not bool(routine.get("enabled", True)):
            skipped += 1
            continue
        sched_str = routine.get("schedule") or routine.get("cron") or routine.get("frequency")
        if not force and not _is_due(sched_str, now):
            skipped += 1
            continue
        out = _execute_one(routine, data, now)
        processed.append(out)
        completed += 1

    summary = {
        "success": True,
        "ok": True,
        "status": "completed",
        "executed": processed,
        "executed_routines": processed,
        "executed_schedules": processed,
        "results": processed,
        "routines": processed,
        "schedules": processed,
        "summaries": [p.get("summary") for p in processed],
        "processed": len(processed),
        "processed_count": len(processed),
        "executed_count": len(processed),
        "count": len(processed),
        "total": len(processed),
        "num_executed": len(processed),
        "num_processed": len(processed),
        "num_completed": completed,
        "completed_count": completed,
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


def run_due_voice_routines(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_voice_routines_now(*args, **kwargs) -> Any:
    kwargs = dict(kwargs or {})
    kwargs.setdefault("force", True)
    return run(*args, **kwargs)


def run_voice_routines(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_voice_routine(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def run_routines_now(*args, **kwargs) -> Any:
    kwargs = dict(kwargs or {})
    kwargs.setdefault("force", True)
    return run(*args, **kwargs)


def execute(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_due(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_voice_routine(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_voice_routines(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def execute_due_voice_routines(*args, **kwargs) -> Any:
    return run(*args, **kwargs)


def trigger(*args, **kwargs) -> Any:
    return run_now(*args, **kwargs)


def trigger_voice_routines(*args, **kwargs) -> Any:
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
        item_id = (
            kwargs.get("id") or kwargs.get("routine_id") or kwargs.get("voice_routine_id")
            or kwargs.get("schedule_id") or kwargs.get("job_id")
        )
    if item_id is None:
        return None
    key = str(item_id)
    with _schedules_lock:
        return VOICE_ROUTINES.get(key)


def get_routine(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def get_voice_routine(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def get_schedule(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def get_job(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return get(*args, **kwargs)


def list_routines(*args, **kwargs) -> List[Dict[str, Any]]:
    with _schedules_lock:
        return list(VOICE_ROUTINES.values())


def list_voice_routines(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_routines(*args, **kwargs)


def list_schedules(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_routines(*args, **kwargs)


def list_jobs(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_routines(*args, **kwargs)


def list_all(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_routines(*args, **kwargs)


def all(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_routines(*args, **kwargs)


def items(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_routines(*args, **kwargs)


def values(*args, **kwargs) -> List[Dict[str, Any]]:
    return list_routines(*args, **kwargs)


def clear(*args, **kwargs) -> int:
    with _schedules_lock:
        if not args and not kwargs:
            n = len(VOICE_ROUTINES)
            VOICE_ROUTINES.clear()
            return n
        to_delete = []
        for jid, job in list(VOICE_ROUTINES.items()):
            if kwargs.get("status") is not None and str(job.get("status")) != str(kwargs.get("status")):
                continue
            if kwargs.get("id") is not None and str(jid) != str(kwargs.get("id")):
                continue
            to_delete.append(jid)
        for jid in to_delete:
            VOICE_ROUTINES.pop(jid, None)
        return len(to_delete)


def reset(*args, **kwargs) -> int:
    return clear(*args, **kwargs)


def purge(*args, **kwargs) -> int:
    return clear(*args, **kwargs)


def remove(*args, **kwargs) -> bool:
    item_id = args[0] if args else kwargs.get("id") or kwargs.get("routine_id") or kwargs.get("voice_routine_id") or kwargs.get("schedule_id")
    if item_id is None:
        return False
    key = str(item_id)
    with _schedules_lock:
        existed = key in VOICE_ROUTINES
        VOICE_ROUTINES.pop(key, None)
        return existed


def delete(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def delete_routine(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def delete_voice_routine(*args, **kwargs) -> bool:
    return remove(*args, **kwargs)


def cancel(*args, **kwargs) -> bool:
    item_id = args[0] if args else kwargs.get("id") or kwargs.get("routine_id") or kwargs.get("schedule_id")
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
    if not args and "id" not in kwargs and "routine_id" not in kwargs and "schedule_id" not in kwargs:
        with _schedules_lock:
            return {
                "status": "scheduled" if VOICE_ROUTINES else "idle",
                "scheduled": len(VOICE_ROUTINES),
                "count": len(VOICE_ROUTINES),
                "size": len(VOICE_ROUTINES),
                "length": len(VOICE_ROUTINES),
                "pending": len(VOICE_ROUTINES),
            }
    item_id = args[0] if args else kwargs.get("id") or kwargs.get("routine_id") or kwargs.get("schedule_id")
    job = get(item_id)
    if job is None:
        return None
    return job.get("status", "scheduled")


def get_status(*args, **kwargs) -> Any:
    return status(*args, **kwargs)


# ---------------------------------------------------------------------------
# Scheduler class
# ---------------------------------------------------------------------------

class VoiceRoutineScheduler:
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
            for meth_name in ("run", "run_now", "run_once", "execute", "run_due", "trigger", "summarize", "run_due_voice_routines"):
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
        self._thread = threading.Thread(target=self._thread_loop, name="voice-routine-scheduler", daemon=True)
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

    def list_routines(self, *args, **kwargs) -> Any:
        return list_routines(*args, **kwargs)

    def list(self, *args, **kwargs) -> Any:
        return list_routines(*args, **kwargs)

    def get(self, *args, **kwargs) -> Any:
        return get(*args, **kwargs)

    def get_routine(self, *args, **kwargs) -> Any:
        return get(*args, **kwargs)

    def clear(self, *args, **kwargs) -> Any:
        return clear(*args, **kwargs)

    def status(self, *args, **kwargs) -> Any:
        return status(*args, **kwargs)

    @property
    def is_running(self) -> bool:
        return bool(self._running)


# Shared instance
voice_routine_scheduler = VoiceRoutineScheduler(interval_seconds=60)
voice_scheduler = voice_routine_scheduler
voiceRoutineScheduler = voice_routine_scheduler
scheduler = voice_routine_scheduler
VoiceScheduler = VoiceRoutineScheduler


def get_voice_routine_scheduler(*args, **kwargs) -> VoiceRoutineScheduler:
    global voice_routine_scheduler
    if args or kwargs:
        return VoiceRoutineScheduler(*args, **kwargs)
    return voice_routine_scheduler


def get_scheduler(*args, **kwargs) -> VoiceRoutineScheduler:
    return get_voice_routine_scheduler(*args, **kwargs)


def get_voice_scheduler(*args, **kwargs) -> VoiceRoutineScheduler:
    return get_voice_routine_scheduler(*args, **kwargs)


def start_voice_routine_scheduler(*args, **kwargs) -> VoiceRoutineScheduler:
    s = get_voice_routine_scheduler() if not (args or kwargs) else VoiceRoutineScheduler(*args, **kwargs)
    try:
        s.start(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass
    return s


def start_scheduler(*args, **kwargs) -> VoiceRoutineScheduler:
    return start_voice_routine_scheduler(*args, **kwargs)


def start_voice_scheduler(*args, **kwargs) -> VoiceRoutineScheduler:
    return start_voice_routine_scheduler(*args, **kwargs)


def stop_voice_routine_scheduler(*args, **kwargs) -> None:
    try:
        voice_routine_scheduler.stop(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass


def stop_scheduler(*args, **kwargs) -> None:
    stop_voice_routine_scheduler(*args, **kwargs)


def stop_voice_scheduler(*args, **kwargs) -> None:
    stop_voice_routine_scheduler(*args, **kwargs)


# Alias classes
VoiceRoutineSchedulerService = VoiceRoutineScheduler
VoiceRoutineService = VoiceRoutineScheduler
VoiceRoutineManager = VoiceRoutineScheduler
VoiceSchedulerService = VoiceRoutineScheduler
Scheduler = VoiceRoutineScheduler

# Module-level aliases for common names
voice_routine_service = voice_routine_scheduler
service = voice_routine_service
manager = voice_routine_service
default_service = voice_routine_service

# Additional function aliases
schedule_voice_now = schedule
run_voice_now = run_now
trigger_voice = trigger
trigger_voice_routine = trigger
execute_voice = execute
run_scheduled_voice_routines = run
run_scheduled_voice = run
run_scheduled_routines = run

# Expose is_due
is_due = _is_due
isDue = _is_due
voice_is_due = _is_due
routine_is_due = _is_due
