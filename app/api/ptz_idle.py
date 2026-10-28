"""PTZ idle home-return API endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, status

import app.services.ptz_idle as idle_mod
from app.services.ptz_idle import (
    check,
    clear,
    configure,
    get_status,
    list_configs,
    notify_activity,
    remove,
    return_home,
)

router = APIRouter(tags=["ptz_idle"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"camera_id": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"camera_id": bytes(payload).decode("utf-8", errors="replace")}
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
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "settings", "message", "idle", "ptz_idle"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _ok(result: Any, default_status: str = "ok") -> dict:
    if not isinstance(result, dict):
        result = {"result": result, "status": default_status, "ok": True, "success": True}
    result.setdefault("status", default_status)
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


_PATHS = (
    "/api/ptz_idle",
    "/api/ptz-idle",
    "/api/ptz/idle",
    "/api/ptz_idle_home",
    "/api/ptz-idle-home",
    "/api/ptz/idle_home",
    "/api/ptz/idle-home",
    "/api/idle_home",
    "/api/idle-home",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_idle(request: Request):
        data = dict(request.query_params)
        try:
            if data.get("camera_id") or data.get("camera") or data.get("id"):
                return idle_mod.get_status(**data)
            return idle_mod.list_configs(**data)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.get(base + "/")
    async def get_idle_slash(request: Request):
        return await get_idle(request)

    @r.get(base + "/list")
    async def get_idle_list(request: Request):
        return await get_idle(request)

    @r.get(base + "/status")
    async def get_idle_status(request: Request):
        data = dict(request.query_params)
        try:
            if data.get("camera_id") or data.get("camera") or data.get("id"):
                return idle_mod.get_status(**data)
            items = idle_mod.list_configs(**data)
            return {"items": items, "configs": items, "count": len(items),
                    "status": "ok", "ok": True, "success": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_idle(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        action = ""
        for k in ("action", "op", "operation"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                action = v.strip().lower()
                break
        try:
            if action in ("check", "tick", "process", "run", "poll"):
                result = check(**data)
            elif action in ("return", "return_home", "go_home", "gohome", "home"):
                result = return_home(**{k: v for k, v in data.items() if k not in ("action", "op", "operation")})
            elif action in ("activity", "notify", "notify_activity", "mark_active", "active", "seen", "heartbeat"):
                result = notify_activity(**data)
            elif action in ("status", "get"):
                result = get_status(**data)
            elif action in ("list",):
                return list_configs(**data)
            elif action in ("clear", "reset", "purge"):
                n = clear(**data)
                return {"cleared": n, "count": n, "ok": True, "success": True}
            elif action in ("delete", "remove"):
                result = remove(**data)
            elif action in ("disable",):
                result = idle_mod.disable(**data)
            elif action in ("enable",):
                result = idle_mod.enable(**data)
            elif data.get("camera_id") and len(data) <= 2 and data.get("home_preset") is None:
                result = get_status(**data)
            else:
                result = configure(**data)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if isinstance(result, list):
            return result
        return _ok(result, "configured" if action in ("", "create", "add", "configure") else "ok")

    @r.post(base + "/")
    async def post_idle_slash(payload: Any = Body(default=None), request: Request = None):
        return await post_idle(payload, request)

    @r.post(base + "/configure")
    async def post_configure(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(configure(**_normalize(payload, request)), "configured")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/activity")
    async def post_activity(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(notify_activity(**_normalize(payload, request)), "active")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/check")
    async def post_check(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(check(**_normalize(payload, request)), "completed")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/tick")
    async def post_tick(payload: Any = Body(default=None), request: Request = None):
        return await post_check(payload, request)

    @r.post(base + "/return")
    async def post_return(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(return_home(**_normalize(payload, request)), "returned_home")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/go_home")
    async def post_go_home(payload: Any = Body(default=None), request: Request = None):
        return await post_return(payload, request)

    @r.post(base + "/go-home")
    async def post_go_home_hyphen(payload: Any = Body(default=None), request: Request = None):
        return await post_return(payload, request)

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None):
        n = clear(**_normalize(payload, request))
        return {"cleared": n, "count": n, "removed": n, "ok": True, "success": True}

    @r.delete(base + "/{camera_id}")
    async def delete_idle_by_id(camera_id: str):
        return _ok(remove(camera_id), "deleted")

    @r.get(base + "/{camera_id}")
    async def get_idle_by_id(camera_id: str):
        try:
            return idle_mod.get_status(camera_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))


for _base in _PATHS:
    _register(router, _base)


underscore_router = APIRouter(prefix="/api/ptz_idle", tags=["ptz_idle"])
hyphen_router = APIRouter(prefix="/api/ptz-idle", tags=["ptz_idle"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        data = dict(request.query_params)
        if data.get("camera_id") or data.get("camera") or data.get("id"):
            try:
                return idle_mod.get_status(**data)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
        return idle_mod.list_configs(**data)

    @r.get("/")
    async def slash_get(request: Request):
        return await root_get(request)

    @r.get("/list")
    async def list_get(request: Request):
        return await root_get(request)

    @r.get("/status")
    async def status_get(request: Request):
        data = dict(request.query_params)
        if data.get("camera_id") or data.get("camera") or data.get("id"):
            try:
                return idle_mod.get_status(**data)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
        items = idle_mod.list_configs(**data)
        return {"items": items, "count": len(items), "status": "ok", "ok": True, "success": True}

    @r.get("/{camera_id}")
    async def by_id_get(camera_id: str):
        try:
            return idle_mod.get_status(camera_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(configure(**_normalize(payload, request)), "configured")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.post("/configure")
    async def configure_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.post("/activity")
    async def activity_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(notify_activity(**_normalize(payload, request)), "active")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/check")
    async def check_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(check(**_normalize(payload, request)), "completed")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/tick")
    async def tick_post(payload: Any = Body(default=None), request: Request = None):
        return await check_post(payload, request)

    @r.post("/return")
    async def return_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(return_home(**_normalize(payload, request)), "returned_home")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/clear")
    async def clear_post(payload: Any = Body(default=None), request: Request = None):
        n = clear(**_normalize(payload, request))
        return {"cleared": n, "count": n, "ok": True, "success": True}

    @r.delete("/{camera_id}")
    async def by_id_delete(camera_id: str):
        return _ok(remove(camera_id), "deleted")


for _r in (underscore_router, hyphen_router):
    _make_prefixed(_r)
