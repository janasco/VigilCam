"""Merge schedule API for VigilCam.

Opt-in periodic auto-merge of motion events. Event merge itself stays
manual-only by default (``app.api.event_merge`` on-demand endpoints);
this router only manages the opt-in scheduler in
``app.services.merge_schedule``.

Auto-registered by ``app.main`` (any module in ``app.api`` exposing
``router`` is included), so no ``app/main.py`` edit is needed.
Both ``/api/merge-schedule`` (canonical) and ``/api/merge_schedule``
(underscore alias) are served.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

import app.services.merge_schedule as svc
from app.api.deps import db_session

router = APIRouter(prefix="/api/merge-schedule", tags=["merge-schedule"])
underscore_router = APIRouter(prefix="/api/merge_schedule", tags=["merge-schedule"])


class ScheduleConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    interval_seconds: Optional[float] = None
    interval: Optional[float] = None
    gap_seconds: Optional[float] = None
    gap: Optional[float] = None
    camera_id: Optional[Any] = None
    camera: Optional[Any] = None
    limit: Optional[int] = None

    class Config:
        extra = "allow"


class MergeRunRequest(BaseModel):
    gap_seconds: Optional[float] = None
    gap: Optional[float] = None
    camera_id: Optional[Any] = None
    camera: Optional[Any] = None
    limit: Optional[int] = None
    dry_run: Optional[bool] = None
    events: Optional[Any] = None
    items: Optional[Any] = None

    class Config:
        extra = "allow"


def _payload(model: Any, body: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if model is not None and hasattr(model, "model_dump"):
        try:
            data.update({k: v for k, v in model.model_dump().items() if v is not None})
        except Exception:  # noqa: BLE001
            pass
    if isinstance(body, dict):
        for k, v in body.items():
            if v is not None:
                data[k] = v
    extra = getattr(model, "model_extra", None) if model is not None else None
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in data and v is not None:
                data[k] = v
    return data


def _register(r: APIRouter) -> None:
    @r.get("/config")
    async def get_config() -> Dict[str, Any]:
        return svc.get_config()

    @r.get("/status")
    async def get_status() -> Dict[str, Any]:
        return svc.get_status()

    @r.get("/results")
    async def get_results() -> Dict[str, Any]:
        status = svc.get_status()
        last = None
        try:
            from app.services.merge_schedule import _STATE as _st

            last = _st.get("last_result")
        except Exception:  # noqa: BLE001
            last = None
        return {
            "merged": last or [],
            "counts": status.get("last_counts", {}),
            "last_run_at": status.get("last_run_at"),
        }

    @r.get("/last")
    async def get_last() -> Dict[str, Any]:
        return await get_results()

    @r.put("/config")
    async def put_config(payload: Optional[ScheduleConfigRequest] = None) -> Dict[str, Any]:
        data = _payload(payload, None)
        return svc.update_config(**data)

    @r.post("/config")
    async def post_config(payload: Optional[ScheduleConfigRequest] = None) -> Dict[str, Any]:
        data = _payload(payload, None)
        return svc.update_config(**data)

    @r.post("/enable")
    async def enable() -> Dict[str, Any]:
        return svc.update_config(enabled=True)

    @r.post("/disable")
    async def disable() -> Dict[str, Any]:
        return svc.update_config(enabled=False)

    @r.post("/start")
    async def start() -> Dict[str, Any]:
        svc.start_merge_schedule_scheduler()
        return {"started": True, "status": svc.get_status()}

    @r.post("/stop")
    async def stop() -> Dict[str, Any]:
        svc.stop_merge_schedule_scheduler()
        return {"stopped": True, "status": svc.get_status()}

    @r.post("/run")
    async def run_merge(
        payload: Optional[MergeRunRequest] = None,
        db=Depends(db_session),
    ) -> Dict[str, Any]:
        data = _payload(payload, None)
        return await svc.run_scheduled_merge(db=db, **data)

    @r.post("/run-now")
    async def run_now(
        payload: Optional[MergeRunRequest] = None,
        db=Depends(db_session),
    ) -> Dict[str, Any]:
        data = _payload(payload, None)
        return await svc.run_scheduled_merge(db=db, **data)

    @r.post("/scan")
    async def scan(
        payload: Optional[MergeRunRequest] = None,
        db=Depends(db_session),
    ) -> Dict[str, Any]:
        data = _payload(payload, None)
        return await svc.run_scheduled_merge(db=db, **data)

    @r.post("/scan-now")
    async def scan_now(
        payload: Optional[MergeRunRequest] = None,
        db=Depends(db_session),
    ) -> Dict[str, Any]:
        data = _payload(payload, None)
        return await svc.run_scheduled_merge(db=db, **data)

    @r.post("/trigger")
    async def trigger(
        payload: Optional[MergeRunRequest] = None,
        db=Depends(db_session),
    ) -> Dict[str, Any]:
        data = _payload(payload, None)
        return await svc.run_scheduled_merge(db=db, **data)

    @r.post("/preview")
    async def preview(
        payload: Optional[MergeRunRequest] = None,
        db=Depends(db_session),
    ) -> Dict[str, Any]:
        data = _payload(payload, None)
        data["dry_run"] = True
        return await svc.run_scheduled_merge(db=db, **data)


_register(router)
_register(underscore_router)


# Side-effect registration so the underscore alias is live even when this
# module is imported after app startup (mirrors discovery-schedule pattern).
# The main app also auto-discovers app.api.* routers at import time.
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    for _r in (router, underscore_router):
        if id(_r) not in _ids:
            _main_app.include_router(_r)
            try:
                _main_app._registered_router_ids = getattr(
                    _main_app, "_registered_router_ids", set()
                ) | {id(_r)}
                _ids = _main_app._registered_router_ids
            except Exception:  # noqa: BLE001
                pass
except Exception:  # noqa: BLE001
    pass


# Compatibility aliases.
merge_schedule_router = router
merge_schedules_router = router
auto_merge_router = router
event_merge_schedule_router = router

__all__ = ["router"]
