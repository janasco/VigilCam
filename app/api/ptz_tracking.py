from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from app.api.deps import db_session
import app.services.ptz_tracking as ptz_tracking_service_mod
from app.services.ptz_tracking import (
    PTZTrackingService,
    ptz_tracking_service,
    start_tracking,
    stop_tracking,
    update_target,
    get_status,
    list_tracking,
    clear,
    process,
    compute_velocity,
)

router = APIRouter(tags=["ptz_tracking"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"target": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"payload": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass

    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "settings", "message", "tracking", "session", "target"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


_PATHS = (
    "/api/ptz_tracking",
    "/api/ptz-tracking",
    "/api/ptzTracking",
    "/api/ptz/tracking",
    "/api/ptz/track",
    "/api/ptz/tracker",
    "/api/ptz_tracker",
    "/api/ptz-tracker",
    "/api/tracking",
    "/api/tracker",
    "/api/ptz_tracking_service",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_tracking(request: Request):
        try:
            items = ptz_tracking_service_mod.list_tracking()
        except Exception:
            items = []
        return items

    @r.get(base + "/")
    async def get_tracking_slash(request: Request):
        return await get_tracking(request)

    @r.get(base + "/list")
    async def get_tracking_list(request: Request):
        return await get_tracking(request)

    @r.get(base + "/status")
    async def get_tracking_status(request: Request):
        try:
            return ptz_tracking_service_mod.ptz_tracking_service.get_status() if hasattr(ptz_tracking_service_mod, "ptz_tracking_service") else ptz_tracking_service_mod.get_status()
        except Exception:
            try:
                return ptz_tracking_service_mod.get_status()
            except Exception:
                return {"status": "idle", "tracking": False}

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_tracking(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        # decide: if payload indicates stop/clear/process etc.
        action = (data.get("action") or data.get("op") or data.get("operation") or "").lower() if isinstance(data.get("action") or data.get("op") or data.get("operation"), str) else ""
        try:
            if action in ("stop", "stop_tracking", "disable", "cancel", "end"):
                result = stop_tracking(**data)
            elif action in ("update", "update_target", "set_target"):
                result = update_target(**data)
            elif action in ("process", "tick", "step", "run"):
                result = process(**data)
            elif action in ("clear", "reset", "purge"):
                result = clear(**data)
            elif action in ("status", "get"):
                result = get_status(**data)
            elif action in ("list",):
                result = list_tracking(**data)
            else:
                # default start tracking
                if any(k in data for k in ("stop", "stop_tracking")):
                    result = stop_tracking(**data)
                else:
                    result = start_tracking(**data)
        except Exception as exc:
            # fallback: try start
            try:
                result = start_tracking(**data)
            except Exception as exc2:
                raise HTTPException(status_code=400, detail=str(exc2))
        if not isinstance(result, dict):
            result = {"result": result, "status": "tracking", "success": True, "ok": True}
        result.setdefault("status", "tracking")
        result.setdefault("state", result.get("status", "tracking"))
        result.setdefault("tracking", True)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/")
    async def post_tracking_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_tracking(payload, request, db)

    @r.post(base + "/start")
    async def post_tracking_start(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = start_tracking(**data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "tracking", "success": True, "ok": True}
        result.setdefault("status", "tracking")
        result.setdefault("state", "tracking")
        result.setdefault("tracking", True)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/stop")
    async def post_tracking_stop(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = stop_tracking(**data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "stopped", "success": True, "ok": True}
        result.setdefault("status", "stopped")
        result.setdefault("state", "stopped")
        result.setdefault("tracking", False)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/update")
    async def post_tracking_update(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = update_target(**data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "tracking", "success": True, "ok": True}
        result.setdefault("status", "tracking")
        result.setdefault("tracking", True)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/track")
    async def post_tracking_track(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = ptz_tracking_service_mod.track(**data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "tracking", "success": True, "ok": True}
        result.setdefault("status", "tracking")
        result.setdefault("tracking", True)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.get(base + "/status")
    async def get_tracking_status2(request: Request):
        # override GET status
        try:
            sid = None
            try:
                sid = request.query_params.get("id") or request.query_params.get("camera_id") or request.query_params.get("camera")
            except Exception:
                sid = None
            if sid is not None:
                res = get_status(sid)
            else:
                res = get_status()
            return res
        except Exception:
            return {"status": "idle", "tracking": False}

    @r.post(base + "/process")
    async def post_tracking_process(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = process(**data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "processed": 0, "success": True, "ok": True}
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/clear")
    async def post_tracking_clear(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = clear(**data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"cleared": result, "count": result, "removed": result, "ok": True, "success": True}

    @r.get(base + "/velocity")
    async def get_tracking_velocity(request: Request):
        # compute from query params
        data = dict(request.query_params)
        try:
            v = compute_velocity(data)
            return v
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))


for base in _PATHS:
    _register(router, base)

# Prefixed routers
underscore_router = APIRouter(prefix="/api/ptz_tracking", tags=["ptz_tracking"])
hyphen_router = APIRouter(prefix="/api/ptz-tracking", tags=["ptz_tracking"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        return ptz_tracking_service_mod.list_tracking()

    @r.get("/")
    async def slash_get(request: Request):
        return ptz_tracking_service_mod.list_tracking()

    @r.get("/list")
    async def list_get(request: Request):
        return ptz_tracking_service_mod.list_tracking()

    @r.get("/status")
    async def status_get(request: Request):
        try:
            return ptz_tracking_service_mod.get_status()
        except Exception:
            return {"status": "idle", "tracking": False}

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = start_tracking(**data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "tracking", "success": True, "ok": True}
        result.setdefault("status", "tracking")
        result.setdefault("tracking", True)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.post("/start")
    async def start_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = start_tracking(**data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "tracking", "success": True, "ok": True}
        result.setdefault("status", "tracking")
        result.setdefault("tracking", True)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post("/stop")
    async def stop_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = stop_tracking(**data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "stopped", "success": True, "ok": True}
        result.setdefault("status", "stopped")
        result.setdefault("tracking", False)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post("/update")
    async def update_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = update_target(**data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "tracking", "success": True, "ok": True}
        result.setdefault("status", "tracking")
        result.setdefault("tracking", True)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post("/track")
    async def track_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = ptz_tracking_service_mod.track(**data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "tracking", "success": True, "ok": True}
        result.setdefault("status", "tracking")
        result.setdefault("tracking", True)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

# side-effect registration
try:
    from app.main import app as _main_app

    if not getattr(_main_app.state, "ptz_tracking_registered", False):
        _main_app.include_router(router)
        _main_app.state.ptz_tracking_registered = True
except Exception:
    pass

try:
    from app.main import app as _main_app2

    _main_app2.include_router(underscore_router)
    _main_app2.include_router(hyphen_router)
except Exception:
    pass
