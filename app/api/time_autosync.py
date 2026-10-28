"""Auto time-sync scheduler API for VigilCam.

Opt-in periodic drift check + NVR clock push for ONVIF cameras. The
underlying :mod:`app.services.time_sync` module only checks/sets on demand;
this router exposes the opt-in :mod:`app.services.time_autosync` scheduler.

Auto-registered by ``app.main`` (any module in ``app.api`` exposing
``router`` is included), so no ``app/main.py`` edit is needed.
Both ``/api/time-autosync`` (canonical) and ``/api/time_autosync``
(underscore alias) are served.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

import app.services.time_autosync as svc

router = APIRouter(prefix="/api/time-autosync", tags=["time-autosync"])
underscore_router = APIRouter(prefix="/api/time_autosync", tags=["time-autosync"])


class AutosyncConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    interval_seconds: Optional[float] = None
    interval: Optional[float] = None
    tolerance_seconds: Optional[float] = None
    tolerance: Optional[float] = None
    timeout: Optional[float] = None
    auto_sync: Optional[bool] = None

    class Config:
        extra = "allow"


class AutosyncRunRequest(BaseModel):
    tolerance_seconds: Optional[float] = None
    tolerance: Optional[float] = None
    timeout: Optional[float] = None
    auto_sync: Optional[bool] = None
    dry_run: Optional[bool] = None
    check_only: Optional[bool] = None
    targets: Optional[Any] = None
    ips: Optional[Any] = None
    cameras: Optional[Any] = None
    hosts: Optional[Any] = None
    username: Optional[str] = None
    password: Optional[str] = None
    port: Optional[int] = None
    scheme: Optional[str] = None

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

    @r.get("/last")
    async def get_last() -> Dict[str, Any]:
        st = svc.get_status()
        last = None
        try:
            from app.services.time_autosync import _STATE as _st

            last = _st.get("last_result")
        except Exception:  # noqa: BLE001
            last = None
        return {
            "results": last or [],
            "counts": st.get("last_counts", {}),
            "last_run_at": st.get("last_run_at"),
        }

    @r.put("/config")
    async def put_config(payload: Optional[AutosyncConfigRequest] = None) -> Dict[str, Any]:
        data = _payload(payload, None)
        return svc.update_config(**data)

    @r.post("/config")
    async def post_config(payload: Optional[AutosyncConfigRequest] = None) -> Dict[str, Any]:
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
        svc.start_time_autosync_scheduler()
        return {"started": True, "status": svc.get_status()}

    @r.post("/stop")
    async def stop() -> Dict[str, Any]:
        svc.stop_time_autosync_scheduler()
        return {"stopped": True, "status": svc.get_status()}

    async def _run(payload: Optional[AutosyncRunRequest] = None) -> Dict[str, Any]:
        data = _payload(payload, None)
        return await svc.run_scheduled_sync(**data)

    for _path in ("/run", "/run-now", "/trigger", "/sync-now", "/sync"):
        r.post(_path)(_run)

    @r.post("/check")
    async def check(payload: Optional[AutosyncRunRequest] = None) -> Dict[str, Any]:
        data = _payload(payload, None)
        data["auto_sync"] = False
        return await svc.run_scheduled_sync(**data)


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
