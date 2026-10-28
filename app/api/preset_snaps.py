"""Preset snapshot API — capture/attach a visual snapshot when saving a preset.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import Response

import app.services.preset_snaps as snaps_mod
from app.services.preset_snaps import (
    capture_preset_snapshot,
    clear,
    delete_preset_snapshot,
    get_preset_snapshot,
    list_preset_snapshots,
)

router = APIRouter(tags=["preset_snaps"])


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
            data = {"image_base64": bytes(payload).decode("ascii")}
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
        "snapshot",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _ok(result: Any, default_status: str = "captured") -> dict:
    if result is None:
        raise HTTPException(status_code=404, detail="Preset snapshot not found")
    if isinstance(result, bool):
        if not result:
            raise HTTPException(status_code=404, detail="Preset snapshot not found")
        return {"status": "deleted", "ok": True, "success": True}
    if not isinstance(result, dict):
        result = {"result": result, "status": default_status, "ok": True, "success": True}
    result.setdefault("status", default_status)
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


_PATHS = (
    "/api/preset_snaps",
    "/api/preset-snaps",
    "/api/preset_library/snapshots",
    "/api/preset-library/snapshots",
    "/api/ptz/preset_snaps",
    "/api/ptz/preset-snaps",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def list_items(
        request: Request,
        camera_id: Optional[str] = Query(default=None),
    ):
        params = dict(request.query_params)
        kwargs: dict = {}
        cam = camera_id if camera_id is not None else params.get("camera_id", params.get("camera"))
        if cam is not None:
            kwargs["camera_id"] = cam
        for k, v in params.items():
            kwargs.setdefault(k, v)
        return list_preset_snapshots(**kwargs)

    @r.get(base + "/")
    async def list_items_slash(request: Request):
        return await list_items(request)

    @r.get(base + "/list")
    async def list_items_named(request: Request):
        return await list_items(request)

    @r.get(base + "/status")
    async def get_status(request: Request):
        params = dict(request.query_params)
        items = list_preset_snapshots(**params)
        return {"status": "ok", "service": "preset_snaps", "count": len(items), "ok": True, "success": True}

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None):
        n = clear(**_normalize(payload, request))
        return {"cleared": n, "count": n, "removed": n, "ok": True, "success": True}

    @r.post(base + "/capture")
    async def post_capture(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            return _ok(capture_preset_snapshot(**data))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/attach")
    async def post_attach(payload: Any = Body(default=None), request: Request = None):
        return await post_capture(payload, request)

    @r.post(base, status_code=status.HTTP_201_CREATED)
    async def create_item(payload: Any = Body(default=None), request: Request = None):
        return await post_capture(payload, request)

    @r.post(base + "/")
    async def create_item_slash(payload: Any = Body(default=None), request: Request = None):
        return await post_capture(payload, request)

    @r.post(base + "/create")
    async def create_item_named(payload: Any = Body(default=None), request: Request = None):
        return await post_capture(payload, request)

    @r.post(base + "/{preset_id}/capture")
    async def post_capture_by_id(preset_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", preset_id)
        try:
            return _ok(capture_preset_snapshot(**data))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/{preset_id}/attach")
    async def post_attach_by_id(preset_id: str, payload: Any = Body(default=None), request: Request = None):
        return await post_capture_by_id(preset_id, payload, request)

    @r.get(base + "/{preset_id}/image")
    async def get_image_by_id(preset_id: str):
        raw = snaps_mod.get_snapshot_bytes(preset_id)
        if raw is None:
            raise HTTPException(status_code=404, detail="Preset snapshot not found")
        media = "image/jpeg"
        if bytes(raw)[:8] == b"\x89PNG\r\n\x1a\n":
            media = "image/png"
        return Response(content=bytes(raw), media_type=media)

    # Detail routes registered last so /list, /status, /capture, /clear match first.
    @r.get(base + "/{preset_id}")
    async def get_item(preset_id: str):
        item = get_preset_snapshot(preset_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Preset snapshot not found")
        return dict(item)

    @r.put(base + "/{preset_id}")
    async def update_item(preset_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", preset_id)
        try:
            return _ok(capture_preset_snapshot(**data))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.patch(base + "/{preset_id}")
    async def patch_item(preset_id: str, payload: Any = Body(default=None), request: Request = None):
        return await update_item(preset_id, payload, request)

    @r.delete(base + "/{preset_id}")
    async def delete_item(preset_id: str):
        ok = delete_preset_snapshot(preset_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Preset snapshot not found")
        return {"status": "deleted", "ok": True, "success": True, "id": preset_id}


for _base in _PATHS:
    _register(router, _base)


@router.get("/api/preset_library/{preset_id}/snapshot")
async def get_library_snapshot(preset_id: str):
    item = get_preset_snapshot(preset_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Preset snapshot not found")
    return dict(item)


@router.get("/api/preset-library/{preset_id}/snapshot")
async def get_library_snapshot_hyphen(preset_id: str):
    return await get_library_snapshot(preset_id)


@router.get("/api/preset_library/{preset_id}/snapshot/image")
async def get_library_snapshot_image(preset_id: str):
    raw = snaps_mod.get_snapshot_bytes(preset_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="Preset snapshot not found")
    media = "image/jpeg"
    if bytes(raw)[:8] == b"\x89PNG\r\n\x1a\n":
        media = "image/png"
    return Response(content=bytes(raw), media_type=media)


@router.post("/api/preset_library/{preset_id}/snapshot", status_code=status.HTTP_201_CREATED)
async def post_library_snapshot(preset_id: str, payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    data.setdefault("id", preset_id)
    try:
        return _ok(capture_preset_snapshot(**data))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/preset-library/{preset_id}/snapshot", status_code=status.HTTP_201_CREATED)
async def post_library_snapshot_hyphen(preset_id: str, payload: Any = Body(default=None), request: Request = None):
    return await post_library_snapshot(preset_id, payload, request)


# Prefixed alias routers for callers expecting router.prefix to be set.
underscore_router = APIRouter(prefix="/api/preset_snaps", tags=["preset_snaps"])
hyphen_router = APIRouter(prefix="/api/preset-snaps", tags=["preset_snaps"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_list(request: Request):
        params = dict(request.query_params)
        return list_preset_snapshots(**params)

    @r.get("/")
    async def slash_list(request: Request):
        params = dict(request.query_params)
        return list_preset_snapshots(**params)

    @r.get("/list")
    async def named_list(request: Request):
        params = dict(request.query_params)
        return list_preset_snapshots(**params)

    @r.get("/status")
    async def prefixed_status(request: Request):
        params = dict(request.query_params)
        items = list_preset_snapshots(**params)
        return {"status": "ok", "service": "preset_snaps", "count": len(items), "ok": True, "success": True}

    @r.get("/{preset_id}")
    async def by_id_get(preset_id: str):
        item = get_preset_snapshot(preset_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Preset snapshot not found")
        return dict(item)

    @r.get("/{preset_id}/image")
    async def by_id_image(preset_id: str):
        raw = snaps_mod.get_snapshot_bytes(preset_id)
        if raw is None:
            raise HTTPException(status_code=404, detail="Preset snapshot not found")
        media = "image/jpeg"
        if bytes(raw)[:8] == b"\x89PNG\r\n\x1a\n":
            media = "image/png"
        return Response(content=bytes(raw), media_type=media)

    @r.post("", status_code=status.HTTP_201_CREATED)
    async def root_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(capture_preset_snapshot(**_normalize(payload, request)))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.post("/create")
    async def create_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.post("/capture")
    async def capture_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.post("/attach")
    async def attach_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.post("/clear")
    async def clear_post(payload: Any = Body(default=None), request: Request = None):
        n = clear(**_normalize(payload, request))
        return {"cleared": n, "count": n, "ok": True, "success": True}

    @r.post("/{preset_id}/capture")
    async def capture_by_id(preset_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", preset_id)
        try:
            return _ok(capture_preset_snapshot(**data))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.put("/{preset_id}")
    async def put_update(preset_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", preset_id)
        try:
            return _ok(capture_preset_snapshot(**data))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.patch("/{preset_id}")
    async def patch_update(preset_id: str, payload: Any = Body(default=None), request: Request = None):
        return await put_update(preset_id, payload, request)

    @r.delete("/{preset_id}")
    async def by_id_delete(preset_id: str):
        ok = delete_preset_snapshot(preset_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Preset snapshot not found")
        return {"status": "deleted", "ok": True, "success": True, "id": preset_id}


for _r in (underscore_router, hyphen_router):
    _make_prefixed(_r)


# Compatibility aliases.
preset_snaps_router = underscore_router
presetSnapsRouter = underscore_router
preset_snapshots_router = underscore_router

__all__ = [
    "router",
    "underscore_router",
    "hyphen_router",
    "preset_snaps_router",
]
