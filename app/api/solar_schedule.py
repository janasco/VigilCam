"""Solar day/night switching endpoints (astronomical sunrise/sunset).

Auto-registered by ``app.main``'s ``pkgutil`` loader (module exposes
``router``), so no ``app/main.py`` edit is needed. Service work lives in
``app.services.solar_schedule`` (stdlib-only NOAA approximation from
lat/lon/date); this module only validates input and maps errors to HTTP
status codes. Switching is explicit: clients call ``/resolve`` to learn
which profile applies, or ``/apply`` to resolve + write via
``app.services.daynight`` on demand. No background task lives here.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from app.services import solar_schedule as solar_service

router = APIRouter(tags=["solar_schedule"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        s = payload.strip()
        if s:
            data = {"camera_id": s}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
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
    for nested_key in ("payload", "options", "config", "params",
                       "data", "body", "request", "settings",
                       "location", "coords", "coordinates"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


async def list_solar(payload: Any = Body(default=None),
                     request: Request | None = None) -> dict:
    data = _normalize(payload, request)
    raw_cam = None
    for k in ("camera_id", "camera", "cam", "cam_id", "device_id", "id"):
        if data.get(k) is not None:
            raw_cam = data.get(k)
            break
    if raw_cam is not None:
        cfg = solar_service.get_config(raw_cam)
        if cfg is None:
            raise HTTPException(status_code=404, detail="Solar config not found")
        return cfg
    items = solar_service.list_configs()
    return {"ok": True, "success": True, "configs": items,
            "items": items, "count": len(items)}


async def calculate_solar(payload: Any = Body(default=None),
                           request: Request | None = None) -> dict:
    data = _normalize(payload, request)
    try:
        result = solar_service.calculate(**data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


async def configure_solar(payload: Any = Body(default=None),
                           request: Request | None = None) -> dict:
    data = _normalize(payload, request)
    try:
        result = solar_service.configure(**data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not isinstance(result, dict):
        return {"ok": True, "success": True, "result": result}
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


async def resolve_solar(payload: Any = Body(default=None),
                         request: Request | None = None) -> dict:
    data = _normalize(payload, request)
    try:
        result = solar_service.resolve_profile(**data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


async def apply_solar(payload: Any = Body(default=None),
                       request: Request | None = None) -> dict:
    data = _normalize(payload, request)
    try:
        result = await solar_service.apply_solar_profile(**data)
    except ValueError as exc:
        msg = str(exc)
        lowered = msg.lower()
        if "ip/host" in lowered or "no imaging levels" in lowered \
                or "at least one of" in lowered or "invalid imaging level" in lowered:
            raise HTTPException(status_code=422, detail=msg)
        if "not found" in lowered:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    if not isinstance(result, dict):
        return {"ok": True, "success": True, "result": result}
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


async def delete_solar(payload: Any = Body(default=None),
                        request: Request | None = None) -> dict:
    data = _normalize(payload, request)
    raw_cam = None
    for k in ("camera_id", "camera", "cam", "cam_id", "device_id", "id"):
        if data.get(k) is not None:
            raw_cam = data.get(k)
            break
    if raw_cam is not None:
        ok = solar_service.delete_config(raw_cam)
        if not ok:
            raise HTTPException(status_code=404, detail="Solar config not found")
        return {"ok": True, "success": True, "deleted": True,
                "camera_id": raw_cam}
    n = solar_service.clear()
    return {"ok": True, "success": True, "cleared": n, "cleared_count": n}


async def solar_status(payload: Any = Body(default=None),
                        request: Request | None = None) -> dict:
    return solar_service.status()


_PATHS = (
    "/api/solar_schedule",
    "/api/solar-schedule",
    "/api/solar_schedules",
    "/api/solar-schedules",
    "/api/daynight/solar",
    "/api/day-night/solar",
    "/api/day_night/solar",
    "/api/imaging/solar",
)


def _register(r: APIRouter, base: str) -> None:
    tag = base.replace("/", "_")

    @r.get(base, operation_id=f"solar_root{tag}")
    async def root_get(request: Request):
        return await list_solar(None, request)

    @r.get(base + "/", operation_id=f"solar_root_slash{tag}", include_in_schema=False)
    async def root_get_slash(request: Request):
        return await list_solar(None, request)

    @r.get(base + "/list", operation_id=f"solar_list{tag}")
    async def list_get(request: Request):
        return await list_solar(None, request)

    @r.get(base + "/status", operation_id=f"solar_status{tag}")
    async def status_get(request: Request):
        return await solar_status(None, request)

    @r.get(base + "/health", operation_id=f"solar_health{tag}")
    async def health_get():
        return {"ok": True, "success": True, "service": "solar_schedule"}

    @r.post(base + "/calculate", operation_id=f"solar_calculate{tag}")
    async def calculate_post(payload: Any = Body(default=None),
                             request: Request = None):
        return await calculate_solar(payload, request)

    @r.post(base + "/sun-times", operation_id=f"solar_suntimes{tag}", include_in_schema=False)
    async def suntimes_post(payload: Any = Body(default=None),
                            request: Request = None):
        return await calculate_solar(payload, request)

    @r.get(base + "/sun-times", operation_id=f"solar_suntimes_get{tag}", include_in_schema=False)
    async def suntimes_get(request: Request):
        return await calculate_solar(None, request)

    @r.post(base + "/configure", operation_id=f"solar_configure{tag}")
    async def configure_post(payload: Any = Body(default=None),
                             request: Request = None):
        return await configure_solar(payload, request)

    @r.post(base + "/set", operation_id=f"solar_set{tag}", include_in_schema=False)
    async def set_post(payload: Any = Body(default=None),
                       request: Request = None):
        return await configure_solar(payload, request)

    @r.post(base + "/config", operation_id=f"solar_config{tag}", include_in_schema=False)
    async def config_post(payload: Any = Body(default=None),
                          request: Request = None):
        return await configure_solar(payload, request)

    @r.post(base, operation_id=f"solar_create{tag}", include_in_schema=False)
    async def create_post(payload: Any = Body(default=None),
                          request: Request = None):
        data = _normalize(payload, request)
        has_geo = any(data.get(k) is not None for k in (
            "lat", "latitude", "lon", "lng", "longitude", "long"))
        has_cam = any(data.get(k) is not None for k in (
            "camera_id", "camera", "cam", "cam_id", "device_id", "id"))
        if has_geo and has_cam:
            return await configure_solar(payload, request)
        if has_geo:
            return await calculate_solar(payload, request)
        return await list_solar(payload, request)

    @r.post(base + "/resolve", operation_id=f"solar_resolve{tag}")
    async def resolve_post(payload: Any = Body(default=None),
                           request: Request = None):
        return await resolve_solar(payload, request)

    @r.post(base + "/now", operation_id=f"solar_now{tag}", include_in_schema=False)
    async def now_post(payload: Any = Body(default=None),
                       request: Request = None):
        return await resolve_solar(payload, request)

    @r.get(base + "/resolve", operation_id=f"solar_resolve_get{tag}", include_in_schema=False)
    async def resolve_get(request: Request):
        return await resolve_solar(None, request)

    @r.post(base + "/apply", operation_id=f"solar_apply{tag}")
    async def apply_post(payload: Any = Body(default=None),
                         request: Request = None):
        return await apply_solar(payload, request)

    @r.post(base + "/delete", operation_id=f"solar_delete_post{tag}", include_in_schema=False)
    async def delete_post(payload: Any = Body(default=None),
                          request: Request = None):
        return await delete_solar(payload, request)

    @r.delete(base + "/clear", operation_id=f"solar_clear{tag}", include_in_schema=False)
    async def clear_delete(request: Request):
        return await delete_solar(None, request)

    @r.post(base + "/clear", operation_id=f"solar_clear_post{tag}", include_in_schema=False)
    async def clear_post(payload: Any = Body(default=None),
                         request: Request = None):
        return await delete_solar(payload, request)

    @r.get(base + "/next", operation_id=f"solar_next{tag}", include_in_schema=False)
    async def next_get(request: Request):
        data = _normalize(None, request)
        try:
            result = solar_service.next_transition(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return result

    # Detail routes last.
    @r.get(base + "/{camera_id}", operation_id=f"solar_get_camera{tag}", include_in_schema=False)
    async def get_camera(camera_id: str, request: Request):
        data = _normalize(None, request)
        data["camera_id"] = camera_id
        # Prefer stored config; fall back to resolving the solar profile.
        cfg = solar_service.get_config(camera_id)
        if cfg is not None and not any(data.get(k) is not None for k in (
                "lat", "latitude", "lon", "lng", "longitude", "now", "at",
                "datetime", "resolve", "profile")):
            return cfg
        return await resolve_solar(data, None)

    @r.delete(base + "/{camera_id}", operation_id=f"solar_delete_camera{tag}", include_in_schema=False)
    async def delete_camera(camera_id: str, request: Request):
        ok = solar_service.delete_config(camera_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Solar config not found")
        return {"ok": True, "success": True, "deleted": True,
                "camera_id": camera_id}


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix to be set.
underscore_router = APIRouter(prefix="/api/solar_schedule", tags=["solar_schedule"])
hyphen_router = APIRouter(prefix="/api/solar-schedule", tags=["solar_schedule"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root(request: Request):
        return await list_solar(None, request)

    @r.get("/")
    async def slash(request: Request):
        return await list_solar(None, request)

    @r.get("/list")
    async def lst(request: Request):
        return await list_solar(None, request)

    @r.get("/status")
    async def status():
        return solar_service.status()

    @r.get("/health")
    async def health():
        return {"ok": True, "success": True, "service": "solar_schedule"}

    @r.post("/calculate")
    async def calc(payload: Any = Body(default=None), request: Request = None):
        return await calculate_solar(payload, request)

    @r.post("/configure")
    async def conf(payload: Any = Body(default=None), request: Request = None):
        return await configure_solar(payload, request)

    @r.post("/resolve")
    async def reso(payload: Any = Body(default=None), request: Request = None):
        return await resolve_solar(payload, request)

    @r.post("/apply")
    async def appl(payload: Any = Body(default=None), request: Request = None):
        return await apply_solar(payload, request)

    @r.get("/{camera_id}")
    async def get_cam(camera_id: str, request: Request):
        data = _normalize(None, request)
        data["camera_id"] = camera_id
        cfg = solar_service.get_config(camera_id)
        if cfg is not None:
            return cfg
        return await resolve_solar(data, None)


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

solar_router = router
solar_schedule_router = router

__all__ = ["router", "underscore_router", "hyphen_router",
           "list_solar", "calculate_solar", "configure_solar",
           "resolve_solar", "apply_solar", "delete_solar", "solar_status"]
