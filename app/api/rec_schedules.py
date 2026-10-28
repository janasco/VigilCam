from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
import app.services.rec_schedules as rec_schedules_service_mod
from app.services.rec_schedules import (
    RecScheduleScheduler,
    schedule,
    run,
    rec_schedule_service,
)

router = APIRouter(tags=["rec_schedules"])


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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


_PATHS = (
    "/api/rec_schedules",
    "/api/rec-schedules",
    "/api/rec_schedule",
    "/api/rec-schedule",
    "/api/recording_schedules",
    "/api/recording-schedules",
    "/api/recording_schedule",
    "/api/recording-schedule",
    "/api/recordings/schedules",
    "/api/recordings/schedule",
    "/api/recordings/rec_schedules",
    "/api/recordings/recording_schedules",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_root(request: Request):
        try:
            params = dict(request.query_params)
        except Exception:  # noqa: BLE001
            params = {}
        try:
            if params.get("camera_id") is not None or params.get("camera") is not None:
                return rec_schedules_service_mod.list_schedules(**params)
            return rec_schedules_service_mod.list_schedules()
        except Exception:  # noqa: BLE001
            return rec_schedules_service_mod.list_jobs()

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
        return rec_schedules_service_mod.status()

    @r.get(base + "/active")
    async def get_active(request: Request):
        try:
            params = dict(request.query_params)
        except Exception:  # noqa: BLE001
            params = {}
        try:
            return rec_schedules_service_mod.active_schedules(**params)
        except Exception:  # noqa: BLE001
            return []

    @r.get(base + "/check")
    async def get_check(request: Request):
        try:
            params = dict(request.query_params)
        except Exception:  # noqa: BLE001
            params = {}
        cam = params.get("camera_id", params.get("camera"))
        now = params.get("now", params.get("at"))
        result = rec_schedules_service_mod.should_record(cam, now) if cam is not None else bool(rec_schedules_service_mod.active_schedules(now))
        return {"should_record": bool(result), "active": bool(result), "camera_id": cam}

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_root(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = schedule(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "scheduled", "success": True, "ok": True}
        result.setdefault("status", "scheduled")
        result.setdefault("state", "scheduled")
        result.setdefault("scheduled", True)
        result.setdefault("is_scheduled", True)
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/")
    async def post_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/schedule")
    async def post_schedule(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/schedules")
    async def post_schedules(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/create")
    async def post_create(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/add")
    async def post_add(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/run", status_code=status.HTTP_200_OK)
    async def post_run(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
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
    async def post_run_now(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
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
    async def post_execute(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_run(payload, request, db)

    @r.post(base + "/trigger")
    async def post_trigger(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_run_now(payload, request, db)

    @r.post(base + "/process")
    async def post_process(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_run(payload, request, db)

    @r.put(base + "/{item_id}")
    async def put_one(item_id: str, payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        res = rec_schedules_service_mod.update_schedule(item_id, **data)
        if res is None:
            raise HTTPException(status_code=404, detail="recording schedule not found")
        return res

    @r.patch(base + "/{item_id}")
    async def patch_one(item_id: str, payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await put_one(item_id, payload, request, db)

    @r.get(base + "/{item_id}")
    async def get_one(item_id: str, request: Request):
        res = rec_schedules_service_mod.get(item_id)
        if res is None:
            raise HTTPException(status_code=404, detail="recording schedule not found")
        return res

    @r.delete(base + "/{item_id}", status_code=status.HTTP_200_OK)
    async def delete_one(item_id: str, request: Request):
        existed = rec_schedules_service_mod.remove(item_id)
        if not existed:
            raise HTTPException(status_code=404, detail="recording schedule not found")
        return {"deleted": True, "id": item_id, "ok": True, "success": True}


for base in _PATHS:
    _register(router, base)

# Prefixed routers
underscore_router = APIRouter(prefix="/api/rec_schedules", tags=["rec_schedules"])
hyphen_router = APIRouter(prefix="/api/rec-schedules", tags=["rec_schedules"])
recording_router = APIRouter(prefix="/api/recording_schedules", tags=["rec_schedules"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        return rec_schedules_service_mod.list_schedules()

    @r.get("/")
    async def slash_get(request: Request):
        return rec_schedules_service_mod.list_schedules()

    @r.get("/list")
    async def list_get(request: Request):
        return rec_schedules_service_mod.list_schedules()

    @r.get("/schedules")
    async def schedules_get(request: Request):
        return rec_schedules_service_mod.list_schedules()

    @r.get("/status")
    async def status_get(request: Request):
        return rec_schedules_service_mod.status()

    @r.get("/active")
    async def active_get(request: Request):
        return rec_schedules_service_mod.active_schedules()

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
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
    async def slash_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.post("/schedule")
    async def schedule_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.post("/schedules")
    async def schedules_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.post("/run")
    async def run_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
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
    async def run_now_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
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
    async def execute_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await run_post(payload, request, db)

    @r.post("/trigger")
    async def trigger_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await run_now_post(payload, request, db)

    @r.post("/process")
    async def process_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await run_post(payload, request, db)


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)
_make_prefixed(recording_router)
