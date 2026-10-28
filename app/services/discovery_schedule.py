"""Periodic discovery scheduler (opt-in).

Discovery stays manual-only by default: ``network_discovery`` subnet scans
and ``ws_discovery`` WS-Probe scans run on demand via their API endpoints.
This module adds an *opt-in* periodic scheduler that, when enabled, runs
both scans on a cadence and emits a ``discovery_new_device`` event (via
``app.services.event_bus``) for devices not seen before.

Nothing here starts automatically; callers must explicitly enable/start it
(e.g. via the ``app.api.discovery_schedule`` router).
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "DiscoveryScheduleScheduler",
    "DiscoveryScheduler",
    "Scheduler",
    "discovery_schedule_scheduler",
    "discovery_scheduler",
    "scheduler",
    "get_discovery_schedule_scheduler",
    "get_discovery_scheduler",
    "get_scheduler",
    "start_discovery_schedule_scheduler",
    "start_discovery_scheduler",
    "start_scheduler",
    "stop_discovery_schedule_scheduler",
    "stop_discovery_scheduler",
    "stop_scheduler",
    "run_discovery_scan_now",
    "trigger_discovery_scan",
    "run_scheduled_discovery",
    "get_config",
    "get_status",
    "update_config",
    "set_config",
    "configure",
    "reset_known",
    "clear_known",
]

DEFAULT_INTERVAL_SECONDS = 3600
MIN_INTERVAL_SECONDS = 60

NEW_DEVICE_EVENT = "discovery_new_device"
SCAN_COMPLETED_EVENT = "discovery_scan_completed"


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


# ---------------------------------------------------------------------------
# In-memory configuration + state (opt-in; disabled by default)
# ---------------------------------------------------------------------------

_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "interval_seconds": DEFAULT_INTERVAL_SECONDS,
    "subnets": None,
    "network_timeout": 0.5,
    "ws_timeout": 4.0,
    "include_network": True,
    "include_ws": True,
    "notify_new": True,
}

_STATE: Dict[str, Any] = {
    "last_run_at": None,
    "last_result": None,
    "last_new_devices": [],
    "last_counts": {},
    "run_count": 0,
    "last_error": None,
}

_known_keys: set[str] = set()
_lock = threading.Lock()


def get_config() -> Dict[str, Any]:
    with _lock:
        cfg = dict(_CONFIG)
    # Return a copy with a list copy for subnets.
    subnets = cfg.get("subnets")
    if isinstance(subnets, list):
        cfg["subnets"] = list(subnets)
    return cfg


def _normalize_subnets(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        parts = [p.strip() for p in text.replace(",", " ").split() if p.strip()]
        return parts or None
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return parts or None
    return None


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
        for key in ("subnets", "subnet", "subnet_list", "subnets_to_scan"):
            if key in kwargs:
                _CONFIG["subnets"] = _normalize_subnets(kwargs[key])
                break
        for key in ("network_timeout", "timeout", "scan_timeout"):
            if key in kwargs and kwargs[key] is not None:
                _CONFIG["network_timeout"] = _coerce_float(kwargs[key], 0.5)
                break
        if kwargs.get("ws_timeout") is not None:
            _CONFIG["ws_timeout"] = _coerce_float(kwargs["ws_timeout"], 4.0)
        for key in ("include_network", "network", "scan_network"):
            if key in kwargs and kwargs[key] is not None:
                _CONFIG["include_network"] = bool(kwargs[key])
                break
        for key in ("include_ws", "ws", "scan_ws", "include_ws_discovery"):
            if key in kwargs and kwargs[key] is not None:
                _CONFIG["include_ws"] = bool(kwargs[key])
                break
        for key in ("notify_new", "notify", "notifications"):
            if key in kwargs and kwargs[key] is not None:
                _CONFIG["notify_new"] = bool(kwargs[key])
                break
    return get_config()


set_config = update_config
configure = update_config
set_schedule = update_config
update_schedule = update_config


def reset_known() -> Dict[str, Any]:
    with _lock:
        _known_keys.clear()
        _STATE["last_new_devices"] = []
    return {"reset": True, "known_count": 0}


clear_known = reset_known
clear_discovered = reset_known
reset_discovered = reset_known


def get_status() -> Dict[str, Any]:
    cfg = get_config()
    with _lock:
        state = {
            "last_run_at": _STATE.get("last_run_at"),
            "last_counts": dict(_STATE.get("last_counts") or {}),
            "last_new_count": len(_STATE.get("last_new_devices") or []),
            "run_count": _STATE.get("run_count", 0),
            "last_error": _STATE.get("last_error"),
            "known_count": len(_known_keys),
        }
    return {
        "enabled": bool(cfg.get("enabled")),
        "running": bool(_singleton_running()),
        "interval_seconds": cfg.get("interval_seconds"),
        "subnets": cfg.get("subnets"),
        "network_timeout": cfg.get("network_timeout"),
        "ws_timeout": cfg.get("ws_timeout"),
        "include_network": cfg.get("include_network"),
        "include_ws": cfg.get("include_ws"),
        "notify_new": cfg.get("notify_new"),
        **state,
    }


status = get_status


# ---------------------------------------------------------------------------
# Device normalization / new-device detection
# ---------------------------------------------------------------------------

def _device_key(device: Dict[str, Any]) -> str:
    for k in ("key", "id", "endpoint", "xaddr", "stream_url", "source"):
        v = device.get(k)
        if v:
            return f"{k}:{v}".lower()
    xaddrs = device.get("xaddresses") or device.get("xaddrs") or []
    if isinstance(xaddrs, list) and xaddrs:
        first = str(xaddrs[0]).strip().rstrip("/")
        if first:
            return f"xaddr:{first}".lower()
    for k in ("ip", "ip_address", "host", "address"):
        v = device.get(k)
        if v:
            return f"ip:{str(v).strip()}".lower()
    return ""


def _normalize_ws_device(device: Any) -> Dict[str, Any]:
    if hasattr(device, "to_dict"):
        try:
            data = device.to_dict()
            if isinstance(data, dict):
                device = data
        except Exception:  # noqa: BLE001
            pass
    if isinstance(device, dict):
        out = dict(device)
    else:
        out = {
            "types": list(getattr(device, "types", []) or []),
            "xaddresses": list(getattr(device, "xaddresses", []) or []),
            "model": str(getattr(device, "model", "") or ""),
            "mfr": str(getattr(device, "mfr", "") or ""),
            "address": str(getattr(device, "address", "") or ""),
            "scopes": list(getattr(device, "scopes", []) or []),
            "endpoint": str(getattr(device, "endpoint", "") or ""),
            "metadata_version": str(getattr(device, "metadata_version", "") or ""),
        }
    out.setdefault("kind", "ws-discovery")
    out.setdefault("discovery", "ws-discovery")
    addr = out.get("address") or ""
    if addr and not out.get("ip"):
        out["ip"] = addr
        out["ip_address"] = addr
        out["host"] = addr
    return out


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
    # No running loop: drive the coroutine in a fresh loop so scheduled
    # (thread-based) scans still notify subscribers.
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


async def run_scheduled_discovery(**kwargs: Any) -> Dict[str, Any]:
    """Run one scheduled discovery pass (network + WS) and notify on new.

    Returns a summary dict with ``devices``, ``new_devices`` and ``counts``.
    Only devices whose key was not previously seen are reported as new and
    published via the ``discovery_new_device`` event (when enabled).
    """
    cfg = get_config()
    # Per-run overrides.
    subnets = kwargs.get("subnets", kwargs.get("subnet", cfg.get("subnets")))
    if isinstance(subnets, str):
        subnets = [subnets]
    network_timeout = _coerce_float(
        kwargs.get("network_timeout", kwargs.get("timeout", cfg.get("network_timeout"))), 0.5
    )
    ws_timeout = _coerce_float(kwargs.get("ws_timeout", cfg.get("ws_timeout")), 4.0)
    include_network = bool(kwargs.get("include_network", cfg.get("include_network", True)))
    include_ws = bool(kwargs.get("include_ws", cfg.get("include_ws", True)))
    notify_new = bool(kwargs.get("notify_new", cfg.get("notify_new", True)))

    devices: List[Dict[str, Any]] = []
    errors: List[str] = []

    if include_network:
        try:
            from app.services import network_discovery as nd

            if subnets:
                seen_ips: set[str] = set()
                for sn in subnets:
                    try:
                        found = await nd.scan_subnet(str(sn), timeout=network_timeout)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"network:{sn}:{exc}")
                        continue
                    for d in found or []:
                        if not isinstance(d, dict):
                            continue
                        ip = d.get("ip")
                        if ip and ip in seen_ips:
                            continue
                        if ip:
                            seen_ips.add(ip)
                        devices.append(dict(d))
            else:
                try:
                    found = await nd.discover_network_cameras(timeout=network_timeout)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"network:{exc}")
                    found = []
                for d in found or []:
                    if isinstance(d, dict):
                        devices.append(dict(d))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"network:{exc}")

    if include_ws:
        try:
            from app.services import ws_discovery as wsd

            try:
                found = await wsd.discover(timeout=ws_timeout)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"ws:{exc}")
                found = []
            for d in found or []:
                devices.append(_normalize_ws_device(d))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ws:{exc}")

    # Tag discovery source on each device.
    for d in devices:
        d.setdefault("scheduled", True)

    # New-device detection against the known set.
    new_devices: List[Dict[str, Any]] = []
    with _lock:
        for d in devices:
            key = _device_key(d)
            if not key:
                continue
            if key not in _known_keys:
                _known_keys.add(key)
                flagged = dict(d)
                flagged["new"] = True
                new_devices.append(flagged)
            else:
                d["new"] = False
        for d in new_devices:
            # Mark in main list as well.
            pass
        _STATE["last_run_at"] = _now_iso()
        _STATE["last_result"] = [dict(d) for d in devices]
        _STATE["last_new_devices"] = [dict(d) for d in new_devices]
        _STATE["last_counts"] = {
            "total": len(devices),
            "new": len(new_devices),
            "known_total": len(_known_keys),
            "errors": len(errors),
        }
        _STATE["run_count"] = int(_STATE.get("run_count") or 0) + 1
        _STATE["last_error"] = "; ".join(errors) if errors else None

    if notify_new and new_devices:
        for d in new_devices:
            _fire_event(NEW_DEVICE_EVENT, dict(d))
        _fire_event(SCAN_COMPLETED_EVENT, {
            "total": len(devices),
            "new": len(new_devices),
            "new_devices": [dict(d) for d in new_devices],
            "at": _STATE["last_run_at"],
        })
    else:
        _fire_event(SCAN_COMPLETED_EVENT, {
            "total": len(devices),
            "new": len(new_devices),
            "at": _STATE["last_run_at"],
        })

    return {
        "success": True,
        "ok": True,
        "total": len(devices),
        "count": len(devices),
        "new_count": len(new_devices),
        "new": len(new_devices),
        "devices": [dict(d) for d in devices],
        "new_devices": [dict(d) for d in new_devices],
        "counts": dict(_STATE["last_counts"]),
        "errors": errors,
        "at": _STATE["last_run_at"],
    }


# Aliases for the scan entrypoint.
run_discovery_scan = run_scheduled_discovery
run_scan = run_scheduled_discovery
scheduled_scan = run_scheduled_discovery
scan_now = run_scheduled_discovery


def run_discovery_scan_now(**kwargs: Any) -> Dict[str, Any]:
    """Synchronous wrapper (for scheduler threads / tests without a loop)."""
    try:
        return asyncio.run(run_scheduled_discovery(**kwargs))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(run_scheduled_discovery(**kwargs))
        finally:
            loop.close()


trigger_discovery_scan = run_discovery_scan_now
trigger_scheduled_discovery = run_discovery_scan_now
run_scheduled_discovery_now = run_discovery_scan_now
run_now_sync = run_discovery_scan_now


# ---------------------------------------------------------------------------
# Periodic scheduler (mirrors RetentionScheduler / PTZScheduler conventions)
# ---------------------------------------------------------------------------

class DiscoveryScheduleScheduler:
    """Opt-in periodic scheduler for discovery scans."""

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
        return run_discovery_scan_now(**kwargs)

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

    def start(self, *args: Any, **kwargs: Any) -> "DiscoveryScheduleScheduler":
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
            target=self._thread_loop, name="discovery-schedule", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, *args: Any, **kwargs: Any) -> "DiscoveryScheduleScheduler":
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

    def shutdown(self, *args: Any, **kwargs: Any) -> "DiscoveryScheduleScheduler":
        return self.stop(*args, **kwargs)

    @property
    def is_running(self) -> bool:
        return bool(self._running)


DiscoveryScheduler = DiscoveryScheduleScheduler
Scheduler = DiscoveryScheduleScheduler
DiscoveryScheduleService = DiscoveryScheduleScheduler
DiscoveryScheduleJob = DiscoveryScheduleScheduler


discovery_schedule_scheduler = DiscoveryScheduleScheduler()
discovery_scheduler = discovery_schedule_scheduler
scheduler = discovery_schedule_scheduler
discovery_schedule_service = discovery_schedule_scheduler


def _singleton_running() -> bool:
    try:
        return bool(discovery_schedule_scheduler.is_running)
    except Exception:  # noqa: BLE001
        return False


def get_discovery_schedule_scheduler(*args: Any, **kwargs: Any) -> DiscoveryScheduleScheduler:
    if args or kwargs:
        return DiscoveryScheduleScheduler(*args, **kwargs)
    return discovery_schedule_scheduler


get_discovery_scheduler = get_discovery_schedule_scheduler
get_scheduler = get_discovery_schedule_scheduler


def start_discovery_schedule_scheduler(*args: Any, **kwargs: Any) -> DiscoveryScheduleScheduler:
    s = discovery_schedule_scheduler
    try:
        s.start(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass
    return s


start_discovery_scheduler = start_discovery_schedule_scheduler
start_scheduler = start_discovery_schedule_scheduler


def stop_discovery_schedule_scheduler(*args: Any, **kwargs: Any) -> None:
    try:
        discovery_schedule_scheduler.stop(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass


stop_discovery_scheduler = stop_discovery_schedule_scheduler
stop_scheduler = stop_discovery_schedule_scheduler
