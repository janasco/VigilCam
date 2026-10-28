"""Day/night imaging profile endpoints (on-demand apply, no auto-switching).

Auto-registered by ``app.main``'s ``pkgutil`` loader (module exposes
``router``), so no ``app/main.py`` edit is needed. Service work lives in
``app.services.daynight``; this module only validates input and maps
errors to HTTP status codes. Applying a profile writes to the camera via
``app.services.imaging`` strictly on demand -- there is no schedule or
background task here.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from app.services import daynight as daynight_service

router = APIRouter(tags=["daynight"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        s = payload.strip()
        if s.lower() in ("day", "night", "daytime", "nighttime",
                         "day_time", "night_time"):
            data = {"profile": s}
        elif s:
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

    for nested_key in ("payload", "options", "config", "params", "data",
                       "body", "request", "settings", "levels", "profile_data",
                       "profile", "daynight", "day_night", "day-night"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _require_camera_id(data: dict) -> Any:
    for k in ("camera_id", "camera", "cam", "cam_id", "device_id", "id"):
        if data.get(k) is not None and str(data.get(k)).strip():
            return data.get(k)
    raise HTTPException(status_code=400, detail="camera_id is required")


def _profile_or_404(camera_id: Any, profile: Any) -> dict:
    prof = daynight_service.get_profile(camera_id, profile)
    if prof is None:
        raise HTTPException(status_code=404, detail="Day/night profile not found")
    return prof


async def list_daynight(payload: Any = Body(default=None),
                        request: Request | None = None) -> dict:
    data = _normalize(payload, request)
    raw_cam = None
    for k in ("camera_id", "camera", "cam", "cam_id", "device_id", "id"):
        if data.get(k) is not None:
            raw_cam = data.get(k)
            break
    profile = None
    for k in ("profile", "profile_name", "name", "mode", "preset",
              "when", "period", "phase"):
        if data.get(k) is not None:
            profile = data.get(k)
            break
    if raw_cam is not None and profile is not None:
        return _profile_or_404(raw_cam, profile)
    if raw_cam is not None:
        try:
            return daynight_service.get_daynight(raw_cam)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    items = daynight_service.list_profiles()
    return {"ok": True, "success": True, "profiles": items,
            "items": items, "count": len(items)}


async def get_daynight(payload: Any = Body(default=None),
                       request: Request | None = None) -> dict:
    data = _normalize(payload, request)
    raw_cam = None
    for k in ("camera_id", "camera", "cam", "cam_id", "device_id", "id"):
        if data.get(k) is not None:
            raw_cam = data.get(k)
            break
    if raw_cam is None:
        # No camera given -> list everything (tolerant).
        return await list_daynight(payload, request)
    profile = None
    for k in ("profile", "profile_name", "name", "mode", "preset",
              "when", "period", "phase"):
        if data.get(k) is not None:
            profile = data.get(k)
            break
    if profile is not None:
        return _profile_or_404(raw_cam, profile)
    try:
        return daynight_service.get_daynight(raw_cam)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def set_daynight(payload: Any = Body(default=None),
                       request: Request | None = None) -> dict:
    data = _normalize(payload, request)
    try:
        result = daynight_service.set_profile(**data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not isinstance(result, dict):
        return {"ok": True, "success": True, "result": result}
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


async def apply_daynight(payload: Any = Body(default=None),
                         request: Request | None = None) -> dict:
    data = _normalize(payload, request)
    try:
        result = await daynight_service.apply_profile(**data)
    except ValueError as exc:
        msg = str(exc)
        # Missing connection/levels -> 422 like the imaging write path;
        # unknown camera/profile -> 404; otherwise 400.
        lowered = msg.lower()
        if "ip/host" in lowered or "at least one of" in lowered \
                or "no imaging levels" in lowered or "invalid imaging level" in lowered:
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


async def delete_daynight(payload: Any = Body(default=None),
                          request: Request | None = None) -> dict:
    data = _normalize(payload, request)
    raw_cam = None
    for k in ("camera_id", "camera", "cam", "cam_id", "device_id", "id"):
        if data.get(k) is not None:
            raw_cam = data.get(k)
            break
    profile = None
    for k in ("profile", "profile_name", "name", "mode", "preset",
              "when", "period", "phase"):
        if data.get(k) is not None:
            profile = data.get(k)
            break
    if raw_cam is not None and profile is not None:
        ok = daynight_service.delete_profile(raw_cam, profile)
        if not ok:
            raise HTTPException(status_code=404, detail="Day/night profile not found")
        return {"ok": True, "success": True, "deleted": True,
                "camera_id": raw_cam, "profile": profile}
    if raw_cam is not None:
        n = daynight_service.delete_profiles_for_camera(raw_cam)
        return {"ok": True, "success": True, "deleted_count": n,
                "camera_id": raw_cam}
    n = daynight_service.clear()
    return {"ok": True, "success": True, "cleared": n, "cleared_count": n}


async def daynight_status(payload: Any = Body(default=None),
                          request: Request | None = None) -> dict:
    return daynight_service.verify_no_auto_switch()


_PATHS = (
    "/api/daynight",
    "/api/day-night",
    "/api/day_night",
    "/api/imaging/daynight",
    "/api/imaging/day-night",
    "/api/imaging/day_night",
)


def _register(r: APIRouter, base: str) -> None:
    tag = base.replace("/", "_")

    @r.get(base, operation_id=f"daynight_root{tag}")
    async def root_get(request: Request):
        return await list_daynight(None, request)

    @r.get(base + "/", operation_id=f"daynight_root_slash{tag}", include_in_schema=False)
    async def root_get_slash(request: Request):
        return await list_daynight(None, request)

    @r.get(base + "/list", operation_id=f"daynight_list{tag}")
    async def list_get(request: Request):
        return await list_daynight(None, request)

    @r.get(base + "/status", operation_id=f"daynight_status{tag}")
    async def status_get(request: Request):
        return await daynight_status(None, request)

    @r.get(base + "/health", operation_id=f"daynight_health{tag}")
    async def health_get():
        return {"ok": True, "success": True, "service": "daynight"}

    @r.get(base + "/verify", operation_id=f"daynight_verify{tag}")
    async def verify_get():
        return daynight_service.verify_no_auto_switch()

    @r.post(base + "/set", operation_id=f"daynight_set{tag}")
    async def set_post(payload: Any = Body(default=None), request: Request = None):
        return await set_daynight(payload, request)

    @r.put(base + "/set", operation_id=f"daynight_set_put{tag}", include_in_schema=False)
    async def set_put(payload: Any = Body(default=None), request: Request = None):
        return await set_daynight(payload, request)

    @r.patch(base + "/set", operation_id=f"daynight_set_patch{tag}", include_in_schema=False)
    async def set_patch(payload: Any = Body(default=None), request: Request = None):
        return await set_daynight(payload, request)

    @r.post(base, operation_id=f"daynight_create{tag}", include_in_schema=False)
    async def create_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        # Bare create with levels/camera -> set; otherwise list.
        has_levels = any(data.get(k) is not None for k in (
            "brightness", "contrast", "saturation", "bright", "cont",
            "sat", "satur"))
        has_cam = any(data.get(k) is not None for k in (
            "camera_id", "camera", "cam", "cam_id", "device_id", "id"))
        if has_levels or (has_cam and any(data.get(k) is not None for k in (
                "profile", "profile_name", "name", "mode"))):
            return await set_daynight(payload, request)
        return await list_daynight(payload, request)

    @r.post(base + "/get", operation_id=f"daynight_get{tag}")
    async def get_post(payload: Any = Body(default=None), request: Request = None):
        return await get_daynight(payload, request)

    @r.get(base + "/get", operation_id=f"daynight_get_get{tag}", include_in_schema=False)
    async def get_get(request: Request):
        return await get_daynight(None, request)

    @r.post(base + "/apply", operation_id=f"daynight_apply{tag}")
    async def apply_post(payload: Any = Body(default=None), request: Request = None):
        return await apply_daynight(payload, request)

    @r.put(base + "/apply", operation_id=f"daynight_apply_put{tag}", include_in_schema=False)
    async def apply_put(payload: Any = Body(default=None), request: Request = None):
        return await apply_daynight(payload, request)

    @r.post(base + "/apply-day", operation_id=f"daynight_apply_day{tag}", include_in_schema=False)
    async def apply_day_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data["profile"] = "day"
        return await apply_daynight(data, None)

    @r.post(base + "/apply-night", operation_id=f"daynight_apply_night{tag}", include_in_schema=False)
    async def apply_night_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data["profile"] = "night"
        return await apply_daynight(data, None)

    @r.post(base + "/delete", operation_id=f"daynight_delete_post{tag}", include_in_schema=False)
    async def delete_post(payload: Any = Body(default=None), request: Request = None):
        return await delete_daynight(payload, request)

    @r.delete(base + "/clear", operation_id=f"daynight_clear{tag}", include_in_schema=False)
    async def clear_delete(request: Request):
        return await delete_daynight(None, request)

    @r.post(base + "/clear", operation_id=f"daynight_clear_post{tag}", include_in_schema=False)
    async def clear_post(payload: Any = Body(default=None), request: Request = None):
        return await delete_daynight(payload, request)

    # Detail routes last so /list, /status, /apply, etc. match first.
    @r.get(base + "/{camera_id}", operation_id=f"daynight_get_camera{tag}", include_in_schema=False)
    async def get_camera(camera_id: str, request: Request):
        data = _normalize(None, request)
        data["camera_id"] = camera_id
        return await get_daynight(data, None)

    @r.get(base + "/{camera_id}/{profile}", operation_id=f"daynight_get_one{tag}", include_in_schema=False)
    async def get_one(camera_id: str, profile: str, request: Request):
        return _profile_or_404(camera_id, profile)

    @r.post(base + "/{camera_id}/{profile}", operation_id=f"daynight_set_one{tag}", include_in_schema=False)
    async def set_one(camera_id: str, profile: str,
                      payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data["camera_id"] = camera_id
        data["profile"] = profile
        return await set_daynight(data, None)

    @r.post(base + "/{camera_id}/apply/{profile}",
            operation_id=f"daynight_apply_one{tag}", include_in_schema=False)
    async def apply_one(camera_id: str, profile: str,
                        payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data["camera_id"] = camera_id
        data["profile"] = profile
        return await apply_daynight(data, None)


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix to be set.
underscore_router = APIRouter(prefix="/api/daynight", tags=["daynight"])
hyphen_router = APIRouter(prefix="/api/day-night", tags=["daynight"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root(request: Request):
        return await list_daynight(None, request)

    @r.get("/")
    async def slash(request: Request):
        return await list_daynight(None, request)

    @r.get("/list")
    async def lst(request: Request):
        return await list_daynight(None, request)

    @r.get("/status")
    async def status():
        return daynight_service.verify_no_auto_switch()

    @r.get("/health")
    async def health():
        return {"ok": True, "success": True, "service": "daynight"}

    @r.post("/set")
    async def apply_set(payload: Any = Body(default=None), request: Request = None):
        return await set_daynight(payload, request)

    @r.post("/get")
    async def get_p(payload: Any = Body(default=None), request: Request = None):
        return await get_daynight(payload, request)

    @r.post("/apply")
    async def apply_p(payload: Any = Body(default=None), request: Request = None):
        return await apply_daynight(payload, request)

    @r.get("/{camera_id}")
    async def get_cam(camera_id: str, request: Request):
        data = _normalize(None, request)
        data["camera_id"] = camera_id
        return await get_daynight(data, None)

    @r.get("/{camera_id}/{profile}")
    async def get_prof(camera_id: str, profile: str):
        return _profile_or_404(camera_id, profile)


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

daynight_router = router
day_night_router = router

__all__ = ["router", "underscore_router", "hyphen_router",
           "list_daynight", "get_daynight", "set_daynight",
           "apply_daynight", "delete_daynight", "daynight_status"]
