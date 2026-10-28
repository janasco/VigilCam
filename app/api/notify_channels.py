"""Notification channels config API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
import app.services.notify_channels as notify_channels_mod
from app.services.notify_channels import (
    clear,
    delete_channel,
    disable_channel,
    enable_channel,
    get_channel,
    list_channels,
    reset_channel,
    set_channel,
    status as service_status,
    update_channel,
)

router = APIRouter(tags=["notify_channels"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        s = payload.strip()
        if s:
            data = {"channel": s}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"channel": bytes(payload).decode("utf-8", errors="replace")}
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

    for nested_key in ("payload", "options", "settings", "params", "data",
                       "body", "request", "config", "configuration",
                       "channel_config", "channelConfig"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


def _channel_from_data(data: dict) -> Optional[str]:
    for key in ("channel", "channel_name", "channelName", "name", "id",
                "channel_id", "channelId", "type", "kind", "service"):
        if key in data and data.get(key) is not None:
            s = str(data.get(key)).strip()
            if s:
                return s
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in ("channel", "channel_name", "channelname", "name", "id",
                "channel_id", "channelid", "type", "kind"):
        if key in lower and lower[key] is not None:
            s = str(lower[key]).strip()
            if s:
                return s
    return None


_PATHS = (
    "/api/notify_channels",
    "/api/notify-channels",
    "/api/notifyChannels",
    "/api/notification_channels",
    "/api/notification-channels",
    "/api/notifications/channels",
    "/api/notification/channels",
    "/api/channels/notify",
    "/api/alerts/channels",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_root(request: Request):
        data = _normalize(None, request)
        ch = _channel_from_data(data)
        if ch is not None:
            res = get_channel(ch)
            if res is None:
                raise HTTPException(status_code=404, detail="channel not found")
            return res
        items = list_channels()
        return {"ok": True, "success": True, "status": "ok",
                "items": items, "channels": items, "configs": items,
                "count": len(items), "total": len(items)}

    @r.get(base + "/")
    async def get_slash(request: Request):
        return await get_root(request)

    @r.get(base + "/list")
    async def get_list(request: Request):
        return await get_root(request)

    @r.get(base + "/all")
    async def get_all(request: Request):
        return await get_root(request)

    @r.get(base + "/status")
    async def get_status(request: Request):
        return service_status()

    @r.get(base + "/health")
    async def get_health(request: Request):
        return {"ok": True, "service": "notify_channels"}

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_root(payload: Any = Body(default=None), request: Request = None,
                        db=Depends(db_session)):
        data = _normalize(payload, request)
        action = str(data.get("action", "")).lower()
        ch = _channel_from_data(data)
        if action in ("clear", "reset_all", "purge"):
            return clear()
        if action in ("enable", "activate", "on") and ch is not None:
            try:
                result = enable_channel(**data)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            result.setdefault("ok", True)
            result.setdefault("success", True)
            return result
        if action in ("disable", "deactivate", "off") and ch is not None:
            try:
                result = disable_channel(**data)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            result.setdefault("ok", True)
            result.setdefault("success", True)
            return result
        if ch is None:
            raise HTTPException(status_code=400, detail="channel is required")
        try:
            result = set_channel(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "configured", "success": True, "ok": True}
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/")
    async def post_slash(payload: Any = Body(default=None), request: Request = None,
                         db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/configure")
    async def post_configure(payload: Any = Body(default=None), request: Request = None,
                             db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/set")
    async def post_set(payload: Any = Body(default=None), request: Request = None,
                       db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/update")
    async def post_update(payload: Any = Body(default=None), request: Request = None,
                          db=Depends(db_session)):
        data = _normalize(payload, request)
        ch = _channel_from_data(data)
        if ch is None:
            raise HTTPException(status_code=400, detail="channel is required")
        try:
            result = update_channel(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None,
                         db=Depends(db_session)):
        return clear()

    @r.post(base + "/reset")
    async def post_reset(payload: Any = Body(default=None), request: Request = None,
                         db=Depends(db_session)):
        data = _normalize(payload, request)
        ch = _channel_from_data(data)
        if ch is None:
            return clear()
        return reset_channel(ch)

    @r.put(base + "/{channel}")
    async def put_one(channel: str, payload: Any = Body(default=None),
                      request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.setdefault("channel", channel)
        try:
            result = set_channel(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.patch(base + "/{channel}")
    async def patch_one(channel: str, payload: Any = Body(default=None),
                        request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.setdefault("channel", channel)
        try:
            result = update_channel(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/{channel}")
    async def post_one(channel: str, payload: Any = Body(default=None),
                       request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.setdefault("channel", channel)
        action = str(data.get("action", "")).lower()
        try:
            if action in ("enable", "activate", "on"):
                result = enable_channel(**data)
            elif action in ("disable", "deactivate", "off"):
                result = disable_channel(**data)
            elif action in ("reset",):
                result = reset_channel(channel)
            elif action in ("delete", "remove", "clear"):
                delete_channel(channel)
                return {"ok": True, "success": True, "status": "deleted",
                        "channel": channel, "id": channel}
            else:
                result = update_channel(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/{channel}/enable")
    async def post_enable(channel: str, payload: Any = Body(default=None),
                          request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.setdefault("channel", channel)
        result = enable_channel(**data)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/{channel}/disable")
    async def post_disable(channel: str, payload: Any = Body(default=None),
                           request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.setdefault("channel", channel)
        result = disable_channel(**data)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.delete(base + "/clear")
    async def delete_clear(request: Request):
        return clear()

    @r.delete(base + "/{channel}")
    async def delete_one(channel: str, request: Request):
        existed = delete_channel(channel)
        if not existed:
            # still return defaults view (channel resets to defaults)
            res = get_channel(channel)
            if res is None:
                raise HTTPException(status_code=404, detail="channel not found")
            return {"ok": True, "success": True, "status": "deleted",
                    "channel": channel, "id": channel, "config": res}
        return {"ok": True, "success": True, "status": "deleted",
                "channel": channel, "id": channel}

    @r.get(base + "/{channel}")
    async def get_one(channel: str, request: Request):
        res = get_channel(channel)
        if res is None:
            raise HTTPException(status_code=404, detail="channel not found")
        # merge query params as config view only; do not mutate
        return res


for base in _PATHS:
    _register(router, base)


# Compatibility aliases.
notify_channels_router = router
notification_channels_router = router
channels_router = router
api_router = router

__all__ = ["router"]
