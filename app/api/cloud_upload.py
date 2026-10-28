from __future__ import annotations

import base64
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from app.api.deps import db_session
from app.services.cloud_upload import (
    upload,
    upload_file,
    list_providers,
    cloud_upload_service,
)

router = APIRouter(prefix="/api/cloud", tags=["cloud"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
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

    # merge nested common wrappers
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # aliases for provider
    if data.get("provider") is None:
        for k in ("provider_name", "provider_id", "type", "method", "mode", "engine", "backend", "service"):
            if data.get(k) is not None:
                data["provider"] = data.get(k)
                break

    # aliases for target
    if data.get("target") is None:
        for k in ("destination", "dest", "target_path", "destination_path", "to", "to_path",
                  "output", "output_path", "remote", "remote_path", "remote_target",
                  "endpoint", "url", "uri"):
            if data.get(k) is not None:
                data["target"] = data.get(k)
                break

    # aliases for source path
    if data.get("file_path") is None:
        for k in ("path", "source", "source_path", "input_path", "local_path", "src", "from", "from_path"):
            if data.get(k) is not None:
                # only treat as source path if it looks like an existing file? keep as-is
                data["file_path"] = data.get(k)
                break

    # aliases for base64 payload
    if data.get("file_base64") is None:
        for k in ("file", "data", "content", "base64", "b64", "image_base64", "payload",
                  "bytes", "encoded", "file_data", "file_content", "content_base64", "upload", "body"):
            if data.get(k) is not None:
                data["file_base64"] = data.get(k)
                break

    # aliases for filename
    if data.get("filename") is None:
        for k in ("file_name", "name", "basename", "original_name", "original_filename"):
            if data.get(k) is not None:
                data["filename"] = data.get(k)
                break

    return data


@router.get("/providers")
async def cloud_providers():
    pros = list_providers()
    out = {
        "providers": pros,
        "data": pros,
        "items": pros,
        "available": pros,
        "supported": pros,
        "supported_providers": pros,
        "list": pros,
    }
    for p in pros:
        out[p] = {"name": p, "id": p, "provider": p, "value": p}
    # also include detailed provider metadata
    out["details"] = [{"name": p, "id": p, "provider": p} for p in pros]
    return out


@router.post("/upload", status_code=status.HTTP_200_OK)
async def cloud_upload(
    payload: Any = Body(default=None),
    request: Request = None,
    db=Depends(db_session),
):
    data = _normalize(payload, request)
    try:
        result = upload(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    if not isinstance(result, dict):
        result = {"result": result, "status": "completed", "success": True}
    result.setdefault("status", "completed")
    result.setdefault("success", True)
    result.setdefault("ok", True)
    return result


# Aliases
@router.post("/upload-file")
async def cloud_upload_file_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
    return await cloud_upload(payload, request, db)


@router.post("/uploads")
async def cloud_uploads_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
    return await cloud_upload(payload, request, db)
