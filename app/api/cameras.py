from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.schemas import CameraCreate, CameraRead, DetectionRequest, DetectionResult, EventRead
from app.services.cameras import (
    create_camera,
    delete_camera,
    get_camera,
    list_cameras,
    recent_events,
    record_event,
    simulate_detection,
    update_camera,
)

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


@router.get("", response_model=list[CameraRead])
async def api_list_cameras(db: AsyncSession = Depends(db_session)):
    return await list_cameras(db)


@router.post("", response_model=CameraRead, status_code=status.HTTP_201_CREATED)
async def api_create_camera(payload: CameraCreate, db: AsyncSession = Depends(db_session)):
    return await create_camera(db, payload)


@router.get("/{camera_id}", response_model=CameraRead)
async def api_get_camera(camera_id: int, db: AsyncSession = Depends(db_session)):
    camera = await get_camera(db, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return camera


@router.patch("/{camera_id}", response_model=CameraRead)
async def api_update_camera(camera_id: int, payload: dict, db: AsyncSession = Depends(db_session)):
    camera = await update_camera(db, camera_id, payload)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return camera


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def api_delete_camera(camera_id: int, db: AsyncSession = Depends(db_session)):
    ok = await delete_camera(db, camera_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Camera not found")


@router.post("/{camera_id}/detect", response_model=DetectionResult)
async def api_detect(camera_id: int, req: DetectionRequest, db: AsyncSession = Depends(db_session)):
    camera = await get_camera(db, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    result = simulate_detection(camera_id=camera_id)
    for d in result["detections"]:
        await record_event(db, camera_id, d["label"], d["confidence"])
    return DetectionResult(**result)


@router.post("/detect", response_model=DetectionResult)
async def api_detect_global(req: DetectionRequest, db: AsyncSession = Depends(db_session)):
    result = simulate_detection(camera_id=req.camera_id)
    for d in result["detections"]:
        await record_event(db, req.camera_id, d["label"], d["confidence"])
    return DetectionResult(**result)


@router.get("/events/recent", response_model=list[EventRead])
async def api_events(limit: int = 50, db: AsyncSession = Depends(db_session)):
    events = await recent_events(db, limit=limit)
    return events
