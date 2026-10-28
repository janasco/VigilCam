"""Periodic auto-merge scheduler (opt-in).

Event merge stays manual-only by default: ``app.services.event_merge``
provides pure helpers (``merge_events`` / ``split_event`` / ``suggest_merges``
/ ``auto_merge``) plus DB-backed ``merge_events_db`` / ``split_event_db``,
and ``app.api.event_merge`` exposes them on demand via
``POST /api/events/merge`` / ``/api/events/split`` / suggest endpoints.
Nothing merges automatically at import time or at app startup.

This module adds an *opt-in* periodic scheduler that, when enabled, groups
close-in-time events (per camera, within ``gap_seconds``) and merges each
group. Nothing here starts automatically; callers must explicitly enable
and start it (e.g. via the ``app.api.merge_schedule`` router).
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "MergeScheduleScheduler",
    "MergeScheduler",
    "Scheduler",
    "merge_schedule_scheduler",
    "merge_scheduler",
    "scheduler",
    "get_merge_schedule_scheduler",
    "get_merge_scheduler",
    "get_scheduler",
    "start_merge_schedule_scheduler",
    "start_merge_scheduler",
    "start_scheduler",
    "stop_merge_schedule_scheduler",
    "stop_merge_scheduler",
    "stop_scheduler",
    "run_scheduled_merge",
    "run_merge_now",
    "trigger_merge",
    "get_config",
    "get_status",
    "update_config",
    "set_config",
    "configure",
]

DEFAULT_INTERVAL_SECONDS = 300
MIN_INTERVAL_SECONDS = 60
DEFAULT_GAP_SECONDS = 60.0
DEFAULT_LIMIT = 500

MERGE_COMPLETED_EVENT = "events_auto_merged"
SCAN_COMPLETED_EVENT = "merge_schedule_completed"
MERGE_SUGGESTED_EVENT = "events_merge_suggested"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_interval(value: Any, default: int = DEFAULT_INTERVAL_SECONDS) -> int:
    try:
        iv = int(value)
    except (TypeError, ValueError):
        try:
            iv = int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
    if iv < MIN_INTERVAL_SECONDS:
        iv = MIN_INTERVAL_SECONDS
    return iv


def _coerce_gap(value: Any, default: float = DEFAULT_GAP_SECONDS) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        try:
            f = float(str(value).strip())
        except Exception:
            return default
    if f < 0:
        return default
    return f


def _coerce_int(value: Any, default: int = DEFAULT_LIMIT) -> int:
    try:
        iv = int(value)
    except (TypeError, ValueError):
        try:
            iv = int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
    if iv <= 0:
        return default
    return iv


def _coerce_camera(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# In-memory configuration + state (opt-in; disabled by default)
# ---------------------------------------------------------------------------

_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "interval_seconds": DEFAULT_INTERVAL_SECONDS,
    "gap_seconds": DEFAULT_GAP_SECONDS,
    "camera_id": None,
    "limit": DEFAULT_LIMIT,
}

_STATE: Dict[str, Any] = {
    "last_run_at": None,
    "last_result": None,
    "last_merged": [],
    "last_suggestions": [],
    "last_counts": {},
    "run_count": 0,
    "last_error": None,
}

_lock = threading.Lock()


def get_config() -> Dict[str, Any]:
    with _lock:
        return dict(_CONFIG)


def update_config(**kwargs: Any) -> Dict[str, Any]:
    """Update scheduler configuration; returns the new config dict."""
    with _lock:
        for key in ("enabled", "active", "is_enabled", "on", "auto_merge", "autoMerge"):
            if key in kwargs and kwargs[key] is not None:
                _CONFIG["enabled"] = bool(kwargs[key])
                break
        for key in ("interval_seconds", "interval", "seconds", "cadence", "period", "poll_interval"):
            if key in kwargs and kwargs[key] is not None:
                _CONFIG["interval_seconds"] = _coerce_interval(kwargs[key])
                break
        for key in (
            "gap_seconds", "gapSeconds", "gap", "max_gap", "maxGap",
            "max_gap_seconds", "maxGapSeconds", "threshold", "threshold_seconds",
            "window", "window_seconds", "merge_window", "mergeWindow", "merge_gap",
        ):
            if key in kwargs and kwargs[key] is not None:
                _CONFIG["gap_seconds"] = _coerce_gap(kwargs[key])
                break
        for key in ("camera_id", "cameraId", "camera", "cam", "camera_filter"):
            if key in kwargs:
                _CONFIG["camera_id"] = _coerce_camera(kwargs[key])
                break
        for key in ("limit", "n", "max", "max_events", "maxEvents", "batch_size", "batchSize"):
            if key in kwargs and kwargs[key] is not None:
                _CONFIG["limit"] = _coerce_int(kwargs[key])
                break
    return get_config()


set_config = update_config
configure = update_config
set_schedule = update_config
update_schedule = update_config


def get_status() -> Dict[str, Any]:
    cfg = get_config()
    with _lock:
        state = {
            "last_run_at": _STATE.get("last_run_at"),
            "last_counts": dict(_STATE.get("last_counts") or {}),
            "last_merged_count": len(_STATE.get("last_merged") or []),
            "run_count": _STATE.get("run_count", 0),
            "last_error": _STATE.get("last_error"),
        }
    return {
        "enabled": bool(cfg.get("enabled")),
        "running": bool(_singleton_running()),
        "interval_seconds": cfg.get("interval_seconds"),
        "gap_seconds": cfg.get("gap_seconds"),
        "camera_id": cfg.get("camera_id"),
        "limit": cfg.get("limit"),
        **state,
    }


status = get_status


def reset_state() -> Dict[str, Any]:
    with _lock:
        _STATE["last_run_at"] = None
        _STATE["last_result"] = None
        _STATE["last_merged"] = []
        _STATE["last_suggestions"] = []
        _STATE["last_counts"] = {}
        _STATE["run_count"] = 0
        _STATE["last_error"] = None
    return {"reset": True}


clear_state = reset_state
reset_stats = reset_state
clear_stats = reset_state


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------

def _fire_event(event_type: str, payload: Any) -> None:
    """Best-effort publish of an event via the shared event bus."""
    try:
        from app.services.event_bus import event_bus as _bus
    except Exception:  # noqa: BLE001
        return
    if _bus is None:
        return
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            loop.create_task(_bus.publish(event_type, payload))
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        coro = _bus.publish(event_type, payload)
        if not asyncio.iscoroutine(coro):
            return
        try:
            asyncio.run(coro)
        except RuntimeError:
            new_loop = asyncio.new_event_loop()
            try:
                new_loop.run_until_complete(coro)
            finally:
                new_loop.close()
        except Exception:  # noqa: BLE001
            try:
                coro.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def _extract_inline_events(kwargs: Dict[str, Any]) -> Optional[List[Any]]:
    for key in ("events", "items", "sources", "to_merge"):
        val = kwargs.get(key)
        if isinstance(val, list) and val:
            return list(val)
    return None


def _orm_to_dict(ev: Any) -> Dict[str, Any]:
    try:
        from app.services.event_merge import _orm_to_dict as _conv
        return _conv(ev)
    except Exception:  # noqa: BLE001
        pass
    out: Dict[str, Any] = {}
    for attr in ("id", "camera_id", "kind", "label", "confidence",
                 "payload", "tags", "snapshot_path", "created_at"):
        try:
            out[attr] = getattr(ev, attr, None)
        except Exception:  # noqa: BLE001
            out[attr] = None
    if hasattr(out.get("created_at"), "isoformat"):
        try:
            out["created_at"] = out["created_at"].isoformat()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    return out


async def _fetch_recent_events(
    db: Any, camera_id: Optional[int], limit: int
) -> List[Any]:
    try:
        from sqlalchemy import select
        from app.models.entities import Event as EventModel
    except Exception:  # noqa: BLE001
        return []
    try:
        capped = min(max(int(limit), 1), 2000)
    except Exception:  # noqa: BLE001
        capped = DEFAULT_LIMIT
    try:
        result = await db.execute(
            select(EventModel).order_by(EventModel.id.desc()).limit(capped)
        )
        rows = list(result.scalars().all())
    except Exception:  # noqa: BLE001
        return []
    if camera_id is not None:
        try:
            cid = int(camera_id)
            rows = [r for r in rows if getattr(r, "camera_id", None) == cid]
        except (TypeError, ValueError):
            pass
    return rows


async def run_scheduled_merge(
    *args: Any,
    db: Any = None,
    session: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Run one scheduled auto-merge pass.

    - With inline ``events`` (list of dicts): pure ``auto_merge`` computation,
      no DB writes.
    - With ``db``/``session``: fetch recent events from the DB, group them,
      and persist each group via ``merge_events_db`` (unless ``dry_run``).
    - With neither: try to open a short-lived session via
      ``app.core.database.SessionLocal``; if unavailable, return an empty
      successful summary (no crash when the DB is not configured).

    Returns a summary dict with ``merged``, ``suggestions`` and ``counts``.
    """
    # Normalize positional dict arg.
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "request", "options", "config"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)

    cfg = get_config()
    gap = cfg.get("gap_seconds")
    for _k in ("gap_seconds", "gapSeconds", "gap", "max_gap", "threshold",
               "window", "merge_window"):
        if flat.get(_k) is not None:
            gap = flat.get(_k)
            break
    gap_val = _coerce_gap(gap, DEFAULT_GAP_SECONDS)
    cam_raw = cfg.get("camera_id")
    for _k in ("camera_id", "cameraId", "camera", "cam"):
        if flat.get(_k) is not None:
            cam_raw = flat.get(_k)
            break
    camera_id = _coerce_camera(cam_raw)
    lim_raw = cfg.get("limit")
    for _k in ("limit", "n", "max_events", "maxEvents", "batch_size"):
        if flat.get(_k) is not None:
            lim_raw = flat.get(_k)
            break
    try:
        limit = _coerce_int(lim_raw, DEFAULT_LIMIT)
    except Exception:  # noqa: BLE001
        limit = DEFAULT_LIMIT
    dry_run = bool(flat.get("dry_run", flat.get("dryRun", flat.get("preview", False))))

    sess = db if db is not None else session
    inline_events = _extract_inline_events(flat)

    from app.services import event_merge as merge_mod

    merged: List[Dict[str, Any]] = []
    suggestions: List[Dict[str, Any]] = []
    groups: List[List[Dict[str, Any]]] = []
    errors: List[str] = []
    total_considered = 0

    try:
        if inline_events is not None:
            groups = merge_mod.group_events_for_merge(inline_events, gap_seconds=gap_val)
            total_considered = len(inline_events)
            suggestions = merge_mod.suggest_merges(events=inline_events, gap_seconds=gap_val)
            if dry_run:
                merged = []
            else:
                merged = merge_mod.auto_merge(events=inline_events, gap_seconds=gap_val)
        elif sess is not None:
            rows = await _fetch_recent_events(sess, camera_id, limit)
            total_considered = len(rows)
            dicts = [_orm_to_dict(r) for r in rows]
            groups = merge_mod.group_events_for_merge(dicts, gap_seconds=gap_val)
            suggestions = [
                {
                    "event_ids": [merge_mod._get_id(e) for e in g if merge_mod._get_id(e) is not None],
                    "count": len(g),
                    "camera_id": merge_mod._get_camera(g[0]) if g else None,
                    "gap_seconds": gap_val,
                }
                for g in groups
            ]
            if dry_run:
                merged = []
            else:
                for g in groups:
                    ids = [merge_mod._get_id(e) for e in g if merge_mod._get_id(e) is not None]
                    if len(ids) < 2:
                        continue
                    try:
                        out = await merge_mod.merge_events_db(sess, {"event_ids": ids})
                        merged.append(out)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(str(exc))
        else:
            # No inline events and no session: try a short-lived session.
            opened_here = None
            try:
                from app.core.database import SessionLocal as _SessionLocal
                opened_here = _SessionLocal()
            except Exception:  # noqa: BLE001
                opened_here = None
            if opened_here is None:
                with _lock:
                    _STATE["last_run_at"] = _now_iso()
                    _STATE["last_result"] = []
                    _STATE["last_merged"] = []
                    _STATE["last_suggestions"] = []
                    _STATE["last_counts"] = {"total": 0, "groups": 0, "merged": 0, "errors": 0}
                    _STATE["run_count"] = int(_STATE.get("run_count") or 0) + 1
                    _STATE["last_error"] = None
                return {
                    "success": True,
                    "ok": True,
                    "total": 0,
                    "count": 0,
                    "groups": 0,
                    "merged": [],
                    "merged_count": 0,
                    "suggestions": [],
                    "counts": {"total": 0, "groups": 0, "merged": 0, "errors": 0},
                    "errors": [],
                    "at": _STATE["last_run_at"],
                    "gap_seconds": gap_val,
                    "dry_run": dry_run,
                }
            try:
                async with opened_here as _sess:
                    return await run_scheduled_merge(
                        db=_sess, gap_seconds=gap_val, camera_id=camera_id,
                        limit=limit, dry_run=dry_run,
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))

    with _lock:
        _STATE["last_run_at"] = _now_iso()
        _STATE["last_result"] = [dict(m) for m in merged]
        _STATE["last_merged"] = [dict(m) for m in merged]
        _STATE["last_suggestions"] = [dict(s) for s in suggestions]
        _STATE["last_counts"] = {
            "total": total_considered,
            "groups": len(groups),
            "merged": len(merged),
            "suggestions": len(suggestions),
            "errors": len(errors),
        }
        _STATE["run_count"] = int(_STATE.get("run_count") or 0) + 1
        _STATE["last_error"] = "; ".join(errors) if errors else None
        at = _STATE["last_run_at"]

    if merged and not dry_run:
        _fire_event(MERGE_COMPLETED_EVENT, {
            "merged": len(merged),
            "groups": len(groups),
            "at": at,
        })
        _fire_event(SCAN_COMPLETED_EVENT, {
            "merged": len(merged),
            "groups": len(groups),
            "at": at,
        })
    else:
        _fire_event(SCAN_COMPLETED_EVENT, {
            "merged": len(merged),
            "groups": len(groups),
            "at": at,
        })

    return {
        "success": len(errors) == 0,
        "ok": len(errors) == 0,
        "total": total_considered,
        "count": len(merged),
        "groups": len(groups),
        "group_count": len(groups),
        "merged": [dict(m) for m in merged],
        "results": [dict(m) for m in merged],
        "merged_count": len(merged),
        "suggestions": [dict(s) for s in suggestions],
        "suggestion_count": len(suggestions),
        "counts": dict(_STATE["last_counts"]),
        "errors": errors,
        "at": at,
        "gap_seconds": gap_val,
        "camera_id": camera_id,
        "dry_run": dry_run,
    }


