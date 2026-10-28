"""Camera service-log API router (structured per-camera service history).

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.service_log as service_log_mod
from app.services.service_log import (
    create_entry,
    delete_entry,
    delete_entries_for_camera,
    get_entry,
    list_entries,
    update_entry,
)

router = APIRouter(tags=["service_log"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"summary": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"summary": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {"summary": str(payload)}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass

    for nested_key in ("entry", "log", "record", "payload", "options",
                       "config", "params", "data", "body", "request",
                       "fields", "service"):
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


def _normalize_camera_id(value: Any) -> Any:
    coerced = _coerce_int_or_none(value)
    if coerced is not None:
        return coerced
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _require_camera_id(camera_id: Any) -> Any:
    cid = _normalize_camera_id(camera_id)
    if cid is None or (isinstance(cid, str) and not cid.strip()):
        raise HTTPException(status_code=400, detail="camera_id is required")
    return cid


def _entry_or_404(entry: Optional[dict], entry_id: Any = None) -> dict:
    if entry is None:
        raise HTTPException(status_code=404, detail="Service log entry not found")
    return entry


def _create_for_camera(camera_id: Any, data: dict) -> dict:
    data = dict(data)
    data["camera_id"] = camera_id
    try:
        return create_entry(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _get_scoped(camera_id: Any, entry_id: int) -> dict:
    entry = get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Service log entry not found")
    try:
        if str(entry.get("camera_id")) != str(camera_id):
            raise HTTPException(status_code=404, detail="Service log entry not found")
    except HTTPException:
        raise
    except (TypeError, ValueError):
        pass
    return entry


def _update_scoped(camera_id: Any, entry_id: int, data: dict) -> dict:
    _get_scoped(camera_id, entry_id)
    data = {k: v for k, v in dict(data).items()
            if k not in ("camera_id", "cameraId", "camera", "cam_id", "cam")}
    updated = update_entry(entry_id, **data) if data else update_entry(entry_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Service log entry not found")
    return updated


def _delete_scoped(camera_id: Any, entry_id: int) -> dict:
    _get_scoped(camera_id, entry_id)
    ok = delete_entry(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Service log entry not found")
    return {"success": True, "ok": True, "deleted": [int(entry_id)], "id": int(entry_id)}


def _register_camera_routes(r: APIRouter, base: str) -> None:
    @r.get(base, summary="List service log entries for camera")
    async def _list(
        camera_id: Any,
        request: Request,
        limit: Optional[int] = Query(None),
        offset: Optional[int] = Query(None),
        skip: Optional[int] = Query(None),
        q: Optional[str] = Query(None),
        service_type: Optional[str] = Query(None),
    ):
        cid = _require_camera_id(camera_id)
        params: dict = {"camera_id": cid}
        if limit is not None:
            params["limit"] = limit
        params["offset"] = offset if offset is not None else (skip if skip is not None else 0)
        if q is not None:
            params["q"] = q
        if service_type is not None:
            params["service_type"] = service_type
        try:
            for k, v in request.query_params.items():
                params.setdefault(k, v)
        except Exception:
            pass
        return list_entries(**params)

    @r.post(base, status_code=status.HTTP_200_OK, summary="Create service log entry for camera")
    @r.post(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _create(
        camera_id: Any,
        payload: Any = Body(default=None),
        request: Request = None,
    ):
        cid = _require_camera_id(camera_id)
        data = _normalize(payload, request)
        return _create_for_camera(cid, data)

    @r.get(base + "/{entry_id}", summary="Get single service log entry")
    async def _get(camera_id: Any, entry_id: int):
        cid = _require_camera_id(camera_id)
        nid = _coerce_int_or_none(entry_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="entry_id is required")
        return _get_scoped(cid, nid)

    @r.put(base + "/{entry_id}", summary="Update service log entry")
    @r.patch(base + "/{entry_id}", include_in_schema=False)
    async def _update(
        camera_id: Any,
        entry_id: int,
        payload: Any = Body(default=None),
        request: Request = None,
    ):
        cid = _require_camera_id(camera_id)
        nid = _coerce_int_or_none(entry_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="entry_id is required")
        data = _normalize(payload, request)
        return _update_scoped(cid, nid, data)

    @r.delete(base + "/{entry_id}", summary="Delete service log entry")
    async def _delete(camera_id: Any, entry_id: int):
        cid = _require_camera_id(camera_id)
        nid = _coerce_int_or_none(entry_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="entry_id is required")
        return _delete_scoped(cid, nid)

    @r.delete(base, include_in_schema=False)
    async def _delete_all(camera_id: Any):
        cid = _require_camera_id(camera_id)
        n = delete_entries_for_camera(cid)
        return {"success": True, "ok": True, "deleted_count": n}


_CAMERA_BASES = (
    "/api/cameras/{camera_id}/service-log",
    "/api/cameras/{camera_id}/service_log",
    "/api/cameras/{camera_id}/service-logs",
    "/api/cameras/{camera_id}/service_logs",
)
for _base in _CAMERA_BASES:
    _register_camera_routes(router, _base)


def _register_top_level(r: APIRouter, base: str) -> None:
    @r.get(base, summary="List all camera service log entries")
    @r.get(base + "/", include_in_schema=False)
    async def _list_all(
        request: Request,
        camera_id: Optional[str] = Query(None),
        limit: Optional[int] = Query(None),
        offset: Optional[int] = Query(None),
        skip: Optional[int] = Query(None),
        q: Optional[str] = Query(None),
        service_type: Optional[str] = Query(None),
    ):
        params: dict = {}
        cid = camera_id
        if cid is None:
            try:
                for alias in ("camera_id", "cameraId", "camera", "cam_id", "cam"):
                    if alias in request.query_params:
                        raw = request.query_params.get(alias)
                        cid = raw
                        break
            except Exception:
                pass
        if cid is not None:
            norm = _normalize_camera_id(cid)
            params["camera_id"] = norm if norm is not None else cid
        if limit is not None:
            params["limit"] = limit
        params["offset"] = offset if offset is not None else (skip if skip is not None else 0)
        if q is not None:
            params["q"] = q
        if service_type is not None:
            params["service_type"] = service_type
        try:
            for k, v in request.query_params.items():
                params.setdefault(k, v)
        except Exception:
            pass
        return list_entries(**params)

    @r.post(base, status_code=status.HTTP_200_OK, summary="Create camera service log entry")
    @r.post(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _create_top(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        raw_cid = data.get("camera_id", data.get("cameraId", data.get("camera", data.get("cam_id", data.get("cam")))))
        cid = _normalize_camera_id(raw_cid)
        if cid is None or (isinstance(cid, str) and not str(cid).strip()):
            raise HTTPException(status_code=400, detail="camera_id is required")
        return _create_for_camera(cid, data)

    @r.get(base + "/{entry_id}", summary="Get camera service log entry")
    async def _get_top(entry_id: int):
        nid = _coerce_int_or_none(entry_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="entry_id is required")
        return _entry_or_404(get_entry(nid), nid)

    @r.put(base + "/{entry_id}", summary="Update camera service log entry")
    @r.patch(base + "/{entry_id}", include_in_schema=False)
    async def _update_top(entry_id: int, payload: Any = Body(default=None), request: Request = None):
        nid = _coerce_int_or_none(entry_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="entry_id is required")
        data = _normalize(payload, request)
        data = {k: v for k, v in data.items() if k not in ("camera_id", "cameraId", "camera", "cam_id", "cam")}
        updated = update_entry(nid, **data) if data else update_entry(nid)
        return _entry_or_404(updated, nid)

    @r.delete(base + "/{entry_id}", summary="Delete camera service log entry")
    async def _delete_top(entry_id: int):
        nid = _coerce_int_or_none(entry_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="entry_id is required")
        ok = delete_entry(nid)
        if not ok:
            raise HTTPException(status_code=404, detail="Service log entry not found")
        return {"success": True, "ok": True, "deleted": [int(nid)], "id": int(nid)}


_TOP_BASES = (
    "/api/camera-service-log",
    "/api/camera_service_log",
    "/api/service-log",
    "/api/service_log",
)
_seen_top: set = set()
for _base in _TOP_BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen_top:
        continue
    _seen_top.add(_norm)
    _register_top_level(router, _norm)


# Compatibility aliases.
service_log_router = router
camera_service_log_router = router
service_logs_router = router
api_router = router

__all__ = ["router"]
