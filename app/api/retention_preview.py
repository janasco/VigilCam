"""Retention preview report API router.

Exposes :class:`app.services.retention_preview.RetentionPreviewService`
over HTTP under a broad set of alias prefixes. All endpoints are dry-run
only: they never delete files.

Auto-registered by ``app.main`` (it imports every ``app.api`` module and
includes ``router``), so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["retention_preview"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "policy", "settings", "filter", "filters"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    if data.get("storage_root") is None:
        for k in (
            "storage_path", "root_path", "root", "directory", "dir", "folder",
            "location", "recordings_dir", "recording_dir", "storage_dir",
            "base_dir", "scan_path", "recordings_root", "target_dir",
            "target_dirs", "target", "path",
        ):
            if data.get(k) is not None:
                data["storage_root"] = data.get(k)
                break
    if data.get("path") is None and data.get("storage_root") is not None:
        data["path"] = data.get("storage_root")
    if data.get("max_age_days") is None:
        for k in (
            "max_age", "retention_days", "retention", "age_days", "maxAgeDays",
            "maxAge", "retentionDays", "age", "days", "older_than_days",
            "max_file_age_days",
        ):
            if data.get(k) is not None:
                data["max_age_days"] = data.get(k)
                break
    if data.get("max_total_bytes") is None:
        for k in (
            "max_bytes", "max_size", "max", "capacity", "quota", "limit",
            "max_total_size", "max_total_size_bytes", "quota_bytes",
            "limit_bytes", "allowed_bytes", "maxTotalBytes",
        ):
            if data.get(k) is not None:
                data["max_total_bytes"] = data.get(k)
                break
    if data.get("camera_name") is None:
        for k in ("cameraName", "camera", "cam", "name", "camera_slug", "slug"):
            if data.get(k) is not None and not isinstance(data.get(k), dict):
                data["camera_name"] = data.get(k)
                break
    if data.get("camera_id") is None:
        for k in ("cameraId", "camera-id", "cam_id", "camId"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    # Force preview semantics: never delete via this router.
    data["dry_run"] = True
    return data


async def _do_preview(data: dict) -> Any:
    from app.services.retention_preview import RetentionPreviewService

    payload = dict(data)
    payload["dry_run"] = True
    return RetentionPreviewService().preview(**payload)


async def _preview_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    path: Optional[str] = Query(default=None),
    storage_root: Optional[str] = Query(default=None),
    max_age_days: Optional[float] = Query(default=None),
    max_total_bytes: Optional[int] = Query(default=None),
    camera_name: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if path is not None and data.get("storage_root") is None:
        data["storage_root"] = path
        data["path"] = path
    if storage_root is not None and data.get("storage_root") is None:
        data["storage_root"] = storage_root
    if max_age_days is not None and data.get("max_age_days") is None:
        data["max_age_days"] = max_age_days
    if max_total_bytes is not None and data.get("max_total_bytes") is None:
        data["max_total_bytes"] = max_total_bytes
    if camera_name is not None and data.get("camera_name") is None:
        data["camera_name"] = camera_name
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    return await _do_preview(data)


async def _preview_camera(
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
    if path is not None and data.get("storage_root") is None:
        data["storage_root"] = path
    return await _do_preview(data)


_prefixes = [
    "/api/retention_preview",
    "/api/retention-preview",
    "/api/retention/preview",
    "/api/retention/report",
    "/api/retention/preview-report",
    "/api/retention/preview_report",
    "/api/retention_preview/report",
    "/api/retention-preview/report",
    "/api/preview/retention",
    "/api/retention",
]

_suffixes = [
    "",
    "/",
    "/preview",
    "/report",
    "/preview-report",
    "/preview_report",
    "/dry-run",
    "/dry_run",
    "/scan",
    "/check",
    "/summary",
    "/status",
    "/run",
    "/generate",
]

_camera_suffixes = [
    "",
    "/preview",
    "/report",
    "/status",
    "/summary",
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
        _add(_p, _preview_all)
    for _suf in _camera_suffixes:
        _base1 = f"{_prefix}/cameras/{{camera_id}}" + (_suf or "")
        _base2 = f"{_prefix}/camera/{{camera_id}}" + (_suf or "")
        for _p in (_base1, _base2):
            _add(_p, _preview_camera)

for _p in (
    "/api/cameras/{camera_id}/retention",
    "/api/cameras/{camera_id}/retention/preview",
    "/api/cameras/{camera_id}/retention/report",
    "/api/cameras/{camera_id}/retention-preview",
    "/api/cameras/{camera_id}/retention_preview",
):
    _add(_p, _preview_camera)


# Compatibility aliases.
retention_preview_router = router
router_retention_preview = router
preview_router = router
retention_router = router
retention_preview_api_router = router

__all__ = ["router"]
