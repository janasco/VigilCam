"""File bin router (iSpy-style saved clips bin / archive manager).

Mounted without a fixed prefix so it serves ``/api/filebin*``,
``/api/file-bin*``, ``/api/bin*`` and ``/api/archive*`` style paths.
The main app auto-registers any ``app.api`` module exposing ``router``,
so no edit to ``app/main.py`` is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

import app.services.filebin as svc

router = APIRouter(tags=["filebin"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"name": payload}
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
    for nested_key in ("payload", "options", "config", "params", "data", "body", "entry", "file", "clip", "attributes"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    if data.get("camera_id") is None:
        for k in ("camera", "cam", "cameraId", "cameraID", "cam_id", "source_id"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    if data.get("locked") is None:
        for k in ("protected", "pinned", "is_locked", "lock"):
            if data.get(k) is not None:
                data["locked"] = data.get(k)
                break
    if data.get("name") is None:
        for k in ("filename", "file_name", "title", "label", "clip_name"):
            if data.get(k) is not None:
                data["name"] = data.get(k)
                break
    return data


def _to_response(entry: Any) -> dict:
    if entry is None:
        return {}
    if isinstance(entry, dict):
        return dict(entry)
    if hasattr(entry, "model_dump"):
        try:
            return dict(entry.model_dump(mode="json"))
        except Exception:  # noqa: BLE001
            try:
                return dict(entry.model_dump())
            except Exception:  # noqa: BLE001
                pass
    try:
        return dict(entry)
    except Exception:  # noqa: BLE001
        return {"value": entry}


def _collect_filters(raw: dict) -> dict:
    filters: dict[str, Any] = {}
    for key in ("camera_id", "camera", "cam", "locked", "protected", "pinned", "q", "query", "search", "name", "label"):
        if raw.get(key) is not None:
            filters[key] = raw.get(key)
    return filters


def _parse_limit_skip(raw: dict) -> tuple[Any, Any]:
    limit = raw.get("limit", raw.get("n", raw.get("count", raw.get("size", raw.get("max", raw.get("top"))))))
    skip = raw.get("skip", raw.get("offset", raw.get("start_index", 0)))
    try:
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None
    try:
        skip = int(skip or 0)
    except (TypeError, ValueError):
        skip = 0
    return limit, skip


async def _list_handler(
    request: Request,
    camera_id: Optional[str] = Query(default=None),
    camera: Optional[str] = Query(default=None),
    locked: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    skip: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
):
    raw: dict[str, Any] = dict(request.query_params)
    if camera_id is not None:
        raw["camera_id"] = camera_id
    if camera is not None and raw.get("camera_id") is None:
        raw["camera_id"] = camera
    if locked is not None:
        raw["locked"] = locked
    eff_search = search if search is not None else q
    if eff_search is not None:
        raw["search"] = eff_search
    filters = _collect_filters(raw)
    lim, sk = _parse_limit_skip({**raw, "limit": limit if limit is not None else raw.get("limit"),
                                 "skip": skip if skip is not None else raw.get("skip"),
                                 "offset": offset if offset is not None else raw.get("offset")})
    items = svc.list_bin(limit=lim, skip=sk, **filters)
    return [_to_response(e) for e in items]


async def _create_handler(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    try:
        entry = svc.archive_file(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    resp = _to_response(entry)
    resp.setdefault("ok", True)
    resp.setdefault("success", True)
    return JSONResponse(resp, status_code=status.HTTP_201_CREATED)


async def _get_handler(entry_id: str):
    entry = svc.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="File bin entry not found")
    return _to_response(entry)


async def _update_handler(entry_id: str, payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    entry = svc.update_entry(entry_id, **data)
    if entry is None:
        raise HTTPException(status_code=404, detail="File bin entry not found")
    return _to_response(entry)


async def _delete_handler(entry_id: str, request: Request = None):
    raw: dict[str, Any] = dict(request.query_params) if request is not None else {}
    force = str(raw.get("force", "")).lower() in ("1", "true", "yes", "on")
    delete_file = str(raw.get("delete_file", raw.get("deleteFile", "true"))).lower() not in ("0", "false", "no", "off")
    ok = svc.delete_entry(entry_id, delete_file=delete_file, force=force)
    if not ok:
        # Distinguish locked vs missing.
        existing = svc.get_entry(entry_id)
        if existing is not None and bool(existing.get("locked")) and not force:
            raise HTTPException(status_code=409, detail="Entry is locked; pass force=true to delete")
        raise HTTPException(status_code=404, detail="File bin entry not found")
    return {"status": "deleted", "ok": True, "success": True, "id": entry_id}


async def _clear_handler(request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    force = str(raw.get("force", "")).lower() in ("1", "true", "yes", "on")
    filters = _collect_filters(raw)
    if force:
        filters["force"] = True
    n = svc.clear_bin(**filters)
    return {"status": "cleared", "ok": True, "success": True, "deleted": n, "count": n}


async def _stats_handler():
    return svc.stats()


async def _count_handler(request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    filters = _collect_filters(raw)
    items = svc.list_bin(**filters)
    return {"count": len(items), "total": len(items), "ok": True}


def _health_handler():
    try:
        s = svc.stats()
        return {"status": "ok", "count": s.get("count", 0), "entries": s.get("count", 0)}
    except Exception:  # noqa: BLE001
        return {"status": "ok", "count": 0, "entries": 0}


async def _lock_handler(entry_id: str):
    entry = svc.filebin_service.lock(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="File bin entry not found")
    return _to_response(entry)


async def _unlock_handler(entry_id: str):
    entry = svc.filebin_service.unlock(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="File bin entry not found")
    return _to_response(entry)


_FILEBIN_BASES = [
    "/api/filebin",
    "/api/file-bin",
    "/api/file_bin",
    "/api/bin",
    "/api/archive",
    "/api/archives",
    "/api/saved-clips",
    "/api/saved_clips",
    "/api/savedclips",
    "/api/clips/bin",
]

for _base in _FILEBIN_BASES:
    router.add_api_route(_base, _list_handler, methods=["GET"])
    router.add_api_route(_base + "/", _list_handler, methods=["GET"])
    router.add_api_route(_base, _create_handler, methods=["POST"])
    router.add_api_route(_base + "/", _create_handler, methods=["POST"])
    router.add_api_route(_base + "/list", _list_handler, methods=["GET", "POST"])
    router.add_api_route(_base + "/stats", _stats_handler, methods=["GET", "POST"])
    router.add_api_route(_base + "/count", _count_handler, methods=["GET", "POST"])
    router.add_api_route(_base + "/clear", _clear_handler, methods=["GET", "POST", "DELETE"])
    router.add_api_route(_base + "/health", _health_handler, methods=["GET"], include_in_schema=False)
    router.add_api_route(_base + "/{entry_id}/lock", _lock_handler, methods=["GET", "POST"])
    router.add_api_route(_base + "/{entry_id}/unlock", _unlock_handler, methods=["GET", "POST"])
    router.add_api_route(_base + "/{entry_id}", _get_handler, methods=["GET"])
    router.add_api_route(_base + "/{entry_id}", _update_handler, methods=["PUT", "PATCH"])
    router.add_api_route(_base + "/{entry_id}", _update_handler, methods=["POST"])
    router.add_api_route(_base + "/{entry_id}", _delete_handler, methods=["DELETE"])


@router.post("/api/filebin/archive")
@router.post("/api/file-bin/archive")
@router.post("/api/bin/archive")
async def archive_alias(payload: Any = Body(default=None), request: Request = None):
    return await _create_handler(payload, request)


# Compatibility aliases.
filebin_router = router
api_router = router
bin_router = router
archive_router = router
file_bin_router = router
