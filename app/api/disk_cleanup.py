from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

from app.services import disk_cleanup as disk_cleanup_mod
from app.services.disk_cleanup import DiskCleanupManager

router = APIRouter(tags=["disk_cleanup"])


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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "policy", "settings"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    if data.get("path") is None:
        for k in ("storage_path", "root_path", "storage_root", "root", "directory", "dir", "folder", "location", "target_dir", "target", "scan_path", "cleanup_path", "area_path", "base_dir", "target_dirs", "directories", "dirs"):
            if data.get(k) is not None:
                data["path"] = data.get(k)
                break
    if data.get("max_age_days") is None:
        for k in ("max_age", "retention_days", "retention", "age_days", "maxAgeDays", "maxAge", "retentionDays", "age", "days", "older_than_days", "max_file_age_days"):
            if data.get(k) is not None:
                data["max_age_days"] = data.get(k)
                break
    if data.get("min_free_bytes") is None:
        for k in ("min_free", "minFreeBytes", "min_free_gb", "minFreeGb", "min_free_mb", "free_bytes_required", "required_free_bytes", "reserve_bytes", "keep_free_bytes", "free", "minimum_free_bytes"):
            if data.get(k) is not None:
                data["min_free_bytes"] = data.get(k)
                break
    if data.get("max_usage_percent") is None:
        for k in ("max_percent", "maxUsagePercent", "usage_percent_limit", "max_disk_usage", "max_disk_usage_percent", "usage_limit", "disk_usage_limit", "max_usage"):
            if data.get(k) is not None:
                data["max_usage_percent"] = data.get(k)
                break
    if data.get("max_total_bytes") is None:
        for k in ("max_bytes", "max_size", "max", "capacity", "quota", "limit", "max_total_size", "max_total_size_bytes", "quota_bytes", "limit_bytes", "allowed_bytes"):
            if data.get(k) is not None:
                data["max_total_bytes"] = data.get(k)
                break
    if data.get("cleanup_temp") is None:
        for k in ("clean_temp", "cleanupTemp", "cleanTmp", "remove_temp", "delete_temp", "temp_cleanup"):
            if data.get(k) is not None:
                data["cleanup_temp"] = data.get(k)
                break
    if data.get("dry_run") is None:
        for k in ("dryRun", "dry-run", "dry", "simulate", "preview", "preview_only", "what_if"):
            if k in data:
                data["dry_run"] = bool(data.get(k))
                break
    return data


_POLICY_UPDATE_KEYS = {
    "enabled", "active", "max_age_days", "max_age", "retention_days", "retention",
    "age_days", "min_free_bytes", "min_free", "min_free_gb", "max_usage_percent",
    "max_percent", "max_total_bytes", "max_bytes", "max_size", "cleanup_temp",
    "clean_temp", "target_dirs", "target_dir", "path", "exclude_patterns",
}


async def _do_status(data: dict) -> Any:
    return DiskCleanupManager().get_status(**data)


async def _do_preview(data: dict) -> Any:
    payload = dict(data)
    payload["dry_run"] = True
    return DiskCleanupManager().run(**payload)


async def _do_cleanup(data: dict) -> Any:
    return DiskCleanupManager().run(**data)


async def _do_policy(data: dict, method: str) -> Any:
    # GET with no update keys -> read; otherwise update.
    has_update = any(k in data for k in _POLICY_UPDATE_KEYS)
    if method in ("PUT", "PATCH") or (method == "POST" and has_update):
        return disk_cleanup_mod.update_policy(**data)
    if has_update and method == "GET" and len(data) > 0:
        # GET with policy params is a status-style read; do not mutate.
        # But explicit update suffixes call _do_policy_update directly.
        return disk_cleanup_mod.get_policy()
    return disk_cleanup_mod.get_policy()


async def _do_policy_update(data: dict) -> Any:
    if not any(k in data for k in _POLICY_UPDATE_KEYS):
        return disk_cleanup_mod.get_policy()
    return disk_cleanup_mod.update_policy(**data)


async def _status_all(payload: Any = Body(default=None), request: Request = None):
    return await _do_status(_normalize(payload, request))


async def _preview_all(payload: Any = Body(default=None), request: Request = None):
    return await _do_preview(_normalize(payload, request))


async def _cleanup_all(
    payload: Any = Body(default=None),
    request: Request = None,
    dry_run: Optional[bool] = Query(default=None),
    path: Optional[str] = Query(default=None),
    max_age_days: Optional[float] = Query(default=None),
):
    data = _normalize(payload, request)
    if dry_run is not None:
        data["dry_run"] = bool(dry_run)
    if path is not None and data.get("path") is None:
        data["path"] = path
    if max_age_days is not None and data.get("max_age_days") is None:
        data["max_age_days"] = max_age_days
    return await _do_cleanup(data)


async def _policy_all(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    method = (request.method if request is not None else "GET").upper()
    return await _do_policy(data, method)


async def _policy_update_all(payload: Any = Body(default=None), request: Request = None):
    return await _do_policy_update(_normalize(payload, request))


_prefixes = [
    "/api/disk_cleanup",
    "/api/disk-cleanup",
    "/api/disk/cleanup",
    "/api/cleanup",
]

_status_suffixes = ["", "/", "/status", "/usage", "/summary"]
_policy_suffixes = ["/policy", "/config"]
_preview_suffixes = ["/preview", "/scan", "/dry-run", "/check"]
_cleanup_suffixes = ["/run", "/cleanup", "/clean", "/enforce", "/execute", "/prune"]
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
    for _suf in _cleanup_suffixes:
        _add(f"{_prefix}{_suf}", _cleanup_all, methods=("GET", "POST", "PUT", "DELETE"))
    for _suf in _policy_update_suffixes:
        _add(f"{_prefix}{_suf}", _policy_update_all, methods=("POST", "PUT", "PATCH", "GET"))


# Compatibility aliases.
disk_cleanup_router = router
router_disk_cleanup = router
cleanup_router = router
disk_router = router
disk_cleanup_api_router = router

__all__ = ["router"]