# Aliases for the scheduled-run entrypoint.
run_scheduled_merges = run_scheduled_merge
run_merge_schedule = run_scheduled_merge
run_scheduled_auto_merge = run_scheduled_merge
scheduled_merge = run_scheduled_merge
auto_merge_scheduled = run_scheduled_merge
merge_now = run_scheduled_merge


def run_merge_now(**kwargs: Any) -> Dict[str, Any]:
    """Synchronous wrapper (for scheduler threads / tests without a loop)."""
    try:
        return asyncio.run(run_scheduled_merge(**kwargs))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(run_scheduled_merge(**kwargs))
        finally:
            loop.close()


trigger_merge = run_merge_now
trigger_scheduled_merge = run_merge_now
run_scheduled_merge_now = run_merge_now
run_now_sync = run_merge_now


# ---------------------------------------------------------------------------
# Periodic scheduler (mirrors RetentionScheduler / PTZScheduler conventions)
# ---------------------------------------------------------------------------

class MergeScheduleScheduler:
    """Opt-in periodic scheduler for event auto-merge."""

    def __init__(
        self,
        *args: Any,
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
        iv = interval_seconds
        if iv is None:
            iv = interval
        if iv is None:
            iv = seconds
        if iv is None:
            iv = cadence
        if iv is None:
            iv = kwargs.get("interval_seconds", kwargs.get("interval"))
        if iv is None:
            try:
                iv = get_config().get("interval_seconds")
            except Exception:  # noqa: BLE001
                iv = None
        if iv is None:
            iv = DEFAULT_INTERVAL_SECONDS
        self.interval_seconds = _coerce_interval(iv)
        self.job = job or func or callback or runner or kwargs.get("job")
        self._running = False
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self._task: Optional[Any] = None

    # -- execution ------------------------------------------------------
    def _run_job(self, **kwargs: Any) -> Any:
        if callable(self.job):
            try:
                return self.job(**kwargs)
            except TypeError:
                try:
                    return self.job()
                except Exception:  # noqa: BLE001
                    pass
        return run_merge_now(**kwargs)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        for a in args:
            if isinstance(a, dict):
                kwargs.update(a)
        return self._run_job(**kwargs)

    def run_once(self, *args: Any, **kwargs: Any) -> Any:
        return self.run(*args, **kwargs)

    def run_now(self, *args: Any, **kwargs: Any) -> Any:
        return self.run(*args, **kwargs)

    def trigger(self, *args: Any, **kwargs: Any) -> Any:
        return self.run(*args, **kwargs)

    async def run_async(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.run, *args, **kwargs)

    # -- lifecycle ------------------------------------------------------
    def _thread_loop(self) -> None:
        self._running = True
        self._stop = False
        next_run = time.time() + self.interval_seconds
        while not self._stop:
            now = time.time()
            if now >= next_run:
                cfg = get_config()
                if cfg.get("enabled"):
                    try:
                        self.run()
                    except Exception:  # noqa: BLE001
                        pass
                next_run = time.time() + self.interval_seconds
            time.sleep(min(1.0, max(0.05, next_run - now)))
        self._running = False

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
                if get_config().get("enabled"):
                    await self.run_async()
            except Exception:  # noqa: BLE001
                pass

    def start(self, *args: Any, **kwargs: Any) -> "MergeScheduleScheduler":
        if kwargs.get("interval_seconds") is not None:
            self.interval_seconds = _coerce_interval(kwargs["interval_seconds"])
        if self._running:
            if self._thread is not None and self._thread.is_alive():
                return self
            if self._task is not None and not getattr(self._task, "done", lambda: True)():
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
        self._thread = threading.Thread(
            target=self._thread_loop, name="merge-schedule", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, *args: Any, **kwargs: Any) -> "MergeScheduleScheduler":
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

    def shutdown(self, *args: Any, **kwargs: Any) -> "MergeScheduleScheduler":
        return self.stop(*args, **kwargs)

    @property
    def is_running(self) -> bool:
        return bool(self._running)


MergeScheduler = MergeScheduleScheduler
Scheduler = MergeScheduleScheduler
MergeScheduleService = MergeScheduleScheduler
MergeScheduleJob = MergeScheduleScheduler


merge_schedule_scheduler = MergeScheduleScheduler()
merge_scheduler = merge_schedule_scheduler
scheduler = merge_schedule_scheduler
merge_schedule_service = merge_schedule_scheduler


def _singleton_running() -> bool:
    try:
        return bool(merge_schedule_scheduler.is_running)
    except Exception:  # noqa: BLE001
        return False


def get_merge_schedule_scheduler(*args: Any, **kwargs: Any) -> MergeScheduleScheduler:
    if args or kwargs:
        return MergeScheduleScheduler(*args, **kwargs)
    return merge_schedule_scheduler


get_merge_scheduler = get_merge_schedule_scheduler
get_scheduler = get_merge_schedule_scheduler


def start_merge_schedule_scheduler(*args: Any, **kwargs: Any) -> MergeScheduleScheduler:
    s = merge_schedule_scheduler
    try:
        s.start(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass
    return s


start_merge_scheduler = start_merge_schedule_scheduler
start_scheduler = start_merge_schedule_scheduler


def stop_merge_schedule_scheduler(*args: Any, **kwargs: Any) -> None:
    try:
        merge_schedule_scheduler.stop(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass


stop_merge_scheduler = stop_merge_schedule_scheduler
stop_scheduler = stop_merge_schedule_scheduler
