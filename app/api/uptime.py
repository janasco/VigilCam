"""Camera uptime stats API for VigilCam.

Exposes ``app.services.uptime`` windowed uptime statistics over HTTP.
Auto-discovered by ``app.main`` (no ``app/main.py`` edit); a side-effect
registration block below mirrors the ``watchdog`` pattern so the routes
are live even when imported after startup.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request

import app.services.uptime as uptime_mod

router = APIRouter(prefix="/api/uptime", tags=["uptime"])
underscore_router = APIRouter(prefix="/api/camera_uptime", tags=["uptime"])
hyphen_router = APIRouter(prefix="/api/camera-uptime", tags=["uptime"])


def _payload(data: Any) -> dict:
    out: dict = {}
    if data is None:
        return out
    if hasattr(data, "model_dump"):
        try:
            raw = data.model_dump()
            out = {k: v for k, v in raw.items() if v is not None}
        except Exception:  # noqa: BLE001
            out = {}
    elif isinstance(data, dict):
        out = {k: v for k, v in data.items() if v is not None}
    for nested_key in ("camera", "uptime", "config", "settings", "params", "options", "data", "body"):
        nested = out.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                out.setdefault(k, v)
    return out


def _window_from_request(request: Optional[Request], explicit: Any = None) -> float:
    if explicit is not None:
        try:
            return max(0.0, float(explicit))
        except (TypeError, ValueError):
            pass
    if request is not None:
        try:
            qp = request.query_params
        except Exception:  # noqa: BLE001
            qp = {}
        for key in (
            "window_seconds", "window", "seconds", "within", "within_seconds",
            "duration", "period", "period_seconds", "window_secs",
        ):
            if key in qp:
                try:
                    return max(0.0, float(qp.get(key)))
                except (TypeError, ValueError):
                    continue
        try:
            lower = {str(k).lower(): k for k in qp.keys()}
            for key in ("window_seconds", "window", "seconds", "within"):
                if key in lower:
                    try:
                        return max(0.0, float(qp.get(lower[key])))
                    except (TypeError, ValueError):
                        continue
        except Exception:  # noqa: BLE001
            pass
    return float(uptime_mod.DEFAULT_WINDOW_SECONDS)


def _status_payload(window: float) -> dict:
    return {
        **uptime_mod.get_config(),
        "window_seconds": float(window),
        "summary": uptime_mod.get_summary(window_seconds=window),
        "cameras": uptime_mod.list_uptime(window_seconds=window),
    }


def _register_routes(target: APIRouter) -> None:
    @target.get("/status")
    async def get_uptime_status(request: Request, window_seconds: Optional[float] = Query(default=None)):
        window = _window_from_request(request, window_seconds)
        return _status_payload(window)

    @target.get("/config")
    async def get_uptime_config():
        return uptime_mod.get_config()

    @target.get("/summary")
    async def get_uptime_summary(request: Request, window_seconds: Optional[float] = Query(default=None)):
        window = _window_from_request(request, window_seconds)
        return uptime_mod.get_summary(window_seconds=window)

    @target.get("/cameras")
    async def list_uptime_cameras(request: Request, window_seconds: Optional[float] = Query(default=None)):
        window = _window_from_request(request, window_seconds)
        return uptime_mod.list_uptime(window_seconds=window)

    @target.get("/cameras/{camera_id}")
    async def get_uptime_camera(camera_id: int, request: Request, window_seconds: Optional[float] = Query(default=None)):
        window = _window_from_request(request, window_seconds)
        # Unknown cameras with no transitions and no health entry -> 404,
        # mirroring the watchdog per-camera behaviour.
        rows = uptime_mod.list_uptime(window_seconds=window)
        known = {int(r.get("camera_id")) for r in rows if r.get("camera_id") is not None}
        if int(camera_id) not in known:
            # Still allow health-known cameras that list_uptime may have missed
            # if health import failed; check directly.
            try:
                from app.services import camera_health as _ch

                if _ch.get_health(camera_id, create=False) is None:
                    raise HTTPException(status_code=404, detail="Unknown camera")
            except HTTPException:
                raise
            except Exception:  # noqa: BLE001
                raise HTTPException(status_code=404, detail="Unknown camera")
        return uptime_mod.get_uptime(camera_id, window_seconds=window)

    @target.post("/cameras/{camera_id}/online")
    async def mark_uptime_online(camera_id: int, payload: Any = Body(default=None)):
        data = _payload(payload)
        out = uptime_mod.mark_online(camera_id, **data)
        out.setdefault("ok", True)
        out["stats"] = uptime_mod.get_uptime(camera_id)
        return out

    @target.post("/cameras/{camera_id}/offline")
    async def mark_uptime_offline(camera_id: int, payload: Any = Body(default=None)):
        data = _payload(payload)
        out = uptime_mod.mark_offline(camera_id, **data)
        out.setdefault("ok", True)
        out["stats"] = uptime_mod.get_uptime(camera_id)
        return out

    @target.post("/cameras/{camera_id}/frame")
    async def mark_uptime_frame(camera_id: int, payload: Any = Body(default=None)):
        data = _payload(payload)
        out = uptime_mod.mark_frame_received(camera_id, **data)
        out.setdefault("ok", True)
        out["stats"] = uptime_mod.get_uptime(camera_id)
        return out

    @target.post("/cameras/{camera_id}/transition")
    async def record_uptime_transition(camera_id: int, payload: Any = Body(default=None)):
        data = _payload(payload)
        online = data.get("online", data.get("is_online", data.get("status", True)))
        if isinstance(online, str):
            online = online.strip().lower() in ("online", "healthy", "up", "true", "1", "yes", "on")
        when = data.get("when", data.get("at", data.get("ts", data.get("timestamp"))))
        out = uptime_mod.record_transition(camera_id, online, when)
        out.setdefault("ok", True)
        out["stats"] = uptime_mod.get_uptime(camera_id)
        return out

    @target.post("/reset/{camera_id}")
    async def reset_uptime_camera(camera_id: int):
        uptime_mod.clear(camera_id)
        return {"ok": True, "camera_id": int(camera_id), "summary": uptime_mod.get_summary()}

    @target.post("/reset")
    async def reset_uptime_all():
        uptime_mod.clear()
        return {"ok": True, "success": True, "summary": uptime_mod.get_summary()}

    @target.delete("/cameras/{camera_id}")
    async def delete_uptime_camera(camera_id: int):
        ok = uptime_mod.remove(camera_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Unknown camera")
        return {"ok": True, "removed": True, "camera_id": int(camera_id)}


_register_routes(router)
_register_routes(underscore_router)
_register_routes(hyphen_router)

# Side-effect registration into the main app (mirrors watchdog pattern).
# The main app also auto-discovers app.api.* routers at import time.
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    for _r in (router, underscore_router, hyphen_router):
        if id(_r) not in _ids:
            _main_app.include_router(_r)
            _ids = _ids | {id(_r)}
    try:
        _main_app._registered_router_ids = _ids
    except Exception:  # noqa: BLE001
        pass
except Exception:  # noqa: BLE001
    pass
