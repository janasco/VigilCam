"""Alert-triggered PTZ goto router (auto-registered, no main.py edits)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

import app.services.alert_goto as svc
from app.services.alert_goto import (
    alert_goto_service,
    clear,
    configure_rule,
    get_status,
    handle_detection,
    list_rules,
    remove_rule,
)

router = APIRouter(tags=["alert_goto"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"label": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
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
                       "body", "request", "settings", "detection", "event",
                       "alert", "rule"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _ok(result: Any) -> dict:
    if isinstance(result, bool):
        return {"ok": True, "success": True, "result": result}
    if isinstance(result, list):
        return {"ok": True, "success": True, "items": result,
                "rules": result, "count": len(result)}
    if not isinstance(result, dict):
        return {"ok": True, "success": True, "result": result}
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


_PATHS = (
    "/api/alert_goto",
    "/api/alert-goto",
    "/api/alerts/goto",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_rules(request: Request):
        return _ok(list_rules(_normalize(None, request)))

    @r.get(base + "/")
    async def get_rules_slash(request: Request):
        return await get_rules(request)

    @r.post(base)
    async def post_rule(payload: Any = Body(default=None), request: Request = None):
        return _ok(configure_rule(_normalize(payload, request)))

    @r.post(base + "/event")
    async def post_event(payload: Any = Body(default=None), request: Request = None):
        return _ok(handle_detection(_normalize(payload, request)))

    @r.post(base + "/trigger")
    async def post_trigger(payload: Any = Body(default=None), request: Request = None):
        return _ok(handle_detection(_normalize(payload, request)))

    @r.post(base + "/detection")
    async def post_detection(payload: Any = Body(default=None), request: Request = None):
        return _ok(handle_detection(_normalize(payload, request)))

    @r.post(base + "/handle")
    async def post_handle(payload: Any = Body(default=None), request: Request = None):
        return _ok(handle_detection(_normalize(payload, request)))

    @r.get(base + "/status")
    async def get_state(request: Request):
        return _ok(get_status(_normalize(None, request)))

    @r.post(base + "/reset")
    async def post_reset(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if data:
            return _ok(remove_rule(data))
        cleared = clear()
        return {"ok": True, "success": True, "reset": True, "cleared": cleared}

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if data:
            return _ok(remove_rule(data))
        cleared = clear()
        return {"ok": True, "success": True, "reset": True, "cleared": cleared}

    @r.delete(base + "/{rule_id}")
    async def delete_rule(rule_id: str):
        return _ok(remove_rule({"id": rule_id}))

    @r.get(base + "/health")
    async def get_health():
        return {"ok": True, "success": True, "service": "alert_goto"}


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix
underscore_router = APIRouter(prefix="/api/alert_goto", tags=["alert_goto"])
hyphen_router = APIRouter(prefix="/api/alert-goto", tags=["alert_goto"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        return _ok(list_rules(_normalize(None, request)))

    @r.get("/")
    async def slash_get(request: Request):
        return await root_get(request)

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None):
        return _ok(configure_rule(_normalize(payload, request)))

    @r.post("/event")
    async def event_post(payload: Any = Body(default=None), request: Request = None):
        return _ok(handle_detection(_normalize(payload, request)))

    @r.post("/trigger")
    async def trigger_post(payload: Any = Body(default=None), request: Request = None):
        return _ok(handle_detection(_normalize(payload, request)))

    @r.post("/detection")
    async def detection_post(payload: Any = Body(default=None), request: Request = None):
        return _ok(handle_detection(_normalize(payload, request)))

    @r.get("/status")
    async def status_get(request: Request):
        return _ok(get_status(_normalize(None, request)))

    @r.post("/reset")
    async def reset_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if data:
            return _ok(remove_rule(data))
        return {"ok": True, "success": True, "reset": True, "cleared": clear()}

    @r.delete("/{rule_id}")
    async def delete_by_id(rule_id: str):
        return _ok(remove_rule({"id": rule_id}))

    @r.get("/health")
    async def health_get():
        return {"ok": True, "success": True, "service": "alert_goto"}


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

__all__ = ["router", "underscore_router", "hyphen_router"]
