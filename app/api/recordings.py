from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session, get_current_optional_user
from app.models.entities import Camera, Recording
from app.schemas import RecordingRead, RecordingStartRequest, RecordingStopRequest, RetentionRunRequest
from app.services.cameras import get_camera
from app.services.recording_manager import recording_manager

router = APIRouter(prefix="/api/recordings", tags=["recordings"])


def _camera_name_for(camera_id: Any, camera: Optional[Camera]) -> str:
    if camera is not None:
        return camera.name
    if camera_id is None:
        return "default"
    return str(camera_id)


def _as_int_camera_id(camera_id: Any) -> int:
    try:
        if camera_id is None:
            return 0
        return int(camera_id)
    except (TypeError, ValueError):
        return 0


@router.get("", response_model=List[RecordingRead])
async def list_recordings(
    camera_id: Optional[int] = Query(default=None),
    db: AsyncSession = Depends(db_session),
):
    stmt = select(Recording).order_by(Recording.id.desc())
    if camera_id is not None:
        stmt = stmt.where(Recording.camera_id == camera_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/start", status_code=status.HTTP_201_CREATED)
async def start_recording(
    payload: RecordingStartRequest,
    db: AsyncSession = Depends(db_session),
    current_user=Depends(get_current_optional_user),
):
    camera = None
    try:
        cid = int(payload.camera_id) if payload.camera_id is not None else None
    except (TypeError, ValueError):
        cid = None
    if cid is not None:
        try:
            camera = await get_camera(db, cid)
        except Exception:  # noqa: BLE001
            camera = None

    camera_name = _camera_name_for(payload.camera_id, camera)
    camera_id_int = camera.id if camera is not None else _as_int_camera_id(payload.camera_id)

    recording_manager.start_recording(camera_name, payload.main_url, sub_url=payload.sub_url)

    rec = Recording(
        camera_id=camera_id_int,
        path=str(recording_manager.ingestor._camera_dir(camera_name)),
        started_at=datetime.utcnow(),
    )
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    try:
        from app.services.event_bus import event_bus

        await event_bus.publish(
            "recording_started",
            {
                "camera_name": camera_name,
                "camera_id": rec.camera_id,
                "recording_id": rec.id,
                "started_at": rec.started_at,
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return {"status": "started", "camera_name": camera_name, "recording": RecordingRead.model_validate(rec)}


@router.post("/stop")
async def stop_recording(
    payload: RecordingStopRequest,
    db: AsyncSession = Depends(db_session),
):
    camera_name = payload.camera_name
    recording_manager.stop_recording(camera_name)

    camera = None
    try:
        # try resolve camera id by name from db (best effort)
        result = await db.execute(select(Camera).where(Camera.name == camera_name))
        camera = result.scalars().first()
    except Exception:  # noqa: BLE001
        camera = None

    camera_id = camera.id if camera is not None else None
    now = datetime.utcnow()
    stmt = select(Recording).where(Recording.ended_at.is_(None))
    if camera_id is not None:
        stmt = stmt.where(Recording.camera_id == camera_id)
    else:
        # best effort: paths containing the camera name
        pass
    result = await db.execute(stmt)
    rows = result.scalars().all()
    # filter by path if no camera id resolved
    if camera_id is None:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in camera_name)
        rows = [r for r in rows if safe in (r.path or "")]

    total_size = 0
    storage_dir = Path(recording_manager.ingestor.storage_root) / (
        "".join(c if c.isalnum() or c in "-_" else "_" for c in camera_name)
    )
    if storage_dir.exists():
        for p in storage_dir.rglob("*.mp4"):
            try:
                total_size += int(p.stat().st_size)
            except OSError:
                pass

    for r in rows:
        r.ended_at = now
        r.size_bytes = total_size // max(len(rows), 1)

    await db.commit()
    try:
        from app.services.event_bus import event_bus

        await event_bus.publish(
            "recording_stopped",
            {
                "camera_name": camera_name,
                "stopped_recordings": len(rows),
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return {"status": "stopped", "camera_name": camera_name, "stopped_recordings": len(rows)}


@router.post("/retention/run")
async def run_retention(
    payload: Optional[RetentionRunRequest] = Body(default=None),
    dry_run: Optional[bool] = Query(default=None),
):
    req = payload or RetentionRunRequest()
    effective_dry = dry_run if dry_run is not None else req.dry_run
    kwargs: dict[str, Any] = {}
    if req.max_age_days is not None:
        kwargs["max_age_days"] = req.max_age_days
    if req.max_total_bytes is not None:
        kwargs["max_total_bytes"] = req.max_total_bytes
    if req.camera_name:
        kwargs["camera_name"] = req.camera_name
    result = recording_manager.run_retention(dry_run=effective_dry, **kwargs)
    return result


@router.post("/retention/schedule/run")
async def run_scheduled_retention(
    payload: Optional[RetentionRunRequest] = Body(default=None),
    dry_run: Optional[bool] = Query(default=None),
):
    from app.services.scheduler import get_retention_scheduler

    req = payload or RetentionRunRequest()
    effective_dry = dry_run if dry_run is not None else getattr(req, "dry_run", False)
    kwargs: dict[str, Any] = {}
    if getattr(req, "max_age_days", None) is not None:
        kwargs["max_age_days"] = req.max_age_days
    if getattr(req, "max_total_bytes", None) is not None:
        kwargs["max_total_bytes"] = req.max_total_bytes
    if getattr(req, "camera_name", None):
        kwargs["camera_name"] = req.camera_name
    sched = get_retention_scheduler()
    import asyncio

    result = await asyncio.to_thread(sched.run, dry_run=effective_dry, **kwargs)
    if not isinstance(result, dict):
        result = {"result": result}
    result = dict(result)
    result.setdefault("status", "completed")
    result.setdefault("scheduled", True)
    result.setdefault("triggered", True)
    return result


@router.get("/status")
async def recordings_status():
    status = recording_manager.status()
    try:
        from app.services.event_bus import event_bus

        await event_bus.publish("recordings_status", status)
    except Exception:  # noqa: BLE001
        pass
    return status
