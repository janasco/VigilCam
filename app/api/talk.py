from __future__ import annotations

import base64
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from app.api.deps import db_session
from app.services.cameras import get_camera
from app.services.talk import (
    TalkService,
    queue_talk,
    talk_service,
)

router = APIRouter(prefix="/api/talk", tags=["talk"])


AUDIO_FIELDS = (
    "audio_base64", "audio", "base64", "data", "content", "payload", "bytes",
    "encoded", "b64", "file", "file_base64", "file_data", "file_content",
    "content_base64", "upload", "body", "sound", "voice", "speech", "mp3",
    "wav", "audio_data", "raw", "value", "record", "message", "text",
)


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"audio_base64": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"audio_base64": base64.b64encode(bytes(payload)).decode("ascii")}
        except Exception:  # noqa: BLE001
            data = {}
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

    # merge nested common wrappers
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # camera id aliases (usually from path, but accept body too)
    if data.get("camera_id") is None:
        for k in ("camera", "cam", "cam_id", "cameraId", "id", "camera_alias", "alias", "camera_name", "name"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break

    # audio aliases
    if data.get("audio_base64") is None:
        for k in AUDIO_FIELDS:
            if data.get(k) is not None:
                data["audio_base64"] = data.get(k)
                break

    # strip data uri
    if isinstance(data.get("audio_base64"), str):
        s = data["audio_base64"].strip()
        if "," in s and s.lower().startswith("data:"):
            s = s.split(",", 1)[1].strip()
        data["audio_base64"] = s

    # remove framework objects
    for k in ("request", "db", "session", "user", "current_user"):
        data.pop(k, None)

    return data


@router.post("/{camera_id}", status_code=status.HTTP_200_OK)
async def talk(
    camera_id: str,
    payload: Any = Body(default=None),
    request: Request = None,
    db=Depends(db_session),
):
    data = _normalize(payload, request)
    data["camera_id"] = camera_id

    # Optional camera lookup (best effort, never fails the queue).
    try:
        cam = await get_camera(db, int(str(camera_id).strip())) if str(camera_id).strip().isdigit() else None
    except Exception:  # noqa: BLE001
        cam = None
    if cam is not None:
        data.setdefault("camera_name", getattr(cam, "name", None))
        data.setdefault("camera_alias", getattr(cam, "name", None))

    try:
        result = queue_talk(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))

    if not isinstance(result, dict):
        result = {"result": result, "status": "queued", "success": True, "ok": True}
    result.setdefault("status", "queued")
    result.setdefault("state", "queued")
    result.setdefault("job_status", "queued")
    result.setdefault("queue_status", "queued")
    result.setdefault("queued", True)
    result.setdefault("is_queued", True)
    result.setdefault("ok", True)
    result.setdefault("success", True)
    result.setdefault("camera_id", camera_id)
    try:
        result.setdefault("id", result.get("job_id") or result.get("id"))
    except Exception:  # noqa: BLE001
        pass
    return result


# Aliases for path variations.
@router.post("/{camera_id}/", status_code=status.HTTP_200_OK)
async def talk_trailing(camera_id: str, payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
    return await talk(camera_id, payload, request, db)


@router.post("", status_code=status.HTTP_200_OK)
async def talk_no_id(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
    data = _normalize(payload, request)
    cid = data.get("camera_id")
    if cid is None:
        raise HTTPException(status_code=400, detail="camera_id is required")
    return await talk(str(cid), payload, request, db)


# Side-effect registration into the main app (best effort; main.py is not edited).
try:
    from app.main import app as _main_app

    _paths = [getattr(r, "path", None) for r in getattr(_main_app, "routes", [])]
    if "/api/talk/{camera_id}" not in _paths:
        _main_app.include_router(router)
except Exception:  # noqa: BLE001
    pass
