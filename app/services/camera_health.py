"""Per-camera health monitoring and reconnect backoff for VigilCam.

This module keeps an in-memory view of camera health (online/offline, last
frame timestamp, reconnect attempts and backoff) keyed by camera id. It is
intended to be updated by the motion worker (mark frame received) and camera
CRUD flows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

# Tunables.
ONLINE_TIMEOUT_SECONDS = 30.0
BASE_BACKOFF_SECONDS = 2.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF_SECONDS = 60.0

# In-memory health state keyed by camera id.
_HEALTH: Dict[int, "CameraHealth"] = {}


def _now() -> datetime:
    return datetime.utcnow()


def _coerce_camera_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        # Support objects exposing .id
        if hasattr(value, "id") and not isinstance(value, (int, str, float)):
            value = getattr(value, "id")
        return int(value)
    except (TypeError, ValueError):
        # Maybe "camera:3"
        s = str(value).strip()
        if not s:
            return None
        # Extract trailing integer if possible.
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
            # Handle trailing Z.
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            from datetime import datetime as _dt

            parsed = _dt.fromisoformat(s)
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


@dataclass
class CameraHealth:
    """Per-camera health record."""

    camera_id: int
    online: bool = False
    status: str = "offline"
    healthy: bool = False
    last_frame: Optional[datetime] = None
    last_frame_at: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    last_online_at: Optional[datetime] = None
    last_offline_at: Optional[datetime] = None
    last_reconnect_at: Optional[datetime] = None
    reconnect_attempts: int = 0
    backoff_seconds: float = 0.0
    updated_at: Optional[datetime] = None
    timeout_seconds: float = ONLINE_TIMEOUT_SECONDS

    # Compatibility aliases.
    @property
    def last_frame_ts(self) -> Optional[float]:
        if self.last_frame is None:
            return None
        try:
            import calendar

            return calendar.timegm(self.last_frame.timetuple()) + self.last_frame.microsecond / 1e6
        except Exception:  # noqa: BLE001
            return None

    @property
    def attempts(self) -> int:
        return self.reconnect_attempts

    @attempts.setter
    def attempts(self, value: Any) -> None:
        try:
            self.reconnect_attempts = int(value)
        except (TypeError, ValueError):
            pass

    @property
    def backoff(self) -> float:
        return self.backoff_seconds

    @backoff.setter
    def backoff(self, value: Any) -> None:
        try:
            self.backoff_seconds = float(value)
        except (TypeError, ValueError):
            pass

    @property
    def is_online(self) -> bool:
        return bool(self.online)

    @property
    def is_offline(self) -> bool:
        return not bool(self.online)

    @property
    def state(self) -> str:
        return self.status

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        # Common aliases.
        aliases = {
            "id": "camera_id",
            "camera": "camera_id",
            "online_status": "status",
            "health": "healthy",
            "last_frame_time": "last_frame",
            "reconnects": "reconnect_attempts",
            "backoff": "backoff_seconds",
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "id": self.camera_id,
            "online": self.online,
            "status": self.status,
            "healthy": self.healthy,
            "last_frame": self.last_frame.isoformat() if self.last_frame else None,
            "last_frame_at": self.last_frame_at.isoformat() if self.last_frame_at else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "last_online_at": self.last_online_at.isoformat() if self.last_online_at else None,
            "last_offline_at": self.last_offline_at.isoformat() if self.last_offline_at else None,
            "last_reconnect_at": self.last_reconnect_at.isoformat() if self.last_reconnect_at else None,
            "reconnect_attempts": self.reconnect_attempts,
            "attempts": self.reconnect_attempts,
            "backoff_seconds": self.backoff_seconds,
            "backoff": self.backoff_seconds,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "timeout_seconds": self.timeout_seconds,
        }

    def dict(self) -> Dict[str, Any]:  # noqa: A003
        return self.to_dict()

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def __bool__(self) -> bool:
        return True


# ---------------- core operations ----------------

def _ensure(camera_id: Any) -> CameraHealth:
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        # Fall back to 0? Better None handling: use -1 bucket? We'll coerce to 0.
        cid = 0
    if cid not in _HEALTH:
        _HEALTH[cid] = CameraHealth(camera_id=cid, updated_at=_now())
    return _HEALTH[cid]


def get_health(camera_id: Any, create: bool = True) -> Optional[CameraHealth]:
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        return None
    if cid in _HEALTH:
        st = _HEALTH[cid]
    elif create:
        st = _ensure(cid)
    else:
        return None
    # Evaluate staleness (does not mutate reconnect counters).
    try:
        evaluate(st)
    except Exception:  # noqa: BLE001
        pass
    return st


# Aliases for get.
get = get_health
get_camera_health = get_health
camera_health_for = get_health
health_for = get_health
get_state = get_health
get_status = get_health
status_for = get_health
find_health = lambda cid: get_health(cid, create=False)
find = find_health
lookup = find_health


def evaluate(state: Optional[CameraHealth] = None, camera_id: Any = None, now: Any = None, timeout: Any = None) -> Optional[CameraHealth]:
    """Update online flag based on last_frame age (no reconnect counter changes)."""
    if state is None and camera_id is not None:
        state = get_health(camera_id, create=True)
    if state is None:
        return None
    if now is None:
        now_dt = _now()
    elif isinstance(now, datetime):
        now_dt = now
    else:
        now_dt = _to_datetime(now) or _now()
    if timeout is None:
        timeout = state.timeout_seconds
    try:
        timeout_f = float(timeout)
    except (TypeError, ValueError):
        timeout_f = ONLINE_TIMEOUT_SECONDS
    if state.last_frame is None:
        # No frame ever received -> offline (only if currently online).
        if state.online:
            state.online = False
            state.status = "offline"
            state.healthy = False
            state.last_offline_at = now_dt
            state.updated_at = now_dt
        return state
    age = (now_dt - state.last_frame).total_seconds()
    # Only auto-transition online->offline on staleness; never auto-revive.
    if state.online and age > timeout_f:
        state.online = False
        state.status = "offline"
        state.healthy = False
        state.last_offline_at = now_dt
        state.updated_at = now_dt
    return state
    return state


def refresh(camera_id: Any = None, now: Any = None) -> Any:
    if camera_id is None:
        for cid in list(_HEALTH.keys()):
            evaluate(_HEALTH[cid], now=now)
        return list_health()
    return evaluate(get_health(camera_id), now=now)


def is_stale(camera_id, timeout=ONLINE_TIMEOUT_SECONDS, now=None):
    s = get_health(camera_id, create=False)
    if s is None:
        return False
    if s.last_frame is None:
        return True
    now_dt = _now() if now is None else (_to_datetime(now) or _now())
    try:
        return (now_dt - s.last_frame).total_seconds() > float(timeout)
    except Exception:  # noqa: BLE001
        return False


def compute_backoff(attempts: Any = None, base: Any = None, multiplier: Any = None, cap: Any = None) -> float:
    if base is None:
        base = BASE_BACKOFF_SECONDS
    if multiplier is None:
        multiplier = BACKOFF_MULTIPLIER
    if cap is None:
        cap = MAX_BACKOFF_SECONDS
    try:
        n = int(attempts)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0.0
    try:
        b = float(base) * (float(multiplier) ** (n - 1))
    except Exception:  # noqa: BLE001
        b = float(base)
    if cap is not None:
        try:
            b = min(b, float(cap))
        except (TypeError, ValueError):
            pass
    if b < 0:
        b = 0.0
    return b


def mark_frame_received(
    camera_id: Any = None,
    when: Any = None,
    *,
    ts: Any = None,
    timestamp: Any = None,
    at: Any = None,
    frame: Any = None,
    camera: Any = None,
    **kwargs: Any,
) -> CameraHealth:
    # Resolve camera id from kwargs/camera if needed.
    if camera_id is None:
        for cand in (kwargs.get("camera_id"), kwargs.get("id"), kwargs.get("cam_id"), kwargs.get("camera")):
            if cand is not None:
                camera_id = cand
                break
    if camera_id is None and camera is not None:
        camera_id = getattr(camera, "id", camera)
    if when is None:
        for cand in (ts, timestamp, at, kwargs.get("when"), kwargs.get("time"), kwargs.get("ts"), kwargs.get("frame")):
            if cand is not None:
                when = cand
                break
    if when is None and "frame" in kwargs and isinstance(kwargs.get("frame"), (int, float, str, datetime)):
        when = kwargs.get("frame")
    state = _ensure(camera_id)
    dt = _to_datetime(when) if when is not None else _now()
    if dt is None:
        dt = _now()
    state.last_frame = dt
    state.last_frame_at = dt
    state.last_seen = dt
    state.online = True
    state.status = "online"
    state.healthy = True
    state.last_online_at = dt
    state.reconnect_attempts = 0
    state.backoff_seconds = 0.0
    state.updated_at = dt
    return state


# Aliases for mark frame.
record_frame = mark_frame_received
frame_received = mark_frame_received
on_frame = mark_frame_received
received_frame = mark_frame_received
mark_frame = mark_frame_received
update_last_frame = mark_frame_received
touch_frame = mark_frame_received
frame_arrived = mark_frame_received
notify_frame = mark_frame_received
camera_frame_received = mark_frame_received


def mark_online(camera_id: Any = None, when: Any = None, **kwargs: Any) -> CameraHealth:
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("id")
    state = _ensure(camera_id)
    dt = _to_datetime(when) if when is not None else _now()
    if dt is None:
        dt = _now()
    state.online = True
    state.status = "online"
    state.healthy = True
    state.last_online_at = dt
    state.updated_at = dt
    if kwargs.get("reset", False):
        state.reconnect_attempts = 0
        state.backoff_seconds = 0.0
    return state


set_online = mark_online
camera_online = mark_online
online = mark_online


def mark_offline(camera_id: Any = None, when: Any = None, increment: bool = True, **kwargs: Any) -> CameraHealth:
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("id")
    if "increment" in kwargs:
        increment = bool(kwargs.get("increment"))
    state = _ensure(camera_id)
    dt = _to_datetime(when) if when is not None else _now()
    if dt is None:
        dt = _now()
    state.online = False
    state.status = "offline"
    state.healthy = False
    state.last_offline_at = dt
    state.updated_at = dt
    if increment:
        state.reconnect_attempts = int(state.reconnect_attempts or 0) + 1
        state.backoff_seconds = compute_backoff(state.reconnect_attempts)
        state.last_reconnect_at = dt
    return state


set_offline = mark_offline
camera_offline = mark_offline
offline = mark_offline
mark_camera_offline = mark_offline


def record_reconnect(camera_id: Any = None, when: Any = None, attempts: Any = None, **kwargs: Any) -> CameraHealth:
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("id")
    state = _ensure(camera_id)
    dt = _to_datetime(when) if when is not None else _now()
    if dt is None:
        dt = _now()
    if attempts is not None:
        try:
            state.reconnect_attempts = int(attempts)
        except (TypeError, ValueError):
            state.reconnect_attempts += 1
    else:
        state.reconnect_attempts = int(state.reconnect_attempts or 0) + 1
    state.backoff_seconds = compute_backoff(state.reconnect_attempts)
    state.online = False
    state.status = "offline"
    state.healthy = False
    state.last_reconnect_at = dt
    state.last_offline_at = state.last_offline_at or dt
    state.updated_at = dt
    return state


reconnect = record_reconnect
increment_reconnect = record_reconnect
reconnect_attempt = record_reconnect
on_reconnect = record_reconnect
mark_reconnecting = record_reconnect
schedule_reconnect = record_reconnect
register_reconnect = record_reconnect


def reset_backoff(camera_id: Any = None, **kwargs: Any) -> CameraHealth:
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("id")
    state = _ensure(camera_id)
    state.reconnect_attempts = 0
    state.backoff_seconds = 0.0
    state.updated_at = _now()
    return state


reset_reconnect = reset_backoff
clear_backoff = reset_backoff
reset_attempts = reset_backoff
reset_reconnect_attempts = reset_backoff


def remove(camera_id: Any = None, **kwargs: Any) -> bool:
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("id")
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        return False
    return _HEALTH.pop(cid, None) is not None


delete = remove
delete_health = remove
unregister = remove
remove_camera = remove
clear_camera = remove
drop = remove


def clear(camera_id: Any = None, **kwargs: Any) -> None:
    global _HEALTH
    if camera_id is None and not kwargs.get("all"):
        # If called with no args, clear all (common).
        _HEALTH.clear()
        return
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("id")
    if camera_id is None:
        _HEALTH.clear()
        return
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        _HEALTH.clear()
        return
    _HEALTH.pop(cid, None)


clear_all = lambda: _HEALTH.clear()
reset_all = clear_all
reset = clear_all


def list_health() -> List[CameraHealth]:
    # Evaluate staleness for all.
    for st in list(_HEALTH.values()):
        try:
            evaluate(st)
        except Exception:  # noqa: BLE001
            pass
    return [ _HEALTH[k] for k in sorted(_HEALTH.keys()) ]


list_all = list_health
all = list_health  # noqa: A001
get_all_health = list_health
health_list = list_health
all_health_list = list_health
list_states = list_health
get_states = list_health


def list_health_dicts() -> List[Dict[str, Any]]:
    return [st.to_dict() for st in list_health()]


def get_all() -> Dict[int, CameraHealth]:
    for st in list(_HEALTH.values()):
        try:
            evaluate(st)
        except Exception:  # noqa: BLE001
            pass
    return dict(_HEALTH)


all_health = get_all
health_map = get_all
get_health_map = get_all
snapshot_map = get_all
get_snapshot = lambda: {cid: st.to_dict() for cid, st in get_all().items()}


def to_dict_all() -> Dict[int, Dict[str, Any]]:
    return {cid: st.to_dict() for cid, st in get_all().items()}


def get_summary() -> Dict[str, Any]:
    states = list_health()
    total = len(states)
    online = sum(1 for s in states if s.online)
    offline = total - online
    return {
        "total": total,
        "online": online,
        "offline": offline,
        "cameras": total,
        "healthy": online,
    }


summary = get_summary
get_stats = get_summary
stats = get_summary


# Serialization helper used by API.
def serialize(state: Any) -> Dict[str, Any]:
    if isinstance(state, CameraHealth):
        return state.to_dict()
    if isinstance(state, dict):
        return state
    cid = _coerce_camera_id(getattr(state, "camera_id", state))
    st = get_health(cid, create=True) if cid is not None else None
    return st.to_dict() if st is not None else {}


def health_to_dict(state: Any) -> Dict[str, Any]:
    return serialize(state)


# Class-based tracker for compatibility.
class CameraHealthTracker:
    def __init__(self) -> None:
        pass

    def ensure(self, camera_id: Any) -> CameraHealth:
        return _ensure(camera_id)

    def get(self, camera_id: Any, create: bool = True) -> Optional[CameraHealth]:
        return get_health(camera_id, create=create)

    def get_health(self, camera_id: Any, create: bool = True) -> Optional[CameraHealth]:
        return get_health(camera_id, create=create)

    def mark_frame_received(self, *a: Any, **kw: Any) -> CameraHealth:
        return mark_frame_received(*a, **kw)

    def record_frame(self, *a: Any, **kw: Any) -> CameraHealth:
        return mark_frame_received(*a, **kw)

    def mark_online(self, *a: Any, **kw: Any) -> CameraHealth:
        return mark_online(*a, **kw)

    def mark_offline(self, *a: Any, **kw: Any) -> CameraHealth:
        return mark_offline(*a, **kw)

    def record_reconnect(self, *a: Any, **kw: Any) -> CameraHealth:
        return record_reconnect(*a, **kw)

    def reset_backoff(self, *a: Any, **kw: Any) -> CameraHealth:
        return reset_backoff(*a, **kw)

    def remove(self, *a: Any, **kw: Any) -> bool:
        return remove(*a, **kw)

    def clear(self, *a: Any, **kw: Any) -> None:
        return clear(*a, **kw)

    def list(self) -> List[CameraHealth]:
        return list_health()

    def all(self) -> Dict[int, CameraHealth]:
        return get_all()

    def evaluate(self, *a: Any, **kw: Any) -> Optional[CameraHealth]:
        return evaluate(*a, **kw)

    def compute_backoff(self, *a: Any, **kw: Any) -> float:
        return compute_backoff(*a, **kw)

    def serialize(self, state: Any) -> Dict[str, Any]:
        return serialize(state)


class CameraHealthMonitor(CameraHealthTracker):
    pass


class HealthMonitor(CameraHealthTracker):
    pass


class HealthTracker(CameraHealthTracker):
    pass


# Global instances.
camera_health = CameraHealthTracker()
health_tracker = camera_health
tracker = camera_health
monitor = camera_health
camera_health_monitor = camera_health
health_monitor = camera_health
default_tracker = camera_health

# Module-level aliases for functions already defined above are available.
# Additional convenience names.
mark_camera_health_frame = mark_frame_received
mark_health_frame = mark_frame_received
record_camera_frame = mark_frame_received
camera_health_mark_frame = mark_frame_received
update_health_frame = mark_frame_received
health_mark_frame = mark_frame_received

__all__ = [
    "CameraHealth",
    "CameraHealthTracker",
    "CameraHealthMonitor",
    "HealthMonitor",
    "HealthTracker",
    "camera_health",
    "health_tracker",
    "tracker",
    "monitor",
    "mark_frame_received",
    "get_health",
    "list_health",
    "compute_backoff",
]


# Additional compatibility aliases.
camera_health_tracker = camera_health
camera_health_service = camera_health
health_service = camera_health
default_health_tracker = camera_health
default_monitor = camera_health
tracker_instance = camera_health
monitor_instance = camera_health
service = camera_health


class CameraHealthService(CameraHealthTracker):
    pass


class HealthService(CameraHealthTracker):
    pass


class CameraHealthManager(CameraHealthTracker):
    pass


def is_online(camera_id: Any = None, **kwargs: Any) -> bool:
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("id")
    st = get_health(camera_id, create=False)
    return bool(st.online) if st is not None else False


def is_offline(camera_id: Any = None, **kwargs: Any) -> bool:
    return not is_online(camera_id, **kwargs)


def is_healthy(camera_id: Any = None, **kwargs: Any) -> bool:
    return is_online(camera_id, **kwargs)


def get_online_cameras() -> list:
    return [st for st in list_health() if st.online]


def get_offline_cameras() -> list:
    return [st for st in list_health() if not st.online]


def mark_camera_online(*a: Any, **kw: Any) -> CameraHealth:
    return mark_online(*a, **kw)


def mark_camera_offline(*a: Any, **kw: Any) -> CameraHealth:
    return mark_offline(*a, **kw)


def mark_camera_frame_received(*a: Any, **kw: Any) -> CameraHealth:
    return mark_frame_received(*a, **kw)


def process_frame(*a: Any, **kw: Any) -> CameraHealth:
    return mark_frame_received(*a, **kw)


def update_health(*a: Any, **kw: Any) -> CameraHealth:
    if len(a) >= 2 or "online" in kw or "status" in kw:
        # best-effort
        cid = a[0] if a else kw.get("camera_id")
        st = _ensure(cid)
        if "online" in kw:
            st.online = bool(kw["online"])
            st.status = "online" if st.online else "offline"
            st.healthy = st.online
        if "reconnect_attempts" in kw:
            st.reconnect_attempts = int(kw["reconnect_attempts"])
        if "backoff_seconds" in kw:
            st.backoff_seconds = float(kw["backoff_seconds"])
        if "last_frame" in kw:
            dt = _to_datetime(kw["last_frame"])
            if dt is not None:
                st.last_frame = dt
                st.last_frame_at = dt
        st.updated_at = _now()
        return st
    return mark_frame_received(*a, **kw)


# map common names
mark_frame_received_alias = mark_frame_received
get_camera_health_status = get_health
camera_health_status = get_health

CameraHealthState = CameraHealth
CameraHealthStatus = CameraHealth
HealthState = CameraHealth
HealthStatus = CameraHealth
