"""Admin broadcast / announcement banner API for VigilCam.

Serves the single global banner managed by :mod:`app.services.announce`.
Auto-registered by the ``pkgutil`` auto-loader in ``app/main.py``, so no
edits to ``app/main.py`` or ``app/templates/index.html`` are required.

Distinct from other "broadcast" endpoints (push delivery, talk audio,
event-bus fan-out, Agent ``/command/broadcast``): these routes only manage
the UI announcement banner.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

import app.services.announce as svc
from app.services.announce import (
    clear_announcement,
    disable,
    enable,
    get_announcement,
    get_status,
    is_active,
    list_announcements,
    set_announcement,
)

router = APIRouter(tags=["announce"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
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
            data = {"message": str(payload)}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass

    for nested_key in (
        "payload", "options", "config", "params", "data", "body",
        "request", "settings", "announcement", "banner", "notice",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _has_message(data: dict) -> bool:
    for k in (
        "message", "text", "body", "content", "announcement",
        "banner", "title", "msg", "notice", "description",
    ):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return True
        if v is not None and not isinstance(v, str):
            return True
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in ("message", "text", "body", "content", "title", "msg"):
        v = lower.get(k)
        if isinstance(v, str) and v.strip():
            return True
        if v is not None and not isinstance(v, str):
            return True
    return False


def _public_current() -> dict:
    st = get_status()
    return st


_PATHS = (
    "/api/announce",
    "/api/announcement",
    "/api/announcements",
    "/api/banner",
    "/api/banners",
)


def _register(r: APIRouter, base: str) -> None:
    tag = base.replace("/", "_")

    @r.get(base, operation_id=f"announce_root{tag}")
    async def root_get(request: Request):
        _normalize(None, request)
        return _public_current()

    @r.get(base + "/", operation_id=f"announce_root_slash{tag}")
    async def root_get_slash(request: Request):
        return await root_get(request)

    @r.get(base + "/current", operation_id=f"announce_current{tag}")
    async def current_get(request: Request):
        return _public_current()

    @r.get(base + "/status", operation_id=f"announce_status{tag}")
    async def status_get(request: Request):
        _normalize(None, request)
        return get_status()

    @r.get(base + "/active", operation_id=f"announce_active{tag}")
    async def active_get(request: Request):
        data = _normalize(None, request)
        active = is_active(**data)
        b = get_announcement()
        return {
            "ok": True,
            "success": True,
            "active": bool(active),
            "is_active": bool(active),
            "enabled": bool(b.get("enabled", True)) if b else False,
            "announcement": b,
            "banner": b,
            "message": b.get("message") if b else None,
            "level": b.get("level") if b else None,
        }

    @r.get(base + "/list", operation_id=f"announce_list{tag}")
    async def list_get(request: Request):
        _normalize(None, request)
        items = list_announcements()
        return {
            "ok": True,
            "success": True,
            "announcements": items,
            "banners": items,
            "items": items,
            "count": len(items),
        }

    @r.get(base + "/health", operation_id=f"announce_health{tag}")
    async def health_get():
        return {"ok": True, "success": True, "service": "announce"}

    @r.post(base, operation_id=f"announce_set{tag}")
    async def set_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not _has_message(data):
            raise HTTPException(status_code=400, detail="message is required")
        result = set_announcement(**data)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "message is required"))
        result.setdefault("success", True)
        return result

    @r.put(base, operation_id=f"announce_put{tag}")
    async def set_put(payload: Any = Body(default=None), request: Request = None):
        return await set_post(payload, request)

    @r.patch(base, operation_id=f"announce_patch{tag}")
    async def set_patch(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not _has_message(data) and get_announcement() is None:
            raise HTTPException(status_code=400, detail="message is required")
        if not _has_message(data):
            result = svc.update_announcement(**data)
        else:
            result = set_announcement(**data)
        if not result.get("ok", True):
            raise HTTPException(status_code=400, detail=result.get("error", "update failed"))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/clear", operation_id=f"announce_clear{tag}")
    async def clear_post(payload: Any = Body(default=None), request: Request = None):
        _normalize(payload, request)
        return clear_announcement()

    @r.post(base + "/dismiss", operation_id=f"announce_dismiss{tag}")
    async def dismiss_post(payload: Any = Body(default=None), request: Request = None):
        _normalize(payload, request)
        return clear_announcement()

    @r.delete(base + "/clear", operation_id=f"announce_clear_del{tag}")
    async def clear_delete(request: Request):
        _normalize(None, request)
        return clear_announcement()

    @r.delete(base, operation_id=f"announce_delete{tag}")
    async def delete_root(request: Request):
        _normalize(None, request)
        return clear_announcement()

    @r.post(base + "/enable", operation_id=f"announce_enable{tag}")
    async def enable_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if get_announcement() is None and _has_message(data):
            data["enabled"] = True
            return set_announcement(**data)
        result = enable()
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error", "no announcement is set"))
        return result

    @r.post(base + "/disable", operation_id=f"announce_disable{tag}")
    async def disable_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if get_announcement() is None and _has_message(data):
            data["enabled"] = False
            return set_announcement(**data)
        result = disable()
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error", "no announcement is set"))
        return result


for _base in _PATHS:
    _register(router, _base)


# Compatibility aliases.
announce_router = router
announcement_router = router
banner_router = router
api_router = router

__all__ = ["router", "announce_router", "announcement_router", "banner_router"]
