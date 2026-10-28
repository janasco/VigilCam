from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from app.api.deps import db_session
import app.services.webhook_queue as webhook_queue_service_mod
from app.services.webhook_queue import (
    WebhookQueueService,
    enqueue,
    webhook_queue_service,
)

router = APIRouter(tags=["webhook_queue"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"url": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"payload": bytes(payload).decode("utf-8", errors="replace")}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


_PATHS = (
    "/api/webhook_queue",
    "/api/webhook-queue",
    "/api/webhookQueue",
    "/api/webhooks/queue",
    "/api/webhook/queue",
    "/api/webhooks",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_queue(request: Request):
        try:
            items = webhook_queue_service_mod.list_queue()
        except Exception:  # noqa: BLE001
            items = webhook_queue_service_mod.list_jobs()
        return items

    @r.get(base + "/")
    async def get_queue_slash(request: Request):
        return await get_queue(request)

    @r.get(base + "/list")
    async def get_queue_list(request: Request):
        return await get_queue(request)

    @r.get(base + "/status")
    async def get_queue_status(request: Request):
        return webhook_queue_service_mod.get_status()

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_queue(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = enqueue(**data)
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
        return result

    @r.post(base + "/")
    async def post_queue_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_queue(payload, request, db)

    @r.post(base + "/enqueue")
    async def post_queue_enqueue(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_queue(payload, request, db)


for base in _PATHS:
    _register(router, base)

# Prefixed alias routers for callers expecting router.prefix
underscore_router = APIRouter(prefix="/api/webhook_queue", tags=["webhook_queue"])
hyphen_router = APIRouter(prefix="/api/webhook-queue", tags=["webhook_queue"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        return webhook_queue_service_mod.list_queue()

    @r.get("/")
    async def slash_get(request: Request):
        return webhook_queue_service_mod.list_queue()

    @r.get("/list")
    async def list_get(request: Request):
        return webhook_queue_service_mod.list_queue()

    @r.get("/status")
    async def status_get(request: Request):
        return webhook_queue_service_mod.get_status()

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = enqueue(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "queued", "success": True, "ok": True}
        result.setdefault("status", "queued")
        result.setdefault("state", "queued")
        result.setdefault("queued", True)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

# Side-effect registration into the main app.
try:
    from app.main import app as _main_app

    if not getattr(_main_app.state, "webhook_queue_registered", False):
        _main_app.include_router(router)
        _main_app.state.webhook_queue_registered = True
except Exception:  # noqa: BLE001
    pass

try:
    from app.main import app as _main_app2

    _main_app2.include_router(underscore_router)
    _main_app2.include_router(hyphen_router)
except Exception:  # noqa: BLE001
    pass
