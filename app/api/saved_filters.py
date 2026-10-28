"""Saved event filter API router (CRUD for named event queries).

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.saved_filters as filters_mod
from app.services.saved_filters import (
    create_filter,
    delete_filter,
    get_filter,
    get_filter_query,
    list_filters,
    update_filter,
)

router = APIRouter(tags=["saved_filters"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"name": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"name": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {"name": str(payload)}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass

    for nested_key in ("saved_filter", "savedFilter", "filter", "filters", "payload",
                        "options", "config", "params", "data", "body", "request", "fields"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _coerce_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _filter_or_404(item: Optional[dict], filter_id: Any = None) -> dict:
    if item is None:
        raise HTTPException(status_code=404, detail="Saved filter not found")
    return item


def _create_from_data(data: dict) -> dict:
    try:
        return create_filter(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _resolve_id(filter_id: Any) -> Any:
    # Path param may be int-like or a name; pass through to service.
    if isinstance(filter_id, str):
        s = filter_id.strip()
        if not s:
            raise HTTPException(status_code=400, detail="filter_id is required")
        nid = _coerce_int_or_none(s)
        return nid if nid is not None else s
    if filter_id is None or isinstance(filter_id, bool):
        raise HTTPException(status_code=400, detail="filter_id is required")
    return filter_id


def _register(r: APIRouter, base: str) -> None:
    @r.get(base, summary="List saved event filters")
    async def _list(
        request: Request,
        q: Optional[str] = Query(None),
        name: Optional[str] = Query(None),
        camera_id: Optional[str] = Query(None),
        limit: Optional[int] = Query(None),
        offset: Optional[int] = Query(None),
        skip: Optional[int] = Query(None),
    ):
        params: dict = {}
        if q is not None:
            params["q"] = q
        if name is not None:
            params["name"] = name
        if camera_id is not None:
            params["camera_id"] = camera_id
        if limit is not None:
            params["limit"] = limit
        params["offset"] = offset if offset is not None else (skip if skip is not None else 0)
        try:
            for k, v in request.query_params.items():
                params.setdefault(k, v)
        except Exception:
            pass
        return list_filters(**params)

    @r.get(base + "/", include_in_schema=False)
    async def _list_slash(
        request: Request,
        q: Optional[str] = Query(None),
        name: Optional[str] = Query(None),
        camera_id: Optional[str] = Query(None),
        limit: Optional[int] = Query(None),
        offset: Optional[int] = Query(None),
        skip: Optional[int] = Query(None),
    ):
        return await _list(request, q, name, camera_id, limit, offset, skip)

    @r.post(base, status_code=status.HTTP_200_OK, summary="Create saved event filter")
    async def _create(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        return _create_from_data(data)

    @r.post(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _create_slash(payload: Any = Body(default=None), request: Request = None):
        return await _create(payload, request)

    @r.post(base + "/create", include_in_schema=False)
    async def _create_named(payload: Any = Body(default=None), request: Request = None):
        return await _create(payload, request)

    @r.get(base + "/{filter_id}", summary="Get saved event filter")
    async def _get(filter_id: str):
        fid = _resolve_id(filter_id)
        return _filter_or_404(get_filter(fid), fid)

    @r.get(base + "/{filter_id}/query", summary="Get saved filter query criteria")
    async def _get_query(filter_id: str):
        fid = _resolve_id(filter_id)
        item = _filter_or_404(get_filter(fid), fid)
        criteria = item.get("criteria") or item.get("filters") or item.get("query") or {}
        return {"filter": item, "criteria": criteria, "query": criteria, "params": criteria}

    @r.get(base + "/{filter_id}/apply", include_in_schema=False)
    async def _apply(filter_id: str):
        return await _get_query(filter_id)

    @r.put(base + "/{filter_id}", summary="Update saved event filter")
    async def _update(filter_id: str, payload: Any = Body(default=None), request: Request = None):
        fid = _resolve_id(filter_id)
        _filter_or_404(get_filter(fid), fid)
        data = _normalize(payload, request)
        # Never allow id overwrite via body.
        for k in ("id", "filter_id", "filterId", "saved_filter_id", "savedFilterId"):
            data.pop(k, None)
        updated = update_filter(fid, **data) if data else update_filter(fid)
        return _filter_or_404(updated, fid)

    @r.patch(base + "/{filter_id}", include_in_schema=False)
    async def _patch(filter_id: str, payload: Any = Body(default=None), request: Request = None):
        return await _update(filter_id, payload, request)

    @r.post(base + "/{filter_id}", include_in_schema=False)
    async def _post_update(filter_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not data:
            fid = _resolve_id(filter_id)
            return _filter_or_404(get_filter(fid), fid)
        return await _update(filter_id, payload, request)

    @r.delete(base + "/{filter_id}", summary="Delete saved event filter")
    async def _delete(filter_id: str):
        fid = _resolve_id(filter_id)
        ok = delete_filter(fid)
        if not ok:
            raise HTTPException(status_code=404, detail="Saved filter not found")
        return {"success": True, "ok": True, "deleted": True, "id": fid}


_BASES = (
    "/api/saved-filters",
    "/api/saved_filters",
    "/api/event-filters",
    "/api/event_filters",
    "/api/events-filters",
    "/api/event-saved-filters",
)
_seen: set = set()
for _base in _BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen:
        continue
    _seen.add(_norm)
    _register(router, _norm)


# Compatibility aliases.
saved_filters_router = router
saved_filter_router = router
event_filters_router = router
event_filter_router = router
api_router = router

__all__ = ["router"]
