"""Periodic auto time-sync scheduler (opt-in).

On-demand check/set lives in :mod:`app.services.time_sync`
(:func:`~app.services.time_sync.get_time_sync_status` /
:func:`~app.services.time_sync.sync_camera_time`) — that module never
auto-syncs on drift; every clock push requires an explicit call. This module
adds an *opt-in* periodic scheduler that, when enabled, checks each known
ONVIF camera for drift and pushes the NVR clock to drifted cameras.

Nothing here starts automatically; callers must explicitly enable/start it
(e.g. via the ``app.api.time_autosync`` router).
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select

__all__ = [
    "TimeAutosyncScheduler",
    "TimeAutoSyncScheduler",
    "AutoSyncScheduler",
    "Scheduler",
    "time_autosync_scheduler",
    "time_auto_sync_scheduler",
    "autosync_scheduler",
    "scheduler",
    "get_time_autosync_scheduler",
    "get_time_auto_sync_scheduler",
    "get_scheduler",
    "start_time_autosync_scheduler",
    "start_scheduler",
    "stop_time_autosync_scheduler",
    "stop_scheduler",
    "run_time_autosync_now",
    "trigger_time_autosync",
    "run_scheduled_sync",
    "get_config",
    "get_status",
    "update_config",
    "set_config",
    "configure",
]

DEFAULT_INTERVAL_SECONDS = 3600
MIN_INTERVAL_SECONDS = 60
DEFAULT_TOLERANCE_SECONDS = 5.0
DEFAULT_TIMEOUT = 10.0

SYNC_COMPLETED_EVENT = "time_autosync_completed"
CAMERA_RESYNCED_EVENT = "time_camera_resynced"


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


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_tolerance(value: Any, default: float = DEFAULT_TOLERANCE_SECONDS) -> float:
    try:
        tol = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, tol)


# ---------------------------------------------------------------------------
# In-memory configuration + state (opt-in; disabled by default)
# ---------------------------------------------------------------------------

_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "interval_seconds": DEFAULT_INTERVAL_SECONDS,
    "tolerance_seconds": DEFAULT_TOLERANCE_SECONDS,
    "timeout": DEFAULT_TIMEOUT,
    "auto_sync": True,
}

_STATE: Dict[str, Any] = {
    "last_run_at": None,
    "last_result": None,
    "last_synced": [],
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
        for key in ("enabled", "active", "is_enabled", "on"):
            if key in kwargs and kwargs[key] is not None:
                _CONFIG["enabled"] = bool(kwargs[key])
                break
        for key in ("interval_seconds", "interval", "seconds", "cadence", "period"):
            if key in kwargs and kwargs[key] is not None:
                _CONFIG["interval_seconds"] = _coerce_interval(kwargs[key])
                break
        for key in ("tolerance_seconds", "tolerance", "tolerance_secs", "drift_tolerance"):
            if key in kwargs and kwargs[key] is not None:
                _CONFIG["tolerance_seconds"] = _coerce_tolerance(kwargs[key])
                break
        for key in ("timeout", "timeouts", "network_timeout", "onvif_timeout"):
            if key in kwargs and kwargs[key] is not None:
                _CONFIG["timeout"] = _coerce_float(kwargs[key], DEFAULT_TIMEOUT)
                break
        for key in ("auto_sync", "autosync", "auto", "sync_enabled", "push_on_drift"):
            if key in kwargs and kwargs[key] is not None:
                _CONFIG["auto_sync"] = bool(kwargs[key])
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
            "last_synced_count": len(_STATE.get("last_synced") or []),
            "run_count": _STATE.get("run_count", 0),
            "last_error": _STATE.get("last_error"),
        }
    return {
        "enabled": bool(cfg.get("enabled")),
        "running": bool(_singleton_running()),
        "interval_seconds": cfg.get("interval_seconds"),
        "tolerance_seconds": cfg.get("tolerance_seconds"),
        "timeout": cfg.get("timeout"),
        "auto_sync": cfg.get("auto_sync"),
        **state,
    }


status = get_status


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

def _normalize_target(value: Any, defaults: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Normalize one camera target to a dict. Returns None when unusable."""
    defaults = defaults or {}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Accept "ip", "ip:port", or "scheme://ip[:port]".
        scheme = str(defaults.get("scheme") or "http")
        port = defaults.get("port") or 80
        host = text
        if "://" in text:
            scheme_part, _, rest = text.partition("://")
            if scheme_part.strip():
                scheme = scheme_part.strip().lower()
            host = rest
        host = host.strip().rstrip("/")
        if ":" in host and not host.endswith("]"):
            # Split trailing :port (handles plain host:port).
            maybe_host, _, maybe_port = host.rpartition(":")
            try:
                port = int(maybe_port)
                host = maybe_host
            except (TypeError, ValueError):
                pass
        host = host.strip("[]").strip()
        if not host:
            return None
        out = {
            "ip": host,
            "port": port,
            "scheme": scheme,
            "username": defaults.get("username") or "",
            "password": defaults.get("password") or "",
        }
        if defaults.get("camera_id") is not None:
            out["camera_id"] = defaults.get("camera_id")
        if defaults.get("name") is not None:
            out["name"] = defaults.get("name")
        return out
    if isinstance(value, dict):
        ip = str(
            value.get("ip")
            or value.get("host")
            or value.get("onvif_host")
            or value.get("address")
            or value.get("ip_address")
            or ""
        ).strip()
        if not ip:
            return None
        try:
            port = int(value.get("port", value.get("onvif_port", defaults.get("port", 80))) or 80)
        except (TypeError, ValueError):
            port = 80
        scheme = str(
            value.get("scheme", value.get("control_scheme", defaults.get("scheme", "http"))) or "http"
        ).strip().lower() or "http"
        return {
            "ip": ip,
            "port": port,
            "scheme": scheme,
            "username": value.get("username", value.get("user", defaults.get("username") or "")) or "",
            "password": value.get("password", value.get("pass_", defaults.get("password") or "")) or "",
            "camera_id": value.get("camera_id", value.get("id", defaults.get("camera_id"))),
            "name": value.get("name", defaults.get("name")),
        }
    # ORM-like object.
    ip = str(
        getattr(value, "onvif_host", None)
        or getattr(value, "ip_address", None)
        or getattr(value, "ip", None)
        or getattr(value, "host", None)
        or ""
    ).strip()
    if not ip:
        return None
    try:
        port = int(getattr(value, "onvif_port", None) or defaults.get("port", 80) or 80)
    except (TypeError, ValueError):
        port = 80
    scheme = str(getattr(value, "control_scheme", None) or defaults.get("scheme", "http") or "http").strip().lower() or "http"
    return {
        "ip": ip,
        "port": port,
        "scheme": scheme,
        "username": getattr(value, "username", None) or defaults.get("username") or "",
        "password": getattr(value, "password", None) or defaults.get("password") or "",
        "camera_id": getattr(value, "id", None),
        "name": getattr(value, "name", None),
    }


