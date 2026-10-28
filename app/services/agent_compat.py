"""Agent DVR compatibility helpers.

Maps `Agent DVR local API <https://ispysoftware.github.io/Agent_API/>`_
concepts (``oid``/``ot`` device addressing, armed state, profiles) onto
VigilCam's camera/event/recording models.

Reference docs inspected (read-only, no vendoring):
``/opt/others/ispy-agent-api/swagger.yaml`` (Agent DVR 8.1.1.0) and
``/opt/others/ispy-agent-api/README.md``.

Only endpoints VigilCam otherwise lacks are exposed here; the router lives
in :mod:`app.api.agent_compat` and is auto-registered by ``app.main``'s
plugin-style loader, so ``app/main.py`` needs no edits.
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.entities import Camera, Event, Recording

# Agent DVR object-type ids: 1 = microphone, 2 = camera.
OT_MICROPHONE = 1
OT_CAMERA = 2

#: Agent DVR API version this compat layer targets.
AGENT_API_VERSION = "8.1.1.0"

#: Profiles mirror Agent's default Home/Away/Night triple.
PROFILES = ["Home", "Away", "Night"]

_armed: bool = False
_profile_index: int = 0


def is_armed() -> bool:
    """Return the compat-layer armed flag."""
    return _armed


def set_armed(value: bool) -> bool:
    """Set the compat-layer armed flag and return the new state."""
    global _armed
    _armed = bool(value)
    return _armed


def get_profiles() -> List[Dict[str, Any]]:
    """Return Agent-shaped profile list."""
    return [{"index": i, "name": name} for i, name in enumerate(PROFILES)]


def get_profile_index() -> int:
    """Return the currently applied profile index."""
    return _profile_index


def set_profile_index(ind: int) -> Dict[str, Any]:
    """Apply profile ``ind``; raises :class:`ValueError` when out of range."""
    global _profile_index
    try:
        idx = int(ind)
    except (TypeError, ValueError):
        raise ValueError(f"invalid profile index: {ind!r}")
    if idx < 0 or idx >= len(PROFILES):
        raise ValueError(f"profile index out of range: {ind!r}")
    _profile_index = idx
    return {"index": idx, "name": PROFILES[idx]}


def reset_state() -> None:
    """Reset in-memory compat state (used by tests)."""
    global _armed, _profile_index
    _armed = False
    _profile_index = 0


def validate_ot(ot: Any) -> int:
    """Validate an Agent object-type id; raises :class:`ValueError`."""
    try:
        ot_int = int(ot)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"invalid ot: {ot!r}")
    if ot_int not in (OT_MICROPHONE, OT_CAMERA):
        raise ValueError(f"invalid ot: {ot!r} (want 1 or 2)")
    return ot_int


def to_agent_device(camera: Camera) -> Dict[str, Any]:
    """Serialize a VigilCam camera to an Agent-style device dict."""
    enabled = bool(getattr(camera, "enabled", True))
    return {
        "oid": camera.id,
        "ot": OT_CAMERA,
        "name": camera.name,
        "group": getattr(camera, "group", None),
        "enabled": enabled,
        "state": "ok" if enabled else "off",
        "recording": bool(getattr(camera, "recording_enabled", True)),
        "alerts": bool(getattr(camera, "alert_enabled", True)),
        "detector": bool(
            getattr(camera, "detector_enabled", True)
            and getattr(camera, "detection_enabled", True)
        ),
        "kind": getattr(camera, "kind", None),
        "source": getattr(camera, "source", None),
    }


async def get_camera_or_none(db: AsyncSession, oid: Any) -> Optional[Camera]:
    """Fetch a camera by id, returning ``None`` for bad/missing ids."""
    try:
        cid = int(oid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    try:
        return await db.get(Camera, cid)
    except Exception:  # noqa: BLE001
        return None


async def list_agent_devices(db: AsyncSession) -> List[Dict[str, Any]]:
    """List all cameras in Agent device shape."""
    result = await db.execute(select(Camera).order_by(Camera.id))
    return [to_agent_device(cam) for cam in result.scalars().all()]


async def match_cameras(
    db: AsyncSession,
    oid: Any = None,
    group: Optional[str] = None,
) -> List[Camera]:
    """Resolve Agent targeting (single oid or csv group list) to cameras."""
    result = await db.execute(select(Camera).order_by(Camera.id))
    cameras = list(result.scalars().all())
    if oid is not None:
        try:
            cid = int(oid)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return []
        return [c for c in cameras if c.id == cid]
    if group:
        wanted = {g.strip().lower() for g in str(group).split(",") if g.strip()}
        return [
            c
            for c in cameras
            if str(getattr(c, "group", "") or "").strip().lower() in wanted
        ]
    return cameras


async def build_status(db: AsyncSession) -> Dict[str, Any]:
    """Build an Agent ``getStatus``-shaped server status dict."""
    total = (await db.execute(select(func.count(Camera.id)))).scalar() or 0
    active = (
        await db.execute(
            select(func.count(Camera.id)).where(Camera.enabled.is_(True))
        )
    ).scalar() or 0
    recording = (
        await db.execute(
            select(func.count(Recording.id)).where(Recording.ended_at.is_(None))
        )
    ).scalar() or 0
    try:
        machine = socket.gethostname()
    except Exception:  # noqa: BLE001
        machine = "vigilcam"
    return {
        "armed": is_armed(),
        "devices": int(total),
        "activeDevices": int(active),
        "recording": int(recording),
        "profile": {"index": _profile_index, "name": PROFILES[_profile_index]},
        "remote": "offline",
        "serverId": "vigilcam",
        "machine": machine,
        "version": getattr(settings, "APP_VERSION", "0.1.0"),
        "agentApiVersion": AGENT_API_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def build_system_stats(db: AsyncSession | None = None) -> Dict[str, Any]:
    """Build an Agent ``getSystemStats``-shaped dict from VigilCam metrics."""
    raw: Dict[str, Any] = {}
    try:
        from app.services.system_metrics import collect_system_metrics

        raw = await collect_system_metrics(db)
        if not isinstance(raw, dict):
            raw = {}
    except Exception:  # noqa: BLE001
        raw = {}
    cpu = raw.get("cpu_percent", raw.get("cpu"))
    mem = raw.get("memory", {}) if isinstance(raw.get("memory"), dict) else {}
    disk = raw.get("disk", {}) if isinstance(raw.get("disk"), dict) else {}
    try:
        cpu_f = float(cpu) if cpu is not None else 0.0
    except (TypeError, ValueError):
        cpu_f = 0.0
    try:
        mem_pct = float(mem.get("percent", 0.0)) if mem else 0.0
    except (TypeError, ValueError):
        mem_pct = 0.0
    try:
        disk_pct = float(disk.get("percent", 0.0)) if disk else 0.0
    except (TypeError, ValueError):
        disk_pct = 0.0
    return {
        "cpu_used": f"{cpu_f:.1f}%",
        "ram_free": str(mem.get("available", raw.get("ram_free", "n/a"))),
        "disk_free": str(disk.get("free", raw.get("disk_free", "n/a"))),
        "cpup": cpu_f,
        "ramp": mem_pct,
        "ramu": mem.get("used", None),
        "diskp": disk_pct,
        "disks": disk.get("disks", raw.get("disks", [])),
        "vigilcam": raw,
    }


async def record_trigger_event(
    db: AsyncSession,
    camera_id: int,
    kind: str,
    label: str,
    tags: Optional[List[str]] = None,
    payload: Optional[str] = None,
) -> Event:
    """Persist a trigger/alert event and return it."""
    event = Event(
        camera_id=camera_id,
        kind=kind,
        label=label,
        confidence=None,
        payload=payload,
        tags=list(tags or []),
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


def to_agent_alert(event: Event) -> Dict[str, Any]:
    """Serialize a VigilCam event to an Agent ``Alert`` shape."""
    tags = event.tags if isinstance(event.tags, list) else []
    created = event.created_at
    if isinstance(created, datetime):
        stamp = created.replace(tzinfo=timezone.utc).isoformat() if created.tzinfo is None else created.isoformat()
    else:
        stamp = str(created)
    return {
        "ot": OT_CAMERA,
        "oid": event.camera_id,
        "reason": event.kind,
        "msg": event.label,
        "time": stamp,
        "id": str(event.id),
        "read": False,
        "tags": ",".join(str(t) for t in tags),
    }


def to_agent_recording(rec: Recording, tags: str = "") -> Dict[str, Any]:
    """Serialize a recording row to an Agent ``getEvents``-shaped dict."""
    started = rec.started_at
    if isinstance(started, datetime):
        stamp_ms = int(started.replace(tzinfo=timezone.utc).timestamp() * 1000)
    else:
        stamp_ms = 0
    duration = 0
    try:
        if rec.ended_at and rec.started_at:
            duration = max(0, int((rec.ended_at - rec.started_at).total_seconds()))
    except Exception:  # noqa: BLE001
        duration = 0
    return {
        "id": rec.id,
        "oid": rec.camera_id,
        "ot": OT_CAMERA,
        "timestamp": stamp_ms,
        "date": started.isoformat() if isinstance(started, datetime) else str(started),
        "duration": duration,
        "path": rec.path,
        "size": rec.size_bytes,
        "tag": tags,
    }


async def count_events_since(
    db: AsyncSession, camera_id: int, secs: int = 60
) -> int:
    """Count events for a camera within the last ``secs`` seconds."""
    try:
        window = max(0, int(secs))
    except (TypeError, ValueError):
        window = 60
    since = datetime.utcnow() - timedelta(seconds=window)
    count = (
        await db.execute(
            select(func.count(Event.id)).where(
                Event.camera_id == camera_id, Event.created_at >= since
            )
        )
    ).scalar()
    return int(count or 0)
