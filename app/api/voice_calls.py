"""Voice call alerts API router (Twilio-style stub).

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
from app.services.voice_calls import (
    clear,
    get_call,
    list_calls,
    place_call,
    status as service_status,
    voice_calls_service,
)

router = APIRouter(tags=["voice_calls"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"message": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"message": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            data = {}
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

    for nested_key in ("payload", "options", "config", "params", "data",
                        "body", "request", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


_PATHS = (
    "/api/alerts/voice-call",
    "/api/alerts/voice-calls",
    "/api/alerts/voice_call",
    "/api/alerts/voice_calls",
    "/api/alerts/voice",
    "/api/alerts/call",
    "/api/alerts/calls",
    "/api/voice-calls",
    "/api/voice_calls",
    "/api/voice-call",
    "/api/voice_call",
    "/api/voice/calls",
    "/api/voice/call",
    "/api/calls/voice",
)


def _register(r: APIRouter, base: str) -> None:
    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_root(payload: Any = Body(default=None), request: Request = None,
                        db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = place_call(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post(base + "/")
    async def post_slash(payload: Any = Body(default=None), request: Request = None,
                         db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/send")
    async def post_send(payload: Any = Body(default=None), request: Request = None,
                        db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/place")
    async def post_place(payload: Any = Body(default=None), request: Request = None,
                         db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/dial")
    async def post_dial(payload: Any = Body(default=None), request: Request = None,
                        db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.get(base + "/health")
    async def get_health():
        return {"ok": True, "service": "voice_calls"}

    @r.get(base + "/status")
    async def get_status():
        return service_status()

    @r.get(base + "/list")
    async def get_list(request: Request):
        return {"ok": True, "success": True, "status": "ok",
                "items": list_calls(), "calls": list_calls(),
                "count": len(list_calls()), "total": len(list_calls())}

    @r.get(base + "/{call_sid}")
    async def get_one(call_sid: str):
        res = get_call(call_sid)
        if res is None:
            raise HTTPException(status_code=404, detail="call not found")
        return res


for _base in _PATHS:
    _register(router, _base)


# Compatibility aliases.
voice_calls_router = router
voice_call_router = router
calls_router = router
api_router = router

__all__ = ["router"]
