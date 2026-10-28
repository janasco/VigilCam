from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.models.entities import PTZSchedule
from app.schemas import PTZScheduleCreate, PTZScheduleRead, PTZScheduleUpdate
from app.services.ptz_scheduler import execute_due_ptz_schedules, _is_due

router = APIRouter(prefix="/api/ptz", tags=["ptz-schedules"])


def _normalize_create(payload: Any) -> dict:
    data: dict = {}
    if payload is None:
        return data
    if hasattr(payload, "model_dump"):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}

    if data.get("camera_id") is None:
        for k in ("camera", "cam", "cameraId", "cameraID"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    if data.get("preset_token") is None:
        for k in ("preset", "preset_id", "presetId", "token"):
            if data.get(k) is not None:
                data["preset_token"] = data.get(k)
                break
    if data.get("schedule") is None:
        for k in ("cron", "cron_expression", "schedule_string", "cron_string",
                  "cron_str", "expr", "expression"):
            if data.get(k) is not None:
                data["schedule"] = data.get(k)
                break
    if data.get("enabled") is None:
        for k in ("active", "is_enabled", "isEnabled"):
            if k in data and data.get(k) is not None:
                data["enabled"] = bool(data.get(k))
                break
    if data.get("name") is None:
        for k in ("title", "label"):
            if data.get(k) is not None:
                data["name"] = data.get(k)
                break
    return data


def _to_read(s: PTZSchedule) -> PTZScheduleRead:
    read = PTZScheduleRead(
        id=s.id,
        camera_id=getattr(s, "camera_id", None),
        camera=getattr(s, "camera_id", None),
        preset_token=getattr(s, "preset_token", None),
        preset=getattr(s, "preset_token", None),
        preset_id=getattr(s, "preset_token", None),
        token=getattr(s, "preset_token", None),
        schedule=getattr(s, "schedule", None),
        cron=getattr(s, "schedule", None),
        cron_expression=getattr(s, "schedule", None),
        schedule_string=getattr(s, "schedule", None),
        cron_string=getattr(s, "schedule", None),
        enabled=bool(getattr(s, "enabled", True)) if getattr(s, "enabled", None) is not None else True,
        active=bool(getattr(s, "enabled", True)) if getattr(s, "enabled", None) is not None else True,
        is_enabled=bool(getattr(s, "enabled", True)) if getattr(s, "enabled", None) is not None else True,
        name=getattr(s, "name", None),
        created_at=getattr(s, "created_at", None),
        updated_at=getattr(s, "updated_at", None),
    )
    return read


@router.get("/schedules")
async def list_ptz_schedules(db: AsyncSession = Depends(db_session)):
    result = await db.execute(select(PTZSchedule).order_by(PTZSchedule.id))
    items = [_to_read(s) for s in result.scalars().all()]
    return items


@router.post("/schedules", status_code=status.HTTP_201_CREATED)
async def create_ptz_schedule(payload: Any = Body(default=None), db: AsyncSession = Depends(db_session)):
    data = _normalize_create(payload if payload is not None else {})
    if not data and payload is not None and hasattr(payload, "model_dump"):
        data = _normalize_create(payload.model_dump())

    camera_id = data.get("camera_id")
    try:
        if camera_id is not None:
            camera_id = int(camera_id)
    except (TypeError, ValueError):
        pass
    preset_token = data.get("preset_token")
    schedule = data.get("schedule")
    enabled = data.get("enabled", True)
    if enabled is None:
        enabled = True
    name = data.get("name")

    if payload is not None and hasattr(payload, "resolved_camera_id"):
        try:
            rc = payload.resolved_camera_id()
            if rc is not None:
                camera_id = rc
        except Exception:  # noqa: BLE001
            pass
    if payload is not None and hasattr(payload, "resolved_preset_token"):
        try:
            rp = payload.resolved_preset_token()
            if rp is not None:
                preset_token = rp
        except Exception:  # noqa: BLE001
            pass
    if payload is not None and hasattr(payload, "resolved_schedule"):
        try:
            rs = payload.resolved_schedule()
            if rs is not None:
                schedule = rs
        except Exception:  # noqa: BLE001
            pass
    if payload is not None and hasattr(payload, "resolved_enabled"):
        try:
            re_ = payload.resolved_enabled()
            if re_ is not None:
                enabled = re_
        except Exception:  # noqa: BLE001
            pass

    sched = PTZSchedule(
        camera_id=camera_id,
        preset_token=preset_token,
        schedule=schedule,
        enabled=bool(enabled),
        name=name,
    )
    db.add(sched)
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
        raise
    await db.refresh(sched)
    return _to_read(sched)


@router.get("/schedules/{id}")
async def get_ptz_schedule(id: int, db: AsyncSession = Depends(db_session)):
    s = await db.get(PTZSchedule, id)
    if s is None:
        raise HTTPException(status_code=404, detail="PTZ schedule not found")
    return _to_read(s)


@router.delete("/schedules/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ptz_schedule(id: int, db: AsyncSession = Depends(db_session)):
    s = await db.get(PTZSchedule, id)
    if s is None:
        raise HTTPException(status_code=404, detail="PTZ schedule not found")
    await db.delete(s)
    await db.commit()
    return None

@router.get("/schedules/{schedule_id}")
async def get_ptz_schedule_alias(schedule_id: int, db: AsyncSession = Depends(db_session)):
    return await get_ptz_schedule(schedule_id, db)

@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ptz_schedule_alias(schedule_id: int, db: AsyncSession = Depends(db_session)):
    return await delete_ptz_schedule(schedule_id, db)

@router.get("/schedules/{ptz_schedule_id}")
async def get_ptz_schedule_alias2(ptz_schedule_id: int, db: AsyncSession = Depends(db_session)):
    return await get_ptz_schedule(ptz_schedule_id, db)

@router.delete("/schedules/{ptz_schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ptz_schedule_alias2(ptz_schedule_id: int, db: AsyncSession = Depends(db_session)):
    return await delete_ptz_schedule(ptz_schedule_id, db)


@router.post("/schedules/run")
async def run_ptz_schedules(db: AsyncSession = Depends(db_session)):
    result = await execute_due_ptz_schedules(db=db)
    return result
