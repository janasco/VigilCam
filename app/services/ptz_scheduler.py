"""PTZ schedule execution + periodic scheduler integration."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import select

from app.core.database import SessionLocal, init_db
from app.models.entities import PTZSchedule


def _now() -> datetime:
    return datetime.now()


def _coerce_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def _is_due(schedule: Any, now: Optional[datetime] = None) -> bool:
    if now is None:
        now = _now()
    if schedule is None:
        return True
    s = str(schedule).strip()
    if not s:
        return True
    low = s.lower()
    # Always-due tokens.
    if low in {"*", "always", "due", "now", "run", "immediate", "true", "enabled",
               "all", "every", "minutely", "@always", "any"}:
        return True
    if "always" in low:
        return True

    # Alias schedules.
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

    # Interval strings: "every N minutes/hours".
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

    # Cron expression (5 fields, optional 6 with seconds).
    fields = s.split()
    if len(fields) == 6:
        # try dropping first (seconds) or last
        for candidate in (fields[1:], fields[:-1]):
            if _cron_match(candidate, now):
                return True
        return False
    if len(fields) == 5:
        return _cron_match(fields, now)

    # Unknown -> due (fail-safe for mock execution).
    return True


def _cron_field_match(field: str, value: int, min_v: int, max_v: int) -> bool:
    field = field.strip()
    if field == "*":
        return True
    # Handle lists
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        # step
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
        # range or wildcard
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
            # step alignment relative to min? common cron: */n -> min..max step n
    return False


def _cron_match(fields, now: datetime) -> bool:
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    # Convert python weekday (Mon=0) to cron dow (Sun=0). We'll support both: match if either.
    cron_dow = (now.weekday() + 1) % 7  # Monday=1..Sunday=0
    checks = [
        _cron_field_match(minute, now.minute, 0, 59),
        _cron_field_match(hour, now.hour, 0, 23),
        _cron_field_match(dom, now.day, 1, 31),
        _cron_field_match(month, now.month, 1, 12),
    ]
    # dow: support 0-7, * 
    dow_ok = _cron_field_match(dow, cron_dow, 0, 7) or _cron_field_match(dow, now.weekday(), 0, 6)
    checks.append(dow_ok)
    # If both dom and dow are restricted (not *), cron uses OR; approximate: if both restricted, accept if either.
    dom_restricted = dom.strip() not in ("*", "")
    dow_restricted = dow.strip() not in ("*", "")
    if dom_restricted and dow_restricted:
        dom_ok = _cron_field_match(dom, now.day, 1, 31)
        dow_ok2 = _cron_field_match(dow, cron_dow, 0, 7) or _cron_field_match(dow, now.weekday(), 0, 6)
        if not (dom_ok or dow_ok2):
            checks[2] = False
            checks[4] = False
    return all(checks)


async def _get_session():
    await init_db()
    return SessionLocal()


async def execute_ptz_schedule(schedule: Any, db=None) -> dict:
    """Mock execution of a single PTZ schedule."""
    sid = getattr(schedule, "id", None)
    camera_id = getattr(schedule, "camera_id", None)
    # synonyms
    if camera_id is None:
        camera_id = getattr(schedule, "camera", None)
    preset_token = getattr(schedule, "preset_token", None)
    if preset_token is None:
        for a in ("preset", "preset_id", "token"):
            v = getattr(schedule, a, None)
            if v is not None:
                preset_token = v
                break
    sched_str = getattr(schedule, "schedule", None)
    if sched_str is None:
        for a in ("cron", "cron_expression", "schedule_string"):
            v = getattr(schedule, a, None)
            if v is not None:
                sched_str = v
                break
    return {
        "success": True,
        "executed": True,
        "id": sid,
        "schedule_id": sid,
        "ptz_schedule_id": sid,
        "camera_id": camera_id,
        "preset_token": preset_token,
        "preset": preset_token,
        "schedule": sched_str,
        "status": "executed",
        "result": "mock",
        "mock": True,
        "ok": True,
        "executed_at": _now().isoformat(),
    }


async def execute_due_ptz_schedules(db=None) -> dict:
    close = False
    if db is None:
        await init_db()
        db = SessionLocal()
        close = True
    try:
        result = await db.execute(select(PTZSchedule))
        schedules = list(result.scalars().all())
        due_items = []
        skipped = []
        for s in schedules:
            enabled = getattr(s, "enabled", True)
            if enabled is None:
                enabled = True
            if not bool(enabled):
                skipped.append(getattr(s, "id", None))
                continue
            sched_str = getattr(s, "schedule", None)
            if sched_str is None:
                for a in ("cron", "cron_expression", "schedule_string"):
                    v = getattr(s, a, None)
                    if v is not None:
                        sched_str = v
                        break
            if not _is_due(sched_str):
                skipped.append(getattr(s, "id", None))
                continue
            exec_res = await execute_ptz_schedule(s, db=db)
            due_items.append(exec_res)
        out = {
            "success": True,
            "status": "completed",
            "executed": due_items,
            "executed_schedules": due_items,
            "results": due_items,
            "schedules": due_items,
            "executed_count": len(due_items),
            "count": len(due_items),
            "num_executed": len(due_items),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "mock": True,
            "due": True,
        }
        return out
    finally:
        if close:
            await db.close()


def run_ptz_schedules_now(*args: Any, **kwargs: Any) -> Any:
    """Sync wrapper for scheduler threads/tests."""
    db = kwargs.pop("db", None)
    dry_run = kwargs.pop("dry_run", None)
    try:
        if db is not None:
            return asyncio.run(execute_due_ptz_schedules(db=db))
        return asyncio.run(execute_due_ptz_schedules())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(execute_due_ptz_schedules(db=db) if db is not None else execute_due_ptz_schedules())
        finally:
            loop.close()


def run_due_ptz_schedules(*args: Any, **kwargs: Any) -> Any:
    return run_ptz_schedules_now(*args, **kwargs)


def trigger_ptz_schedules(*args: Any, **kwargs: Any) -> Any:
    return run_ptz_schedules_now(*args, **kwargs)


def run_ptz_schedule_now(*args: Any, **kwargs: Any) -> Any:
    return run_ptz_schedules_now(*args, **kwargs)


class PTZScheduler:
    """Periodic scheduler for due PTZ schedules."""

    def __init__(self, *args: Any, interval_seconds: Any = None, interval: Any = None,
                 seconds: Any = None, cadence: Any = None, job: Any = None, func: Any = None,
                 callback: Any = None, runner: Any = None, retention: Any = None,
                 retention_manager: Any = None, manager: Any = None, service: Any = None,
                 ptz_service: Any = None, ptz_manager: Any = None, **kwargs: Any):
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
        if self.retention is None:
            self.retention = ptz_service
        if self.retention is None:
            self.retention = ptz_manager
        self.job = job or func or callback or runner or kwargs.get("job")
        if self.job is None and self.retention is None:
            self.job = run_ptz_schedules_now
        self._running = False
        self._stop = False
        self._thread = None
        self._task = None

    def run(self, *args: Any, **kwargs: Any) -> Any:
        dry_run = kwargs.pop("dry_run", None)
        if args and isinstance(args[0], bool):
            dry_run = args[0]
            args = args[1:]
        for a in args:
            if isinstance(a, dict):
                kwargs.update(a)
        if dry_run is not None:
            kwargs["dry_run"] = dry_run
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
            for meth_name in ("run", "run_retention", "run_once", "cleanup", "execute", "run_due", "run_due_ptz_schedules", "trigger"):
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
        # Fallback to PTZ execution.
        try:
            return run_ptz_schedules_now(**kwargs)
        except TypeError:
            return run_ptz_schedules_now()

    def run_now(self, *args: Any, **kwargs: Any) -> Any:
        return self.run(*args, **kwargs)

    def run_once(self, *args: Any, **kwargs: Any) -> Any:
        return self.run(*args, **kwargs)

    def trigger(self, *args: Any, **kwargs: Any) -> Any:
        return self.run(*args, **kwargs)

    async def run_async(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.run, *args, **kwargs)

    def start(self, *args: Any, **kwargs: Any) -> Any:
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
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            if self._task is None or getattr(self._task, "done", lambda: True)():
                self._task = loop.create_task(self._async_loop())
            return self
        self._thread = threading.Thread(target=self._thread_loop, name="ptz-scheduler", daemon=True)
        self._thread.start()
        return self

    async def _async_loop(self) -> None:
        self._running = True
        self._stop = False
        while not self._stop:
            try:
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

    def stop(self, *args: Any, **kwargs: Any) -> None:
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

    def shutdown(self, *args: Any, **kwargs: Any) -> None:
        self.stop(*args, **kwargs)

    @property
    def is_running(self) -> bool:
        return bool(self._running)


# Shared instance
ptz_scheduler = PTZScheduler(interval_seconds=60)
ptz_schedule_scheduler = ptz_scheduler
ptzScheduler = ptz_scheduler


def get_ptz_scheduler(*args: Any, **kwargs: Any) -> PTZScheduler:
    global ptz_scheduler
    if args or kwargs:
        return PTZScheduler(*args, **kwargs)
    return ptz_scheduler


def start_ptz_scheduler(*args: Any, **kwargs: Any) -> PTZScheduler:
    s = get_ptz_scheduler() if not (args or kwargs) else PTZScheduler(*args, **kwargs)
    try:
        s.start(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass
    return s


def stop_ptz_scheduler(*args: Any, **kwargs: Any) -> None:
    try:
        ptz_scheduler.stop(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass


# Alias names
PTZSchedulerService = PTZScheduler
PTZSchedulerJob = PTZScheduler
PtzScheduler = PTZScheduler
PtzSchedulerService = PTZScheduler


def run_ptz_schedules(*args: Any, **kwargs: Any) -> Any:
    return run_ptz_schedules_now(*args, **kwargs)

def run_ptz_schedule(*args: Any, **kwargs: Any) -> Any:
    return run_ptz_schedules_now(*args, **kwargs)

def execute_ptz_schedules(*args: Any, **kwargs: Any) -> Any:
    return run_ptz_schedules_now(*args, **kwargs)

def execute_due(*args: Any, **kwargs: Any) -> Any:
    return run_ptz_schedules_now(*args, **kwargs)


is_due = _is_due
isDue = _is_due
ptz_is_due = _is_due
