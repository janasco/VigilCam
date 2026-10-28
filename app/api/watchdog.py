"""Camera watchdog auto-restart API for VigilCam."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

import app.services.watchdog as watchdog_service_mod

router = APIRouter(prefix="/api/watchdog", tags=["watchdog"])


class WatchdogConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    auto_restart: Optional[bool] = None
    check_interval_seconds: Optional[float] = None
    check_interval: Optional[float] = None
    interval: Optional[float] = None
    offline_threshold_seconds: Optional[float] = None
    offline_threshold: Optional[float] = None
    threshold: Optional[float] = None
    max_restarts: Optional[int] = None
    max_attempts: Optional[int] = None
    restart_cooldown_seconds: Optional[float] = None
    cooldown: Optional[float] = None

    class Config:
        extra = "allow"


def _serialize(state: Any) -> Any:
    if state is None:
        return None
    if isinstance(state, dict):
        return state
    if isinstance(state, list):
        return [_serialize(s) for s in state]
    for meth in ("to_dict", "model_dump", "dict", "as_dict"):
        fn = getattr(state, meth, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                continue
    try:
        return dict(state)
    except Exception:  # noqa: BLE001
        return {"value": str(state)}


def _status_payload() -> dict:
    svc = watchdog_service_mod.watchdog
    return {
        **svc.get_config(),
        "summary": svc.get_summary(),
        "cameras": _serialize(svc.list_status()),
    }


@router.get("/status")
async def get_watchdog_status():
    return _status_payload()


@router.get("/config")
async def get_watchdog_config():
    return watchdog_service_mod.get_config()


@router.post("/config")
async def update_watchdog_config(payload: Any = Body(default=None)):
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = {k: v for k, v in payload.model_dump().items() if v is not None}
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = {k: v for k, v in payload.items() if v is not None}
    # Allow nested config objects.
    for nested_key in ("config", "settings", "params", "options", "data", "body"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    # Query-style string coercions are handled by the service; just forward.
    return watchdog_service_mod.configure(**data)


@router.post("/enable")
async def enable_watchdog():
    return watchdog_service_mod.enable()


@router.post("/disable")
async def disable_watchdog():
    return watchdog_service_mod.disable()


@router.get("/summary")
async def get_watchdog_summary():
    return watchdog_service_mod.get_summary()


@router.get("/cameras")
async def list_watchdog_cameras():
    return _serialize(watchdog_service_mod.list_status())


@router.get("/cameras/{camera_id}")
async def get_watchdog_camera(camera_id: int):
    st = watchdog_service_mod.get_status(camera_id, create=False)
    if st is None:
        raise HTTPException(status_code=404, detail="Camera not under watchdog")
    return _serialize(st)


@router.post("/check")
async def run_watchdog_check(payload: Any = Body(default=None)):
    data: dict = {}
    if isinstance(payload, dict):
        data = dict(payload)
    elif payload is not None and hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    results = watchdog_service_mod.check_all(**data)
    return {
        "checked": len(results),
        "count": len(results),
        "cameras": _serialize(results),
        "summary": watchdog_service_mod.get_summary(),
        "ok": True,
        "success": True,
    }


@router.post("/restart/{camera_id}")
async def restart_watchdog_camera(camera_id: int, payload: Any = Body(default=None)):
    data: dict = {}
    if isinstance(payload, dict):
        data = dict(payload)
    elif payload is not None and hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    reason = data.get("reason", "manual")
    force = bool(data.get("force", True))
    st = watchdog_service_mod.restart_camera(camera_id, reason=reason, force=force)
    out = _serialize(st)
    out.setdefault("ok", True)
    out.setdefault("success", True)
    out.setdefault("restarted", True)
    return out


@router.post("/cameras/{camera_id}/restart")
async def restart_watchdog_camera_alias(camera_id: int, payload: Any = Body(default=None)):
    return await restart_watchdog_camera(camera_id, payload)


@router.post("/reset/{camera_id}")
async def reset_watchdog_camera(camera_id: int):
    st = watchdog_service_mod.reset(camera_id)
    out = _serialize(st)
    if isinstance(out, dict):
        out.setdefault("ok", True)
    return out


@router.post("/reset")
async def reset_watchdog_all():
    result = watchdog_service_mod.reset()
    if isinstance(result, dict):
        result.setdefault("ok", True)
        return result
    return {"ok": True, "summary": _serialize(result)}


@router.post("/start")
async def start_watchdog_service():
    watchdog_service_mod.start_watchdog()
    return {"started": True, "ok": True, **watchdog_service_mod.get_config()}


@router.post("/stop")
async def stop_watchdog_service():
    watchdog_service_mod.stop_watchdog()
    return {"stopped": True, "ok": True, **watchdog_service_mod.get_config()}


# Side-effect registration into the main app so the router is live even when
# the module is imported after app startup (mirrors webhook_queue pattern).
# The main app also auto-discovers app.api.* routers at import time.
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    if id(router) not in _ids:
        _main_app.include_router(router)
        try:
            _main_app._registered_router_ids = _ids | {id(router)}
        except Exception:  # noqa: BLE001
            pass
except Exception:  # noqa: BLE001
    pass
