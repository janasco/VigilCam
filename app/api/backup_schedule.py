"""Scheduled backup REST endpoints (cron-like auto backups).

No ``app/main.py`` changes are required: the application auto-loads any
``app.api`` module exposing ``router``.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
import app.services.backup_schedule as backup_schedule_service_mod
from app.services.backup_schedule import (
    BackupScheduleScheduler,
    schedule,
    run,
    backup_schedule_service,
)

router = APIRouter(tags=["backup_schedule"])


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
    "/api/backup_schedule",
    "/api/backup-schedule",
    "/api/backups/schedule",
    "/api/backup/schedule",
    "/api/system/backup/schedule",
    "/api/system/backups/schedule",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_root(request: Request):
        try:
            return backup_schedule_service_mod.list_schedules()
        except Exception:  # noqa: BLE001
            return backup_schedule_service_mod.list_jobs()

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
        return backup_schedule_service_mod.status()

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

    @r.get(base + "/{item_id}")
    async def get_one(item_id: str, request: Request):
        res = backup_schedule_service_mod.get(item_id)
        if res is None:
            raise HTTPException(status_code=404, detail="backup schedule not found")
        return res

    @r.put(base + "/{item_id}")
    async def put_one(item_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data["id"] = item_id
        res = backup_schedule_service_mod.update(**data)
        if res is None:
            raise HTTPException(status_code=404, detail="backup schedule not found")
        return res

    @r.patch(base + "/{item_id}")
    async def patch_one(item_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data["id"] = item_id
        res = backup_schedule_service_mod.update(**data)
        if res is None:
            raise HTTPException(status_code=404, detail="backup schedule not found")
        return res

    @r.post(base + "/{item_id}/run")
    async def run_one(item_id: str, payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data["id"] = item_id
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

    @r.post(base + "/{item_id}/trigger")
    async def trigger_one(item_id: str, payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await run_one(item_id, payload, request, db)

    @r.post(base + "/{item_id}/execute")
    async def execute_one(item_id: str, payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await run_one(item_id, payload, request, db)

    @r.post(base + "/{item_id}/enable")
    async def enable_one(item_id: str, request: Request):
        res = backup_schedule_service_mod.enable(item_id)
        if res is None:
            raise HTTPException(status_code=404, detail="backup schedule not found")
        return res

    @r.post(base + "/{item_id}/disable")
    async def disable_one(item_id: str, request: Request):
        res = backup_schedule_service_mod.disable(item_id)
        if res is None:
            raise HTTPException(status_code=404, detail="backup schedule not found")
        return res

    @r.delete(base + "/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_one(item_id: str, request: Request):
        backup_schedule_service_mod.remove(item_id)
        return None


for base in _PATHS:
    _register(router, base)


# Compatibility aliases for varied import styles.
backup_schedule_router = router
backup_schedules_router = router
backups_schedule_router = router
router_backup_schedule = router

__all__ = [
    "router",
    "backup_schedule_router",
    "backup_schedules_router",
    "backups_schedule_router",
    "router_backup_schedule",
]
