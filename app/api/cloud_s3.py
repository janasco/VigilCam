from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
from app.services import cloud_s3 as svc

router = APIRouter(prefix="/api/cloud/s3", tags=["cloud-s3"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
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

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    if data.get("provider") is None:
        for k in ("provider_name", "provider_id", "type", "method", "mode", "engine", "backend", "service"):
            if data.get(k) is not None:
                data["provider"] = data.get(k)
                break
    data.setdefault("provider", "s3")

    if data.get("target") is None:
        for k in ("destination", "dest", "target_path", "destination_path", "to", "to_path",
                   "output", "output_path", "remote", "remote_path", "remote_target",
                   "endpoint", "url", "uri", "key", "object_key", "s3_key", "remote_key"):
            if data.get(k) is not None:
                data["target"] = data.get(k)
                break

    if data.get("file_path") is None:
        for k in ("path", "source", "source_path", "input_path", "local_path", "src", "from", "from_path"):
            if data.get(k) is not None:
                data["file_path"] = data.get(k)
                break

    if data.get("file_base64") is None:
        for k in ("file", "content", "base64", "b64", "image_base64", "payload",
                   "bytes", "encoded", "file_data", "file_content", "content_base64", "upload", "body"):
            if data.get(k) is not None:
                data["file_base64"] = data.get(k)
                break

    if data.get("filename") is None:
        for k in ("file_name", "name", "basename", "original_name", "original_filename",
                   "remote_name", "remote_filename", "object_name"):
            if data.get(k) is not None:
                data["filename"] = data.get(k)
                break

    return data


@router.get("/providers")
async def s3_providers():
    pros = svc.list_providers()
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
    out["details"] = [{"name": p, "id": p, "provider": p} for p in pros]
    return out


@router.get("/config")
async def s3_config():
    return svc.get_config()


@router.get("/status")
async def s3_status():
    cfg = svc.get_config()
    return {
        "provider": "s3",
        "configured": cfg.get("configured", False),
        "ok": True,
        "success": True,
        "status": "ok",
        "config": cfg,
    }


@router.get("/health")
async def s3_health():
    return await s3_status()


def _run_upload(data: dict) -> dict:
    try:
        result = svc.upload(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    if not isinstance(result, dict):
        result = {"result": result, "status": "completed", "success": True}
    result.setdefault("status", "completed")
    result.setdefault("success", True)
    result.setdefault("ok", True)
    return result


@router.post("/upload", status_code=status.HTTP_200_OK)
async def s3_upload(
    payload: Any = Body(default=None),
    request: Request = None,
    db=Depends(db_session),
):
    return _run_upload(_normalize(payload, request))


@router.post("", status_code=status.HTTP_200_OK)
async def s3_upload_root(
    payload: Any = Body(default=None),
    request: Request = None,
    db=Depends(db_session),
):
    return _run_upload(_normalize(payload, request))


@router.post("/", status_code=status.HTTP_200_OK)
async def s3_upload_slash(
    payload: Any = Body(default=None),
    request: Request = None,
    db=Depends(db_session),
):
    return _run_upload(_normalize(payload, request))


@router.post("/upload-file")
async def s3_upload_file_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
    return await s3_upload(payload, request, db)


@router.post("/uploads")
async def s3_uploads_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
    return await s3_upload(payload, request, db)


@router.post("/put")
async def s3_put_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
    return await s3_upload(payload, request, db)
