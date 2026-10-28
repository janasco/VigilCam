"""Recurring quiet-hours API for VigilCam.

Daily time-of-day alert suppression per camera (see
:mod:`app.services.quiet_hours`). Distinct from one-off maintenance
windows (explicit ``[start, end]`` date ranges): these rules repeat every
day (or on selected days) and never expire.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

import app.services.quiet_hours as svc
from app.services.quiet_hours import (
    clear,
    create_rule,
    delete_rule,
    disable,
    enable,
    get_active,
    get_rule,
    get_status,
    is_quiet,
    list_rules,
)

router = APIRouter(tags=["quiet-hours"])


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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "rule", "quiet_hours", "quiet"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _result(payload: Any, request: Request | None, fn) -> dict:
    data = _normalize(payload, request)
    result = fn(**data)
    if isinstance(result, bool):
        return {"ok": True, "success": True, "result": result, "quiet": result, "is_quiet": result, "suppressed": result, "is_suppressed": result}
    if isinstance(result, list):
        return {"ok": True, "success": True, "rules": result, "items": result, "count": len(result)}
    if not isinstance(result, dict):
        return {"ok": True, "success": True, "result": result}
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


_PATHS = (
    "/api/quiet-hours",
    "/api/quiet_hours",
)


def _register(r: APIRouter, base: str) -> None:
    tag = base.replace("/", "_")

    @r.get(base, operation_id=f"quiet_hours_root{tag}")
    async def root_get(request: Request):
        data = _normalize(None, request)
        cams = data.get("camera_id") or data.get("camera")
        if cams is not None:
            items = list_rules(camera_id=cams)
        else:
            items = list_rules()
        return {"ok": True, "success": True, "rules": items, "items": items, "count": len(items)}

    @r.get(base + "/", operation_id=f"quiet_hours_root_slash{tag}")
    async def root_get_slash(request: Request):
        return await root_get(request)

    @r.get(base + "/rules", operation_id=f"quiet_hours_list{tag}")
    async def list_get(request: Request, camera_id: Optional[str] = Query(default=None)):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            qp = dict(request.query_params)
        except Exception:  # noqa: BLE001
            qp = {}
        params.update({k: v for k, v in qp.items() if k not in params})
        items = list_rules(**params)
        return {"ok": True, "success": True, "rules": items, "items": items, "count": len(items)}

    @r.post(base, operation_id=f"quiet_hours_create{tag}")
    async def create_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, create_rule)

    @r.post(base + "/rules", operation_id=f"quiet_hours_create_rule{tag}")
    async def create_rule_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, create_rule)

    @r.get(base + "/check", operation_id=f"quiet_hours_check_get{tag}")
    async def check_get(request: Request, camera_id: Optional[str] = Query(default=None)):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            qp = dict(request.query_params)
        except Exception:  # noqa: BLE001
            qp = {}
        params.update({k: v for k, v in qp.items() if k not in params})
        quiet = is_quiet(**params)
        return {
            "ok": True, "success": True,
            "camera_id": params.get("camera_id") or params.get("camera"),
            "quiet": bool(quiet), "is_quiet": bool(quiet),
            "suppressed": bool(quiet), "is_suppressed": bool(quiet),
            "active_rules": get_active(**params),
        }

    @r.post(base + "/check", operation_id=f"quiet_hours_check_post{tag}")
    async def check_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        quiet = is_quiet(**data)
        return {
            "ok": True, "success": True,
            "camera_id": data.get("camera_id") or data.get("camera"),
            "quiet": bool(quiet), "is_quiet": bool(quiet),
            "suppressed": bool(quiet), "is_suppressed": bool(quiet),
            "active_rules": get_active(**data),
        }

    @r.get(base + "/status", operation_id=f"quiet_hours_status{tag}")
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

    @r.get(base + "/active", operation_id=f"quiet_hours_active{tag}")
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
        return {"ok": True, "success": True, "rules": items, "items": items, "count": len(items)}

    @r.post(base + "/clear", operation_id=f"quiet_hours_clear{tag}")
    async def clear_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, clear)

    @r.delete(base + "/clear", operation_id=f"quiet_hours_clear_del{tag}")
    async def clear_delete(request: Request):
        return _result(None, request, clear)

    @r.get(base + "/health", operation_id=f"quiet_hours_health{tag}")
    async def health_get():
        return {"ok": True, "success": True, "service": "quiet-hours"}

    @r.get(base + "/{rule_id}", operation_id=f"quiet_hours_get_one{tag}")
    async def get_one(rule_id: str):
        w = get_rule(id=rule_id)
        if w is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Quiet-hours rule not found")
        w.setdefault("ok", True)
        w.setdefault("success", True)
        return w

    @r.delete(base + "/{rule_id}", operation_id=f"quiet_hours_delete{tag}")
    async def delete_one(rule_id: str):
        return delete_rule(id=rule_id)

    @r.post(base + "/{rule_id}/enable", operation_id=f"quiet_hours_enable{tag}")
    async def enable_one(rule_id: str, payload: Any = Body(default=None)):
        data = _normalize(payload, None)
        data["id"] = rule_id
        return _result(data, None, enable)

    @r.post(base + "/{rule_id}/disable", operation_id=f"quiet_hours_disable{tag}")
    async def disable_one(rule_id: str, payload: Any = Body(default=None)):
        data = _normalize(payload, None)
        data["id"] = rule_id
        return _result(data, None, disable)


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix
underscore_router = APIRouter(prefix="/api/quiet_hours", tags=["quiet-hours"])
hyphen_router = APIRouter(prefix="/api/quiet-hours", tags=["quiet-hours"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        data = _normalize(None, request)
        items = list_rules(**data) if data else list_rules()
        return {"ok": True, "success": True, "rules": items, "items": items, "count": len(items)}

    @r.get("/")
    async def slash_get(request: Request):
        return await root_get(request)

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, create_rule)

    @r.get("/rules")
    async def list_rules_get(request: Request):
        return _result(None, request, lambda **kw: {"rules": list_rules(**kw), "count": len(list_rules(**kw))})

    @r.post("/rules")
    async def create_rule_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, create_rule)

    @r.get("/check")
    async def check_win(request: Request, camera_id: Optional[str] = Query(default=None)):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            params.update({k: v for k, v in request.query_params.items() if k not in params})
        except Exception:  # noqa: BLE001
            pass
        q = bool(is_quiet(**params))
        return {"ok": True, "success": True, "quiet": q, "is_quiet": q, "suppressed": q, "is_suppressed": q, "active_rules": get_active(**params)}

    @r.post("/check")
    async def check_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        q = bool(is_quiet(**data))
        return {"ok": True, "success": True, "quiet": q, "is_quiet": q, "suppressed": q, "is_suppressed": q, "active_rules": get_active(**data)}

    @r.get("/status")
    async def status_win(request: Request):
        return _result(None, request, get_status)

    @r.get("/health")
    async def health():
        return {"ok": True, "success": True, "service": "quiet-hours"}

    @r.get("/{rule_id}")
    async def get_win(rule_id: str):
        w = get_rule(id=rule_id)
        if w is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Quiet-hours rule not found")
        w.setdefault("ok", True)
        return w

    @r.delete("/{rule_id}")
    async def del_win(rule_id: str):
        return delete_rule(id=rule_id)


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

__all__ = ["router", "underscore_router", "hyphen_router", "svc"]
