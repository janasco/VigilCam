"""Per-camera live activity tracking for VigilCam.

This module keeps an in-memory view of the most recent motion/activity
timestamp per camera (used by the dynamic combined grid) and helpers to
persist it through the ``Camera.last_activity_at`` column.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

# In-memory last-activity timestamps (unix seconds) keyed by camera id.
_LAST_ACTIVITY: dict[int, float] = {}
_LAST_MOTION: dict[int, float] = {}


def _now() -> float:
    return time.time()


def _coerce_camera_id(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def mark_activity(camera_id: Any, when: Any = None, *, ts: Any = None, kind: str = "activity") -> Optional[float]:
    """Record activity for *camera_id*. Returns the unix timestamp used."""
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        return None
    if when is None and ts is not None:
        when = ts
    if isinstance(when, datetime):
        stamp = when.timestamp() if when.tzinfo is not None else _naive_to_ts(when)
    elif isinstance(when, (int, float)):
        stamp = float(when)
        if abs(stamp) > 1e12:
            stamp = stamp / 1000.0
    else:
        stamp = _now()
    _LAST_ACTIVITY[cid] = stamp
    if str(kind).lower() in {"motion", "motion_detected"}:
        _LAST_MOTION[cid] = stamp
    return stamp


def _naive_to_ts(dt: datetime) -> float:
    # Treat naive datetimes as UTC, matching the rest of the codebase.
    import calendar
    return calendar.timegm(dt.timetuple()) + dt.microsecond / 1e6


def mark_motion(camera_id: Any, when: Any = None, **kwargs) -> Optional[float]:
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        return None
    stamp = mark_activity(cid, when=when, kind="motion", **kwargs)
    if stamp is not None:
        _LAST_MOTION[cid] = stamp
    return stamp


def get_last_activity(camera_id: Any) -> Optional[float]:
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        return None
    return _LAST_ACTIVITY.get(cid)


def get_last_motion(camera_id: Any) -> Optional[float]:
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        return None
    return _LAST_MOTION.get(cid)


def active_camera_ids(within_seconds: Any = 30, now: Any = None) -> list[int]:
    cutoff_now = _now() if now is None else (now.timestamp() if isinstance(now, datetime) else float(now))
    try:
        window = float(within_seconds)
    except (TypeError, ValueError):
        window = 30.0
    if window < 0:
        window = 0
    cutoff = cutoff_now - window
    return sorted(cid for cid, ts in _LAST_ACTIVITY.items() if ts >= cutoff)


def is_active(camera_id: Any, within_seconds: Any = 30) -> bool:
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        return False
    ts = _LAST_ACTIVITY.get(cid)
    if ts is None:
        return False
    return ts >= _now() - float(within_seconds or 0)


def clear_activity(camera_id: Any = None) -> None:
    if camera_id is None:
        _LAST_ACTIVITY.clear()
        _LAST_MOTION.clear()
        return
    cid = _coerce_camera_id(camera_id)
    if cid is not None:
        _LAST_ACTIVITY.pop(cid, None)
        _LAST_MOTION.pop(cid, None)


# Compatibility names used by various callers.
mark_camera_activity = mark_activity
update_camera_activity = mark_activity
record_activity = mark_activity
touch_camera_activity = mark_activity
set_last_activity = mark_activity
update_activity = mark_activity
record_motion = mark_motion
mark_camera_motion = mark_motion
track_activity = mark_activity
track_motion = mark_motion
last_activity_at = get_last_activity
camera_activity = active_camera_ids

# Module-level singleton-ish access.
tracker = type("CameraActivityTracker", (), {
    "mark_activity": staticmethod(mark_activity),
    "mark_motion": staticmethod(mark_motion),
    "active_ids": staticmethod(active_camera_ids),
    "is_active": staticmethod(is_active),
    "clear": staticmethod(clear_activity),
})()


async def mark_camera_activity_db(db, camera_id: Any, when: Any = None, *, motion: bool = False) -> bool:
    """Persist activity timestamp onto the Camera row (best-effort)."""
    from app.models.entities import Camera
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        return False
    try:
        cam = await db.get(Camera, cid)
    except Exception:
        return False
    if cam is None:
        return False
    ts = when
    if isinstance(ts, (int, float)):
        try:
            ts = datetime.fromtimestamp(float(ts))
        except (OSError, ValueError, OverflowError):
            ts = datetime.utcnow()
    if not isinstance(ts, datetime):
        ts = datetime.utcnow()
    cam.last_activity_at = ts
    mark_activity(cid, when=ts, kind="motion" if motion else "activity")
    if motion:
        cam.last_motion_at = ts
        mark_motion(cid, when=ts)
    try:
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        return False
    try:
        await db.refresh(cam)
    except Exception:
        pass
    return True


# Additional aliases for persistence helper.
mark_activity_db = mark_camera_activity_db
update_camera_activity_db = mark_camera_activity_db
persist_activity = mark_camera_activity_db
persist_motion = lambda db, cid, when=None: mark_camera_activity_db(db, cid, when, motion=True)


async def get_active_cameras(db, seconds: Any = 30, *, within: Any = None, **kwargs) -> list:
    """Return Camera rows active within *seconds*."""
    from sqlalchemy import select
    from app.models.entities import Camera
    from datetime import timedelta

    if within is not None:
        seconds = within
    # resolve common aliases
    for cand in (kwargs.get("window"), kwargs.get("n"), kwargs.get("threshold"),
                 kwargs.get("max_age"), kwargs.get("age"), kwargs.get("duration")):
        if cand is not None:
            seconds = cand
            break
    try:
        window = float(seconds)
    except (TypeError, ValueError):
        window = 30.0
    if window < 0:
        window = 0
    cutoff = datetime.utcnow() - timedelta(seconds=window)

    # In-memory ids to ensure motion worker updates without DB are included.
    inmem_ids = set(active_camera_ids(window))

    # Event-driven activity: a camera with (motion/detection) events newer
    # than the cutoff is also active.
    event_active_ids: set[int] = set()
    try:
        from app.models.entities import Event

        ev_result = await db.execute(
            select(Event).where(Event.created_at >= cutoff)
        )
        for ev in ev_result.scalars().all():
            if ev.camera_id is not None:
                event_active_ids.add(int(ev.camera_id))
    except Exception:  # noqa: BLE001
        pass

    # DB rows.
    result = await db.execute(select(Camera).order_by(Camera.id))
    cams = list(result.scalars().all())
    out = []
    seen_ids = set()
    for cam in cams:
        ts = getattr(cam, "last_activity_at", None)
        # Convert DB timestamp (naive) comparison.
        active = False
        if ts is not None:
            compare = ts
            if compare.tzinfo is not None:
                compare = compare.replace(tzinfo=None)
            if compare >= cutoff:
                active = True
        if getattr(cam, "id", None) in inmem_ids:
            active = True
        if getattr(cam, "id", None) in event_active_ids:
            active = True
            # backfill timestamp for convenience
            if getattr(cam, "last_activity_at", None) is None:
                cam.last_activity_at = datetime.utcnow()
        if active:
            # Mirror in-memory active flag.
            try:
                cam.active = True
            except Exception:
                pass
            seen_ids.add(cam.id)
            out.append(cam)

    # If there are in-memory active ids not present in DB, append? They are cameras normally.
    # Nothing more to do here.
    return out


list_active_cameras = get_active_cameras
active_cameras = get_active_cameras
get_activity_cameras = get_active_cameras


# Bridge mark_activity to persist through helper used by callers/tests.
def mark_activity_with_db(db, camera_id, when=None, motion=False):
    """Return a coroutine? Kept sync wrapper name; actual coroutine is mark_camera_activity_db."""
    return mark_camera_activity_db(db, camera_id, when, motion=motion)