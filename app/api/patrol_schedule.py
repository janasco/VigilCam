"""PTZ patrol schedule API (time-based tour triggering)."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request, status

import app.services.patrol_schedule as patrol_schedule_service_mod
from app.services.patrol_schedule import (
    PatrolScheduleScheduler,
    schedule,
    run,
    patrol_schedule_service,
)

router = APIRouter(tags=["patrol_schedule"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"schedule": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"name": bytes(payload).decode("utf-8", errors="replace")}
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
                       "request", "settings", "message", "tour", "patrol"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


_PATHS = (
    "/api/patrol_schedule",
    "/api/patrol-schedule",
    "/api/patrol_schedules",
    "/api/patrol-schedules",
    "/api/patrol/schedule",
    "/api/patrol/schedules",
    "/api/patrols/schedule",
    "/api/patrols/schedules",
    "/api/ptz/patrol_schedule",
    "/api/ptz/patrol-schedule",
    "/api/ptz/patrol_schedules",
    "/api/ptz/patrol-schedules",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_root(request: Request):
        try:
            return patrol_schedule_service_mod.list_schedules()
        except Exception:  # noqa: BLE001
            return patrol_schedule_service_mod.list_jobs()

    @r.get(base + "/")
    async def get_slash(request: Request):
        return await get_root(request)

    @r.get(base + "/list")
    async def get_list(request: Request):
        return await get_root(request)

    @r.get(base + "/schedules")
    async def get_schedules(request: Request):
        return await get_root(request)

    @r.get(base + "/status")
    async def get_status(request: Request):
        return patrol_schedule_service_mod.status()

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_root(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            result = schedule(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "scheduled", "success": True, "ok": True}
        result.setdefault("status", "scheduled")
        result.setdefault("state", "scheduled")
        result.setdefault("job_status", "scheduled")
        result.setdefault("schedule_status", "scheduled")
        result.setdefault("scheduled", True)
        result.setdefault("is_scheduled", True)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/")
    async def post_slash(payload: Any = Body(default=None), request: Request = None):
        return await post_root(payload, request)

    @r.post(base + "/schedule")
    async def post_schedule(payload: Any = Body(default=None), request: Request = None):
        return await post_root(payload, request)

    @r.post(base + "/schedules")
    async def post_schedules(payload: Any = Body(default=None), request: Request = None):
        return await post_root(payload, request)

    @r.post(base + "/create")
    async def post_create(payload: Any = Body(default=None), request: Request = None):
        return await post_root(payload, request)

    @r.post(base + "/add")
    async def post_add(payload: Any = Body(default=None), request: Request = None):
        return await post_root(payload, request)

    @r.post(base + "/run", status_code=status.HTTP_200_OK)
    async def post_run(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            result = run(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post(base + "/run-now")
    async def post_run_now(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("force", True)
        try:
            result = run(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post(base + "/execute")
    async def post_execute(payload: Any = Body(default=None), request: Request = None):
        return await post_run(payload, request)

    @r.post(base + "/trigger")
    async def post_trigger(payload: Any = Body(default=None), request: Request = None):
        return await post_run_now(payload, request)

    @r.post(base + "/process")
    async def post_process(payload: Any = Body(default=None), request: Request = None):
        return await post_run(payload, request)

    @r.get(base + "/{item_id}")
    async def get_one(item_id: str, request: Request):
        res = patrol_schedule_service_mod.get(item_id)
        if res is None:
            raise HTTPException(status_code=404, detail="patrol schedule not found")
        return res

    @r.delete(base + "/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_one(item_id: str, request: Request):
        patrol_schedule_service_mod.remove(item_id)
        return None


for base in _PATHS:
    _register(router, base)

# Prefixed routers
underscore_router = APIRouter(prefix="/api/patrol_schedule", tags=["patrol_schedule"])
hyphen_router = APIRouter(prefix="/api/patrol-schedule", tags=["patrol_schedule"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        return patrol_schedule_service_mod.list_schedules()

    @r.get("/")
    async def slash_get(request: Request):
        return patrol_schedule_service_mod.list_schedules()

    @r.get("/list")
    async def list_get(request: Request):
        return patrol_schedule_service_mod.list_schedules()

    @r.get("/schedules")
    async def schedules_get(request: Request):
        return patrol_schedule_service_mod.list_schedules()

    @r.get("/status")
    async def status_get(request: Request):
        return patrol_schedule_service_mod.status()

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            result = schedule(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "scheduled", "success": True, "ok": True}
        result.setdefault("status", "scheduled")
        result.setdefault("scheduled", True)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.post("/schedule")
    async def schedule_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.post("/schedules")
    async def schedules_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.post("/run")
    async def run_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            result = run(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post("/run-now")
    async def run_now_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("force", True)
        try:
            result = run(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post("/execute")
    async def execute_post(payload: Any = Body(default=None), request: Request = None):
        return await run_post(payload, request)

    @r.post("/trigger")
    async def trigger_post(payload: Any = Body(default=None), request: Request = None):
        return await run_now_post(payload, request)

    @r.post("/process")
    async def process_post(payload: Any = Body(default=None), request: Request = None):
        return await run_post(payload, request)


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)
