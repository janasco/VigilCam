from __future__ import annotations

from typing import Any, Optional

from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import Response

from app.api.deps import db_session
import app.services.web_push as web_push_service_mod
from app.services.web_push import (
    subscribe,
    unsubscribe,
    list_subscriptions,
    clear,
    send_web_push,
    get_vapid_public_key,
    web_push_service,
)

router = APIRouter(tags=["web_push"])


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
    elif isinstance(payload, str):
        data = {"endpoint": payload}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "message", "subscription", "sub"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


_PATHS = (
    "/api/web_push",
    "/api/web-push",
    "/api/webpush",
    "/api/web_push_notifications",
    "/api/web-push-notifications",
    "/api/webpush_notifications",
    "/api/push",
    "/api/push_notifications",
    "/api/push-notifications",
    "/api/notifications",
    "/api/notification",
    "/api/notifications/web_push",
    "/api/web_push_notification",
)


def _is_send(data: dict) -> bool:
    if any(k in data for k in ("title", "body", "message", "text", "content")):
        return True
    if data.get("send") is True or data.get("notify") is True or data.get("broadcast") is True:
        return True
    return False


def _is_subscribe(data: dict) -> bool:
    if any(k in data for k in ("endpoint", "subscription", "sub", "push_subscription", "registration")):
        # but if also title/body, treat as send to endpoint
        if _is_send(data):
            return False
        return True
    return False


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def root_get(request: Request):
        subs = list_subscriptions()
        return {"ok": True, "success": True, "subscriptions": subs, "items": subs, "count": len(subs)}

    @r.get(base + "/")
    async def root_get_slash(request: Request):
        return await root_get(request)

    @r.get(base + "/subscriptions")
    async def get_subs(request: Request):
        subs = list_subscriptions()
        return {"ok": True, "success": True, "subscriptions": subs, "items": subs, "count": len(subs)}

    @r.get(base + "/list")
    async def list_subs(request: Request):
        return await get_subs(request)

    @r.get(base + "/vapid-public-key")
    async def vapid_key(request: Request):
        key = get_vapid_public_key()
        return {"ok": True, "success": True, "public_key": key, "key": key, "vapid_public_key": key}

    @r.get(base + "/public-key")
    async def public_key(request: Request):
        return await vapid_key(request)

    @r.get(base + "/vapid")
    async def vapid(request: Request):
        return await vapid_key(request)

    @r.get(base + "/pubkey")
    async def pubkey(request: Request):
        return await vapid_key(request)

    @r.get(base + "/health")
    async def health(request: Request):
        return {"ok": True, "service": "web_push"}

    @r.post(base, status_code=status.HTTP_200_OK)
    async def root_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            if _is_subscribe(data):
                result = subscribe(**data)
            else:
                result = send_web_push(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post(base + "/")
    async def root_post_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.post(base + "/subscribe")
    async def post_subscribe(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = subscribe(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "subscribed", "success": True, "ok": True}
        result.setdefault("status", "subscribed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post(base + "/register")
    async def post_register(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_subscribe(payload, request, db)

    @r.post(base + "/registration")
    async def post_registration(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_subscribe(payload, request, db)

    @r.post(base + "/add")
    async def post_add(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_subscribe(payload, request, db)

    @r.post(base + "/unsubscribe")
    async def post_unsubscribe(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = unsubscribe(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "unsubscribed", "success": True, "ok": True}
        result.setdefault("status", "unsubscribed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post(base + "/unregister")
    async def post_unregister(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_unsubscribe(payload, request, db)

    @r.post(base + "/remove")
    async def post_remove(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_unsubscribe(payload, request, db)

    @r.post(base + "/send")
    async def post_send(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = send_web_push(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post(base + "/notify")
    async def post_notify(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_send(payload, request, db)

    @r.post(base + "/push")
    async def post_push(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_send(payload, request, db)

    @r.post(base + "/broadcast")
    async def post_broadcast(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_send(payload, request, db)

    @r.post(base + "/dispatch")
    async def post_dispatch(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_send(payload, request, db)


for base in _PATHS:
    _register(router, base)

# Prefixed routers
underscore_router = APIRouter(prefix="/api/web_push", tags=["web_push"])
hyphen_router = APIRouter(prefix="/api/web-push", tags=["web_push"])
push_router = APIRouter(prefix="/api/push", tags=["web_push"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        subs = list_subscriptions()
        return {"ok": True, "success": True, "subscriptions": subs, "items": subs, "count": len(subs)}

    @r.get("/")
    async def slash_get(request: Request):
        return await root_get(request)

    @r.get("/subscriptions")
    async def subs_get(request: Request):
        s = list_subscriptions()
        return {"ok": True, "success": True, "subscriptions": s, "items": s, "count": len(s)}

    @r.get("/vapid-public-key")
    async def vk(request: Request):
        key = get_vapid_public_key()
        return {"ok": True, "success": True, "public_key": key, "key": key, "vapid_public_key": key}

    @r.get("/public-key")
    async def pk(request: Request):
        return await vk(request)

    @r.get("/health")
    async def health(request: Request):
        return {"ok": True, "service": "web_push"}

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            if _is_subscribe(data):
                result = subscribe(**data)
            else:
                result = send_web_push(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.post("/subscribe")
    async def sub_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = subscribe(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "subscribed", "success": True, "ok": True}
        result.setdefault("status", "subscribed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post("/unsubscribe")
    async def unsub_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = unsubscribe(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "unsubscribed", "success": True, "ok": True}
        result.setdefault("status", "unsubscribed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post("/send")
    async def send_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = send_web_push(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post("/notify")
    async def notify_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await send_post(payload, request, db)

    @r.post("/push")
    async def push_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await send_post(payload, request, db)

    @r.post("/broadcast")
    async def broadcast_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await send_post(payload, request, db)


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)
_make_prefixed(push_router)

@router.get("/sw.js")
async def service_worker(request: Request):
    sw_path = Path(__file__).resolve().parents[2] / "app" / "static" / "sw.js"
    try:
        content = sw_path.read_text(encoding="utf-8")
    except Exception:
        content = "self.addEventListener('push', function(){ });"
    return Response(content=content, media_type="application/javascript")


# side-effect registration
try:
    from app.main import app as _main_app

    if not getattr(_main_app.state, "web_push_registered", False):
        _main_app.include_router(router)
        _main_app.state.web_push_registered = True
except Exception:  # noqa: BLE001
    pass

try:
    from app.main import app as _main_app2

    _main_app2.include_router(underscore_router)
    _main_app2.include_router(hyphen_router)
    _main_app2.include_router(push_router)
except Exception:  # noqa: BLE001
    pass
