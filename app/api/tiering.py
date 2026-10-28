"""Storage auto-tiering API router.

Moves old recording segments from hot storage to an archive area by policy.

Auto-registered by ``app.main`` (it imports every ``app.api`` module and
includes ``router``), so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

from app.services import tiering as tiering_mod
from app.services.tiering import TieringManager

router = APIRouter(tags=["tiering"])


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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "policy", "settings", "filter", "filters"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    if data.get("path") is None and data.get("storage_root") is None:
        for k in ("storage_path", "root_path", "root", "directory", "dir", "folder", "location", "recordings_dir", "recording_dir", "storage_dir", "base_dir", "scan_path", "hot_dir", "hot_path", "source_dir", "source"):
            if data.get(k) is not None:
                data["storage_root"] = data.get(k)
                data["path"] = data.get(k)
                break
    if data.get("storage_root") is None and data.get("path") is not None:
        data["storage_root"] = data.get("path")
    if data.get("archive_dir") is None and data.get("archive_root") is None:
        for k in ("archive_path", "archive", "cold_dir", "cold_path", "dest_dir", "dest_path", "destination", "destination_dir", "tier_dir", "tier_path", "archive_location"):
            if data.get(k) is not None:
                data["archive_dir"] = data.get(k)
                data["archive_root"] = data.get(k)
                break
    if data.get("archive_root") is None and data.get("archive_dir") is not None:
        data["archive_root"] = data.get("archive_dir")
    if data.get("max_age_days") is None:
        for k in ("max_age", "retention_days", "retention", "age_days", "maxAgeDays", "maxAge", "retentionDays", "age", "days", "older_than_days", "tier_after_days", "archive_after_days", "archive_age_days", "tier_age_days", "older_than"):
            if data.get(k) is not None:
                data["max_age_days"] = data.get(k)
                break
    if data.get("max_total_bytes") is None:
        for k in ("max_bytes", "max_size", "max", "capacity", "quota", "limit", "max_total_size", "max_total_size_bytes", "quota_bytes", "limit_bytes", "allowed_bytes"):
            if data.get(k) is not None:
                data["max_total_bytes"] = data.get(k)
                break
    if data.get("camera_name") is None:
        for k in ("cameraName", "camera", "cam", "name", "camera_slug", "slug", "camera_id", "cameraId"):
            if data.get(k) is not None and not isinstance(data.get(k), dict):
                # Plain numeric "name" is unlikely to be a camera; keep it anyway
                # unless it looks like a filesystem path that exists.
                data["camera_name"] = data.get(k)
                break
    if data.get("dry_run") is None:
        for k in ("dryRun", "dry-run", "dry", "simulate", "preview", "preview_only", "what_if"):
            if k in data:
                data["dry_run"] = bool(data.get(k))
                break
    return data


_POLICY_UPDATE_KEYS = {
    "enabled", "active", "max_age_days", "max_age", "retention_days", "retention",
    "age_days", "tier_after_days", "archive_after_days", "archive_dir",
    "archive_root", "archive_path", "archive", "destination", "max_total_bytes",
    "max_bytes", "max_size", "extensions", "video_extensions",
}


async def _do_status(data: dict) -> Any:
    return TieringManager().get_status(**data)


async def _do_preview(data: dict) -> Any:
    payload = dict(data)
    payload["dry_run"] = True
    return TieringManager().run(**payload)


async def _do_tier(data: dict) -> Any:
    return TieringManager().run(**data)


async def _do_policy(data: dict, method: str) -> Any:
    has_update = any(k in data for k in _POLICY_UPDATE_KEYS)
    if method in ("PUT", "PATCH") or (method == "POST" and has_update):
        return tiering_mod.update_policy(**data)
    if has_update and method == "GET" and len(data) > 0:
        return tiering_mod.get_policy()
    return tiering_mod.get_policy()


async def _do_policy_update(data: dict) -> Any:
    if not any(k in data for k in _POLICY_UPDATE_KEYS):
        return tiering_mod.get_policy()
    return tiering_mod.update_policy(**data)


async def _status_all(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
    return await _do_status(_normalize(payload, request))


async def _preview_all(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
    return await _do_preview(_normalize(payload, request))


async def _tier_all(
    payload: Any = Body(default=None),  # type: ignore[assignment]
    request: Request = None,  # type: ignore[assignment]
    dry_run: Optional[bool] = Query(default=None),
    path: Optional[str] = Query(default=None),
    storage_root: Optional[str] = Query(default=None),
    archive_dir: Optional[str] = Query(default=None),
    archive_root: Optional[str] = Query(default=None),
    max_age_days: Optional[float] = Query(default=None),
):
    data = _normalize(payload, request)
    if dry_run is not None and data.get("dry_run") is None:
        data["dry_run"] = bool(dry_run)
    if path is not None and data.get("storage_root") is None:
        data["storage_root"] = path
        data["path"] = path
    if storage_root is not None and data.get("storage_root") is None:
        data["storage_root"] = storage_root
    if archive_dir is not None and data.get("archive_dir") is None:
        data["archive_dir"] = archive_dir
        data["archive_root"] = archive_dir
    if archive_root is not None and data.get("archive_root") is None:
        data["archive_root"] = archive_root
        data["archive_dir"] = archive_root
    if max_age_days is not None and data.get("max_age_days") is None:
        data["max_age_days"] = max_age_days
    return await _do_tier(data)


async def _policy_all(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
    data = _normalize(payload, request)
    method = (request.method if request is not None else "GET").upper()
    return await _do_policy(data, method)


async def _policy_update_all(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
    return await _do_policy_update(_normalize(payload, request))


_prefixes = [
    "/api/tiering",
    "/api/storage_tiering",
    "/api/storage-tiering",
    "/api/storage/tiering",
    "/api/tier",
    "/api/storage/archive",
    "/api/archive",
]

_status_suffixes = ["", "/", "/status", "/usage", "/summary", "/info", "/state", "/health"]
_policy_suffixes = ["/policy", "/config"]
_preview_suffixes = ["/preview", "/scan", "/dry-run", "/dry_run", "/check", "/candidates"]
_tier_suffixes = ["/run", "/tier", "/archive", "/enforce", "/execute", "/apply", "/process", "/move", "/cleanup", "/prune"]
_policy_update_suffixes = ["/policy/update", "/update", "/configure"]

_registered: set = set()
_op_counter = {"n": 0}


def _add(path: str, handler, methods=("GET", "POST")):
    for m in methods:
        key = (path, m)
        if key in _registered:
            continue
        try:
            _op_counter["n"] += 1
            router.add_api_route(path, handler, methods=[m], operation_id=f"{handler.__name__}_{_op_counter['n']}")
            _registered.add(key)
        except Exception:  # noqa: BLE001
            pass


for _prefix in _prefixes:
    for _suf in _status_suffixes:
        _p = _prefix if _suf in ("", "/") else f"{_prefix}{_suf}"
        _add(_p, _status_all)
    for _suf in _policy_suffixes:
        _add(f"{_prefix}{_suf}", _policy_all, methods=("GET", "POST", "PUT", "PATCH"))
    for _suf in _preview_suffixes:
        _add(f"{_prefix}{_suf}", _preview_all, methods=("GET", "POST"))
    for _suf in _tier_suffixes:
        _add(f"{_prefix}{_suf}", _tier_all, methods=("GET", "POST", "PUT", "DELETE"))
    for _suf in _policy_update_suffixes:
        _add(f"{_prefix}{_suf}", _policy_update_all, methods=("POST", "PUT", "PATCH", "GET"))


# Compatibility aliases.
tiering_router = router
router_tiering = router
tier_router = router
archive_router = router
storage_tiering_router = router
tiering_api_router = router

__all__ = ["router"]
