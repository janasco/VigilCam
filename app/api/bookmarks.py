from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.bookmarks as bookmarks_service_mod
from app.services.bookmarks import bookmark_service

router = APIRouter(tags=["bookmarks"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"label": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "bookmark", "attributes"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _to_response(bm: Any) -> dict:
    if bm is None:
        return {}
    if isinstance(bm, dict):
        return dict(bm)
    if hasattr(bm, "model_dump"):
        try:
            return dict(bm.model_dump())
        except Exception:  # noqa: BLE001
            pass
    try:
        return dict(bm)
    except Exception:  # noqa: BLE001
        return {"value": bm}


_PATHS = (
    "/api/bookmarks",
    "/api/bookmark",
    "/api/recording_bookmarks",
    "/api/recording-bookmarks",
    "/api/recordings/bookmarks",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def list_items(
        request: Request,
        recording_id: Optional[str] = Query(default=None),
        camera_id: Optional[str] = Query(default=None),
    ):
        params = dict(request.query_params)
        rec = recording_id if recording_id is not None else params.get("recording_id", params.get("recording"))
        cam = camera_id if camera_id is not None else params.get("camera_id", params.get("camera"))
        items = bookmarks_service_mod.list_bookmarks(recording_id=rec, camera_id=cam)
        return [_to_response(b) for b in items]

    @r.get(base + "/")
    async def list_items_slash(
        request: Request,
        recording_id: Optional[str] = Query(default=None),
        camera_id: Optional[str] = Query(default=None),
    ):
        return await list_items(request, recording_id, camera_id)

    @r.get(base + "/list")
    async def list_items_named(
        request: Request,
        recording_id: Optional[str] = Query(default=None),
        camera_id: Optional[str] = Query(default=None),
    ):
        return await list_items(request, recording_id, camera_id)

    @r.get(base + "/{bookmark_id}")
    async def get_item(bookmark_id: str):
        bm = bookmarks_service_mod.get_bookmark(bookmark_id)
        if bm is None:
            raise HTTPException(status_code=404, detail="Bookmark not found")
        return _to_response(bm)

    @r.post(base, status_code=status.HTTP_201_CREATED)
    async def create_item(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            bm = bookmarks_service_mod.create_bookmark(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        resp = _to_response(bm)
        resp.setdefault("status", "created")
        resp.setdefault("ok", True)
        resp.setdefault("success", True)
        return resp

    @r.post(base + "/")
    async def create_item_slash(payload: Any = Body(default=None), request: Request = None):
        return await create_item(payload, request)

    @r.post(base + "/create")
    async def create_item_named(payload: Any = Body(default=None), request: Request = None):
        return await create_item(payload, request)

    @r.put(base + "/{bookmark_id}")
    async def update_item(bookmark_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        bm = bookmarks_service_mod.update_bookmark(bookmark_id, **data)
        if bm is None:
            raise HTTPException(status_code=404, detail="Bookmark not found")
        return _to_response(bm)

    @r.patch(base + "/{bookmark_id}")
    async def patch_item(bookmark_id: str, payload: Any = Body(default=None), request: Request = None):
        return await update_item(bookmark_id, payload, request)

    @r.post(base + "/{bookmark_id}")
    async def post_update_item(bookmark_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not data:
            bm = bookmarks_service_mod.get_bookmark(bookmark_id)
            if bm is None:
                raise HTTPException(status_code=404, detail="Bookmark not found")
            return _to_response(bm)
        return await update_item(bookmark_id, payload, request)

    @r.delete(base + "/{bookmark_id}")
    async def delete_item(bookmark_id: str):
        ok = bookmarks_service_mod.delete_bookmark(bookmark_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Bookmark not found")
        return {"status": "deleted", "ok": True, "success": True, "id": bookmark_id}

    @r.get("/api/recordings/{recording_id}/bookmarks")
    async def list_for_recording(recording_id: str, request: Request):
        params = dict(request.query_params)
        cam = params.get("camera_id", params.get("camera"))
        items = bookmarks_service_mod.list_bookmarks(recording_id=recording_id, camera_id=cam)
        return [_to_response(b) for b in items]

    @r.post("/api/recordings/{recording_id}/bookmarks", status_code=status.HTTP_201_CREATED)
    async def create_for_recording(recording_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("recording_id", recording_id)
        try:
            bm = bookmarks_service_mod.create_bookmark(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        resp = _to_response(bm)
        resp.setdefault("status", "created")
        resp.setdefault("ok", True)
        resp.setdefault("success", True)
        return resp


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias router for callers expecting router.prefix == "/api/bookmarks".
bookmarks_router = APIRouter(prefix="/api/bookmarks", tags=["bookmarks"])


@bookmarks_router.get("")
async def _alias_list(
    request: Request,
    recording_id: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
):
    params = dict(request.query_params)
    rec = recording_id if recording_id is not None else params.get("recording_id", params.get("recording"))
    cam = camera_id if camera_id is not None else params.get("camera_id", params.get("camera"))
    return [_to_response(b) for b in bookmarks_service_mod.list_bookmarks(recording_id=rec, camera_id=cam)]


@bookmarks_router.get("/")
async def _alias_list_slash(request: Request):
    return [_to_response(b) for b in bookmarks_service_mod.list_bookmarks()]


@bookmarks_router.post("", status_code=status.HTTP_201_CREATED)
async def _alias_create(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    bm = bookmarks_service_mod.create_bookmark(**data)
    resp = _to_response(bm)
    resp.setdefault("status", "created")
    resp.setdefault("ok", True)
    return resp


@bookmarks_router.get("/{bookmark_id}")
async def _alias_get(bookmark_id: str):
    bm = bookmarks_service_mod.get_bookmark(bookmark_id)
    if bm is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    return _to_response(bm)


@bookmarks_router.put("/{bookmark_id}")
async def _alias_put(bookmark_id: str, payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    bm = bookmarks_service_mod.update_bookmark(bookmark_id, **data)
    if bm is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    return _to_response(bm)


@bookmarks_router.delete("/{bookmark_id}")
async def _alias_delete(bookmark_id: str):
    ok = bookmarks_service_mod.delete_bookmark(bookmark_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    return {"status": "deleted", "ok": True, "id": bookmark_id}
