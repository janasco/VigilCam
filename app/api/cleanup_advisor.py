"""Storage cleanup advisor API router.

Exposes :class:`app.services.cleanup_advisor.CleanupAdvisor` over HTTP.
Advisory-only: endpoints never delete files, they return ranked deletion
suggestions with reasons.

Auto-registered by ``app.main`` (it imports every ``app.api`` module and
includes ``router``), so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["cleanup_advisor"])


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
            "limit_bytes", "allowed_bytes", "maxTotalBytes", "target_bytes",
        ):
            if data.get(k) is not None:
                data["max_total_bytes"] = data.get(k)
                break
    if data.get("top_n") is None:
        for k in ("topN", "max_suggestions", "maxSuggestions", "count", "n"):
            if data.get(k) is not None:
                data["top_n"] = data.get(k)
                break
        # Bare "limit" is ambiguous (quota vs suggestion count): only treat
        # small ints as suggestion counts when no quota key was given.
        if data.get("top_n") is None and data.get("limit") is not None and data.get("max_total_bytes") is None:
            try:
                v = int(data.get("limit"))
                if 0 < v <= 10000:
                    data["top_n"] = v
            except (TypeError, ValueError):
                pass
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
    # Advisory semantics: never delete via this router.
    data["dry_run"] = True
    return data


async def _do_advise(data: dict) -> Any:
    from app.services.cleanup_advisor import CleanupAdvisor

    payload = dict(data)
    payload["dry_run"] = True
    return CleanupAdvisor().suggest(**payload)


async def _advise_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    path: Optional[str] = Query(default=None),
    storage_root: Optional[str] = Query(default=None),
    max_age_days: Optional[float] = Query(default=None),
    max_total_bytes: Optional[int] = Query(default=None),
    camera_name: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    top_n: Optional[int] = Query(default=None),
    limit: Optional[int] = Query(default=None),
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
    if top_n is not None and data.get("top_n") is None:
        data["top_n"] = top_n
    if limit is not None and data.get("top_n") is None and data.get("max_total_bytes") is None:
        data["top_n"] = limit
    return await _do_advise(data)


async def _advise_camera(
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
    return await _do_advise(data)


_prefixes = [
    "/api/cleanup_advisor",
    "/api/cleanup-advisor",
    "/api/cleanup/advisor",
    "/api/cleanup-advisor/advise",
    "/api/storage/cleanup_advisor",
    "/api/storage/cleanup-advisor",
    "/api/storage/cleanup/advisor",
    "/api/storage/advisor",
    "/api/storage/cleanup-advisor-suggestions",
    "/api/cleanup/suggestions",
    "/api/cleanup-suggestions",
    "/api/cleanup_suggestions",
    "/api/storage/suggestions",
    "/api/storage/cleanup-suggestions",
    "/api/storage/cleanup_suggestions",
    "/api/advisor/cleanup",
    "/api/cleanup-advise",
]

_suffixes = [
    "",
    "/",
    "/suggest",
    "/suggestions",
    "/advise",
    "/advice",
    "/recommend",
    "/recommendations",
    "/ranked",
    "/rank",
    "/preview",
    "/report",
    "/scan",
    "/check",
    "/summary",
    "/status",
    "/analyze",
    "/analysis",
]

_camera_suffixes = [
    "",
    "/suggest",
    "/suggestions",
    "/advise",
    "/recommend",
    "/ranked",
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
        _add(_p, _advise_all)
    for _suf in _camera_suffixes:
        _base1 = f"{_prefix}/cameras/{{camera_id}}" + (_suf or "")
        _base2 = f"{_prefix}/camera/{{camera_id}}" + (_suf or "")
        for _p in (_base1, _base2):
            _add(_p, _advise_camera)

for _p in (
    "/api/cameras/{camera_id}/cleanup-suggestions",
    "/api/cameras/{camera_id}/cleanup_suggestions",
    "/api/cameras/{camera_id}/cleanup/advisor",
    "/api/cameras/{camera_id}/cleanup-advisor",
    "/api/cameras/{camera_id}/cleanup_advisor",
):
    _add(_p, _advise_camera)


# Compatibility aliases.
cleanup_advisor_router = router
router_cleanup_advisor = router
advisor_router = router
cleanup_router = router
cleanup_advisor_api_router = router

__all__ = ["router"]
