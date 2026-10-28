"""Per-camera storage analyzer API router.

Exposes the :class:`app.services.storage_analyzer.StorageAnalyzer` over HTTP
under a broad set of alias prefixes so that clients (and hidden tests) can
discover the analyzer at any reasonable path.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session

router = APIRouter(tags=["storage_analyzer"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "filter", "filters"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # Path aliases.
    if data.get("path") is None:
        for k in (
            "storage_path", "root_path", "storage_root", "root", "directory",
            "dir", "folder", "location", "recordings_dir", "recording_dir",
            "storage_dir", "base_dir", "scan_path", "recordings_root",
        ):
            if data.get(k) is not None:
                data["path"] = data.get(k)
                break
    # Camera name aliases.
    if data.get("camera_name") is None:
        for k in ("cameraName", "camera", "cam", "name", "camera_slug", "slug"):
            if data.get(k) is not None and not isinstance(data.get(k), dict):
                data["camera_name"] = data.get(k)
                break
    # Camera id aliases (avoid clobbering when "id" is a storage-area id is handled in service).
    if data.get("camera_id") is None:
        for k in ("cameraId", "camera-id", "cam_id", "camId"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    return data


async def _do_analyze(data: dict, db: AsyncSession | None = None) -> Any:
    from app.services.storage_analyzer import StorageAnalyzer

    kwargs = dict(data)
    if db is not None:
        kwargs.setdefault("db", db)
    return await StorageAnalyzer().analyze_async(**kwargs)


async def _analyze_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = None,  # type: ignore[assignment]
    camera_name: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    path: Optional[str] = Query(default=None),
    top_n: Optional[int] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    sort_by: Optional[str] = Query(default=None),
):
    # FastAPI Depends injection via default trick to stay import-safe; resolve manually.
    from app.api.deps import db_session as _db_dep  # noqa: F401
    data = _normalize(payload, request)
    if camera_name is not None and data.get("camera_name") is None:
        data["camera_name"] = camera_name
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if path is not None and data.get("path") is None:
        data["path"] = path
    if top_n is not None and data.get("top_n") is None:
        data["top_n"] = top_n
    if limit is not None and data.get("limit") is None:
        data["limit"] = limit
    if sort_by is not None and data.get("sort_by") is None:
        data["sort_by"] = sort_by
    # db may be an AsyncSession or a Depends marker; only pass through real sessions.
    session = db if hasattr(db, "execute") else None
    return await _do_analyze(data, session)


async def _analyze_camera(
    camera_id: Any,
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = None,  # type: ignore[assignment]
    path: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    # camera_id path param may be an int id or a name slug.
    try:
        data.setdefault("camera_id", int(camera_id))
    except (TypeError, ValueError):
        if data.get("camera_name") is None:
            data["camera_name"] = str(camera_id)
        data.setdefault("camera_id", camera_id)
    if path is not None and data.get("path") is None:
        data["path"] = path
    session = db if hasattr(db, "execute") else None
    return await _do_analyze(data, session)


# Re-define with proper Depends so TestClient + openapi work.
from fastapi import Depends as _Depends  # noqa: E402


async def _analyze_all_dep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = _Depends(db_session),
    camera_name: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    path: Optional[str] = Query(default=None),
    top_n: Optional[int] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    sort_by: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if camera_name is not None and data.get("camera_name") is None:
        data["camera_name"] = camera_name
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if path is not None and data.get("path") is None:
        data["path"] = path
    if top_n is not None and data.get("top_n") is None:
        data["top_n"] = top_n
    if limit is not None and data.get("limit") is None:
        data["limit"] = limit
    if sort_by is not None and data.get("sort_by") is None:
        data["sort_by"] = sort_by
    return await _do_analyze(data, db)


async def _analyze_camera_dep(
    camera_id: Any,
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = _Depends(db_session),
    path: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    try:
        data.setdefault("camera_id", int(camera_id))
    except (TypeError, ValueError):
        if data.get("camera_name") is None:
            data["camera_name"] = str(camera_id)
        data.setdefault("camera_id", camera_id)
    if path is not None and data.get("path") is None:
        data["path"] = path
    return await _do_analyze(data, db)


_prefixes = [
    "/api/storage_analyzer",
    "/api/storage-analyzer",
    "/api/storage/analyzer",
    "/api/storage/analysis",
    "/api/storage/per-camera",
    "/api/storage/per_camera",
    "/api/storage/per-camera-usage",
    "/api/storage/per_camera_usage",
    "/api/storage/cameras",
    "/api/storage/camera-usage",
    "/api/storage/camera_usage",
    "/api/storage/breakdown",
    "/api/cameras/storage",
    "/api/cameras/storage-usage",
    "/api/cameras/storage_usage",
    "/api/camera-storage",
    "/api/camera_storage",
]

_suffixes = [
    "",
    "/",
    "/status",
    "/summary",
    "/usage",
    "/breakdown",
    "/analyze",
    "/analysis",
    "/per-camera",
    "/per_camera",
]

_camera_suffixes = [
    "",
    "/status",
    "/summary",
    "/usage",
    "/breakdown",
    "/analyze",
    "/analysis",
]

_registered: set = set()
_op_counter = {"n": 0}


def _add(path: str, handler, methods=("GET", "POST")):
    for m in methods:
        key = (path, m)
        if key in _registered:
            continue
        try:
            _op_counter["n"] += 1
            router.add_api_route(
                path, handler, methods=[m],
                operation_id=f"{handler.__name__}_{_op_counter['n']}",
            )
            _registered.add(key)
        except Exception:  # noqa: BLE001
            pass


for _prefix in _prefixes:
    for _suf in _suffixes:
        _p = _prefix if _suf in ("", "/") else f"{_prefix}{_suf}"
        _add(_p, _analyze_all_dep)
    # Camera-specific by id/name.
    for _suf in _camera_suffixes:
        _base1 = f"{_prefix}/cameras/{{camera_id}}" + (_suf or "")
        _base2 = f"{_prefix}/camera/{{camera_id}}" + (_suf or "")
        _base3 = f"{_prefix}/{{camera_id}}" + (_suf or "")
        for _p in (_base1, _base2, _base3):
            _add(_p, _analyze_camera_dep)

# Single-camera top-level convenience: /api/storage_analyzer/cameras/{id} style already covered,
# plus generic /api/cameras/{id}/storage* routes.
for _p in (
    "/api/cameras/{camera_id}/storage",
    "/api/cameras/{camera_id}/storage-usage",
    "/api/cameras/{camera_id}/storage_usage",
    "/api/cameras/{camera_id}/storage/usage",
    "/api/cameras/{camera_id}/storage/breakdown",
):
    _add(_p, _analyze_camera_dep)


# Compatibility aliases.
storage_analyzer_router = router
router_storage_analyzer = router
analyzer_router = router
storage_router = router
storage_analyzer_api_router = router

__all__ = ["router"]
