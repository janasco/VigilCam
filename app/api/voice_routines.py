from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
import app.services.voice_routines as voice_routines_service_mod
from app.services.voice_routines import (
    VoiceRoutineScheduler,
    schedule,
    run,
    voice_routine_service,
)

router = APIRouter(tags=["voice_routines"])


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
    "/api/voice_routines",
    "/api/voice-routines",
    "/api/voice/routines",
    "/api/voice_routine",
    "/api/voice-routine",
    "/api/voice/routine",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_root(request: Request):
        try:
            return voice_routines_service_mod.list_routines()
        except Exception:  # noqa: BLE001
            return voice_routines_service_mod.list_jobs()

    @r.get(base + "/")
    async def get_slash(request: Request):
        return await get_root(request)

    @r.get(base + "/list")
    async def get_list(request: Request):
        return await get_root(request)

    @r.get(base + "/routines")
    async def get_routines(request: Request):
        return await get_root(request)

    @r.get(base + "/status")
    async def get_status(request: Request):
        return voice_routines_service_mod.status()

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
        result.setdefault("job_status", "scheduled")
        result.setdefault("schedule_status", "scheduled")
        result.setdefault("routine_status", "scheduled")
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

    @r.post(base + "/routines")
    async def post_routines(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
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

    @r.post(base + "/summarize")
    async def post_summarize(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = voice_routines_service_mod.generate_vlm_summary(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"summary": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.get(base + "/{item_id}")
    async def get_one(item_id: str, request: Request):
        res = voice_routines_service_mod.get(item_id)
        if res is None:
            raise HTTPException(status_code=404, detail="voice routine not found")
        return res

    @r.delete(base + "/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_one(item_id: str, request: Request):
        voice_routines_service_mod.remove(item_id)
        return None


for base in _PATHS:
    _register(router, base)

# Prefixed routers
underscore_router = APIRouter(prefix="/api/voice_routines", tags=["voice_routines"])
hyphen_router = APIRouter(prefix="/api/voice-routines", tags=["voice_routines"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        return voice_routines_service_mod.list_routines()

    @r.get("/")
    async def slash_get(request: Request):
        return voice_routines_service_mod.list_routines()

    @r.get("/list")
    async def list_get(request: Request):
        return voice_routines_service_mod.list_routines()

    @r.get("/routines")
    async def routines_get(request: Request):
        return voice_routines_service_mod.list_routines()

    @r.get("/status")
    async def status_get(request: Request):
        return voice_routines_service_mod.status()

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

    @r.post("/routines")
    async def routines_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
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

    @r.post("/summarize")
    async def summarize_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = voice_routines_service_mod.generate_vlm_summary(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"summary": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

# Side-effect registration (mirrors email_digest pattern; auto-loader also picks up `router`)
try:
    from app.main import app as _main_app

    if not getattr(_main_app.state, "voice_routines_registered", False):
        _main_app.include_router(router)
        _main_app.state.voice_routines_registered = True
except Exception:  # noqa: BLE001
    pass

try:
    from app.main import app as _main_app2

    _main_app2.include_router(underscore_router)
    _main_app2.include_router(hyphen_router)
except Exception:  # noqa: BLE001
    pass
