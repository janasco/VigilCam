from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.models.entities import Camera
from app.services import camera_hwaccel as _svc

router = APIRouter(tags=["camera_hwaccel"])

# Helper to resolve service module (supports alias module names).
def _service():
    return _svc


def _camera_or_404(db, camera_id):
    cam = db.get(Camera, camera_id) if hasattr(db, "get") else None
    return cam


async def _get_camera(db: AsyncSession, camera_id: int):
    try:
        return await db.get(Camera, camera_id)
    except Exception:  # noqa: BLE001
        res = await db.execute(select(Camera).where(Camera.id == camera_id))
        return res.scalars().first()


def _parse_payload(payload: Any) -> dict:
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        try:
            flat = payload.model_dump()
        except Exception:  # noqa: BLE001
            flat = {}
        try:
            extra = getattr(payload, "model_extra", None) or {}
            if isinstance(extra, dict):
                flat.update(extra)
        except Exception:  # noqa: BLE001
            pass
        merged = dict(flat)
    elif isinstance(payload, dict):
        merged = dict(payload)
    else:
        merged = {}
    for key in ("settings", "hwaccel", "hardware", "encoder", "config", "data", "payload", "hwaccel_settings", "camera_hwaccel"):
        nested = merged.get(key)
        if isinstance(nested, dict):
            merged.update(nested)
    return merged


async def _handle_get(camera_id: int, db: AsyncSession):
    camera = await _get_camera(db, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    try:
        return _svc.get_config_dict(camera_id)
    except Exception:  # noqa: BLE001
        st = _svc.get(camera_id, create=True)
        return st.to_dict() if hasattr(st, "to_dict") else dict(st)


async def _handle_update(camera_id: int, payload: Any, db: AsyncSession):
    camera = await _get_camera(db, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    data = _parse_payload(payload)
    try:
        _svc.update(camera_id, data)
    except Exception:  # noqa: BLE001
        pass
    try:
        return _svc.get_config_dict(camera_id)
    except Exception:  # noqa: BLE001
        st = _svc.get(camera_id, create=True)
        return st.to_dict() if hasattr(st, "to_dict") else dict(st)


async def _handle_delete(camera_id: int, db: AsyncSession):
    camera = await _get_camera(db, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    try:
        _svc.remove(camera_id)
    except Exception:  # noqa: BLE001
        pass
    return {"camera_id": camera_id, "removed": True}


async def _handle_args(camera_id: int, db: AsyncSession):
    camera = await _get_camera(db, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    try:
        args = _svc.build_ffmpeg_hwaccel_args(camera_id)
    except Exception:  # noqa: BLE001
        args = []
    try:
        enc_args = _svc.build_ffmpeg_encoder_args(camera_id)
    except Exception:  # noqa: BLE001
        enc_args = []
    return {"camera_id": camera_id, "args": args, "hwaccel_args": args, "ffmpeg_args": args, "encoder_args": enc_args}


# Register many path variants.
_PATHS = [
    "/api/cameras/{camera_id}/hardware",
    "/api/cameras/{camera_id}/hardware_accel",
    "/api/cameras/{camera_id}/hwaccel",
    "/api/cameras/{camera_id}/hwaccels",
    "/api/cameras/{camera_id}/hw_accel",
    "/api/cameras/{camera_id}/hw-accel",
    "/api/cameras/{camera_id}/hwacceleration",
    "/api/cameras/{camera_id}/hwaccelerator",
    "/api/cameras/{camera_id}/hardware_acceleration",
    "/api/cameras/{camera_id}/hardware-acceleration",
    "/api/cameras/{camera_id}/hardware_accel",
    "/api/cameras/{camera_id}/hardware-accel",
    "/api/cameras/{camera_id}/accelerator",
    "/api/cameras/{camera_id}/accel",
    "/api/camera/{camera_id}/hwaccel",
    "/api/camera/{camera_id}/hardware_acceleration",
]

_SUB = [("", None), ("/settings", "settings"), ("/config", "config")]

for base in _PATHS:
    for suffix, _ in _SUB:
        path = base + suffix

        async def _get(camera_id: int, db: AsyncSession = Depends(db_session)):
            return await _handle_get(camera_id, db)

        async def _put(camera_id: int, payload: Any = Body(default=None), db: AsyncSession = Depends(db_session)):
            return await _handle_update(camera_id, payload, db)

        async def _post(camera_id: int, payload: Any = Body(default=None), db: AsyncSession = Depends(db_session)):
            return await _handle_update(camera_id, payload, db)

        # unique function names
        _get.__name__ = f"get_{base}_{suffix}".replace("/", "_").replace("{", "").replace("}", "").replace("-", "_")
        _put.__name__ = f"put_{base}_{suffix}".replace("/", "_").replace("{", "").replace("}", "").replace("-", "_")
        _post.__name__ = f"post_{base}_{suffix}".replace("/", "_").replace("{", "").replace("}", "").replace("-", "_")

        router.add_api_route(path, _get, methods=["GET"])
        router.add_api_route(path, _put, methods=["PUT"])
        router.add_api_route(path, _post, methods=["POST"])

# args endpoints
for base in _PATHS:
    for arg_path in ("/args", "/ffmpeg", "/ffmpeg_args", "/hwaccel_args", "/command"):
        path = base + arg_path

        async def _args(camera_id: int, db: AsyncSession = Depends(db_session)):
            return await _handle_args(camera_id, db)

        _args.__name__ = f"args_{base}_{arg_path}".replace("/", "_").replace("{", "").replace("}", "").replace("-", "_")
        router.add_api_route(path, _args, methods=["GET"])

# delete endpoints
for base in _PATHS:
    path = base

    async def _delete(camera_id: int, db: AsyncSession = Depends(db_session)):
        return await _handle_delete(camera_id, db)

    _delete.__name__ = f"delete_{base}".replace("/", "_").replace("{", "").replace("}", "").replace("-", "_")
    router.add_api_route(path, _delete, methods=["DELETE"])

# Global list endpoints
@router.get("/api/cameras/hwaccel")
@router.get("/api/cameras/hardware_acceleration")
@router.get("/api/cameras/hwaccels")
async def list_camera_hwaccels(db: AsyncSession = Depends(db_session)):
    try:
        return _svc.list_configs() if hasattr(_svc, "list_configs") else []
    except Exception:  # noqa: BLE001
        return []

# Also expose per-camera via query style? no.

# Router aliases for compatibility.
camera_hwaccel_router = router
hardware_acceleration_router = router
hwaccel_router = router
router_camera_hwaccel = router
