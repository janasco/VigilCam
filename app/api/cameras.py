from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session, check_camera_access, can_access_camera, camera_access_guard, require_camera_access
from app.schemas import CameraCreate, CameraRead, DetectionRequest, DetectionResult, EventRead
from app.drivers.capabilities import probe_and_store
from app.services import camera_onboarding
from app.services.cameras import (
    create_camera,
    delete_camera,
    get_camera,
    get_camera_by_slug,
    list_cameras,
    recent_events,
    record_event,
    simulate_detection,
    update_camera,
)

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

# Camera health endpoints are registered early so the static "/health" path
# is not swallowed by the "/{camera_id}" integer route.
async def _api_cameras_health(db):
    from app.services import camera_health as _ch

    cams = await list_cameras(db)
    out = []
    for cam in cams:
        try:
            st = _ch.get_health(cam.id, create=True)
            _ch.evaluate(st)
            out.append(_ch.serialize(st))
        except Exception:  # noqa: BLE001
            out.append(
                {
                    "camera_id": cam.id,
                    "online": False,
                    "status": "offline",
                    "healthy": False,
                    "last_frame": None,
                    "reconnect_attempts": 0,
                    "backoff_seconds": 0.0,
                }
            )
    return out


@router.get("/health")
async def api_cameras_health(db: AsyncSession = Depends(db_session)):
    return await _api_cameras_health(db)

# Camera access guard helpers (Wave 21) re-exported for convenience.
check_camera_permission = check_camera_access
has_camera_access = can_access_camera


def _camera_to_read(camera):
    return CameraRead.model_validate(camera)


async def _resolve_camera(db: AsyncSession, ident: str):
    try:
        cid = int(ident)
    except (TypeError, ValueError):
        cid = None
    if cid is not None:
        cam = await get_camera(db, cid)
        if cam is not None:
            return cam
    return await get_camera_by_slug(db, ident)


@router.get("/by-slug/{slug}", response_model=CameraRead)
async def api_get_camera_by_slug(slug: str, db: AsyncSession = Depends(db_session)):
    camera = await get_camera_by_slug(db, slug)
    if not camera:
        # also allow numeric id through this path
        camera = await _resolve_camera(db, slug)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return camera


@router.patch("/by-slug/{slug}", response_model=CameraRead)
async def api_update_camera_by_slug(slug: str, payload: dict = Body(...), db: AsyncSession = Depends(db_session)):
    camera = await get_camera_by_slug(db, slug)
    if not camera:
        camera = await _resolve_camera(db, slug)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    updated = await update_camera(db, camera.id, payload)
    return updated


@router.delete("/by-slug/{slug}", status_code=status.HTTP_200_OK)
async def api_delete_camera_by_slug(slug: str, db: AsyncSession = Depends(db_session)):
    camera = await get_camera_by_slug(db, slug)
    if not camera:
        camera = await _resolve_camera(db, slug)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    ok = await delete_camera(db, camera.id)
    return {"deleted": camera.id, "slug": slug, "ok": ok}


@router.post("/by-slug/{slug}/detect", response_model=DetectionResult)
async def api_detect_camera_by_slug(slug: str, req: DetectionRequest, db: AsyncSession = Depends(db_session)):
    camera = await get_camera_by_slug(db, slug)
    if not camera:
        camera = await _resolve_camera(db, slug)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    result = simulate_detection(camera_id=camera.id)
    for d in result["detections"]:
        await record_event(db, camera.id, d["label"], d["confidence"])
    return DetectionResult(**result)
verify_camera_access = can_access_camera


@router.get("", response_model=list[CameraRead])
async def api_list_cameras(db: AsyncSession = Depends(db_session)):
    return await list_cameras(db)


@router.get("/activity", response_model=list[CameraRead])
async def api_camera_activity(request: Request, db: AsyncSession = Depends(db_session)):
    alias_keys = [
        "seconds", "within", "window", "n", "threshold", "max_age", "age",
        "duration", "limit_seconds", "activity_window", "active_within",
        "since_seconds", "max_age_seconds", "within_seconds", "since",
        "n_seconds", "window_seconds", "threshold_seconds", "seconds_ago",
        "recent_seconds", "time_window", "timewindow",
    ]
    seconds = None
    for key in alias_keys:
        if key in request.query_params:
            seconds = request.query_params.get(key)
            break
    # case-insensitive
    if seconds is None:
        qp_lower = {k.lower(): k for k in request.query_params}
        for key in alias_keys:
            if key in qp_lower:
                seconds = request.query_params.get(qp_lower[key])
                break
    if seconds is None:
        seconds = request.query_params.get("sec") or request.query_params.get("s")
    if seconds is None:
        seconds = 60
    try:
        window = float(seconds)
    except (TypeError, ValueError):
        window = 60.0
    if window < 0:
        window = 0.0

    from app.services.activity import get_active_cameras, active_camera_ids

    cams = await get_active_cameras(db, seconds=window)
    inmem = set(active_camera_ids(window))
    for cam in cams:
        try:
            cam.active = True
        except Exception:  # noqa: BLE001
            pass
        if getattr(cam, "id", None) in inmem:
            # ensure row timestamp reflects activity if missing
            if getattr(cam, "last_activity_at", None) is None:
                from datetime import datetime

                cam.last_activity_at = datetime.utcnow()
    return cams


