from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

import app.services.alert_throttle as svc
from app.services.alert_throttle import (
    alert_throttle_service,
    check,
    check_and_record,
    clear,
    get_config,
    get_status,
    list_status,
    record,
    reset,
    set_cooldown,
    set_throttle,
)

router = APIRouter(tags=["alert_throttle"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"key": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"key": bytes(payload).decode("utf-8", errors="replace")}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "alert", "throttle"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _result(payload: Any, request: Request | None, fn) -> dict:
    data = _normalize(payload, request)
    result = fn(**data)
    if isinstance(result, bool):
        return {"ok": True, "success": True, "allowed": result, "throttled": not result}
    if not isinstance(result, dict):
        return {"ok": True, "success": True, "result": result}
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


_PATHS = (
    "/api/alert_throttle",
    "/api/alert-throttle",
    "/api/alerts/throttle",
    "/api/alerts/throttling",
    "/api/throttle",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_all_status(request: Request):
        return _result(None, request, lambda **kw: {"ok": True, "success": True, "items": list_status(), "config": get_config()})

    @r.get(base + "/")
    async def get_all_status_slash(request: Request):
        return await get_all_status(request)

    @r.get(base + "/status")
    async def get_key_status(request: Request, key: Optional[str] = Query(default=None)):
        params: dict = {}
        if key is not None:
            params["key"] = key
        try:
            qp = dict(request.query_params)
        except Exception:  # noqa: BLE001
            qp = {}
        params.update({k: v for k, v in qp.items() if k not in params})
        return _result(params, None, get_status)

    @r.get(base + "/check")
    async def get_check(request: Request, key: Optional[str] = Query(default=None)):
        params: dict = {}
        if key is not None:
            params["key"] = key
        try:
            qp = dict(request.query_params)
        except Exception:  # noqa: BLE001
            qp = {}
        params.update({k: v for k, v in qp.items() if k not in params})
        return _result(params, None, check)

    @r.post(base + "/check")
    async def post_check(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, check)

    @r.post(base + "/allow")
    async def post_allow(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, check_and_record)

    @r.post(base + "/record")
    async def post_record(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, record)

    @r.post(base + "/mark-sent")
    async def post_mark_sent(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, record)

    @r.post(base + "/reset")
    async def post_reset(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, reset)

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not data:
            return _result(None, None, clear)
        return _result(data, None, reset)

    @r.delete(base + "/clear")
    async def delete_clear(request: Request):
        return _result(None, request, clear)

    @r.get(base + "/config")
    async def get_cfg(request: Request, key: Optional[str] = Query(default=None)):
        params: dict = {}
        if key is not None:
            params["key"] = key
        return _result(params, request, get_config)

    @r.post(base + "/config")
    async def post_config(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        out: dict = {"ok": True, "success": True}
        cd_keys = ("cooldown", "cooldown_seconds", "cooldown_secs", "interval", "interval_seconds", "delay", "delay_seconds")
        if any(k in data for k in cd_keys):
            out["cooldown"] = set_cooldown(**data)
        max_keys = ("max_alerts", "max", "max_per_window", "limit", "window_seconds", "window", "period_seconds")
        if any(k in data for k in max_keys):
            out["throttle"] = set_throttle(**data)
        if "cooldown" not in out and "throttle" not in out:
            out["config"] = set_throttle(**data) if data else get_config()
        else:
            out["config"] = get_config(**({"key": data["key"]} if data.get("key") else {}))
        return out

    @r.get(base + "/health")
    async def get_health():
        return {"ok": True, "success": True, "service": "alert_throttle"}


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix
underscore_router = APIRouter(prefix="/api/alert_throttle", tags=["alert_throttle"])
hyphen_router = APIRouter(prefix="/api/alert-throttle", tags=["alert_throttle"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        return {"ok": True, "success": True, "items": list_status(), "config": get_config()}

    @r.get("/")
    async def slash_get(request: Request):
        return await root_get(request)

    @r.get("/status")
    async def status_get(request: Request, key: Optional[str] = Query(default=None)):
        params: dict = {"key": key} if key is not None else {}
        try:
            params.update({k: v for k, v in request.query_params.items() if k not in params})
        except Exception:  # noqa: BLE001
            pass
        return get_status(**params)

    @r.get("/check")
    async def check_get(request: Request, key: Optional[str] = Query(default=None)):
        params: dict = {"key": key} if key is not None else {}
        try:
            params.update({k: v for k, v in request.query_params.items() if k not in params})
        except Exception:  # noqa: BLE001
            pass
        return check(**params)

    @r.post("/check")
    async def check_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, check)

    @r.post("/allow")
    async def allow_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, check_and_record)

    @r.post("/record")
    async def record_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, record)

    @r.post("/reset")
    async def reset_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, reset)

    @r.get("/config")
    async def config_get(request: Request):
        return _result(None, request, get_config)

    @r.post("/config")
    async def config_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if any(k in data for k in ("cooldown", "cooldown_seconds", "interval", "delay_seconds")):
            set_cooldown(**data)
        if any(k in data for k in ("max_alerts", "max", "max_per_window", "limit", "window_seconds", "window")):
            set_throttle(**data)
        return get_config(**({"key": data["key"]} if data.get("key") else {}))

    @r.get("/health")
    async def health_get():
        return {"ok": True, "success": True, "service": "alert_throttle"}


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

__all__ = ["router", "underscore_router", "hyphen_router"]
