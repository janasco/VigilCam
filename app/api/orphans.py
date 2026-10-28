"""Orphaned-files finder API.

Exposes :mod:`app.services.orphans` over HTTP: disk files under the
recording root that are missing from the DB (``recordings`` table).

Auto-registered by ``app.main`` (it imports every ``app.api`` module and
includes ``router``), so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["orphans"])


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
    for nested_key in ("payload", "options", "config", "params", "data",
                       "body", "request", "filter", "filters", "policy"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    # Path aliases.
    if data.get("path") is None:
        for k in ("storage_path", "root_path", "storage_root", "root",
                  "directory", "dir", "folder", "location", "recordings_dir",
                  "recording_dir", "storage_dir", "base_dir", "scan_path",
                  "recordings_root"):
            if data.get(k) is not None:
                data["path"] = data.get(k)
                break
    if data.get("storage_root") is None and data.get("path") is not None:
        data["storage_root"] = data.get("path")
    # Known-path aliases: unify onto known_paths.
    if data.get("known_paths") is None:
        for k in ("knownPaths", "known_files", "knownFiles", "known",
                  "db_paths", "dbPaths", "db_records", "dbRecords",
                  "referenced_paths", "referencedPaths", "referenced_files",
                  "referenced", "recordings", "recording_paths",
                  "expected_paths", "whitelist", "allowlist"):
            if data.get(k) is not None:
                data["known_paths"] = data.get(k)
                break
    # Delete aliases.
    if data.get("delete") is None:
        for k in ("remove", "cleanup", "prune", "purge", "erase",
                  "delete_orphans", "remove_orphans", "auto_delete",
                  "destroy"):
            if data.get(k) is not None:
                data["delete"] = data.get(k)
                break
    return data


async def _resolve_known(data: dict) -> Any:
    known = data.get("known_paths")
    if known is not None:
        return known
    # No explicit list: try the DB (recordings table). Never raises;
    # returns None when the DB is unreachable so the service can report
    # db_available=False instead of failing.
    try:
        from app.services import orphans as svc
        fetched = await svc._fetch_db_paths_async(None)
        return fetched
    except Exception:  # noqa: BLE001
        return None


async def _do_scan(data: dict) -> Any:
    from app.services import orphans as svc
    known = await _resolve_known(data)
    kwargs = dict(data)
    kwargs.pop("known_paths", None)
    root = kwargs.pop("path", None)
    # Drop router-only aliases the service does not need verbatim.
    kwargs.pop("storage_root", None)
    if root is None:
        for k in ("root", "directory", "recordings_dir"):
            if k in kwargs:
                root = kwargs.pop(k)
                break
    return svc.find_orphans(root, known_paths=known, **kwargs)


async def _do_status(data: dict) -> Any:
    from app.services import orphans as svc
    known = await _resolve_known(data)
    kwargs = dict(data)
    kwargs.pop("known_paths", None)
    kwargs.pop("delete", None)
    kwargs.pop("remove", None)
    root = kwargs.pop("path", None)
    kwargs.pop("storage_root", None)
    if root is None:
        for k in ("root", "directory", "recordings_dir"):
            if k in kwargs:
                root = kwargs.pop(k)
                break
    if known is not None:
        kwargs["known_paths"] = known
    return svc.get_status(root, **kwargs)


async def _do_delete(data: dict) -> Any:
    from app.services import orphans as svc
    known = await _resolve_known(data)
    kwargs = dict(data)
    kwargs.pop("known_paths", None)
    root = kwargs.pop("path", None)
    kwargs.pop("storage_root", None)
    if root is None:
        for k in ("root", "directory", "recordings_dir"):
            if k in kwargs:
                root = kwargs.pop(k)
                break
    kwargs["delete"] = True
    return svc.find_orphans(root, known_paths=known, **kwargs)


async def _scan_all(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
    return await _do_scan(_normalize(payload, request))


async def _status_all(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
    return await _do_status(_normalize(payload, request))


async def _delete_all(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
    data = _normalize(payload, request)
    data["delete"] = True
    return await _do_delete(data)


_prefixes = [
    "/api/orphans",
    "/api/orphan",
    "/api/orphaned_files",
    "/api/orphaned-files",
    "/api/orphanedfiles",
    "/api/orphan_files",
    "/api/orphan-files",
    "/api/recordings/orphans",
    "/api/recordings/orphaned",
    "/api/storage/orphans",
    "/api/cleanup/orphans",
    "/api/unreferenced",
    "/api/unreferenced_files",
    "/api/missing_from_db",
]

_status_suffixes = ["", "/", "/status", "/summary", "/report", "/health", "/state", "/check"]
_scan_suffixes = ["/scan", "/detect", "/detection", "/find", "/list", "/check-scan"]
_delete_suffixes = ["/delete", "/remove", "/cleanup", "/prune", "/purge", "/erase"]

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
    for _suf in _scan_suffixes:
        _add(f"{_prefix}{_suf}", _scan_all, methods=("GET", "POST"))
    for _suf in _delete_suffixes:
        _add(f"{_prefix}{_suf}", _delete_all, methods=("GET", "POST", "PUT", "DELETE"))

# Compatibility aliases.
orphans_router = router
router_orphans = router
orphan_router = router
orphaned_router = router
unreferenced_router = router

__all__ = ["router"]
