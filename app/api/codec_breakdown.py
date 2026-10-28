"""Per-codec storage breakdown API router.

Exposes :class:`app.services.codec_breakdown.CodecBreakdownService`
over HTTP (auto-registered by ``app.main``'s ``pkgutil`` loader, so no
``main.py`` edits are required).

Paths intentionally avoid colliding with ``app.api.storage_analyzer``
(``/api/storage/breakdown``) and ``app.api.storage_breakdown`` routes.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["codec_breakdown"])


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
    # Camera id aliases.
    if data.get("camera_id") is None:
        for k in ("cameraId", "camera-id", "cam_id", "camId"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    return data


async def _do_breakdown(data: dict) -> Any:
    from app.services.codec_breakdown import CodecBreakdownService

    return CodecBreakdownService().analyze(**data)


async def _breakdown_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    camera_name: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    path: Optional[str] = Query(default=None),
    codec: Optional[str] = Query(default=None),
    top_n: Optional[int] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    sort_by: Optional[str] = Query(default=None),
    include_snapshots: Optional[bool] = Query(default=None),
):
    data = _normalize(payload, request)
    if camera_name is not None and data.get("camera_name") is None:
        data["camera_name"] = camera_name
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if path is not None and data.get("path") is None:
        data["path"] = path
    if codec is not None and data.get("codec") is None:
        data["codec"] = codec
    if top_n is not None and data.get("top_n") is None:
        data["top_n"] = top_n
    if limit is not None and data.get("limit") is None:
        data["limit"] = limit
    if sort_by is not None and data.get("sort_by") is None:
        data["sort_by"] = sort_by
    if include_snapshots is not None and data.get("include_snapshots") is None:
        data["include_snapshots"] = include_snapshots
    return await _do_breakdown(data)


async def _breakdown_camera(
    camera_id: Any,
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
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
    return await _do_breakdown(data)


_prefixes = [
    "/api/codec_breakdown",
    "/api/codec-breakdown",
    "/api/codecs",
    "/api/storage/codec_breakdown",
    "/api/storage/codec-breakdown",
    "/api/storage/codecs",
    "/api/storage/by_codec",
    "/api/storage/by-codec",
]

_suffixes = [
    "",
    "/",
    "/summary",
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
                operation_id=f"{handler.__name__}_codec_{_op_counter['n']}",
            )
            _registered.add(key)
        except Exception:  # noqa: BLE001
            pass


for _prefix in _prefixes:
    for _suf in _suffixes:
        _p = _prefix if _suf in ("", "/") else f"{_prefix}{_suf}"
        _add(_p, _breakdown_all)

for _p in (
    "/api/codec_breakdown/cameras/{camera_id}",
    "/api/codec-breakdown/cameras/{camera_id}",
    "/api/cameras/{camera_id}/codec_breakdown",
    "/api/cameras/{camera_id}/codec-breakdown",
    "/api/cameras/{camera_id}/codecs",
):
    _add(_p, _breakdown_camera)


# Compatibility aliases.
codec_breakdown_router = router
router_codec_breakdown = router
codecs_router = router
codec_router = router
codec_breakdown_api_router = router

__all__ = ["router"]
