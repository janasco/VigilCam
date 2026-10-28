"""Note snapshot API — attach a visual snapshot to an event/camera note.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import Response

import app.services.note_snaps as snaps_mod
from app.services.note_snaps import (
    attach_snapshot,
    capture_note_snapshot,
    clear,
    delete_note_snapshot,
    get_note_snapshot,
    list_note_snapshots,
)

router = APIRouter(tags=["note_snaps"])


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
        "note",
        "comment",
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
        raise HTTPException(status_code=404, detail="Note snapshot not found")
    if isinstance(result, bool):
        if not result:
            raise HTTPException(status_code=404, detail="Note snapshot not found")
        return {"status": "deleted", "ok": True, "success": True}
    if not isinstance(result, dict):
        result = {"result": result, "status": default_status, "ok": True, "success": True}
    result.setdefault("status", default_status)
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


_PATHS = (
    "/api/note_snaps",
    "/api/note-snaps",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def list_items(
        request: Request,
        note_type: Optional[str] = Query(default=None),
    ):
        params = dict(request.query_params)
        kwargs: dict = {}
        if note_type is not None:
            kwargs["note_type"] = note_type
        for k, v in params.items():
            kwargs.setdefault(k, v)
        return list_note_snapshots(**kwargs)

    @r.get(base + "/")
    async def list_items_slash(request: Request):
        return await list_items(request)

    @r.get(base + "/list")
    async def list_items_named(request: Request):
        return await list_items(request)

    @r.get(base + "/status")
    async def get_status(request: Request):
        params = dict(request.query_params)
        items = list_note_snapshots(**params)
        return {"status": "ok", "service": "note_snaps", "count": len(items), "ok": True, "success": True}

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None):
        n = clear(**_normalize(payload, request))
        return {"cleared": n, "count": n, "removed": n, "ok": True, "success": True}

    @r.post(base + "/capture")
    async def post_capture(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            return _ok(capture_note_snapshot(**data))
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

    @r.post(base + "/{note_id}/capture")
    async def post_capture_by_id(note_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", note_id)
        try:
            return _ok(capture_note_snapshot(**data))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/{note_id}/attach")
    async def post_attach_by_id(note_id: str, payload: Any = Body(default=None), request: Request = None):
        return await post_capture_by_id(note_id, payload, request)

    @r.get(base + "/{note_id}/image")
    async def get_image_by_id(note_id: str, request: Request):
        params = dict(request.query_params) if request is not None else {}
        raw = snaps_mod.get_snapshot_bytes(note_id, **params)
        if raw is None:
            raise HTTPException(status_code=404, detail="Note snapshot not found")
        media = "image/jpeg"
        if bytes(raw)[:8] == b"\x89PNG\r\n\x1a\n":
            media = "image/png"
        return Response(content=bytes(raw), media_type=media)

    # Detail routes registered last so /list, /status, /capture, /clear match first.
    @r.get(base + "/{note_id}")
    async def get_item(note_id: str, request: Request):
        params = dict(request.query_params) if request is not None else {}
        item = get_note_snapshot(note_id, **params)
        if item is None:
            raise HTTPException(status_code=404, detail="Note snapshot not found")
        return dict(item)

    @r.put(base + "/{note_id}")
    async def update_item(note_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", note_id)
        try:
            return _ok(capture_note_snapshot(**data))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.patch(base + "/{note_id}")
    async def patch_item(note_id: str, payload: Any = Body(default=None), request: Request = None):
        return await update_item(note_id, payload, request)

    @r.delete(base + "/{note_id}")
    async def delete_item(note_id: str, request: Request):
        params = dict(request.query_params) if request is not None else {}
        ok = delete_note_snapshot(note_id, **params)
        if not ok:
            raise HTTPException(status_code=404, detail="Note snapshot not found")
        return {"status": "deleted", "ok": True, "success": True, "id": note_id}


for _base in _PATHS:
    _register(router, _base)


def _scoped_kind(base: str) -> str:
    return "camera" if "camera" in base else "event"


def _register_note_scoped(r: APIRouter, base: str) -> None:
    """Convenience: /api/event-notes/{note_id}/snapshot and camera equivalent."""
    kind = _scoped_kind(base)

    @r.get(base + "/{note_id}/snapshot")
    async def get_scoped(note_id: int, request: Request):
        params = dict(request.query_params) if request is not None else {}
        params.setdefault("note_type", kind)
        item = get_note_snapshot(note_id, **params)
        if item is None:
            raise HTTPException(status_code=404, detail="Note snapshot not found")
        return dict(item)

    @r.get(base + "/{note_id}/snapshot/image")
    async def get_scoped_image(note_id: int, request: Request):
        params = dict(request.query_params) if request is not None else {}
        params.setdefault("note_type", kind)
        raw = snaps_mod.get_snapshot_bytes(note_id, **params)
        if raw is None:
            raise HTTPException(status_code=404, detail="Note snapshot not found")
        media = "image/jpeg"
        if bytes(raw)[:8] == b"\x89PNG\r\n\x1a\n":
            media = "image/png"
        return Response(content=bytes(raw), media_type=media)

    @r.post(base + "/{note_id}/snapshot", status_code=status.HTTP_201_CREATED)
    async def post_scoped(note_id: int, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", note_id)
        data.setdefault("note_type", kind)
        try:
            return _ok(capture_note_snapshot(**data))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/{note_id}/snapshot/capture", status_code=status.HTTP_201_CREATED)
    async def post_scoped_capture(note_id: int, payload: Any = Body(default=None), request: Request = None):
        return await post_scoped(note_id, payload, request)

    @r.post(base + "/{note_id}/snapshot/attach", status_code=status.HTTP_201_CREATED)
    async def post_scoped_attach(note_id: int, payload: Any = Body(default=None), request: Request = None):
        return await post_scoped(note_id, payload, request)

    @r.delete(base + "/{note_id}/snapshot")
    async def delete_scoped(note_id: int, request: Request):
        params = dict(request.query_params) if request is not None else {}
        params.setdefault("note_type", kind)
        ok = delete_note_snapshot(note_id, **params)
        if not ok:
            raise HTTPException(status_code=404, detail="Note snapshot not found")
        return {"status": "deleted", "ok": True, "success": True, "id": note_id}


for _scoped in ("/api/event-notes", "/api/event_notes", "/api/camera-notes", "/api/camera_notes"):
    _register_note_scoped(router, _scoped)


# Prefixed alias routers for callers expecting router.prefix to be set.
underscore_router = APIRouter(prefix="/api/note_snaps", tags=["note_snaps"])
hyphen_router = APIRouter(prefix="/api/note-snaps", tags=["note_snaps"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_list(request: Request):
        params = dict(request.query_params)
        return list_note_snapshots(**params)

    @r.get("/")
    async def slash_list(request: Request):
        params = dict(request.query_params)
        return list_note_snapshots(**params)

    @r.get("/list")
    async def named_list(request: Request):
        params = dict(request.query_params)
        return list_note_snapshots(**params)

    @r.get("/status")
    async def prefixed_status(request: Request):
        params = dict(request.query_params)
        items = list_note_snapshots(**params)
        return {"status": "ok", "service": "note_snaps", "count": len(items), "ok": True, "success": True}

    @r.get("/{note_id}")
    async def by_id_get(note_id: str, request: Request):
        params = dict(request.query_params) if request is not None else {}
        item = get_note_snapshot(note_id, **params)
        if item is None:
            raise HTTPException(status_code=404, detail="Note snapshot not found")
        return dict(item)

    @r.get("/{note_id}/image")
    async def by_id_image(note_id: str, request: Request):
        params = dict(request.query_params) if request is not None else {}
        raw = snaps_mod.get_snapshot_bytes(note_id, **params)
        if raw is None:
            raise HTTPException(status_code=404, detail="Note snapshot not found")
        media = "image/jpeg"
        if bytes(raw)[:8] == b"\x89PNG\r\n\x1a\n":
            media = "image/png"
        return Response(content=bytes(raw), media_type=media)

    @r.post("", status_code=status.HTTP_201_CREATED)
    async def root_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(capture_note_snapshot(**_normalize(payload, request)))
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

    @r.post("/{note_id}/capture")
    async def capture_by_id(note_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", note_id)
        try:
            return _ok(capture_note_snapshot(**data))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.put("/{note_id}")
    async def put_update(note_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", note_id)
        try:
            return _ok(capture_note_snapshot(**data))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.patch("/{note_id}")
    async def patch_update(note_id: str, payload: Any = Body(default=None), request: Request = None):
        return await put_update(note_id, payload, request)

    @r.delete("/{note_id}")
    async def by_id_delete(note_id: str, request: Request):
        params = dict(request.query_params) if request is not None else {}
        ok = delete_note_snapshot(note_id, **params)
        if not ok:
            raise HTTPException(status_code=404, detail="Note snapshot not found")
        return {"status": "deleted", "ok": True, "success": True, "id": note_id}


for _r in (underscore_router, hyphen_router):
    _make_prefixed(_r)


# Compatibility aliases.
note_snaps_router = underscore_router
noteSnapsRouter = underscore_router
note_snapshots_router = underscore_router
attach_router = underscore_router

__all__ = [
    "router",
    "underscore_router",
    "hyphen_router",
    "note_snaps_router",
]
