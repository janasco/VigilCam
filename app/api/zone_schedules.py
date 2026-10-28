"""Time-based zone-set switching API.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required. Exposes :mod:`app.services.zone_schedules`:
named polygon zone sets per camera (e.g. ``day`` / ``night``) switched by
active recurring time windows.

Distinct from:

* ``Camera.motion_zones`` — one static zone list per camera, no time.
* ``quiet-hours`` — recurring alert suppression, no zone geometry.
* ``rec_schedules`` — recording time profiles, no zone geometry.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request

import app.services.zone_schedules as svc
from app.services.zone_schedules import (
    active_zones,
    clear,
    create_schedule,
    delete_schedule,
    disable,
    enable,
    get_active,
    get_schedule,
    get_status,
    list_schedules,
    resolve_zones,
    set_day_night,
    update_schedule,
)

router = APIRouter(tags=["zone_schedules"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"camera_id": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"camera_id": bytes(payload).decode("utf-8", errors="replace")}
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
                        "request", "settings", "zone_schedule", "schedule", "rule"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _result(payload: Any, request: Request | None, fn) -> dict:
    data = _normalize(payload, request)
    result = fn(**data)
    if isinstance(result, bool):
        return {"ok": True, "success": True, "result": result}
    if isinstance(result, list):
        return {"ok": True, "success": True, "schedules": result, "items": result,
                "rules": result, "count": len(result)}
    if not isinstance(result, dict):
        return {"ok": True, "success": True, "result": result}
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


_PATHS = (
    "/api/zone_schedules",
    "/api/zone-schedules",
    "/api/zones/schedules",
    "/api/motion/zone_schedules",
    "/api/motion/zone-schedules",
)


def _register(r: APIRouter, base: str) -> None:
    tag = base.replace("/", "_")

    @r.get(base, operation_id=f"zone_schedules_root{tag}")
    async def root_get(request: Request):
        data = _normalize(None, request)
        items = list_schedules(**data) if data else list_schedules()
        return {"ok": True, "success": True, "schedules": items, "items": items,
                "rules": items, "count": len(items)}

    @r.get(base + "/", operation_id=f"zone_schedules_slash{tag}")
    async def root_slash(request: Request):
        return await root_get(request)

    @r.get(base + "/list", operation_id=f"zone_schedules_list{tag}")
    async def list_get(request: Request, camera_id: Optional[str] = None):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            params.update({k: v for k, v in request.query_params.items() if k not in params})
        except Exception:  # noqa: BLE001
            pass
        items = list_schedules(**params)
        return {"ok": True, "success": True, "schedules": items, "items": items,
                "rules": items, "count": len(items)}

    @r.post(base, operation_id=f"zone_schedules_create{tag}")
    async def create_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        result = create_schedule(**data)
        if not result.get("ok", True):
            raise HTTPException(status_code=400, detail=result.get("error", "invalid zone schedule"))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/schedules", operation_id=f"zone_schedules_create_plural{tag}")
    async def create_plural(payload: Any = Body(default=None), request: Request = None):
        return await create_post(payload, request)

    @r.post(base + "/rules", operation_id=f"zone_schedules_create_rule{tag}")
    async def create_rule_post(payload: Any = Body(default=None), request: Request = None):
        return await create_post(payload, request)

    @r.post(base + "/set-day-night", operation_id=f"zone_schedules_daynight{tag}")
    async def daynight_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        result = set_day_night(**data)
        if not result.get("ok", True):
            raise HTTPException(status_code=400, detail=result.get("error", "day_zones and night_zones are required"))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.get(base + "/active", operation_id=f"zone_schedules_active{tag}")
    async def active_get(request: Request, camera_id: Optional[str] = None):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            params.update({k: v for k, v in request.query_params.items() if k not in params})
        except Exception:  # noqa: BLE001
            pass
        items = get_active(**params)
        return {"ok": True, "success": True, "schedules": items, "items": items,
                "rules": items, "count": len(items)}

    @r.get(base + "/resolve", operation_id=f"zone_schedules_resolve_get{tag}")
    async def resolve_get(request: Request, camera_id: Optional[str] = None):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            params.update({k: v for k, v in request.query_params.items() if k not in params})
        except Exception:  # noqa: BLE001
            pass
        # Zones passed as fallback via query are JSON-ish strings; resolve
        # handles missing fallback as [].
        return _result(params, None, resolve_zones)

    @r.post(base + "/resolve", operation_id=f"zone_schedules_resolve_post{tag}")
    async def resolve_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, resolve_zones)

    @r.get(base + "/zones", operation_id=f"zone_schedules_zones{tag}")
    async def zones_get(request: Request, camera_id: Optional[str] = None):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            params.update({k: v for k, v in request.query_params.items() if k not in params})
        except Exception:  # noqa: BLE001
            pass
        zones = active_zones(**params)
        return {"ok": True, "success": True, "zones": zones, "count": len(zones),
                "camera_id": params.get("camera_id")}

    @r.get(base + "/status", operation_id=f"zone_schedules_status{tag}")
    async def status_get(request: Request, camera_id: Optional[str] = None):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            params.update({k: v for k, v in request.query_params.items() if k not in params})
        except Exception:  # noqa: BLE001
            pass
        return _result(params, None, get_status)

    @r.post(base + "/clear", operation_id=f"zone_schedules_clear{tag}")
    async def clear_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, clear)

    @r.delete(base + "/clear", operation_id=f"zone_schedules_clear_del{tag}")
    async def clear_delete(request: Request):
        return _result(None, request, clear)

    @r.get(base + "/health", operation_id=f"zone_schedules_health{tag}")
    async def health_get():
        return {"ok": True, "success": True, "service": "zone-schedules"}

    @r.put(base + "/{schedule_id}", operation_id=f"zone_schedules_update{tag}")
    async def update_one(schedule_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        res = update_schedule(schedule_id, **data)
        if res is None:
            raise HTTPException(status_code=404, detail="Zone schedule not found")
        res.setdefault("ok", True)
        res.setdefault("success", True)
        return res

    @r.patch(base + "/{schedule_id}", operation_id=f"zone_schedules_patch{tag}")
    async def patch_one(schedule_id: str, payload: Any = Body(default=None), request: Request = None):
        return await update_one(schedule_id, payload, request)

    @r.get(base + "/{schedule_id}", operation_id=f"zone_schedules_get_one{tag}")
    async def get_one(schedule_id: str):
        w = get_schedule(id=schedule_id)
        if w is None:
            raise HTTPException(status_code=404, detail="Zone schedule not found")
        w.setdefault("ok", True)
        w.setdefault("success", True)
        return w

    @r.delete(base + "/{schedule_id}", operation_id=f"zone_schedules_delete{tag}")
    async def delete_one(schedule_id: str):
        return delete_schedule(id=schedule_id)

    @r.post(base + "/{schedule_id}/enable", operation_id=f"zone_schedules_enable{tag}")
    async def enable_one(schedule_id: str, payload: Any = Body(default=None)):
        data = _normalize(payload, None)
        data["id"] = schedule_id
        return _result(data, None, enable)

    @r.post(base + "/{schedule_id}/disable", operation_id=f"zone_schedules_disable{tag}")
    async def disable_one(schedule_id: str, payload: Any = Body(default=None)):
        data = _normalize(payload, None)
        data["id"] = schedule_id
        return _result(data, None, disable)


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix.
underscore_router = APIRouter(prefix="/api/zone_schedules", tags=["zone_schedules"])
hyphen_router = APIRouter(prefix="/api/zone-schedules", tags=["zone_schedules"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        data = _normalize(None, request)
        items = list_schedules(**data) if data else list_schedules()
        return {"ok": True, "success": True, "schedules": items, "items": items, "count": len(items)}

    @r.get("/")
    async def slash_get(request: Request):
        return await root_get(request)

    @r.get("/list")
    async def list_get(request: Request):
        return await root_get(request)

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        result = create_schedule(**data)
        if not result.get("ok", True):
            raise HTTPException(status_code=400, detail=result.get("error", "invalid zone schedule"))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.post("/set-day-night")
    async def daynight_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        result = set_day_night(**data)
        if not result.get("ok", True):
            raise HTTPException(status_code=400, detail=result.get("error", "day_zones and night_zones are required"))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.get("/active")
    async def active_win(request: Request, camera_id: Optional[str] = None):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            params.update({k: v for k, v in request.query_params.items() if k not in params})
        except Exception:  # noqa: BLE001
            pass
        items = get_active(**params)
        return {"ok": True, "success": True, "schedules": items, "items": items, "count": len(items)}

    @r.get("/resolve")
    async def resolve_win(request: Request, camera_id: Optional[str] = None):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            params.update({k: v for k, v in request.query_params.items() if k not in params})
        except Exception:  # noqa: BLE001
            pass
        return _result(params, None, resolve_zones)

    @r.post("/resolve")
    async def resolve_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, resolve_zones)

    @r.get("/zones")
    async def zones_win(request: Request, camera_id: Optional[str] = None):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            params.update({k: v for k, v in request.query_params.items() if k not in params})
        except Exception:  # noqa: BLE001
            pass
        zones = active_zones(**params)
        return {"ok": True, "success": True, "zones": zones, "count": len(zones)}

    @r.get("/status")
    async def status_win(request: Request):
        return _result(None, request, get_status)

    @r.get("/health")
    async def health():
        return {"ok": True, "success": True, "service": "zone-schedules"}

    @r.get("/{schedule_id}")
    async def get_win(schedule_id: str):
        w = get_schedule(id=schedule_id)
        if w is None:
            raise HTTPException(status_code=404, detail="Zone schedule not found")
        w.setdefault("ok", True)
        return w

    @r.delete("/{schedule_id}")
    async def del_win(schedule_id: str):
        return delete_schedule(id=schedule_id)


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

__all__ = ["router", "underscore_router", "hyphen_router", "svc"]
