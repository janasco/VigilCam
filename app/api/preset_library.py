"""PTZ preset library API endpoints.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.preset_library as lib_mod
from app.services.preset_library import (
    clear,
    create_preset,
    delete_preset,
    get_preset,
    goto_preset,
    list_presets,
    update_preset,
)

router = APIRouter(tags=["preset_library"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        s = payload.strip()
        data = {"id": s} if s else {}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"id": bytes(payload).decode("utf-8", errors="replace")}
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
    for nested_key in (
        "payload",
        "options",
        "config",
        "params",
        "data",
        "body",
        "request",
        "settings",
        "message",
        "preset",
        "preset_library",
        "presetLibrary",
        "entry",
        "item",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _ok(result: Any, default_status: str = "ok") -> dict:
    if result is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    if isinstance(result, bool):
        if not result:
            raise HTTPException(status_code=404, detail="Preset not found")
        return {"status": "deleted", "ok": True, "success": True}
    if not isinstance(result, dict):
        result = {"result": result, "status": default_status, "ok": True, "success": True}
    result.setdefault("status", default_status)
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


_PATHS = (
    "/api/preset_library",
    "/api/preset-library",
    "/api/preset_libraries",
    "/api/preset-libraries",
    "/api/ptz/preset_library",
    "/api/ptz/preset-library",
    "/api/ptz/preset-libraries",
    "/api/ptz/presets/library",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def list_items(
        request: Request,
        camera_id: Optional[str] = Query(default=None),
        name: Optional[str] = Query(default=None),
    ):
        params = dict(request.query_params)
        cam = camera_id if camera_id is not None else params.get("camera_id", params.get("camera"))
        nm = name if name is not None else params.get("name", params.get("preset_name"))
        kwargs: dict = {}
        if cam is not None:
            kwargs["camera_id"] = cam
        if nm is not None:
            kwargs["name"] = nm
        # Pass through any other filters (e.g. preset_token).
        for k, v in params.items():
            kwargs.setdefault(k, v)
        return list_presets(**kwargs)

    @r.get(base + "/")
    async def list_items_slash(request: Request):
        return await list_items(request)

    @r.get(base + "/list")
    async def list_items_named(request: Request):
        return await list_items(request)

    @r.get(base + "/status")
    async def get_status(request: Request):
        params = dict(request.query_params)
        items = list_presets(**params)
        return {"status": "ok", "service": "preset_library", "count": len(items), "ok": True, "success": True}

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None):
        n = clear(**_normalize(payload, request))
        return {"cleared": n, "count": n, "removed": n, "ok": True, "success": True}

    @r.post(base + "/goto")
    async def post_goto(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            return _ok(goto_preset(**data), "moving")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.post(base + "/recall")
    async def post_recall(payload: Any = Body(default=None), request: Request = None):
        return await post_goto(payload, request)

    @r.post(base, status_code=status.HTTP_201_CREATED)
    async def create_item(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            result = create_preset(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        return _ok(result, "created")

    @r.post(base + "/")
    async def create_item_slash(payload: Any = Body(default=None), request: Request = None):
        return await create_item(payload, request)

    @r.post(base + "/create")
    async def create_item_named(payload: Any = Body(default=None), request: Request = None):
        return await create_item(payload, request)

    @r.post(base + "/{preset_id}/goto")
    async def post_goto_by_id(preset_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", preset_id)
        try:
            return _ok(goto_preset(**data), "moving")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.post(base + "/{preset_id}/recall")
    async def post_recall_by_id(preset_id: str, payload: Any = Body(default=None), request: Request = None):
        return await post_goto_by_id(preset_id, payload, request)

    # Detail routes registered last so /list, /status, /goto, /clear match first.
    @r.get(base + "/{preset_id}")
    async def get_item(preset_id: str):
        item = get_preset(preset_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Preset not found")
        return dict(item)

    @r.put(base + "/{preset_id}")
    async def update_item(preset_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        updated = update_preset(preset_id, **data)
        if updated is None:
            raise HTTPException(status_code=404, detail="Preset not found")
        return dict(updated)

    @r.patch(base + "/{preset_id}")
    async def patch_item(preset_id: str, payload: Any = Body(default=None), request: Request = None):
        return await update_item(preset_id, payload, request)

    @r.delete(base + "/{preset_id}")
    async def delete_item(preset_id: str):
        ok = delete_preset(preset_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Preset not found")
        return {"status": "deleted", "ok": True, "success": True, "id": preset_id}


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix to be set.
underscore_router = APIRouter(prefix="/api/preset_library", tags=["preset_library"])
hyphen_router = APIRouter(prefix="/api/preset-library", tags=["preset_library"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_list(request: Request):
        params = dict(request.query_params)
        return list_presets(**params)

    @r.get("/")
    async def slash_list(request: Request):
        params = dict(request.query_params)
        return list_presets(**params)

    @r.get("/list")
    async def named_list(request: Request):
        params = dict(request.query_params)
        return list_presets(**params)

    @r.get("/status")
    async def prefixed_status(request: Request):
        params = dict(request.query_params)
        items = list_presets(**params)
        return {"status": "ok", "service": "preset_library", "count": len(items), "ok": True, "success": True}

    @r.get("/{preset_id}")
    async def by_id_get(preset_id: str):
        item = get_preset(preset_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Preset not found")
        return dict(item)

    @r.post("", status_code=status.HTTP_201_CREATED)
    async def root_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(create_preset(**_normalize(payload, request)), "created")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.post("/create")
    async def create_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.post("/goto")
    async def goto_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(goto_preset(**_normalize(payload, request)), "moving")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.post("/recall")
    async def recall_post(payload: Any = Body(default=None), request: Request = None):
        return await goto_post(payload, request)

    @r.post("/clear")
    async def clear_post(payload: Any = Body(default=None), request: Request = None):
        n = clear(**_normalize(payload, request))
        return {"cleared": n, "count": n, "ok": True, "success": True}

    @r.post("/{preset_id}/goto")
    async def goto_by_id(preset_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", preset_id)
        try:
            return _ok(goto_preset(**data), "moving")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.put("/{preset_id}")
    async def put_update(preset_id: str, payload: Any = Body(default=None), request: Request = None):
        updated = update_preset(preset_id, **_normalize(payload, request))
        if updated is None:
            raise HTTPException(status_code=404, detail="Preset not found")
        return dict(updated)

    @r.patch("/{preset_id}")
    async def patch_update(preset_id: str, payload: Any = Body(default=None), request: Request = None):
        return await put_update(preset_id, payload, request)

    @r.delete("/{preset_id}")
    async def by_id_delete(preset_id: str):
        ok = delete_preset(preset_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Preset not found")
        return {"status": "deleted", "ok": True, "success": True, "id": preset_id}


for _r in (underscore_router, hyphen_router):
    _make_prefixed(_r)


# Compatibility aliases.
preset_library_router = underscore_router
presetLibraryRouter = underscore_router
preset_libraries_router = underscore_router
ptz_preset_library_router = underscore_router

__all__ = [
    "router",
    "underscore_router",
    "hyphen_router",
    "preset_library_router",
]
