from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request

from app.services import motion_presets as presets_service

# Primary router with no prefix so we can serve multiple path variants.
router = APIRouter(tags=["motion_presets"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"preset": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"preset": bytes(payload).decode("utf-8", errors="replace")}
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


def _resolve_name(data: dict) -> Optional[Any]:
    for k in ("preset", "preset_name", "name", "level", "sensitivity_preset", "sensitivity", "mode", "value", "id", "key"):
        if data.get(k) is not None:
            return data.get(k)
    return None


def _list() -> list:
    return presets_service.list_motion_presets()


def _categorized() -> dict:
    return presets_service.get_categorized_presets()


_PATHS = (
    "/api/motion_presets",
    "/api/motion-presets",
    "/api/motionPresets",
    "/api/motion/presets",
    "/api/motion/sensitivity_presets",
    "/api/motion/sensitivity-presets",
    "/api/motion_sensitivity_presets",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_root(request: Request):
        return _list()

    @r.get(base + "/")
    async def get_slash(request: Request):
        return _list()

    @r.get(base + "/list")
    async def get_list(request: Request):
        return _list()

    @r.get(base + "/categorized")
    async def get_categorized(request: Request):
        return _categorized()

    @r.get(base + "/categories")
    async def get_categories(request: Request):
        return _list()

    @r.get(base + "/names")
    async def get_names(request: Request):
        return presets_service.list_preset_names()

    @r.get(base + "/status")
    async def get_status(request: Request):
        return {"status": "ok", "service": "motion_presets", "count": len(_list()), "default": presets_service.DEFAULT_PRESET_NAME}

    @r.get(base + "/default")
    async def get_default(request: Request):
        preset = presets_service.get_preset_or_default(presets_service.DEFAULT_PRESET_NAME)
        preset = dict(preset)
        preset.setdefault("ok", True)
        return preset

    @r.post(base + "/apply")
    async def post_apply(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        name = _resolve_name(data)
        if name is None:
            raise HTTPException(status_code=400, detail="Missing preset name")
        canonical = presets_service.normalize_preset_name(name)
        if canonical is None:
            raise HTTPException(status_code=404, detail=f"Unknown motion preset: {name!r}")
        try:
            config = presets_service.apply_preset(canonical, update_singleton=False)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        out = dict(config)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        out.setdefault("preset", canonical)
        out.setdefault("preset_name", canonical)
        return out

    @r.put(base + "/apply")
    async def put_apply(payload: Any = Body(default=None), request: Request = None):
        return await post_apply(payload, request)

    @r.post(base + "/preview")
    async def post_preview(payload: Any = Body(default=None), request: Request = None):
        return await post_apply(payload, request)

    @r.post(base)
    async def post_root(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        name = _resolve_name(data)
        if name is None:
            # No preset specified -> return list for tolerance.
            return _list()
        canonical = presets_service.normalize_preset_name(name)
        if canonical is None:
            raise HTTPException(status_code=404, detail=f"Unknown motion preset: {name!r}")
        try:
            config = presets_service.apply_preset(canonical, update_singleton=False)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        out = dict(config)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        out.setdefault("preset", canonical)
        out.setdefault("preset_name", canonical)
        return out

    @r.post(base + "/")
    async def post_slash(payload: Any = Body(default=None), request: Request = None):
        return await post_root(payload, request)

    # Detail route must be registered last so /list, /apply, etc. match first.
    @r.get(base + "/{preset_name}")
    async def get_detail(preset_name: str, request: Request):
        canonical = presets_service.normalize_preset_name(preset_name)
        if canonical is None:
            raise HTTPException(status_code=404, detail=f"Unknown motion preset: {preset_name!r}")
        preset = presets_service.get_preset(canonical)
        if preset is None:
            raise HTTPException(status_code=404, detail=f"Unknown motion preset: {preset_name!r}")
        preset = dict(preset)
        preset.setdefault("ok", True)
        return preset


for base in _PATHS:
    _register(router, base)


# Prefixed alias routers (for callers expecting router.prefix to be set).
underscore_router = APIRouter(prefix="/api/motion_presets", tags=["motion_presets"])
hyphen_router = APIRouter(prefix="/api/motion-presets", tags=["motion_presets"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root(request: Request):
        return _list()

    @r.get("/")
    async def slash(request: Request):
        return _list()

    @r.get("/list")
    async def lst(request: Request):
        return _list()

    @r.get("/categorized")
    async def categorized(request: Request):
        return _categorized()

    @r.get("/categories")
    async def categories(request: Request):
        return _list()

    @r.get("/names")
    async def names(request: Request):
        return presets_service.list_preset_names()

    @r.get("/status")
    async def status(request: Request):
        return {"status": "ok", "service": "motion_presets", "count": len(_list()), "default": presets_service.DEFAULT_PRESET_NAME}

    @r.get("/default")
    async def default(request: Request):
        preset = presets_service.get_preset_or_default(presets_service.DEFAULT_PRESET_NAME)
        preset = dict(preset)
        preset.setdefault("ok", True)
        return preset

    @r.post("/apply")
    async def apply_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        name = _resolve_name(data)
        if name is None:
            raise HTTPException(status_code=400, detail="Missing preset name")
        canonical = presets_service.normalize_preset_name(name)
        if canonical is None:
            raise HTTPException(status_code=404, detail=f"Unknown motion preset: {name!r}")
        config = presets_service.apply_preset(canonical, update_singleton=False)
        out = dict(config)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        out.setdefault("preset", canonical)
        out.setdefault("preset_name", canonical)
        return out

    @r.put("/apply")
    async def apply_put(payload: Any = Body(default=None), request: Request = None):
        return await apply_post(payload, request)

    @r.post("/preview")
    async def preview_post(payload: Any = Body(default=None), request: Request = None):
        return await apply_post(payload, request)

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        name = _resolve_name(data)
        if name is None:
            return _list()
        canonical = presets_service.normalize_preset_name(name)
        if canonical is None:
            raise HTTPException(status_code=404, detail=f"Unknown motion preset: {name!r}")
        config = presets_service.apply_preset(canonical, update_singleton=False)
        out = dict(config)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        out.setdefault("preset", canonical)
        out.setdefault("preset_name", canonical)
        return out

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.get("/{preset_name}")
    async def detail(preset_name: str, request: Request):
        canonical = presets_service.normalize_preset_name(preset_name)
        if canonical is None:
            raise HTTPException(status_code=404, detail=f"Unknown motion preset: {preset_name!r}")
        preset = presets_service.get_preset(canonical)
        if preset is None:
            raise HTTPException(status_code=404, detail=f"Unknown motion preset: {preset_name!r}")
        preset = dict(preset)
        preset.setdefault("ok", True)
        return preset


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)


# Compatibility aliases.
motion_presets_router = underscore_router
motionPresetsRouter = underscore_router
motion_presets_api_router = underscore_router
motion_presets_hyphen_router = hyphen_router
sensitivity_presets_router = underscore_router
motion_sensitivity_router = underscore_router

__all__ = [
    "router",
    "underscore_router",
    "hyphen_router",
    "motion_presets_router",
    "motionPresetsRouter",
    "motion_presets_api_router",
    "motion_presets_hyphen_router",
    "sensitivity_presets_router",
    "motion_sensitivity_router",
]
