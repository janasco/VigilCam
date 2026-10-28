"""Dashboard summary aggregate service for VigilCam.

Single-call metrics aggregate for the dashboard UI so the frontend can
render overview cards with one request instead of fanning out to
/system, /storage, /events, /cameras and /watchdog endpoints.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


async def get_dashboard_summary(db: Any = None) -> Dict[str, Any]:
    """Collect dashboard metrics in a single call.

    Never raises: each section degrades to None/0/[] on failure so the
    dashboard can still render partial data.
    """
    now = datetime.utcnow()
    timestamp = now.isoformat()

    # --- system (cpu/memory/disk/uptime/processes/cameras) ---
    system: Dict[str, Any] = {}
    try:
        from app.services.system_metrics import collect_system_metrics

        system = await collect_system_metrics(db)
        if not isinstance(system, dict):
            system = {}
    except Exception:  # noqa: BLE001
        system = {}

    cameras: Dict[str, Any] = {}
    events: Dict[str, Any] = {}
    recordings: Dict[str, Any] = {}
    storage: Dict[str, Any] = {}
    health: Dict[str, Any] = {}

    # --- cameras / events / recordings / storage via DB ---
    if db is not None:
        try:
            from sqlalchemy import func, select

            from app.models.entities import Camera, Event, Recording, StorageArea

            try:
                result = await db.execute(select(func.count(Camera.id)))
                total_cameras = int(result.scalar() or 0)
            except Exception:  # noqa: BLE001
                total_cameras = None
            try:
                result = await db.execute(
                    select(func.count(Camera.id)).where(Camera.enabled.is_(True))
                )
                enabled_cameras = int(result.scalar() or 0)
            except Exception:  # noqa: BLE001
                enabled_cameras = None
            try:
                result = await db.execute(select(func.count(Event.id)))
                total_events = int(result.scalar() or 0)
            except Exception:  # noqa: BLE001
                total_events = None
            try:
                cutoff = now - timedelta(hours=24)
                result = await db.execute(
                    select(func.count(Event.id)).where(Event.created_at >= cutoff)
                )
                events_24h = int(result.scalar() or 0)
            except Exception:  # noqa: BLE001
                events_24h = None
            recent: List[Dict[str, Any]] = []
            last_event_at: Optional[str] = None
            try:
                result = await db.execute(
                    select(Event).order_by(Event.id.desc()).limit(5)
                )
                rows = list(result.scalars().all())
                for ev in rows:
                    try:
                        created = getattr(ev, "created_at", None)
                        created_s = (
                            created.isoformat()
                            if isinstance(created, datetime)
                            else (str(created) if created is not None else None)
                        )
                    except Exception:  # noqa: BLE001
                        created_s = None
                    recent.append(
                        {
                            "id": getattr(ev, "id", None),
                            "camera_id": getattr(ev, "camera_id", None),
                            "kind": getattr(ev, "kind", None),
                            "label": getattr(ev, "label", None),
                            "created_at": created_s,
                        }
                    )
                if rows:
                    first_created = getattr(rows[0], "created_at", None)
                    if isinstance(first_created, datetime):
                        last_event_at = first_created.isoformat()
                    elif first_created is not None:
                        last_event_at = str(first_created)
            except Exception:  # noqa: BLE001
                recent = []
            try:
                result = await db.execute(select(func.count(Recording.id)))
                total_recordings = int(result.scalar() or 0)
            except Exception:  # noqa: BLE001
                total_recordings = None
            try:
                result = await db.execute(
                    select(func.coalesce(func.sum(Recording.size_bytes), 0))
                )
                total_rec_bytes = int(result.scalar() or 0)
            except Exception:  # noqa: BLE001
                total_rec_bytes = None
            try:
                result = await db.execute(select(StorageArea).order_by(StorageArea.id))
                areas = list(result.scalars().all())
                areas_used = sum(int(getattr(a, "used_bytes", 0) or 0) for a in areas)
                areas_max_raw = [
                    getattr(a, "max_bytes", None)
                    for a in areas
                    if getattr(a, "max_bytes", None) is not None
                ]
                areas_max = sum(int(v) for v in areas_max_raw) if areas_max_raw else None
                areas_count = len(areas)
            except Exception:  # noqa: BLE001
                areas_count = None
                areas_used = None
                areas_max = None
        except Exception:  # noqa: BLE001
            total_cameras = enabled_cameras = None
            total_events = events_24h = None
            recent = []
            last_event_at = None
            total_recordings = total_rec_bytes = None
            areas_count = areas_used = areas_max = None
    else:
        total_cameras = enabled_cameras = None
        total_events = events_24h = None
        recent = []
        last_event_at = None
        total_recordings = total_rec_bytes = None
        areas_count = areas_used = areas_max = None

    # --- active camera counts from system metrics / recording manager ---
    active_cameras: Optional[int] = None
    try:
        sys_cams = system.get("cameras") if isinstance(system, dict) else None
        if isinstance(sys_cams, dict):
            for key in ("active", "active_count", "active_cameras"):
                val = sys_cams.get(key)
                if isinstance(val, int):
                    active_cameras = val
                    break
        if active_cameras is None:
            active_cameras = system.get("active_cameras") if isinstance(system, dict) else None
            if not isinstance(active_cameras, int):
                active_cameras = None
    except Exception:  # noqa: BLE001
        active_cameras = None
    if active_cameras is None and db is None:
        # Fall back to in-memory recording manager even without db.
        try:
            from app.services.recording_manager import recording_manager

            active_cameras = len(getattr(recording_manager, "active", {}).keys())
        except Exception:  # noqa: BLE001
            pass

    # Prefer DB camera totals when available, else system-metrics values.
    if total_cameras is None:
        try:
            sys_cams = system.get("cameras", {}) if isinstance(system, dict) else {}
            total_cameras = sys_cams.get("total") if isinstance(sys_cams, dict) else None
        except Exception:  # noqa: BLE001
            total_cameras = None
    if enabled_cameras is None:
        try:
            sys_cams = system.get("cameras", {}) if isinstance(system, dict) else {}
            enabled_cameras = sys_cams.get("enabled") if isinstance(sys_cams, dict) else None
        except Exception:  # noqa: BLE001
            enabled_cameras = None

    # --- health (camera_health + watchdog, best-effort) ---
    online: Optional[int] = None
    offline: Optional[int] = None
    try:
        from app.services import camera_health as _ch

        summary = _ch.get_summary()
        if isinstance(summary, dict):
            online = summary.get("online")
            offline = summary.get("offline")
    except Exception:  # noqa: BLE001
        pass
    if online is None or offline is None:
        try:
            from app.services import watchdog as _wd

            summary = _wd.get_summary()
            if isinstance(summary, dict):
                if online is None:
                    online = summary.get("online")
                if offline is None:
                    offline = summary.get("offline")
        except Exception:  # noqa: BLE001
            pass
    try:
        from app.services import watchdog as _wd_mod

        health = _wd_mod.get_summary()
        if not isinstance(health, dict):
            health = {}
    except Exception:  # noqa: BLE001
        health = {}
    if online is not None:
        health.setdefault("online", online)
    if offline is not None:
        health.setdefault("offline", offline)
    if active_cameras is not None:
        health.setdefault("active", active_cameras)

    cameras = {
        "total": total_cameras,
        "total_count": total_cameras,
        "count": total_cameras,
        "enabled": enabled_cameras,
        "enabled_count": enabled_cameras,
        "active": active_cameras,
        "active_count": active_cameras,
        "active_cameras": active_cameras,
        "online": online,
        "offline": offline,
    }

    events = {
        "total": total_events,
        "total_count": total_events,
        "count": total_events,
        "last_24h": events_24h,
        "last_24h_count": events_24h,
        "recent_24h": events_24h,
        "recent": recent,
        "recent_events": recent,
        "last_event_at": last_event_at,
    }

    recordings = {
        "total": total_recordings,
        "total_count": total_recordings,
        "count": total_recordings,
        "total_bytes": total_rec_bytes,
        "size_bytes": total_rec_bytes,
    }

    disk_info: Any = None
    try:
        disk_info = system.get("disk") if isinstance(system, dict) else None
    except Exception:  # noqa: BLE001
        disk_info = None
    storage = {
        "disk": disk_info,
        "areas_count": areas_count,
        "area_count": areas_count,
        "count": areas_count,
        "used_bytes": areas_used,
        "total_used_bytes": areas_used,
        "max_bytes": areas_max,
        "total_max_bytes": areas_max,
    }

    cpu_info: Any = None
    mem_info: Any = None
    uptime: Any = None
    try:
        if isinstance(system, dict):
            cpu_info = system.get("cpu")
            mem_info = system.get("memory")
            uptime = system.get("uptime_seconds", system.get("uptime"))
    except Exception:  # noqa: BLE001
        pass

    result: Dict[str, Any] = {
        "timestamp": timestamp,
        "time": timestamp,
        "sampled_at": timestamp,
        "cameras": cameras,
        "camera": cameras,
        "camera_count": total_cameras,
        "active_cameras": active_cameras,
        "active_camera_count": active_cameras,
        "events": events,
        "event": events,
        "event_count": total_events,
        "recent_events": recent,
        "recordings": recordings,
        "recording": recordings,
        "recording_count": total_recordings,
        "storage": storage,
        "disk": disk_info,
        "cpu": cpu_info,
        "memory": mem_info,
        "mem": mem_info,
        "system": system,
        "health": health,
        "uptime_seconds": uptime,
        "uptime": uptime,
    }
    return result


# Compatibility aliases for alternate naming.
collect_dashboard_summary = get_dashboard_summary
dashboard_summary = get_dashboard_summary
get_summary = get_dashboard_summary
summary = get_dashboard_summary
get_metrics = get_dashboard_summary
collect_summary = get_dashboard_summary
