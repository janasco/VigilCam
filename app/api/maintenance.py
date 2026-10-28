"""Maintenance windows API for VigilCam.

Calendar-based alert suppression per camera (see
:mod:`app.services.maintenance`). Distinct from rate-based alert throttling
and recording retention: these endpoints manage explicit downtime windows and
expose an ``is_suppressed`` gate for alert senders.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

import app.services.maintenance as svc
from app.services.maintenance import (
    clear,
    create_window,
    delete_window,
    disable,
    enable,
    get_active,
    get_status,
    get_window,
    is_suppressed,
    list_windows,
)

router = APIRouter(tags=["maintenance"])


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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "window", "maintenance"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _result(payload: Any, request: Request | None, fn) -> dict:
    data = _normalize(payload, request)
    result = fn(**data)
    if isinstance(result, bool):
        return {"ok": True, "success": True, "result": result, "suppressed": result, "is_suppressed": result}
    if isinstance(result, list):
        return {"ok": True, "success": True, "windows": result, "items": result, "count": len(result)}
    if not isinstance(result, dict):
        return {"ok": True, "success": True, "result": result}
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


_PATHS = (
    "/api/maintenance",
    "/api/maintenance-windows",
    "/api/maintenance_windows",
)


def _register(r: APIRouter, base: str) -> None:
    tag = base.replace("/", "_")

    @r.get(base, operation_id=f"maintenance_root{tag}")
    async def root_get(request: Request):
        data = _normalize(None, request)
        cams = data.get("camera_id") or data.get("camera")
        if cams is not None:
            items = list_windows(camera_id=cams)
        else:
            items = list_windows()
        return {"ok": True, "success": True, "windows": items, "items": items, "count": len(items)}

    @r.get(base + "/", operation_id=f"maintenance_root_slash{tag}")
    async def root_get_slash(request: Request):
        return await root_get(request)

    @r.get(base + "/windows", operation_id=f"maintenance_list{tag}")
    async def list_get(request: Request, camera_id: Optional[str] = Query(default=None)):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            qp = dict(request.query_params)
        except Exception:  # noqa: BLE001
            qp = {}
        params.update({k: v for k, v in qp.items() if k not in params})
        items = list_windows(**params)
        return {"ok": True, "success": True, "windows": items, "items": items, "count": len(items)}

    @r.post(base, operation_id=f"maintenance_create{tag}")
    async def create_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, create_window)

    @r.post(base + "/windows", operation_id=f"maintenance_create_win{tag}")
    async def create_win_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, create_window)

    @r.get(base + "/check", operation_id=f"maintenance_check_get{tag}")
    async def check_get(request: Request, camera_id: Optional[str] = Query(default=None)):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            qp = dict(request.query_params)
        except Exception:  # noqa: BLE001
            qp = {}
        params.update({k: v for k, v in qp.items() if k not in params})
        suppressed = is_suppressed(**params)
        return {
            "ok": True, "success": True,
            "camera_id": params.get("camera_id") or params.get("camera"),
            "suppressed": bool(suppressed), "is_suppressed": bool(suppressed),
            "active_windows": get_active(**params),
        }

    @r.post(base + "/check", operation_id=f"maintenance_check_post{tag}")
    async def check_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        suppressed = is_suppressed(**data)
        return {
            "ok": True, "success": True,
            "camera_id": data.get("camera_id") or data.get("camera"),
            "suppressed": bool(suppressed), "is_suppressed": bool(suppressed),
            "active_windows": get_active(**data),
        }

    @r.get(base + "/suppressed", operation_id=f"maintenance_suppressed{tag}")
    async def suppressed_get(request: Request, camera_id: Optional[str] = Query(default=None)):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            qp = dict(request.query_params)
        except Exception:  # noqa: BLE001
            qp = {}
        params.update({k: v for k, v in qp.items() if k not in params})
        return _result(params, None, get_status)

    @r.get(base + "/status", operation_id=f"maintenance_status{tag}")
    async def status_get(request: Request, camera_id: Optional[str] = Query(default=None)):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            qp = dict(request.query_params)
        except Exception:  # noqa: BLE001
            qp = {}
        params.update({k: v for k, v in qp.items() if k not in params})
        return _result(params, None, get_status)

    @r.get(base + "/active", operation_id=f"maintenance_active{tag}")
    async def active_get(request: Request, camera_id: Optional[str] = Query(default=None)):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            qp = dict(request.query_params)
        except Exception:  # noqa: BLE001
            qp = {}
        params.update({k: v for k, v in qp.items() if k not in params})
        items = get_active(**params)
        return {"ok": True, "success": True, "windows": items, "items": items, "count": len(items)}

    @r.post(base + "/clear", operation_id=f"maintenance_clear{tag}")
    async def clear_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, clear)

    @r.delete(base + "/clear", operation_id=f"maintenance_clear_del{tag}")
    async def clear_delete(request: Request):
        return _result(None, request, clear)

    @r.get(base + "/health", operation_id=f"maintenance_health{tag}")
    async def health_get():
        return {"ok": True, "success": True, "service": "maintenance"}

    @r.get(base + "/{window_id}", operation_id=f"maintenance_get_one{tag}")
    async def get_one(window_id: str):
        w = get_window(id=window_id)
        if w is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Maintenance window not found")
        w.setdefault("ok", True)
        w.setdefault("success", True)
        return w

    @r.delete(base + "/{window_id}", operation_id=f"maintenance_delete{tag}")
    async def delete_one(window_id: str):
        return delete_window(id=window_id)

    @r.post(base + "/{window_id}/enable", operation_id=f"maintenance_enable{tag}")
    async def enable_one(window_id: str, payload: Any = Body(default=None)):
        data = _normalize(payload, None)
        data["id"] = window_id
        return _result(data, None, enable)

    @r.post(base + "/{window_id}/disable", operation_id=f"maintenance_disable{tag}")
    async def disable_one(window_id: str, payload: Any = Body(default=None)):
        data = _normalize(payload, None)
        data["id"] = window_id
        return _result(data, None, disable)


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix
underscore_router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])
hyphen_router = APIRouter(prefix="/api/maintenance-windows", tags=["maintenance"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        data = _normalize(None, request)
        items = list_windows(**data) if data else list_windows()
        return {"ok": True, "success": True, "windows": items, "items": items, "count": len(items)}

    @r.get("/")
    async def slash_get(request: Request):
        return await root_get(request)

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, create_window)

    @r.get("/windows")
    async def list_win(request: Request):
        return _result(None, request, lambda **kw: {"windows": list_windows(**kw), "count": len(list_windows(**kw))})

    @r.post("/windows")
    async def create_win(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, create_window)

    @r.get("/check")
    async def check_win(request: Request, camera_id: Optional[str] = Query(default=None)):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            params.update({k: v for k, v in request.query_params.items() if k not in params})
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "success": True, "suppressed": bool(is_suppressed(**params)), "is_suppressed": bool(is_suppressed(**params)), "active_windows": get_active(**params)}

    @r.post("/check")
    async def check_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        return {"ok": True, "success": True, "suppressed": bool(is_suppressed(**data)), "is_suppressed": bool(is_suppressed(**data)), "active_windows": get_active(**data)}

    @r.get("/status")
    async def status_win(request: Request):
        return _result(None, request, get_status)

    @r.get("/health")
    async def health():
        return {"ok": True, "success": True, "service": "maintenance"}

    @r.get("/{window_id}")
    async def get_win(window_id: str):
        w = get_window(id=window_id)
        if w is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Maintenance window not found")
        w.setdefault("ok", True)
        return w

    @r.delete("/{window_id}")
    async def del_win(window_id: str):
        return delete_window(id=window_id)


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

__all__ = ["router", "underscore_router", "hyphen_router"]
