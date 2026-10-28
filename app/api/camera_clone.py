"""Camera clone API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.

Exposes ``POST /api/cameras/{camera_id}/clone``.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.schemas import CameraRead
from app.services.camera_clone import clone_camera

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


class CameraCloneRequest(BaseModel):
    name: Optional[str] = None

    model_config = {"extra": "allow"}


def _resolve_name(
    payload: Any,
    request: Optional[Request] = None,
) -> Optional[str]:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, BaseModel):
        try:
            data = dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        data = {}
    # Flatten common nested wrappers.
    for nested_key in ("payload", "data", "params", "options", "config", "body"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    name = data.get("name")
    if name is None:
        for alias in ("new_name", "clone_name", "title", "label"):
            if data.get(alias) is not None:
                name = data.get(alias)
                break
    if name is not None:
        name = str(name).strip() or None
    if name is None and request is not None:
        try:
            qp = request.query_params
            for alias in ("name", "new_name", "clone_name", "title"):
                if alias in qp and str(qp.get(alias)).strip():
                    name = str(qp.get(alias)).strip()
                    break
        except Exception:  # noqa: BLE001
            pass
    return name


@router.post(
    "/{camera_id}/clone",
    response_model=CameraRead,
    status_code=status.HTTP_201_CREATED,
    summary="Clone a camera",
)
async def api_clone_camera(
    camera_id: int,
    request: Request,
    payload: Optional[CameraCloneRequest] = Body(default=None),
    db: AsyncSession = Depends(db_session),
):
    name = _resolve_name(payload, request)
    cloned = await clone_camera(db, camera_id, name=name)
    if cloned is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    try:
        from app.services.event_bus import event_bus

        await event_bus.publish(
            "camera_cloned",
            {
                "camera_id": cloned.id,
                "source_camera_id": camera_id,
                "name": cloned.name,
                "kind": cloned.kind,
                "source": cloned.source,
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return cloned


# Compatibility aliases.
camera_clone_router = router
clone_router = router
api_router = router

__all__ = ["router"]