@router.post("", response_model=CameraRead, status_code=status.HTTP_201_CREATED)
async def api_create_camera(payload: CameraCreate, db: AsyncSession = Depends(db_session)):
    camera = await create_camera(db, payload)
    try:
        from app.services.event_bus import event_bus

        await event_bus.publish(
            "camera_created",
            {
                "camera_id": camera.id,
                "name": camera.name,
                "kind": camera.kind,
                "source": camera.source,
                "ip_address": camera.ip_address,
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return camera



from app.services import sources as _camera_sources_service

def _camera_source_presets_list():
    return _camera_sources_service.list_sources()

@router.get("/sources")
async def api_camera_sources():
    return _camera_source_presets_list()

@router.get("/source-presets")
async def api_camera_source_presets():
    return _camera_source_presets_list()

@router.get("/source_presets")
async def api_camera_source_presets_alt():
    return _camera_source_presets_list()

@router.get("/presets/sources")
async def api_presets_sources():
    return _camera_source_presets_list()

@router.get("/presets")
async def api_camera_presets():
    return _camera_source_presets_list()

@router.get("/camera-presets")
async def api_camera_presets2():
    return _camera_source_presets_list()

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


@router.delete("", status_code=status.HTTP_200_OK)
async def api_delete_cameras_bulk(ids: Optional[str] = Query(default=None), db: AsyncSession = Depends(db_session)):
    """Bulk delete. No ids = delete ALL cameras. ids = comma-separated list."""
    cams = await list_cameras(db)
    if ids:
        wanted = set()
        for part in ids.split(","):
            part = part.strip()
            if part.isdigit():
                wanted.add(int(part))
        targets = [c for c in cams if c.id in wanted]
    else:
        targets = list(cams)
    deleted = []
    for cam in targets:
        if await delete_camera(db, cam.id):
            deleted.append(cam.id)
    return {"deleted": deleted, "deleted_count": len(deleted)}


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
    # Mark per-camera activity on detection request (motion/ai worker signal).
    try:
        from app.services.activity import mark_camera_activity_db

        await mark_camera_activity_db(db, camera_id, None, motion=False)
    except Exception:
        pass
    result = simulate_detection(camera_id=camera_id)
    for d in result["detections"]:
        await record_event(db, camera_id, d["label"], d["confidence"])
    return DetectionResult(**result)


@router.post("/detect", response_model=DetectionResult)
async def api_detect_global(req: DetectionRequest, db: AsyncSession = Depends(db_session)):
    if req.camera_id is not None:
        try:
            from app.services.activity import mark_camera_activity_db

            await mark_camera_activity_db(db, req.camera_id, None, motion=False)
        except Exception:
            pass
    result = simulate_detection(camera_id=req.camera_id)
    for d in result["detections"]:
        await record_event(db, req.camera_id, d["label"], d["confidence"])
    return DetectionResult(**result)




@router.post("/{camera_id}/reprobe")
async def api_reprobe(camera_id: int, db: AsyncSession = Depends(db_session)):
    camera = await get_camera(db, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    try:
        cap = await probe_and_store(db, camera_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "camera_id": camera_id,
        "driver_name": cap.driver_name,
        "manufacturer": cap.manufacturer,
        "model": cap.model,
        "firmware_version": cap.firmware_version,
        "onvif_endpoints": cap.onvif_endpoints,
        "supported_areas": cap.supported_areas,
        "capabilities": cap.capabilities,
        "probe_result": cap.probe_result,
        "probe_error": cap.probe_error,
        "probed_at": cap.probed_at,
    }

class CameraProfilesRequest(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    port: Optional[int] = None
    scheme: Optional[str] = None
    ip: Optional[str] = None
    ip_address: Optional[str] = None
    onvif_host: Optional[str] = None




@router.post("/{camera_id}/profiles")
async def api_camera_profiles(
    camera_id: int,
    payload: Optional[CameraProfilesRequest] = Body(default=None),
    username: Optional[str] = Query(default=None),
    password: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(db_session),
):
    camera = await get_camera(db, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    req = payload or CameraProfilesRequest()
    user = username if username is not None else (req.username if req.username is not None else camera.username)
    pwd = password if password is not None else (req.password if req.password is not None else camera.password)
    port = req.port if req.port is not None else camera.onvif_port
    scheme = req.scheme if req.scheme is not None else camera.control_scheme

    ip = req.ip_address or req.ip or camera.ip_address
    if not ip:
        ip = camera_onboarding.derive_ip_address(camera.source, req.onvif_host or camera.onvif_host)
    if ip and not camera.ip_address:
        camera.ip_address = ip

    profiles = await camera_onboarding.get_camera_profiles(
        camera,
        db=db,
        username=user,
        password=pwd,
        port=port,
        scheme=scheme,
        ip=ip,
    )

    if ip and not camera.ip_address:
        camera.ip_address = ip
    await db.commit()
    await db.refresh(camera)

    return {
        "camera_id": camera_id,
        "ip_address": camera.ip_address,
        "profiles": profiles,
    }

@router.get("/events/recent", response_model=list[EventRead])
async def api_events(limit: int = 50, db: AsyncSession = Depends(db_session)):
    events = await recent_events(db, limit=limit)
    return events




@router.get("/{camera_id}/health")
async def api_camera_health(camera_id: int, db: AsyncSession = Depends(db_session)):
    from app.services import camera_health as _ch

    camera = await get_camera(db, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    st = _ch.get_health(camera_id, create=True)
    _ch.evaluate(st)
    return _ch.serialize(st)