def _normalize_targets(value: Any, defaults: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: List[Dict[str, Any]] = []
    for item in items:
        norm = _normalize_target(item, defaults)
        if norm is not None:
            out.append(norm)
    return out


async def list_target_cameras(explicit: Any = None, db: Any = None) -> List[Dict[str, Any]]:
    """Resolve sync targets: explicit list first, else enabled DB cameras.

    Never raises; returns an empty list when nothing is resolvable.
    """
    if explicit:
        targets = _normalize_targets(explicit)
        if targets:
            return targets
    close = False
    try:
        from app.core.database import SessionLocal, init_db
        from app.models.entities import Camera

        if db is None:
            try:
                await init_db()
            except Exception:  # noqa: BLE001
                pass
            try:
                db = SessionLocal()
            except Exception:  # noqa: BLE001
                return []
            close = True
        try:
            result = await db.execute(select(Camera).order_by(Camera.id))
            rows = list(result.scalars().all())
        except Exception:  # noqa: BLE001
            return []
        out: List[Dict[str, Any]] = []
        for row in rows:
            try:
                if getattr(row, "enabled", True) is False:
                    continue
            except Exception:  # noqa: BLE001
                pass
            norm = _normalize_target(row)
            if norm is not None:
                out.append(norm)
        return out
    except Exception:  # noqa: BLE001
        return []
    finally:
        if close:
            try:
                await db.close()
            except Exception:  # noqa: BLE001
                pass


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


async def run_scheduled_sync(**kwargs: Any) -> Dict[str, Any]:
    """Run one auto-sync pass: check drift, push NVR clock where drifted.

    Keyword overrides (all optional): ``tolerance_seconds``/``tolerance``,
    ``timeout``, ``auto_sync`` (False / ``dry_run`` / ``check_only`` means
    check-only, no push), ``targets``/``ips``/``cameras`` (explicit target
    list), ``username``/``password`` (fallback credentials), ``db`` (optional
    async session for camera enumeration).

    Returns a summary dict; never raises.
    """
    from app.services import time_sync as _ts

    cfg = get_config()
    tolerance = _coerce_tolerance(
        kwargs.get("tolerance_seconds", kwargs.get("tolerance", cfg.get("tolerance_seconds"))),
        DEFAULT_TOLERANCE_SECONDS,
    )
    timeout = _coerce_float(
        kwargs.get("timeout", cfg.get("timeout", DEFAULT_TIMEOUT)), DEFAULT_TIMEOUT
    )
    auto_sync = kwargs.get("auto_sync", kwargs.get("autosync", cfg.get("auto_sync", True)))
    if kwargs.get("dry_run") is True or kwargs.get("check_only") is True:
        auto_sync = False
    auto_sync = bool(auto_sync)

    explicit = (
        kwargs.get("targets", kwargs.get("ips", kwargs.get("cameras", kwargs.get("hosts", None))))
    )
    defaults = {
        "port": kwargs.get("port", kwargs.get("onvif_port", 80)),
        "scheme": kwargs.get("scheme", kwargs.get("control_scheme", "http")),
        "username": kwargs.get("username", kwargs.get("user", "")),
        "password": kwargs.get("password", kwargs.get("pass_", kwargs.get("pass", ""))),
    }
    if isinstance(explicit, (str, dict)):
        explicit = [explicit]
    if explicit and defaults.get("username") is not None:
        # Attach fallback creds to dict targets missing them.
        enriched: List[Any] = []
        for item in explicit:  # type: ignore[union-attr]
            if isinstance(item, dict) and defaults.get("username"):
                item = {**item}
                item.setdefault("username", defaults.get("username"))
                item.setdefault("password", defaults.get("password"))
            enriched.append(item)
        explicit = enriched

    try:
        targets = await list_target_cameras(explicit, db=kwargs.get("db"))
    except Exception:  # noqa: BLE001
        targets = []

    checked: List[Dict[str, Any]] = []
    synced: List[Dict[str, Any]] = []
    skipped: List[Any] = []
    errors: List[str] = []

    for target in targets:
        ip = target.get("ip")
        entry: Dict[str, Any] = {
            "ip": ip,
            "camera_id": target.get("camera_id"),
            "name": target.get("name"),
            "port": target.get("port"),
            "scheme": target.get("scheme"),
        }
        try:
            check = await _ts.get_time_sync_status(
                ip=ip,
                port=int(target.get("port") or 80),
                scheme=str(target.get("scheme") or "http"),
                tolerance_seconds=tolerance,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            entry.update({"ok": False, "error": str(exc), "status": "error", "synced": False})
            errors.append(f"{ip}:{exc}")
            checked.append(entry)
            continue
        entry.update(
            {
                "drift_seconds": check.get("drift_seconds"),
                "abs_drift_seconds": check.get("abs_drift_seconds"),
                "in_sync": check.get("in_sync"),
                "status": check.get("status"),
                "camera_time": check.get("camera_time"),
            }
        )
        if not check.get("ok"):
            entry.update({"ok": False, "error": check.get("error"), "synced": False})
            errors.append(f"{ip}:{check.get('error') or 'check failed'}")
            checked.append(entry)
            continue
        entry["ok"] = True
        if check.get("in_sync") is not False:
            # In sync (or unknown without a camera reading): nothing to push.
            entry["synced"] = False
            entry["action"] = "none"
            if check.get("in_sync") is None:
                skipped.append(ip)
            checked.append(entry)
            continue
        # Drifted beyond tolerance.
        if not auto_sync:
            entry["synced"] = False
            entry["action"] = "drifted-check-only"
            checked.append(entry)
            continue
        username = target.get("username") or ""
        password = target.get("password") or ""
        if not (username and password):
            entry["synced"] = False
            entry["action"] = "drifted-no-credentials"
            skipped.append(ip)
            checked.append(entry)
            continue
        try:
            pushed = await _ts.sync_camera_time(
                ip=ip,
                username=username,
                password=password,
                port=int(target.get("port") or 80),
                scheme=str(target.get("scheme") or "http"),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            entry.update({"synced": False, "action": "sync-error", "error": str(exc)})
            errors.append(f"{ip}:{exc}")
            checked.append(entry)
            continue
        if pushed.get("ok"):
            entry.update(
                {
                    "synced": True,
                    "action": "synced",
                    "synced_utc": pushed.get("synced_utc"),
                    "post_drift_seconds": pushed.get("drift_seconds"),
                }
            )
            synced.append(entry)
            _fire_event(CAMERA_RESYNCED_EVENT, dict(entry))
        else:
            entry.update(
                {"synced": False, "action": "sync-failed", "error": pushed.get("error")}
            )
            errors.append(f"{ip}:{pushed.get('error') or 'sync failed'}")
        checked.append(entry)

    with _lock:
        _STATE["last_run_at"] = _now_iso()
        _STATE["last_result"] = [dict(c) for c in checked]
        _STATE["last_synced"] = [dict(c) for c in synced]
        _STATE["last_counts"] = {
            "total": len(checked),
            "checked": len(checked),
            "synced": len(synced),
            "drifted": sum(1 for c in checked if c.get("in_sync") is False),
            "skipped": len(skipped),
            "errors": len(errors),
            "check_only": (not auto_sync),
        }
        _STATE["run_count"] = int(_STATE.get("run_count") or 0) + 1
        _STATE["last_error"] = "; ".join(errors) if errors else None

    summary: Dict[str, Any] = {
        "ok": True,
        "success": True,
        "total": len(checked),
        "count": len(checked),
        "checked": len(checked),
        "synced_count": len(synced),
        "synced": [dict(c) for c in synced],
        "results": [dict(c) for c in checked],
        "checked_cameras": [dict(c) for c in checked],
        "skipped": list(skipped),
        "skipped_count": len(skipped),
        "errors": errors,
        "tolerance_seconds": tolerance,
        "check_only": (not auto_sync),
        "auto_sync": auto_sync,
        "at": _STATE["last_run_at"],
    }
    _fire_event(
        SYNC_COMPLETED_EVENT,
        {
            "total": len(checked),
            "synced": len(synced),
            "errors": len(errors),
            "at": _STATE["last_run_at"],
        },
    )
    return summary


# Aliases for the scan entrypoint.
run_scheduled_autosync = run_scheduled_sync
run_autosync = run_scheduled_sync
scheduled_sync = run_scheduled_sync
sync_now = run_scheduled_sync
check_and_sync = run_scheduled_sync


def run_time_autosync_now(**kwargs: Any) -> Dict[str, Any]:
    """Synchronous wrapper (for scheduler threads / tests without a loop)."""
    try:
        return asyncio.run(run_scheduled_sync(**kwargs))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(run_scheduled_sync(**kwargs))
        finally:
            loop.close()


trigger_time_autosync = run_time_autosync_now
trigger_scheduled_sync = run_time_autosync_now
run_scheduled_sync_now = run_time_autosync_now
run_now_sync = run_time_autosync_now


# ---------------------------------------------------------------------------
# Periodic scheduler (mirrors RetentionScheduler / PTZScheduler conventions)
# ---------------------------------------------------------------------------

class TimeAutosyncScheduler:
    """Opt-in periodic scheduler for camera clock auto-sync."""

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
        return run_time_autosync_now(**kwargs)

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

    def start(self, *args: Any, **kwargs: Any) -> "TimeAutosyncScheduler":
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
            target=self._thread_loop, name="time-autosync", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, *args: Any, **kwargs: Any) -> "TimeAutosyncScheduler":
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

    def shutdown(self, *args: Any, **kwargs: Any) -> "TimeAutosyncScheduler":
        return self.stop(*args, **kwargs)

    @property
    def is_running(self) -> bool:
        return bool(self._running)


TimeAutoSyncScheduler = TimeAutosyncScheduler
AutoSyncScheduler = TimeAutosyncScheduler
TimeSyncAutosyncScheduler = TimeAutosyncScheduler
Scheduler = TimeAutosyncScheduler
TimeAutosyncService = TimeAutosyncScheduler
TimeAutosyncJob = TimeAutosyncScheduler


time_autosync_scheduler = TimeAutosyncScheduler()
time_auto_sync_scheduler = time_autosync_scheduler
autosync_scheduler = time_autosync_scheduler
scheduler = time_autosync_scheduler
time_autosync_service = time_autosync_scheduler


def _singleton_running() -> bool:
    try:
        return bool(time_autosync_scheduler.is_running)
    except Exception:  # noqa: BLE001
        return False


def get_time_autosync_scheduler(*args: Any, **kwargs: Any) -> TimeAutosyncScheduler:
    if args or kwargs:
        return TimeAutosyncScheduler(*args, **kwargs)
    return time_autosync_scheduler


get_time_auto_sync_scheduler = get_time_autosync_scheduler
get_autosync_scheduler = get_time_autosync_scheduler
get_scheduler = get_time_autosync_scheduler


def start_time_autosync_scheduler(*args: Any, **kwargs: Any) -> TimeAutosyncScheduler:
    s = time_autosync_scheduler
    try:
        s.start(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass
    return s


start_time_auto_sync_scheduler = start_time_autosync_scheduler
start_autosync_scheduler = start_time_autosync_scheduler
start_scheduler = start_time_autosync_scheduler


def stop_time_autosync_scheduler(*args: Any, **kwargs: Any) -> None:
    try:
        time_autosync_scheduler.stop(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass


stop_time_auto_sync_scheduler = stop_time_autosync_scheduler
stop_autosync_scheduler = stop_time_autosync_scheduler
stop_scheduler = stop_time_autosync_scheduler
