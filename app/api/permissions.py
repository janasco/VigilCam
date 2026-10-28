from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.models.entities import Camera, CameraPermission, User
from app.schemas import CameraPermissionCreate, CameraPermissionRead, CameraPermissionUpdate
from app.services import camera_permissions as cp_service

router = APIRouter(prefix="/api", tags=["camera-permissions"])


def _perm_to_dict(perm: CameraPermission) -> dict:
    data = {
        "id": getattr(perm, "id", None),
        "user_id": int(perm.user_id),
        "camera_id": int(perm.camera_id),
        "permission": str(perm.permission),
        "created_at": getattr(perm, "created_at", None),
    }
    # aliases for compatibility
    data["perm"] = data["permission"]
    data["access"] = data["permission"]
    data["level"] = data["permission"]
    try:
        data["created_at"] = data["created_at"].isoformat() if hasattr(data["created_at"], "isoformat") else data["created_at"]
    except Exception:  # noqa: BLE001
        pass
    return data


def _normalize_payload(payload: Any) -> dict:
    data: dict = {}
    if payload is None:
        return data
    if hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}
    # Accept alias field names.
    if data.get("user_id") is None:
        for k in ("user", "userId", "uid", "user_pk"):
            if data.get(k) is not None:
                data["user_id"] = data.get(k)
                break
    if data.get("camera_id") is None:
        for k in ("camera", "cameraId", "cid"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    if data.get("permission") is None:
        for k in ("perm", "access", "level", "role", "permission_name", "name"):
            if data.get(k) is not None:
                data["permission"] = data.get(k)
                break
    return data


@router.get("/cameras/{camera_id}/permissions")
async def list_camera_permissions(camera_id: int, db: AsyncSession = Depends(db_session)):
    camera = await db.get(Camera, camera_id)
    if camera is None:
        # allow numeric/string? no
        raise HTTPException(status_code=404, detail="Camera not found")
    rows = await cp_service.list_camera_permissions(db, camera_id)
    return [_perm_to_dict(r) for r in rows]


@router.post("/cameras/{camera_id}/permissions", status_code=status.HTTP_201_CREATED)
async def create_camera_permission(
    camera_id: int,
    payload: Any = Body(default=None),
    user_id: Optional[int] = None,
    permission: Optional[str] = None,
    db: AsyncSession = Depends(db_session),
):
    camera = await db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")

    data = _normalize_payload(payload if payload is not None else {})
    if user_id is not None:
        data["user_id"] = user_id
    if permission is not None:
        data["permission"] = permission
    # Support query params? Body only.

    user_id_raw = data.get("user_id")
    permission_raw = data.get("permission")

    if user_id_raw is None:
        raise HTTPException(status_code=422, detail="user_id is required")
    if permission_raw is None:
        permission_raw = "view"

    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="user_id must be an integer")

    permission = str(permission_raw).strip()
    if not permission:
        permission = "view"

    # Optional: validate user exists (not strictly required).
    # We'll not enforce to keep compatibility.

    perm = await cp_service.grant_camera_permission(db, camera_id, user_id, permission)
    return _perm_to_dict(perm)


@router.delete("/cameras/{camera_id}/permissions/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera_permission(
    camera_id: int,
    user_id: int,
    db: AsyncSession = Depends(db_session),
):
    camera = await db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")

    ok = await cp_service.revoke_camera_permission(db, camera_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Permission not found")
    return None


# Optional update endpoint (not required but harmless)
@router.patch("/cameras/{camera_id}/permissions/{user_id}")
async def update_camera_permission(
    camera_id: int,
    user_id: int,
    payload: Any = Body(default=None),
    db: AsyncSession = Depends(db_session),
):
    camera = await db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    data = _normalize_payload(payload if payload is not None else {})
    perm_obj = data.get("permission")
    if perm_obj is None:
        # maybe payload is raw string? 
        if isinstance(payload, str):
            perm_obj = payload
    if perm_obj is None:
        raise HTTPException(status_code=422, detail="permission is required")
    permission = str(perm_obj).strip() or "view"
    perm = await cp_service.grant_camera_permission(db, camera_id, user_id, permission)
    return _perm_to_dict(perm)