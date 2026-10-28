"""Camera watchdog auto-restart service for VigilCam.

Monitors per-camera health (via :mod:`app.services.camera_health`) and
automatically restarts cameras that go offline or stop producing frames.

The service is intentionally dependency-light: restart is delegated to an
injectable callback so tests and deployments can wire in the real ingestion
restart (e.g. ``StreamCopyIngestor`` / ``RecordingManager``) without coupling
this module to subprocess handling. By default a best-effort restart records
a reconnect in ``camera_health`` and publishes a ``camera_restart`` event.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

# Tunables.
CHECK_INTERVAL_SECONDS = 30.0
OFFLINE_THRESHOLD_SECONDS = 60.0
MAX_RESTARTS = 5
RESTART_COOLDOWN_SECONDS = 60.0
ENABLED_BY_DEFAULT = True

# In-memory watchdog state keyed by camera id.
_STATE: Dict[int, "CameraWatchdogStatus"] = {}


def _now() -> datetime:
    return datetime.utcnow()


def _coerce_camera_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        if hasattr(value, "id") and not isinstance(value, (int, str, float)):
            value = getattr(value, "id")
        return int(value)
    except (TypeError, ValueError):
        s = str(value).strip()
        if not s:
            return None
        import re

        m = re.search(r"(\d+)$", s)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        return None


def _to_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        stamp = float(value)
        if abs(stamp) > 1e12:
            stamp = stamp / 1000.0
        try:
            return datetime.utcfromtimestamp(stamp)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            parsed = datetime.fromisoformat(s)
            if parsed.tzinfo is not None:
                parsed = parsed.replace(tzinfo=None)
            return parsed
        except ValueError:
            try:
                stamp = float(s)
                if abs(stamp) > 1e12:
                    stamp = stamp / 1000.0
                return datetime.utcfromtimestamp(stamp)
            except (ValueError, OSError, OverflowError):
                return None
    return None


def _default_restart(camera_id: int, reason: str = "offline") -> Dict[str, Any]:
    """Best-effort restart: record a reconnect + publish an event."""
    detail: Dict[str, Any] = {"camera_id": camera_id, "reason": reason, "ok": True}
    try:
        from app.services import camera_health as _ch

        _ch.record_reconnect(camera_id)
        detail["health_recorded"] = True
    except Exception:  # noqa: BLE001
        detail["health_recorded"] = False
    # Best-effort event bus notification (fire-and-forget; bus is async).
    try:
        from app.services.event_bus import event_bus as _bus

        if _bus is not None:
            # If there is a running loop, schedule; otherwise skip.
            try:
                import asyncio

                loop = asyncio.get_event_loop()
                if loop is not None and loop.is_running():
                    loop.create_task(
                        _bus.publish(
                            "camera_restart",
                            {"camera_id": camera_id, "reason": reason},
                        )
                    )
                    detail["event_published"] = True
                else:
                    detail["event_published"] = False
            except Exception:  # noqa: BLE001
                detail["event_published"] = False
    except Exception:  # noqa: BLE001
        detail["event_published"] = False
    return detail


@dataclass
class CameraWatchdogStatus:
    """Per-camera watchdog record."""

    camera_id: int
    enabled: bool = True
    online: bool = False
    status: str = "unknown"
    needs_restart: bool = False
    restart_attempts: int = 0
    consecutive_failures: int = 0
    last_check_at: Optional[datetime] = None
    last_restart_at: Optional[datetime] = None
    last_online_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def attempts(self) -> int:
        return self.restart_attempts

    @property
    def restarts(self) -> int:
        return self.restart_attempts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "id": self.camera_id,
            "enabled": self.enabled,
            "online": self.online,
            "status": self.status,
            "state": self.status,
            "healthy": bool(self.online),
            "needs_restart": self.needs_restart,
            "should_restart": self.needs_restart,
            "restart_attempts": self.restart_attempts,
            "attempts": self.restart_attempts,
            "restarts": self.restart_attempts,
            "restart_count": self.restart_attempts,
            "consecutive_failures": self.consecutive_failures,
            "failures": self.consecutive_failures,
            "last_check_at": self.last_check_at.isoformat() if self.last_check_at else None,
            "last_restart_at": self.last_restart_at.isoformat() if self.last_restart_at else None,
            "last_online_at": self.last_online_at.isoformat() if self.last_online_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def dict(self) -> Dict[str, Any]:  # noqa: A003
        return self.to_dict()

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        aliases = {
            "camera": "camera_id",
            "health": "online",
            "restarts": "restart_attempts",
            "restart_count": "restart_attempts",
            "failures": "consecutive_failures",
        }
        if key in aliases:
            return getattr(self, aliases[key])
        return None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = self[key]
            return default if val is None and key not in self.__dict__ else val
        except Exception:  # noqa: BLE001
            return default


class CameraWatchdog:
    """Monitor cameras and auto-restart offline ones."""

    def __init__(
        self,
        *args: Any,
        check_interval_seconds: Any = None,
        check_interval: Any = None,
        interval: Any = None,
        interval_seconds: Any = None,
        offline_threshold_seconds: Any = None,
        offline_threshold: Any = None,
        threshold: Any = None,
        max_restarts: Any = None,
        max_attempts: Any = None,
        restart_cooldown_seconds: Any = None,
        cooldown: Any = None,
        cooldown_seconds: Any = None,
        enabled: Any = None,
        auto_restart: Any = None,
        restart_callback: Optional[Callable[..., Any]] = None,
        restart_handler: Optional[Callable[..., Any]] = None,
        on_restart: Optional[Callable[..., Any]] = None,
        **kwargs: Any,
    ) -> None:
        iv = check_interval_seconds
        if iv is None:
            iv = check_interval
        if iv is None:
            iv = interval
        if iv is None:
            iv = interval_seconds
        if iv is None:
            iv = kwargs.pop("check_interval_secs", None)
        try:
            self.check_interval_seconds = float(iv) if iv is not None else float(CHECK_INTERVAL_SECONDS)
        except (TypeError, ValueError):
            self.check_interval_seconds = float(CHECK_INTERVAL_SECONDS)

        th = offline_threshold_seconds
        if th is None:
            th = offline_threshold
        if th is None:
            th = threshold
        if th is None:
            th = kwargs.pop("offline_timeout", None)
        try:
            self.offline_threshold_seconds = float(th) if th is not None else float(OFFLINE_THRESHOLD_SECONDS)
        except (TypeError, ValueError):
            self.offline_threshold_seconds = float(OFFLINE_THRESHOLD_SECONDS)

        mr = max_restarts
        if mr is None:
            mr = max_attempts
        if mr is None:
            mr = kwargs.pop("max_retries", None)
        try:
            self.max_restarts = int(mr) if mr is not None else int(MAX_RESTARTS)
        except (TypeError, ValueError):
            self.max_restarts = int(MAX_RESTARTS)

        cd = restart_cooldown_seconds
        if cd is None:
            cd = cooldown
        if cd is None:
            cd = cooldown_seconds
        if cd is None:
            cd = kwargs.pop("cooldown_secs", None)
        try:
            self.restart_cooldown_seconds = float(cd) if cd is not None else float(RESTART_COOLDOWN_SECONDS)
        except (TypeError, ValueError):
            self.restart_cooldown_seconds = float(RESTART_COOLDOWN_SECONDS)

        en = enabled
        if en is None:
            en = auto_restart
        if en is None:
            en = kwargs.pop("auto_restart_enabled", None)
        self.enabled = bool(en) if en is not None else bool(ENABLED_BY_DEFAULT)

        cb = restart_callback or restart_handler or on_restart or kwargs.pop("callback", None)
        self.restart_callback: Callable[..., Any] = cb if callable(cb) else _default_restart

        self._lock = threading.Lock()
        self._running = False
        self._stop = False
        self._thread: Optional[threading.Thread] = None

    # ---------- config ----------

    def configure(self, **kwargs: Any) -> Dict[str, Any]:
        if "check_interval_seconds" in kwargs and kwargs["check_interval_seconds"] is not None:
            try:
                self.check_interval_seconds = float(kwargs["check_interval_seconds"])
            except (TypeError, ValueError):
                pass
        for key in ("check_interval", "interval", "interval_seconds"):
            if key in kwargs and kwargs[key] is not None:
                try:
                    self.check_interval_seconds = float(kwargs[key])
                except (TypeError, ValueError):
                    pass
        for key in ("offline_threshold_seconds", "offline_threshold", "threshold", "offline_timeout"):
            if key in kwargs and kwargs[key] is not None:
                try:
                    self.offline_threshold_seconds = float(kwargs[key])
                except (TypeError, ValueError):
                    pass
        for key in ("max_restarts", "max_attempts", "max_retries"):
            if key in kwargs and kwargs[key] is not None:
                try:
                    self.max_restarts = int(kwargs[key])
                except (TypeError, ValueError):
                    pass
        for key in ("restart_cooldown_seconds", "cooldown", "cooldown_seconds"):
            if key in kwargs and kwargs[key] is not None:
                try:
                    self.restart_cooldown_seconds = float(kwargs[key])
                except (TypeError, ValueError):
                    pass
        if "enabled" in kwargs and kwargs["enabled"] is not None:
            self.enabled = bool(kwargs["enabled"])
        if "auto_restart" in kwargs and kwargs["auto_restart"] is not None:
            self.enabled = bool(kwargs["auto_restart"])
        return self.get_config()

    def get_config(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "auto_restart": bool(self.enabled),
            "check_interval_seconds": float(self.check_interval_seconds),
            "check_interval": float(self.check_interval_seconds),
            "interval": float(self.check_interval_seconds),
            "offline_threshold_seconds": float(self.offline_threshold_seconds),
            "offline_threshold": float(self.offline_threshold_seconds),
            "threshold": float(self.offline_threshold_seconds),
            "max_restarts": int(self.max_restarts),
            "max_attempts": int(self.max_restarts),
            "restart_cooldown_seconds": float(self.restart_cooldown_seconds),
            "cooldown": float(self.restart_cooldown_seconds),
            "running": bool(self._running),
            "is_running": bool(self._running),
        }

    def enable(self) -> Dict[str, Any]:
        self.enabled = True
        return self.get_config()

    def disable(self) -> Dict[str, Any]:
        self.enabled = False
        return self.get_config()

    def is_enabled(self) -> bool:
        return bool(self.enabled)

    # ---------- state ----------

    def _ensure(self, camera_id: Any) -> CameraWatchdogStatus:
        cid = _coerce_camera_id(camera_id)
        if cid is None:
            cid = 0
        if cid not in _STATE:
            _STATE[cid] = CameraWatchdogStatus(camera_id=cid, enabled=True, updated_at=_now())
        return _STATE[cid]

    def _health_snapshot(self, camera_id: int) -> Dict[str, Any]:
        try:
            from app.services import camera_health as _ch

            st = _ch.get_health(camera_id, create=False)
            if st is None:
                return {"known": False, "online": False}
            try:
                _ch.evaluate(st)
            except Exception:  # noqa: BLE001
                pass
            online = bool(getattr(st, "online", False))
            last_frame = getattr(st, "last_frame", None)
            age: Optional[float] = None
            if last_frame is not None:
                try:
                    age = (_now() - last_frame).total_seconds()
                except Exception:  # noqa: BLE001
                    age = None
            return {"known": True, "online": online, "last_frame": last_frame, "age_seconds": age}
        except Exception:  # noqa: BLE001
            return {"known": False, "online": False}

    def should_restart(self, camera_id: Any, now: Any = None) -> bool:
        cid = _coerce_camera_id(camera_id)
        if cid is None:
            return False
        snap = self._health_snapshot(cid)
        if snap.get("known") and snap.get("online"):
            return False
        # Unknown cameras with an explicit watchdog entry count as needing attention.
        if not snap.get("known") and cid not in _STATE:
            return False
        st = self._ensure(cid)
        if st.restart_attempts >= self.max_restarts:
            return False
        if st.last_restart_at is not None:
            now_dt = _to_datetime(now) or _now()
            try:
                elapsed = (now_dt - st.last_restart_at).total_seconds()
            except Exception:  # noqa: BLE001
                elapsed = self.restart_cooldown_seconds
            if elapsed < self.restart_cooldown_seconds:
                return False
        # Offline threshold: if we have a recent frame age below threshold, skip.
        age = snap.get("age_seconds")
        if snap.get("online") is False and age is not None and age < self.offline_threshold_seconds:
            # Health already flags offline but frames are recent; still allow restart
            # only if health says offline. Health offline + recent frame is transitional.
            pass
        return True

    def check_camera(
        self,
        camera_id: Any = None,
        now: Any = None,
        auto_restart: Any = None,
        **kwargs: Any,
    ) -> CameraWatchdogStatus:
        if camera_id is None:
            camera_id = kwargs.get("camera_id") or kwargs.get("id")
        cid = _coerce_camera_id(camera_id)
        if cid is None:
            cid = 0
        st = self._ensure(cid)
        now_dt = _to_datetime(now) or _now()
        snap = self._health_snapshot(cid)
        st.last_check_at = now_dt
        st.online = bool(snap.get("online", False))
        if st.online:
            st.status = "online"
            st.consecutive_failures = 0
            st.needs_restart = False
            st.last_online_at = now_dt
        else:
            st.status = "offline"
            st.consecutive_failures = int(st.consecutive_failures or 0) + 1
            st.needs_restart = self.should_restart(cid, now=now_dt)
        st.updated_at = now_dt

        do_restart = auto_restart
        if do_restart is None:
            do_restart = kwargs.get("restart", None)
        if do_restart is None:
            do_restart = True
        if do_restart and st.needs_restart and self.enabled:
            try:
                self.restart_camera(cid, reason=kwargs.get("reason", "offline"))
            except Exception:  # noqa: BLE001
                pass
            st.needs_restart = False
        return st

    def check_all(
        self,
        now: Any = None,
        auto_restart: Any = None,
        **kwargs: Any,
    ) -> List[CameraWatchdogStatus]:
        # Gather candidate camera ids from health + existing watchdog state.
        ids: List[int] = list(_STATE.keys())
        try:
            from app.services import camera_health as _ch

            for st in _ch.list_health():
                try:
                    cid = _coerce_camera_id(getattr(st, "camera_id", None))
                except Exception:  # noqa: BLE001
                    cid = None
                if cid is not None and cid not in ids:
                    ids.append(cid)
        except Exception:  # noqa: BLE001
            pass
        out: List[CameraWatchdogStatus] = []
        for cid in sorted(ids):
            try:
                out.append(self.check_camera(cid, now=now, auto_restart=auto_restart, **kwargs))
            except Exception:  # noqa: BLE001
                continue
        return out

    def restart_camera(
        self,
        camera_id: Any = None,
        reason: Any = None,
        force: bool = False,
        **kwargs: Any,
    ) -> CameraWatchdogStatus:
        if camera_id is None:
            camera_id = kwargs.get("camera_id") or kwargs.get("id")
        cid = _coerce_camera_id(camera_id)
        if cid is None:
            cid = 0
        if reason is None:
            reason = kwargs.get("cause") or kwargs.get("trigger") or "manual"
        st = self._ensure(cid)
        now_dt = _now()
        if not force:
            if st.restart_attempts >= self.max_restarts:
                st.updated_at = now_dt
                return st
            if st.last_restart_at is not None:
                try:
                    elapsed = (now_dt - st.last_restart_at).total_seconds()
                except Exception:  # noqa: BLE001
                    elapsed = self.restart_cooldown_seconds
                if elapsed < self.restart_cooldown_seconds:
                    st.updated_at = now_dt
                    return st
        try:
            result = self.restart_callback(cid, str(reason))
            ok = True
            if isinstance(result, dict):
                ok = bool(result.get("ok", True))
        except Exception:  # noqa: BLE001
            ok = False
        st.restart_attempts = int(st.restart_attempts or 0) + 1
        st.last_restart_at = now_dt
        st.last_check_at = now_dt
        st.updated_at = now_dt
        st.needs_restart = False
        if ok:
            st.consecutive_failures = 0
        return st

    def get_status(self, camera_id: Any, create: bool = False) -> Optional[CameraWatchdogStatus]:
        cid = _coerce_camera_id(camera_id)
        if cid is None:
            return None
        if cid in _STATE:
            return _STATE[cid]
        if create:
            return self._ensure(cid)
        return None

    def list_status(self) -> List[CameraWatchdogStatus]:
        # Refresh via check without auto-restart side effects? Use light refresh.
        for cid in list(_STATE.keys()):
            try:
                snap = self._health_snapshot(cid)
                st = _STATE[cid]
                st.online = bool(snap.get("online", False))
                st.status = "online" if st.online else "offline"
            except Exception:  # noqa: BLE001
                continue
        return [_STATE[k] for k in sorted(_STATE.keys())]

    def get_summary(self) -> Dict[str, Any]:
        states = self.list_status()
        total = len(states)
        online = sum(1 for s in states if s.online)
        offline = total - online
        restarts = sum(int(s.restart_attempts or 0) for s in states)
        return {
            "total": total,
            "cameras": total,
            "online": online,
            "offline": offline,
            "healthy": online,
            "total_restarts": restarts,
            "restart_attempts": restarts,
            "restarts": restarts,
            "enabled": bool(self.enabled),
            "running": bool(self._running),
        }

    def reset(self, camera_id: Any = None, **kwargs: Any) -> Any:
        if camera_id is None:
            camera_id = kwargs.get("camera_id") or kwargs.get("id")
        if camera_id is None:
            for st in _STATE.values():
                st.restart_attempts = 0
                st.consecutive_failures = 0
                st.needs_restart = False
                st.updated_at = _now()
            return self.get_summary()
        st = self._ensure(camera_id)
        st.restart_attempts = 0
        st.consecutive_failures = 0
        st.needs_restart = False
        st.updated_at = _now()
        return st

    def remove(self, camera_id: Any = None, **kwargs: Any) -> bool:
        if camera_id is None:
            camera_id = kwargs.get("camera_id") or kwargs.get("id")
        cid = _coerce_camera_id(camera_id)
        if cid is None:
            return False
        return _STATE.pop(cid, None) is not None

    def clear(self, camera_id: Any = None, **kwargs: Any) -> None:
        if camera_id is None and not kwargs:
            _STATE.clear()
            return
        if camera_id is None:
            camera_id = kwargs.get("camera_id") or kwargs.get("id")
        if camera_id is None:
            _STATE.clear()
            return
        cid = _coerce_camera_id(camera_id)
        if cid is None:
            _STATE.clear()
            return
        _STATE.pop(cid, None)

    # ---------- background loop ----------

    def _loop(self) -> None:
        self._running = True
        self._stop = False
        while not self._stop:
            try:
                if self.enabled:
                    self.check_all()
            except Exception:  # noqa: BLE001
                pass
            deadline = time.time() + max(0.5, float(self.check_interval_seconds))
            while not self._stop and time.time() < deadline:
                time.sleep(min(1.0, max(0.05, deadline - time.time())))
        self._running = False

    def start(self, *args: Any, **kwargs: Any) -> "CameraWatchdog":
        if self._running and self._thread is not None and self._thread.is_alive():
            return self
        if kwargs.get("check_interval_seconds") is not None:
            try:
                self.check_interval_seconds = float(kwargs["check_interval_seconds"])
            except (TypeError, ValueError):
                pass
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="camera-watchdog", daemon=True)
        self._thread.start()
        return self

    def stop(self, *args: Any, **kwargs: Any) -> None:
        self._stop = True
        self._running = False
        if self._thread is not None:
            try:
                self._thread.join(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self._thread = None

    @property
    def is_running(self) -> bool:
        try:
            return bool(self._running and self._thread is not None and self._thread.is_alive())
        except Exception:  # noqa: BLE001
            return bool(self._running)


Watchdog = CameraWatchdog
WatchdogService = CameraWatchdog
CameraWatchdogService = CameraWatchdog
CameraWatchdogMonitor = CameraWatchdog
WatchdogMonitor = CameraWatchdog

watchdog = CameraWatchdog()
camera_watchdog = watchdog
watchdog_service = watchdog
service = watchdog
default_watchdog = watchdog
monitor = watchdog


def get_watchdog(*args: Any, **kwargs: Any) -> CameraWatchdog:
    global watchdog, camera_watchdog
    if args or kwargs:
        return CameraWatchdog(*args, **kwargs)
    return watchdog


def get_status(camera_id: Any, create: bool = False) -> Optional[CameraWatchdogStatus]:
    return watchdog.get_status(camera_id, create=create)


def list_status() -> List[CameraWatchdogStatus]:
    return watchdog.list_status()


def get_summary() -> Dict[str, Any]:
    return watchdog.get_summary()


def get_config() -> Dict[str, Any]:
    return watchdog.get_config()


def configure(**kwargs: Any) -> Dict[str, Any]:
    return watchdog.configure(**kwargs)


def enable() -> Dict[str, Any]:
    return watchdog.enable()


def disable() -> Dict[str, Any]:
    return watchdog.disable()


def is_enabled() -> bool:
    return watchdog.is_enabled()


def check_camera(camera_id: Any = None, **kwargs: Any) -> CameraWatchdogStatus:
    return watchdog.check_camera(camera_id, **kwargs)


def check_all(**kwargs: Any) -> List[CameraWatchdogStatus]:
    return watchdog.check_all(**kwargs)


def check(**kwargs: Any) -> List[CameraWatchdogStatus]:
    # check() with a camera_id behaves like check_camera; otherwise check_all.
    if "camera_id" in kwargs or "id" in kwargs:
        cid = kwargs.pop("camera_id", kwargs.pop("id", None))
        return watchdog.check_camera(cid, **kwargs)  # type: ignore[return-value]
    return watchdog.check_all(**kwargs)


def restart_camera(camera_id: Any = None, **kwargs: Any) -> CameraWatchdogStatus:
    return watchdog.restart_camera(camera_id, **kwargs)


def reset(camera_id: Any = None, **kwargs: Any) -> Any:
    return watchdog.reset(camera_id, **kwargs)


def clear(camera_id: Any = None, **kwargs: Any) -> None:
    return watchdog.clear(camera_id, **kwargs)


def remove(camera_id: Any = None, **kwargs: Any) -> bool:
    return watchdog.remove(camera_id, **kwargs)


def start_watchdog(*args: Any, **kwargs: Any) -> CameraWatchdog:
    watchdog.start(*args, **kwargs)
    return watchdog


def stop_watchdog(*args: Any, **kwargs: Any) -> None:
    watchdog.stop(*args, **kwargs)


def start(*args: Any, **kwargs: Any) -> CameraWatchdog:
    return start_watchdog(*args, **kwargs)


def stop(*args: Any, **kwargs: Any) -> None:
    return stop_watchdog(*args, **kwargs)


# Additional aliases.
restart = restart_camera
trigger_restart = restart_camera
force_restart = lambda camera_id=None, **kw: watchdog.restart_camera(camera_id, force=True, **kw)
status_for = get_status
status = get_status
summary = get_summary
config = get_config

__all__ = [
    "CameraWatchdogStatus",
    "CameraWatchdog",
    "Watchdog",
    "WatchdogService",
    "watchdog",
    "camera_watchdog",
    "get_watchdog",
    "get_status",
    "list_status",
    "get_summary",
    "configure",
    "enable",
    "disable",
    "check_camera",
    "check_all",
    "restart_camera",
    "reset",
    "clear",
    "start_watchdog",
    "stop_watchdog",
]
