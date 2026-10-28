"""Zone shape templates API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required. Exposes the shape presets from
:mod:`app.services.zone_templates` (full / entry / exit / driveway /
perimeter) as polygon zones compatible with ``motion_zoning``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from app.services import zone_templates as templates_service

# Primary router with no prefix so we can serve multiple path variants.
router = APIRouter(tags=["zone_templates"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"template": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"template": bytes(payload).decode("utf-8", errors="replace")}
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
                       "request", "settings", "message", "template", "zone_template"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _resolve_name(data: dict) -> Any | None:
    for k in ("template", "template_name", "name", "shape", "zone_template",
              "preset", "preset_name", "key", "id", "value", "mode"):
        if data.get(k) is not None:
            return data.get(k)
    return None


def _coerce_dim(value: Any) -> Any | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dims(data: dict) -> tuple[Any | None, Any | None]:
    w = None
    h = None
    for k in ("width", "w", "frame_width", "img_width", "cols"):
        if data.get(k) is not None:
            w = _coerce_dim(data.get(k))
            break
    for k in ("height", "h", "frame_height", "img_height", "rows"):
        if data.get(k) is not None:
            h = _coerce_dim(data.get(k))
            break
    return w, h


def _list() -> list:
    return templates_service.list_zone_templates()


def _detail_payload(name: str, data: dict) -> dict:
    template = templates_service.get_template(name)
    if template is None:
        raise HTTPException(status_code=404, detail=f"Unknown zone template: {name!r}")
    out = dict(template)
    w, h = _dims(data)
    if w is not None or h is not None:
        out["zones"] = templates_service.get_template_zones(
            template.get("name"), width=w or 1.0, height=h or 1.0,
        )
    out.setdefault("ok", True)
    return out


_PATHS = (
    "/api/zone_templates",
    "/api/zone-templates",
    "/api/zonetemplates",
    "/api/zones/templates",
    "/api/motion/zone_templates",
    "/api/motion/zone-templates",
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

    @r.get(base + "/names")
    async def get_names(request: Request):
        return templates_service.list_template_names()

    @r.get(base + "/status")
    async def get_status(request: Request):
        return {"status": "ok", "service": "zone_templates",
                "count": len(_list()),
                "default": templates_service.DEFAULT_TEMPLATE_NAME}

    @r.post(base + "/apply")
    async def post_apply(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        name = _resolve_name(data)
        if name is None:
            raise HTTPException(status_code=400, detail="Missing template name")
        canonical = templates_service.normalize_template_name(name)
        if canonical is None:
            raise HTTPException(status_code=404, detail=f"Unknown zone template: {name!r}")
        w, h = _dims(data)
        if w is not None or h is not None:
            zones = templates_service.get_template_zones(canonical, width=w or 1.0, height=h or 1.0)
        else:
            zones = templates_service.get_template_zones(canonical)
        return {"ok": True, "success": True, "template": canonical,
                "template_name": canonical, "name": canonical, "zones": zones}

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
            return _list()
        return await post_apply(payload, request)

    @r.post(base + "/")
    async def post_slash(payload: Any = Body(default=None), request: Request = None):
        return await post_root(payload, request)

    # Detail routes registered last so /list, /apply, etc. match first.
    @r.get(base + "/{template_name}/zones")
    async def get_detail_zones(template_name: str, request: Request):
        data = _normalize(None, request)
        canonical = templates_service.normalize_template_name(template_name)
        if canonical is None:
            raise HTTPException(status_code=404, detail=f"Unknown zone template: {template_name!r}")
        w, h = _dims(data)
        if w is not None or h is not None:
            return templates_service.get_template_zones(canonical, width=w or 1.0, height=h or 1.0)
        return templates_service.get_template_zones(canonical)

    @r.get(base + "/{template_name}")
    async def get_detail(template_name: str, request: Request):
        data = _normalize(None, request)
        canonical = templates_service.normalize_template_name(template_name)
        if canonical is None:
            raise HTTPException(status_code=404, detail=f"Unknown zone template: {template_name!r}")
        return _detail_payload(canonical, data)


for base in _PATHS:
    _register(router, base)


# Prefixed alias routers (for callers expecting router.prefix to be set).
underscore_router = APIRouter(prefix="/api/zone_templates", tags=["zone_templates"])
hyphen_router = APIRouter(prefix="/api/zone-templates", tags=["zone_templates"])


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

    @r.get("/names")
    async def names(request: Request):
        return templates_service.list_template_names()

    @r.get("/status")
    async def status(request: Request):
        return {"status": "ok", "service": "zone_templates",
                "count": len(_list()),
                "default": templates_service.DEFAULT_TEMPLATE_NAME}

    @r.post("/apply")
    async def apply_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        name = _resolve_name(data)
        if name is None:
            raise HTTPException(status_code=400, detail="Missing template name")
        canonical = templates_service.normalize_template_name(name)
        if canonical is None:
            raise HTTPException(status_code=404, detail=f"Unknown zone template: {name!r}")
        w, h = _dims(data)
        if w is not None or h is not None:
            zones = templates_service.get_template_zones(canonical, width=w or 1.0, height=h or 1.0)
        else:
            zones = templates_service.get_template_zones(canonical)
        return {"ok": True, "success": True, "template": canonical,
                "template_name": canonical, "name": canonical, "zones": zones}

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
        return await apply_post(payload, request)

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.get("/{template_name}/zones")
    async def detail_zones(template_name: str, request: Request):
        data = _normalize(None, request)
        canonical = templates_service.normalize_template_name(template_name)
        if canonical is None:
            raise HTTPException(status_code=404, detail=f"Unknown zone template: {template_name!r}")
        w, h = _dims(data)
        if w is not None or h is not None:
            return templates_service.get_template_zones(canonical, width=w or 1.0, height=h or 1.0)
        return templates_service.get_template_zones(canonical)

    @r.get("/{template_name}")
    async def detail(template_name: str, request: Request):
        data = _normalize(None, request)
        canonical = templates_service.normalize_template_name(template_name)
        if canonical is None:
            raise HTTPException(status_code=404, detail=f"Unknown zone template: {template_name!r}")
        return _detail_payload(canonical, data)


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)


# Compatibility aliases.
zone_templates_router = underscore_router
zoneTemplatesRouter = underscore_router
zone_templates_api_router = underscore_router
zone_templates_hyphen_router = hyphen_router
shape_templates_router = underscore_router

__all__ = [
    "router",
    "underscore_router",
    "hyphen_router",
    "zone_templates_router",
    "zoneTemplatesRouter",
    "zone_templates_api_router",
    "zone_templates_hyphen_router",
    "shape_templates_router",
]
