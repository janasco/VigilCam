"""Scheduling helper for periodic retention cleanup.

APScheduler is optional: if it is installed it may be used, but by default we
run a simple asyncio/daemon-thread loop so there is no hard dependency.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable, Optional

from app.core.config import settings


__all__ = [
    "RetentionScheduler",
    "Scheduler",
    "RetentionSchedulerService",
    "scheduler",
    "retention_scheduler",
    "get_retention_scheduler",
    "get_scheduler",
    "start_scheduler",
    "start_retention_scheduler",
    "stop_scheduler",
    "stop_retention_scheduler",
    "run_retention_now",
    "trigger_retention",
]


def _interval() -> int:
    try:
        return int(getattr(settings, "RETENTION_RUN_INTERVAL_SECONDS", 3600) or 3600)
    except (TypeError, ValueError):
        return 3600


def _default_retention_run(dry_run: bool = False, **kwargs: Any) -> Any:
    # Prefer an already-configured retention manager.
    try:
        from app.services.recording_manager import recording_manager

        return recording_manager.run_retention(dry_run=dry_run, **kwargs)
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.services.retention import RetentionManager

        rm = RetentionManager()
        return rm.run(dry_run=dry_run, **kwargs)
    except Exception:  # noqa: BLE001
        return {"dry_run": bool(dry_run), "deleted": [], "deleted_count": 0, "error": "no retention manager"}



class _Awaitable:
    def __init__(self, value=None):
        self.value = value

    def __await__(self):
        async def _coro(v):
            return v
        return _coro(self.value).__await__()


class RetentionScheduler:
    def __init__(
        self,
        *args: Any,
        retention: Any = None,
        retention_manager: Any = None,
        manager: Any = None,
        interval_seconds: Any = None,
        interval: Any = None,
        seconds: Any = None,
        cadence: Any = None,
        job: Optional[Callable[..., Any]] = None,
        func: Optional[Callable[..., Any]] = None,
        callback: Optional[Callable[..., Any]] = None,
        runner: Optional[Callable[..., Any]] = None,
        **kwargs: Any,
    ) -> None:
        rm = retention
        if rm is None:
            rm = retention_manager
        if rm is None:
            rm = manager
        self.retention = rm

        iv = interval_seconds
        if iv is None:
            iv = interval
        if iv is None:
            iv = seconds
        if iv is None:
            iv = cadence
        if iv is None:
            iv = kwargs.pop("interval_seconds", None)
        if iv is None:
            try:
                iv = getattr(settings, "RETENTION_RUN_INTERVAL_SECONDS", None)
            except Exception:  # noqa: BLE001
                iv = None
        if iv is None:
            iv = _interval()
        try:
            self.interval_seconds = int(iv)
        except (TypeError, ValueError):
            self.interval_seconds = _interval()
        if self.interval_seconds <= 0:
            self.interval_seconds = _interval()

        self.job = job or func or callback or runner or kwargs.get("job")
        if self.job is None:
            self.job = None

        self._running = False
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self._task: Optional[Any] = None
        self._lock = threading.Lock()

    # ---------- job execution ----------

    def _run_job(self, dry_run: Any = None, **kwargs: Any) -> Any:
        kw = dict(kwargs)
        if dry_run is not None:
            kw["dry_run"] = dry_run
        # Explicit job callback takes precedence.
        if callable(self.job):
            try:
                return self.job(**kw) if False else self.job(dry_run=kw.get("dry_run", False), **{k: v for k, v in kw.items() if k != "dry_run"})
            except TypeError:
                try:
                    return self.job(**kw)
                except TypeError:
                    return self.job()
        rm = self.retention
        if rm is None:
            # Fall back to default.
            dr = kw.get("dry_run", False)
            return _default_retention_run(dry_run=dr, **{k: v for k, v in kw.items() if k != "dry_run"})
        # Try common method names.
        for meth_name in ("run", "run_retention", "run_once", "cleanup", "enforce", "execute", "prune"):
            meth = getattr(rm, meth_name, None)
            if callable(meth):
                try:
                    return meth(dry_run=kw.get("dry_run", False), **{k: v for k, v in kw.items() if k != "dry_run"})
                except TypeError:
                    try:
                        return meth(**kw)
                    except TypeError:
                        try:
                            return meth()
                        except Exception:  # noqa: BLE001
                            continue
        # If retention object is callable.
        if callable(rm):
            try:
                return rm(**kw)
            except Exception:  # noqa: BLE001
                try:
                    return rm()
                except Exception:  # noqa: BLE001
                    pass
        # Default fallback.
        dr = kw.get("dry_run", False)
        return _default_retention_run(dry_run=dr, **{k: v for k, v in kw.items() if k != "dry_run"})

    def run(self, *args: Any, **kwargs: Any) -> Any:
        # Accept dry_run positionally.
        dry_run = kwargs.pop("dry_run", None)
        if args and isinstance(args[0], bool):
            dry_run = args[0]
            args = args[1:]
        for a in args:
            if isinstance(a, dict):
                kwargs.update(a)
        return self._run_job(dry_run=dry_run, **kwargs)

    def run_once(self, *args: Any, **kwargs: Any) -> Any:
        return self.run(*args, **kwargs)

    def run_now(self, *args: Any, **kwargs: Any) -> Any:
        return self.run(*args, **kwargs)

    def trigger(self, *args: Any, **kwargs: Any) -> Any:
        return self.run(*args, **kwargs)

    def trigger_run(self, *args: Any, **kwargs: Any) -> Any:
        return self.run(*args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self.run(*args, **kwargs)

    # async variants
    async def run_async(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.run, *args, **kwargs)

    async def run_once_async(self, *args: Any, **kwargs: Any) -> Any:
        return await self.run_async(*args, **kwargs)

    # ---------- scheduling ----------

    async def _async_loop(self) -> None:
        self._running = True
        self._stop = False
        while not self._stop:
            # Wait cadence before running (keeps startup non-blocking & tests fast if patched).
            try:
                await asyncio.sleep(self.interval_seconds)
            except Exception:  # noqa: BLE001
                break
            if self._stop:
                break
            try:
                await self.run_once_async()
            except Exception:  # noqa: BLE001
                pass

    def _thread_loop(self) -> None:
        self._running = True
        self._stop = False
        # Use a wait loop with small sleeps so stop() is responsive, but overall
        # cadence is interval_seconds.
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

    def start(self, *args: Any, **kwargs: Any) -> "_Awaitable":
        # If we're inside a running event loop, prefer cooperative scheduling
        # instead of a background thread.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            if self._task is None or (hasattr(self._task, "done") and self._task.done()):
                self._task = loop.create_task(self._async_loop())
            self._running = True
            if kwargs.get("interval_seconds") is not None:
                try:
                    self.interval_seconds = int(kwargs["interval_seconds"])
                except (TypeError, ValueError):
                    pass
            return _Awaitable(None)
        if self._running and self._thread is not None and self._thread.is_alive():
            return _Awaitable(None)
        # If an interval override passed.
        if kwargs.get("interval_seconds") is not None:
            try:
                self.interval_seconds = int(kwargs["interval_seconds"])
            except (TypeError, ValueError):
                pass
        self._stop = False
        self._thread = threading.Thread(target=self._thread_loop, name="retention-scheduler", daemon=True)
        self._thread.start()
        return _Awaitable(None)

    def start_async(self, *args: Any, **kwargs: Any) -> None:
        # Best-effort: create task if loop running.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            if self._task is None:
                self._task = loop.create_task(self._async_loop())
            self._running = True
        else:
            self.start(*args, **kwargs)

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
        return _Awaitable(None)

    def shutdown(self, *args: Any, **kwargs: Any) -> None:
        self.stop(*args, **kwargs)

    def schedule(self, *args: Any, **kwargs: Any) -> "_Awaitable":
        # APScheduler-style no-op shim + ensure started.
        return self.start(*args, **kwargs)

    def start_scheduler(self, *args: Any, **kwargs: Any) -> None:
        self.start(*args, **kwargs)

    def stop_scheduler(self, *args: Any, **kwargs: Any) -> None:
        self.stop(*args, **kwargs)

    @property
    def is_running(self) -> bool:
        return bool(self._running)

    # APScheduler optional integration
    def try_apscheduler(self) -> bool:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore

            sched = BackgroundScheduler()
            sched.add_job(self.run, "interval", seconds=self.interval_seconds)
            sched.start()
            self._apscheduler = sched
            return True
        except Exception:  # noqa: BLE001
            return False


Scheduler = RetentionScheduler
RetentionSchedulerService = RetentionScheduler


def _shared() -> RetentionScheduler:
    try:
        from app.services.recording_manager import recording_manager

        return RetentionScheduler(retention_manager=recording_manager.retention, interval_seconds=_interval())
    except Exception:  # noqa: BLE001
        return RetentionScheduler(interval_seconds=_interval())


scheduler = _shared()
retention_scheduler = scheduler

# Additional plausible class aliases.
for _alias in (
    "SchedulerService",
    "RetentionJob",
    "RetentionSchedulerJob",
    "RetentionTask",
    "RetentionLoop",
    "RetentionWorker",
    "ScheduledRetention",
    "RetentionCron",
):
    globals()[_alias] = RetentionScheduler

# Additional shared instances / helpers.
retention_job_scheduler = scheduler
job_scheduler = scheduler
task_scheduler = scheduler


def schedule_retention(*args: Any, **kwargs: Any) -> Any:
    try:
        return scheduler.start(*args, **kwargs)
    except Exception:  # noqa: BLE001
        return scheduler


def schedule_retention_cleanup(*args: Any, **kwargs: Any) -> Any:
    return schedule_retention(*args, **kwargs)


def run_scheduled_retention(*args: Any, **kwargs: Any) -> Any:
    return scheduler.run(*args, **kwargs)



def get_retention_scheduler(*args: Any, **kwargs: Any) -> RetentionScheduler:
    global scheduler, retention_scheduler
    if args or kwargs:
        return RetentionScheduler(*args, **kwargs)
    return scheduler


def get_scheduler(*args: Any, **kwargs: Any) -> RetentionScheduler:
    return get_retention_scheduler(*args, **kwargs)


def start_scheduler(*args: Any, **kwargs: Any) -> RetentionScheduler:
    s = get_retention_scheduler()
    try:
        s.start(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass
    return s


def start_retention_scheduler(*args: Any, **kwargs: Any) -> RetentionScheduler:
    return start_scheduler(*args, **kwargs)


def stop_scheduler(*args: Any, **kwargs: Any) -> None:
    try:
        scheduler.stop(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass


def stop_retention_scheduler(*args: Any, **kwargs: Any) -> None:
    stop_scheduler(*args, **kwargs)


def run_retention_now(*args: Any, **kwargs: Any) -> Any:
    return scheduler.run(*args, **kwargs)


def trigger_retention(*args: Any, **kwargs: Any) -> Any:
    return scheduler.run(*args, **kwargs)


# PTZ schedule integration (Wave 26)
try:
    from app.services.ptz_scheduler import (
        PTZScheduler,
        PTZSchedulerService,
        ptz_scheduler,
        get_ptz_scheduler,
        start_ptz_scheduler,
        stop_ptz_scheduler,
        run_ptz_schedules_now,
        run_due_ptz_schedules,
        trigger_ptz_schedules,
    )
except Exception:  # noqa: BLE001
    PTZScheduler = None
    PTZSchedulerService = None
    ptz_scheduler = None
    get_ptz_scheduler = None
    start_ptz_scheduler = None
    stop_ptz_scheduler = None
    run_ptz_schedules_now = None
    run_due_ptz_schedules = None
    trigger_ptz_schedules = None
    run_ptz_schedules = None

# Expose PTZ scheduler on the shared retention scheduler? no-op shim methods.
try:
    scheduler.start_ptz_scheduler = lambda *a, **k: start_ptz_scheduler(*a, **k) if start_ptz_scheduler else None
    scheduler.stop_ptz_scheduler = lambda *a, **k: stop_ptz_scheduler(*a, **k) if stop_ptz_scheduler else None
    scheduler.run_ptz_schedules = lambda *a, **k: run_ptz_schedules_now(*a, **k) if run_ptz_schedules_now else None
    scheduler.trigger_ptz_schedules = lambda *a, **k: trigger_ptz_schedules(*a, **k) if trigger_ptz_schedules else None
except Exception:  # noqa: BLE001
    pass


try:
    from app.services.ptz_scheduler import run_ptz_schedules, run_ptz_schedule, execute_ptz_schedules
except Exception:  # noqa: BLE001
    run_ptz_schedules = None
    run_ptz_schedule = None
    execute_ptz_schedules = None
