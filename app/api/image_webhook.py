from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request

import app.services.image_webhook as image_webhook_service_mod

router = APIRouter(tags=["image_webhook"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        s = payload.strip()
        if s.lower().startswith(("http://", "https://")):
            data = {"url": s}
        else:
            data = {"url": s}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        data = {"image_bytes": bytes(payload)}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


def _perform_send(data: dict) -> dict:
    result = image_webhook_service_mod.send_image_webhook(**dict(data or {}))
    if not isinstance(result, dict):
        result = {"result": result}
    result.setdefault("status", "success" if result.get("ok") else "error")
    result.setdefault("ok", False)
    result.setdefault("action", "image_webhook")
    result.setdefault("type", "image_webhook")
    return result


@router.post("/api/image_webhook/send")
async def send_image_webhook(payload: Any = Body(default=None), request: Request = None):
    return _perform_send(_normalize(payload, request))


@router.post("/api/image_webhook/send/")
async def send_image_webhook_slash(payload: Any = Body(default=None), request: Request = None):
    return _perform_send(_normalize(payload, request))


@router.post("/api/image-webhook/send")
async def send_image_webhook_hyphen(payload: Any = Body(default=None), request: Request = None):
    return _perform_send(_normalize(payload, request))


@router.post("/api/image-webhook/send/")
async def send_image_webhook_hyphen_slash(payload: Any = Body(default=None), request: Request = None):
    return _perform_send(_normalize(payload, request))


@router.post("/api/image_webhook")
async def send_image_webhook_root(payload: Any = Body(default=None), request: Request = None):
    return _perform_send(_normalize(payload, request))


@router.post("/api/image_webhook/")
async def send_image_webhook_root_slash(payload: Any = Body(default=None), request: Request = None):
    return _perform_send(_normalize(payload, request))


@router.get("/api/image_webhook/status")
async def image_webhook_status(request: Request = None):
    return {
        "status": "idle",
        "ok": True,
        "success": True,
        "action": "image_webhook",
        "field_name": image_webhook_service_mod.FIELD_NAME,
        "filename": image_webhook_service_mod.FILENAME,
        "content_type": image_webhook_service_mod.CONTENT_TYPE,
    }
