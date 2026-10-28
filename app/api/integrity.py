"""Recording integrity API.

Auto-registered by ``app.main`` (any module in ``app.api`` exposing
``router`` is included), so no ``app/main.py`` edit is needed.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["integrity"])


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
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    # Path aliases.
    if data.get("path") is None:
        for k in ("file", "filepath", "filename", "storage_path", "root_path",
                  "storage_root", "root", "directory", "dir", "folder",
                  "location", "target_dir", "target", "scan_path",
                  "recordings_dir", "recordings_path", "base_dir"):
            if data.get(k) is not None:
                data["path"] = data.get(k)
                break
    return data


def _scan_kwargs(data: dict) -> dict:
    kw: dict = {}
    for k in ("path", "root", "directory", "storage_root", "recordings_dir",
              "extensions", "exts", "min_size_bytes", "min_size", "min_bytes",
              "check_header", "use_ffprobe", "ffprobe", "quarantine",
              "quarantine_dir", "timeout"):
        if k in data and data[k] is not None:
            kw[k] = data[k]
    # Map generic `path` to positional root handled by service.
    return kw


async def _do_status(data: dict) -> Any:
    from app.services import integrity as svc

    kw = _scan_kwargs(data)
    root = kw.pop("path", None)
    # Status = lightweight scan without quarantine.
    kw.pop("quarantine", None)
    kw.pop("quarantine_dir", None)
    if root is None:
        for k in ("root", "directory", "storage_root", "recordings_dir"):
            if k in kw:
                root = kw.pop(k)
                break
    checker = svc.IntegrityChecker()
    return checker.get_status(root, **kw)


async def _do_scan(data: dict) -> Any:
    from app.services import integrity as svc

    kw = _scan_kwargs(data)
    root = kw.pop("path", None)
    if root is None:
        for k in ("root", "directory", "storage_root", "recordings_dir"):
            if k in kw:
                root = kw.pop(k)
                break
    return svc.scan_directory(root, **kw)


async def _do_check_file(data: dict) -> Any:
    from app.services import integrity as svc

    target = data.get("path") or data.get("file") or data.get("filepath")
    if target is None:
        # Fall back to scanning when no single file was given.
        return await _do_scan(data)
    kw = _scan_kwargs(data)
    kw.pop("path", None)
    kw.pop("quarantine", None)
    kw.pop("quarantine_dir", None)
    return svc.check_file(target, **kw)


async def _do_quarantine(data: dict) -> Any:
    from app.services import integrity as svc

    target = data.get("path") or data.get("file") or data.get("filepath")
    qdir = data.get("quarantine_dir") or data.get("quarantineDir")
    if target is not None:
        from pathlib import Path as _Path

        p = _Path(str(target))
        # Single file -> quarantine just it when it looks corrupt (or always
        # when explicitly requested with force=True).
        force = data.get("force")
        if p.is_file():
            if force or bool(svc.check_file(p).get("corrupt")):
                dest = svc.quarantine_file(p, quarantine_dir=qdir)
                return {
                    "status": "completed",
                    "ok": True,
                    "quarantined": [str(dest)] if dest else [],
                    "quarantined_count": 1 if dest else 0,
                    "path": str(target),
                }
            return {
                "status": "completed",
                "ok": True,
                "quarantined": [],
                "quarantined_count": 0,
                "path": str(target),
                "note": "file-healthy",
            }
    # Otherwise scan-and-quarantine the directory.
    kw = _scan_kwargs(data)
    root = kw.pop("path", None)
    if root is None:
        for k in ("root", "directory", "storage_root", "recordings_dir"):
            if k in kw:
                root = kw.pop(k)
                break
    kw["quarantine"] = True
    if qdir is not None:
        kw["quarantine_dir"] = qdir
    return svc.scan_directory(root, **kw)


async def _status_all(payload: Any = Body(default=None), request: Request = None):
    return await _do_status(_normalize(payload, request))


async def _scan_all(payload: Any = Body(default=None), request: Request = None):
    return await _do_scan(_normalize(payload, request))


async def _check_all(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    # If a concrete file was supplied, check it; else scan.
    from pathlib import Path as _Path

    target = data.get("path") or data.get("file")
    if target is not None:
        try:
            if _Path(str(target)).is_file():
                return await _do_check_file(data)
        except Exception:  # noqa: BLE001
            pass
    return await _do_scan(data)


async def _quarantine_all(payload: Any = Body(default=None), request: Request = None):
    return await _do_quarantine(_normalize(payload, request))


_prefixes = [
    "/api/integrity",
    "/api/recording-integrity",
    "/api/recording_integrity",
    "/api/recordings/integrity",
    "/api/integrity-check",
    "/api/integrity_check",
]

_status_suffixes = ["", "/", "/status", "/health", "/summary", "/report", "/state"]
_scan_suffixes = ["/scan", "/check", "/verify", "/validate", "/inspect"]
_quarantine_suffixes = ["/quarantine", "/quarantine-corrupt", "/repair", "/isolate"]

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
        _add(f"{_prefix}{_suf}", _check_all, methods=("GET", "POST")) if False else None
    # Check aliases that inspect a single file vs scan a directory.
    for _suf in ("/file", "/file-check", "/check-file"):
        _add(f"{_prefix}{_suf}", _check_all, methods=("GET", "POST"))
    for _suf in _quarantine_suffixes:
        _add(f"{_prefix}{_suf}", _quarantine_all, methods=("GET", "POST", "PUT", "DELETE"))

# Compatibility aliases.
integrity_router = router
router_integrity = router
recording_integrity_router = router

__all__ = ["router"]
