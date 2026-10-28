"""Donut/pie-ready storage shares API router.

Exposes :class:`app.services.storage_donut.StorageDonutService`
over HTTP. Each response carries a chart-widget-friendly
``[{label, value, color}]`` array (under ``segments``/``slices``/``data``
aliases) plus totals so dashboards can render a donut/pie without extra
transformation.

Registered paths intentionally avoid colliding with the existing
``app.api.storage_analyzer`` (``/api/storage/*``) and
``app.api.storage_breakdown`` (``/api/storage-breakdown*``) routers;
this router uses ``donut``/``pie``/``chart`` variants.
Auto-registered by ``app.main`` (it imports every ``app.api.*`` module
with a ``router`` attribute), so no ``app/main.py`` edit is needed.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["storage_donut"])


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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "filter", "filters", "donut", "pie", "chart"):
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
    # Camera id aliases.
    if data.get("camera_id") is None:
        for k in ("cameraId", "camera-id", "cam_id", "camId"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    # Top-N aliases.
    if data.get("top_n") is None:
        for k in ("topN", "top", "limit", "max_segments", "max_slices", "maxSlices", "maxSegments"):
            if data.get(k) is not None:
                data["top_n"] = data.get(k)
                break
    return data


def _wants_bare_list(data: dict) -> bool:
    fmt = data.get("format", data.get("output", data.get("as", data.get("mode"))))
    if isinstance(fmt, str) and fmt.strip().lower() in ("list", "array", "raw", "bare", "slices", "segments", "donut", "pie", "flat"):
        return True
    for k in ("raw", "bare", "flat", "as_list", "asList", "list_only", "segments_only", "slices_only"):
        v = data.get(k)
        if isinstance(v, str):
            if v.strip().lower() not in ("", "0", "false", "no", "off", "none"):
                return True
        elif v is True or (isinstance(v, (int, float)) and not isinstance(v, bool) and v == 1):
            return True
    return False


async def _do_donut(data: dict) -> Any:
    from app.services.storage_donut import StorageDonutService

    result = StorageDonutService().donut(**data)
    if _wants_bare_list(data):
        return result.get("segments") or []
    return result


async def _donut_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    camera_name: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    path: Optional[str] = Query(default=None),
    top_n: Optional[int] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    sort_by: Optional[str] = Query(default=None),
    format: Optional[str] = Query(default=None),  # noqa: A002
    raw: Optional[str] = Query(default=None),
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
    if limit is not None and data.get("top_n") is None:
        data["top_n"] = limit
    if sort_by is not None and data.get("sort_by") is None:
        data["sort_by"] = sort_by
    if format is not None and data.get("format") is None:
        data["format"] = format
    if raw is not None and data.get("raw") is None:
        data["raw"] = raw
    return await _do_donut(data)


async def _donut_camera(
    camera_id: Any,
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    path: Optional[str] = Query(default=None),
    format: Optional[str] = Query(default=None),  # noqa: A002
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
    if format is not None and data.get("format") is None:
        data["format"] = format
    return await _do_donut(data)


_prefixes = [
    "/api/storage-donut",
    "/api/storage_donut",
    "/api/storage/donut",
    "/api/storage/donuts",
    "/api/storage/donut-ui",
    "/api/storage/donut/ui",
    "/api/storage/pie",
    "/api/storage/pie-chart",
    "/api/storage/chart-donut",
]

_suffixes = [
    "",
    "/summary",
    "/segments",
    "/slices",
    "/chart",
    "/data",
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
                operation_id=f"{handler.__name__}_donut_{_op_counter['n']}",
            )
            _registered.add(key)
        except Exception:  # noqa: BLE001
            pass


for _prefix in _prefixes:
    for _suf in _suffixes:
        _p = _prefix if _suf == "" else f"{_prefix}{_suf}"
        _add(_p, _donut_all)

for _p in (
    "/api/storage-donut/cameras/{camera_id}",
    "/api/storage_donut/cameras/{camera_id}",
    "/api/storage/donut/cameras/{camera_id}",
    "/api/cameras/{camera_id}/storage-donut",
    "/api/cameras/{camera_id}/storage_donut",
    "/api/cameras/{camera_id}/storage/donut",
):
    _add(_p, _donut_camera)


# Compatibility aliases.
storage_donut_router = router
router_storage_donut = router
donut_router = router
pie_router = router
storage_donut_api_router = router
storage_pie_router = router

__all__ = ["router"]
