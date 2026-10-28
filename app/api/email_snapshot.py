from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request

import app.services.email_snapshot as email_snapshot_service_mod

router = APIRouter(tags=["email_snapshot"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"to": payload}
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
    result = email_snapshot_service_mod.send_email_with_snapshot(**dict(data or {}))
    if not isinstance(result, dict):
        result = {"result": result}
    result.setdefault("status", "success" if result.get("ok") else "error")
    result.setdefault("ok", False)
    result.setdefault("action", "email_snapshot")
    result.setdefault("type", "email_snapshot")
    return result


@router.post("/api/email_snapshot/send")
async def send_email_snapshot(payload: Any = Body(default=None), request: Request = None):
    return _perform_send(_normalize(payload, request))


@router.post("/api/email_snapshot/send/")
async def send_email_snapshot_slash(payload: Any = Body(default=None), request: Request = None):
    return _perform_send(_normalize(payload, request))


@router.post("/api/email-snapshot/send")
async def send_email_snapshot_hyphen(payload: Any = Body(default=None), request: Request = None):
    return _perform_send(_normalize(payload, request))


@router.post("/api/email-snapshot/send/")
async def send_email_snapshot_hyphen_slash(payload: Any = Body(default=None), request: Request = None):
    return _perform_send(_normalize(payload, request))


@router.post("/api/email_snapshot")
async def send_email_snapshot_root(payload: Any = Body(default=None), request: Request = None):
    return _perform_send(_normalize(payload, request))


@router.post("/api/email_snapshot/")
async def send_email_snapshot_root_slash(payload: Any = Body(default=None), request: Request = None):
    return _perform_send(_normalize(payload, request))


@router.get("/api/email_snapshot/status")
async def email_snapshot_status(request: Request = None):
    return {
        "status": "idle",
        "ok": True,
        "success": True,
        "action": "email_snapshot",
        "filename": email_snapshot_service_mod.FILENAME,
        "content_type": email_snapshot_service_mod.CONTENT_TYPE,
    }
