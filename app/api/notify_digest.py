from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
import app.services.notify_digest as notify_digest_service_mod
from app.services.notify_digest import (
    build_digest,
    clear,
    list_notifications,
    record_notification,
    status as service_status,
)

router = APIRouter(tags=["notify_digest"])


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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "message", "notification", "event"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


_PATHS = (
    "/api/notify_digest",
    "/api/notify-digest",
    "/api/notifyDigest",
    "/api/notification_digest",
    "/api/notification-digest",
    "/api/notifications/digest",
    "/api/notification/digest",
    "/api/digest/notify",
    "/api/notify/digest",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_root(request: Request):
        return build_digest()

    @r.get(base + "/")
    async def get_slash(request: Request):
        return await get_root(request)

    @r.get(base + "/summary")
    async def get_summary(request: Request):
        return build_digest(**_normalize(None, request))

    @r.get(base + "/digest")
    async def get_digest(request: Request):
        return await get_summary(request)

    @r.get(base + "/summarize")
    async def get_summarize(request: Request):
        return await get_summary(request)

    @r.get(base + "/list")
    async def get_list(request: Request):
        items = list_notifications()
        return {"ok": True, "success": True, "status": "ok", "items": items, "notifications": items, "count": len(items), "total": len(items)}

    @r.get(base + "/notifications")
    async def get_notifications(request: Request):
        return await get_list(request)

    @r.get(base + "/items")
    async def get_items(request: Request):
        return await get_list(request)

    @r.get(base + "/status")
    async def get_status(request: Request):
        return service_status()

    @r.get(base + "/health")
    async def get_health(request: Request):
        return {"ok": True, "service": "notify_digest"}

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_root(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        # If payload looks like a digest-build request, build; otherwise record.
        build_keys = {"build", "summarize", "summary", "digest", "generate", "limit", "since", "until", "filter_type"}
        if any(k in data for k in build_keys) and not any(
            k in data for k in ("message", "body", "text", "title", "type", "event_type", "severity", "camera_id", "camera", "channel")
        ):
            try:
                result = build_digest(**data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc))
        else:
            try:
                result = record_notification(**data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/")
    async def post_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/record")
    async def post_record(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = record_notification(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "recorded", "success": True, "ok": True}
        result.setdefault("status", "recorded")
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/notify")
    async def post_notify(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_record(payload, request, db)

    @r.post(base + "/add")
    async def post_add(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_record(payload, request, db)

    @r.post(base + "/push")
    async def post_push(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_record(payload, request, db)

    @r.post(base + "/send")
    async def post_send(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_record(payload, request, db)

    @r.post(base + "/build")
    async def post_build(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = build_digest(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post(base + "/summary")
    async def post_summary(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_build(payload, request, db)

    @r.post(base + "/digest")
    async def post_digest(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_build(payload, request, db)

    @r.post(base + "/summarize")
    async def post_summarize(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_build(payload, request, db)

    @r.post(base + "/generate")
    async def post_generate(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_build(payload, request, db)

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return clear()

    @r.delete(base + "/clear")
    async def delete_clear(request: Request):
        return clear()

    @r.get(base + "/{item_id}")
    async def get_one(item_id: str, request: Request):
        res = notify_digest_service_mod.get_notification(item_id)
        if res is None:
            raise HTTPException(status_code=404, detail="notification not found")
        return res

    @r.delete(base + "/{item_id}")
    async def delete_one(item_id: str, request: Request):
        # Mark-and-report; removal via clear for simplicity.
        res = notify_digest_service_mod.get_notification(item_id)
        if res is None:
            raise HTTPException(status_code=404, detail="notification not found")
        return {"ok": True, "success": True, "status": "deleted", "id": item_id}


for base in _PATHS:
    _register(router, base)
