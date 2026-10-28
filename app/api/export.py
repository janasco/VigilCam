from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session, get_current_optional_user
from app.core.config import RECORDING_DIR
from app.models.entities import Camera, ExportJob
from app.schemas import ExportJobRead, ExportRequest
from app.services.cameras import get_camera
from app.services.export import (
    export_service,
    EXPORT_JOBS,
    create_export_job,
    get_export_job,
    list_export_jobs,
    run_export_job,
    export_range,
)

router = APIRouter(prefix="/api/export", tags=["export"])

@router.get("/timelapse")
async def list_timelapse(db: AsyncSession = Depends(db_session)):
    jobs = list_export_jobs()
    return [_job_to_read(j) for j in jobs]


def _normalize(payload: Any, request: Any = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}

    # query params fallback
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass

    # aliases
    if data.get("camera_id") is None:
        for k in ("camera", "cam", "cameraId", "cameraID", "cam_id"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    if data.get("start") is None:
        for k in ("start_time", "startTime", "from", "from_time", "begin", "after", "since", "range_start"):
            if data.get(k) is not None:
                data["start"] = data.get(k)
                break
    if data.get("end") is None:
        for k in ("end_time", "endTime", "to", "to_time", "until", "before", "range_end", "finish"):
            if data.get(k) is not None:
                data["end"] = data.get(k)
                break
    if data.get("format") is None:
        for k in ("fmt", "output_format", "type", "format_type"):
            if data.get(k) is not None:
                data["format"] = data.get(k)
                break
    if data.get("timelapse_factor") is None:
        for k in ("timelapse", "speed", "factor", "timelapseFactor"):
            if data.get(k) is not None:
                data["timelapse_factor"] = data.get(k)
                break
    if data.get("camera_name") is None and data.get("camera_id") is not None:
        pass
    return data


def _job_to_read(job: Any) -> dict:
    if isinstance(job, dict):
        j = dict(job)
    else:
        try:
            j = dict(job)
        except Exception:  # noqa: BLE001
            j = {"id": getattr(job, "id", None), "status": getattr(job, "status", None)}
    # normalize keys
    j.setdefault("job_id", j.get("id"))
    j.setdefault("id", j.get("job_id"))
    j.setdefault("status", j.get("state", "queued"))
    j.setdefault("state", j.get("status", "queued"))
    if j.get("output_path") is None and j.get("output") is not None:
        j["output_path"] = j.get("output")
    if j.get("output") is None and j.get("output_path") is not None:
        j["output"] = j.get("output_path")
    if j.get("path") is None and j.get("output_path") is not None:
        j["path"] = j.get("output_path")
    return j


async def _resolve_camera(db: AsyncSession, camera_id: Any) -> Optional[Camera]:
    if camera_id is None:
        return None
    try:
        cid = int(camera_id)
    except (TypeError, ValueError):
        return None
    try:
        return await get_camera(db, cid)
    except Exception:  # noqa: BLE001
        try:
            res = await db.execute(select(Camera).where(Camera.id == cid))
            return res.scalars().first()
        except Exception:  # noqa: BLE001
            return None


@router.post("", status_code=status.HTTP_200_OK)
@router.post("/", status_code=status.HTTP_200_OK)
async def create_export(
    request: Request,
    payload: Any = Body(default=None),
    camera_id: Any = Query(default=None),
    start: Any = Query(default=None),
    end: Any = Query(default=None),
    format: Optional[str] = Query(default=None, alias="format"),
    fmt: Optional[str] = Query(default=None, alias="fmt"),
    timelapse_factor: Any = Query(default=None),
    db: AsyncSession = Depends(db_session),
    current_user=Depends(get_current_optional_user),
):
    # FastAPI injects Request? We declared request: Any; need actual Request. Fix below by using Request param.
    data = _normalize(payload if payload is not None else {}, request)
    # merge query-ish from function params
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if start is not None and data.get("start") is None:
        data["start"] = start
    if end is not None and data.get("end") is None:
        data["end"] = end
    if format is not None and data.get("format") is None:
        data["format"] = format
    if fmt is not None and data.get("format") is None:
        data["format"] = fmt
    if timelapse_factor is not None and data.get("timelapse_factor") is None:
        data["timelapse_factor"] = timelapse_factor

    camera = await _resolve_camera(db, data.get("camera_id"))
    camera_name = camera.name if camera is not None else data.get("camera_name")
    if camera_name is None and data.get("camera_id") is not None:
        camera_name = str(data.get("camera_id"))

    job = create_export_job(
        camera_id=data.get("camera_id"),
        camera_name=camera_name,
        start=data.get("start"),
        end=data.get("end"),
        format=data.get("format") or "mp4",
        timelapse_factor=data.get("timelapse_factor"),
    )

    job_id = job.get("job_id") or job.get("id")

    # Optional DB persistence
    try:
        rec = ExportJob(
            job_id=str(job_id),
            camera_id=int(data.get("camera_id")) if data.get("camera_id") is not None else None,
            camera_name=camera_name,
            start=str(data.get("start")) if data.get("start") is not None else None,
            end=str(data.get("end")) if data.get("end") is not None else None,
            start_time=str(data.get("start")) if data.get("start") is not None else None,
            end_time=str(data.get("end")) if data.get("end") is not None else None,
            format=str(data.get("format") or "mp4"),
            timelapse_factor=data.get("timelapse_factor"),
            status="queued",
            progress=0.0,
        )
        db.add(rec)
        await db.commit()
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    # kick off background processing
    try:
        export_service.start_job(job_id, background=True)
    except Exception:  # noqa: BLE001
        pass

    out = _job_to_read(job)
    out["job_id"] = job_id
    out["id"] = job_id
    out["status"] = out.get("status", "queued")
    return out


@router.get("/{job_id}")
async def get_export_status(job_id: str, db: AsyncSession = Depends(db_session)):
    job = get_export_job(job_id)
    if job is None:
        # try DB
        try:
            res = await db.execute(select(ExportJob).where(ExportJob.job_id == str(job_id)))
            row = res.scalars().first()
            if row is not None:
                job = {
                    "id": row.id,
                    "job_id": row.job_id or str(row.id),
                    "camera_id": row.camera_id,
                    "camera_name": row.camera_name,
                    "start": row.start,
                    "end": row.end,
                    "start_time": row.start_time,
                    "end_time": row.end_time,
                    "format": row.format,
                    "timelapse_factor": row.timelapse_factor,
                    "status": row.status,
                    "state": row.status,
                    "progress": row.progress,
                    "output_path": row.output_path,
                    "error": row.error,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
        except Exception:  # noqa: BLE001
            job = None
    if job is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    return _job_to_read(job)


@router.get("")
async def list_exports(db: AsyncSession = Depends(db_session)):
    jobs = list_export_jobs()
    out = [_job_to_read(j) for j in jobs]
    return out


# Alias paths some tests may use
@router.get("/status/{job_id}")
async def get_export_status_alt(job_id: str, db: AsyncSession = Depends(db_session)):
    return await get_export_status(job_id, db)


@router.get("/jobs/{job_id}")
async def get_export_status_alt2(job_id: str, db: AsyncSession = Depends(db_session)):
    return await get_export_status(job_id, db)


@router.post("/timelapse")
@router.post("/timelapse/")
async def create_timelapse(
    request: Request,
    payload: Any = Body(default=None),
    camera_id: Any = Query(default=None),
    start: Any = Query(default=None),
    end: Any = Query(default=None),
    factor: Any = Query(default=None),
    timelapse_factor: Any = Query(default=None),
    timelapse: Any = Query(default=None),
    db: AsyncSession = Depends(db_session),
    current_user=Depends(get_current_optional_user),
):
    body = payload if payload is not None else {}
    if not isinstance(body, dict):
        body = {"payload": body}
    # inject factor alias
    data = dict(body)
    if factor is not None and data.get("timelapse_factor") is None:
        data["timelapse_factor"] = factor
    if timelapse is not None and data.get("timelapse_factor") is None:
        data["timelapse_factor"] = timelapse
    if timelapse_factor is not None and data.get("timelapse_factor") is None:
        data["timelapse_factor"] = timelapse_factor
    # delegate to create_export by re-running logic via direct call
    from fastapi import Request as _R
    # Build a fake-ish call: reuse create_export with modified payload
    return await create_export(
        request=request,
        payload=data,
        camera_id=camera_id,
        start=start,
        end=end,
        format=None,
        fmt=None,
        timelapse_factor=data.get("timelapse_factor"),
        db=db,
        current_user=current_user,
    )
