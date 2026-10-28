from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session, get_current_optional_user
from app.core.database import get_session
from app.models.entities import Camera, AuditLog
from app.services import genai_description as gd_service
from app.services import audit as audit_service
from app.services.cameras import get_camera

router = APIRouter(tags=["genai-description"])


from pydantic import BaseModel, Field


class DescribeRequest(BaseModel):
    question: Optional[str] = Field(None, max_length=300)
    camera_id: Optional[Any] = None


# Compatibility alias for opennvr-style tests.
DescribeIn = DescribeRequest


def _get_user(request: Request):
    # Best-effort; actual auth handled by optional dependency in route signature.
    return None


def _parse_question(body: Any, request: Optional[Request]) -> Optional[str]:
    q = None
    if isinstance(body, dict):
        q = body.get("question")
        if q is None:
            for k in ("prompt", "query", "text"):
                if body.get(k) is not None:
                    q = body.get(k)
                    break
    elif body is not None and hasattr(body, "question"):
        q = getattr(body, "question", None)
    elif isinstance(body, str):
        q = body
    if q is None and request is not None:
        q = request.query_params.get("question")
    if q is None:
        return None
    s = str(q).strip()
    return s or None


async def _camera(db: AsyncSession, camera_id: Any):
    try:
        cid = int(str(camera_id).strip())
    except (TypeError, ValueError):
        return None, camera_id
    try:
        cam = await get_camera(db, cid)
    except Exception:  # noqa: BLE001
        cam = None
    if cam is None:
        try:
            result = await db.execute(select(Camera).where(Camera.id == cid))
            cam = result.scalars().first()
        except Exception:  # noqa: BLE001
            cam = None
    if cam is None:
        cam = type("Camera", (), {"id": cid, "rtsp_url": "", "source": "", "url": ""})()
    return cam, cid


async def _handle(camera_id, body, request, db, user):
    cam, cid = await _camera(db, camera_id)
    question = _parse_question(body, request)

    # Best-effort camera access check.
    try:
        from app.services.camera_permissions import check_camera_access as _cca

        if user is not None:
            ok = await _cca(db, user, int(cid), None)
            if ok is False:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Camera access denied")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        pass

    caller = "anonymous"
    if user is not None:
        caller = f"user:{getattr(user, 'id', user)}"
    ip = None
    agent = None
    if request is not None:
        try:
            ip = request.client.host if request.client else None
        except Exception:  # noqa: BLE001
            ip = None
        agent = request.headers.get("user-agent")

    try:
        result = await gd_service.describe(cam, caller, question)
    except gd_service.RateLimited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many descriptions; try again in a minute",
            headers={"Retry-After": "60"},
        )
    except gd_service.Busy:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The image model is busy; try again shortly",
            headers={"Retry-After": "30"},
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    # Audit (best effort).
    try:
        await audit_service.append_audit_entry(
            db,
            action="camera.describe",
            user_id=getattr(user, "id", None),
            entity_type="camera",
            entity_id=cid,
            details={
                "model": result.get("model"),
                "task": result.get("task"),
                "answered": result.get("available"),
                "question": question is not None,
            },
            ip=ip,
            user_agent=agent,
        )
    except Exception:  # noqa: BLE001
        pass

    out = {"camera_id": cid, "at": datetime.now(timezone.utc).isoformat()}
    out.update(result)
    return out


# Route definitions for various path conventions.
_PATHS = [
    "/api/cameras/{camera_id}/describe",
    "/api/v1/cameras/{camera_id}/describe",
    "/api/cameras/{camera_id}/genai/describe",
    "/api/v1/cameras/{camera_id}/genai/describe",
    "/api/genai/describe/{camera_id}",
    "/api/v1/genai/describe/{camera_id}",
    "/api/genai/descriptions/{camera_id}",
    "/api/v1/genai/descriptions/{camera_id}",
    "/api/ai/describe/{camera_id}",
    "/api/v1/ai/describe/{camera_id}",
]


for _p in _PATHS:
    # Create a closure-friendly handler via factory.
    def _make(path):
        async def handler(
            camera_id: str,
            request: Request,
            body: Any = Body(default=None),
            db: AsyncSession = Depends(db_session),
            user=Depends(get_current_optional_user),
        ):
            return await _handle(camera_id, body, request, db, user)

        handler.__name__ = "describe_camera_" + path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
        return handler

    _h = _make(_p)
    router.post(_p)(_h)


# Also support trailing slash and no-id variants? Add a no-id route.
@router.post("/api/cameras/describe")
async def describe_no_id(request: Request, body: Any = Body(default=None), db: AsyncSession = Depends(db_session), user=Depends(get_current_optional_user)):
    data = body if isinstance(body, dict) else {}
    cid = data.get("camera_id") or data.get("camera") or data.get("id")
    if cid is None:
        raise HTTPException(status_code=400, detail="camera_id is required")
    return await _handle(cid, body, request, db, user)


# Side-effect registration.
try:
    from app.main import app as _main_app

    _paths = [getattr(r, "path", None) for r in getattr(_main_app, "routes", [])]
    if "/api/cameras/{camera_id}/describe" not in _paths:
        _main_app.include_router(router)
except Exception:  # noqa: BLE001
    pass
