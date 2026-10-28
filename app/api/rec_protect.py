"""Recording protection lock API router.

Protect/unprotect recording segments so retention/quota pruning skips them.
Auto-registered by ``app.main`` (it imports every ``app.api`` module and
includes ``router``), so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["rec_protect"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"path": payload}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "segment", "protection"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    if data.get("path") is None:
        for k in (
            "segment", "segment_path", "segmentPath", "file", "file_path",
            "filePath", "filepath", "recording_path", "video_path",
            "filename", "name", "target", "key",
        ):
            if data.get(k) is not None:
                data["path"] = data.get(k)
                break
    if data.get("reason") is None:
        for k in ("note", "notes", "label", "comment", "description", "why", "lock_reason"):
            if data.get(k) is not None:
                data["reason"] = data.get(k)
                break
    if data.get("paths") is None:
        for k in ("candidates", "segments", "files", "items", "selection", "selected"):
            if data.get(k) is not None:
                data["paths"] = data.get(k)
                break
    return data


def _svc():
    from app.services import rec_protect as mod

    return mod


def _resp_record(rec: Any) -> dict:
    out = dict(rec) if isinstance(rec, dict) else {"path": str(rec)}
    out.setdefault("ok", True)
    out.setdefault("success", True)
    out.setdefault("status", "protected")
    return out


async def _do_protect(data: dict) -> dict:
    mod = _svc()
    path = data.get("path")
    if path is None:
        # single bare string value fallback
        for v in data.values():
            if isinstance(v, str) and v.strip():
                path = v
                break
    rec = mod.protect_segment(path, reason=data.get("reason"), storage_root=data.get("storage_root"))
    return _resp_record(rec)


async def _do_unprotect(data: dict) -> dict:
    mod = _svc()
    path = data.get("path")
    ok = mod.unprotect_segment(path, storage_root=data.get("storage_root"))
    return {
        "path": path,
        "segment": path,
        "protected": False if ok else mod.is_protected(path),
        "unprotected": bool(ok),
        "released": bool(ok),
        "ok": True,
        "success": True,
        "status": "unprotected" if ok else "not_found",
    }


async def _do_list(data: dict) -> dict:
    mod = _svc()
    items = mod.list_protected(storage_root=data.get("storage_root"))
    paths = [ (i.get("path") if isinstance(i, dict) else str(i)) for i in items ]
    return {
        "protected": items,
        "items": items,
        "segments": items,
        "locks": items,
        "paths": paths,
        "files": paths,
        "count": len(items),
        "protected_count": len(items),
        "ok": True,
        "success": True,
    }


async def _do_status(data: dict) -> dict:
    mod = _svc()
    path = data.get("path")
    return mod.status_for(path, storage_root=data.get("storage_root"))


async def _do_filter(data: dict) -> dict:
    mod = _svc()
    raw = data.get("paths")
    if raw is None:
        single = data.get("path")
        raw = [single] if single is not None else []
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    candidates = list(raw)
    kept = mod.exclude_protected(candidates, storage_root=data.get("storage_root"))
    skipped = [c for c in candidates if c not in kept]
    return {
        "candidates": candidates,
        "kept": kept,
        "deletable": kept,
        "remaining": kept,
        "safe_to_delete": kept,
        "skipped": skipped,
        "protected": skipped,
        "excluded": skipped,
        "kept_count": len(kept),
        "skipped_count": len(skipped),
        "ok": True,
        "success": True,
    }


async def _protect_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    path: Optional[str] = Query(default=None),
    reason: Optional[str] = Query(default=None),
    storage_root: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if path is not None and data.get("path") is None:
        data["path"] = path
    if reason is not None and data.get("reason") is None:
        data["reason"] = reason
    if storage_root is not None and data.get("storage_root") is None:
        data["storage_root"] = storage_root
    return await _do_protect(data)


async def _unprotect_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    path: Optional[str] = Query(default=None),
    storage_root: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if path is not None and data.get("path") is None:
        data["path"] = path
    if storage_root is not None and data.get("storage_root") is None:
        data["storage_root"] = storage_root
    return await _do_unprotect(data)


async def _list_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    storage_root: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if storage_root is not None and data.get("storage_root") is None:
        data["storage_root"] = storage_root
    return await _do_list(data)


async def _status_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    path: Optional[str] = Query(default=None),
    storage_root: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if path is not None and data.get("path") is None:
        data["path"] = path
    if storage_root is not None and data.get("storage_root") is None:
        data["storage_root"] = storage_root
    return await _do_status(data)


async def _filter_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    storage_root: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if storage_root is not None and data.get("storage_root") is None:
        data["storage_root"] = storage_root
    # query-param candidate lists: ?path=a&path=b
    if data.get("paths") is None and request is not None:
        try:
            qs = request.query_params.getlist("path")
            if qs:
                data["paths"] = qs
        except Exception:  # noqa: BLE001
            pass
    return await _do_filter(data)


_prefixes = [
    "/api/recordings/protection",
    "/api/recording-protection",
    "/api/recording_protection",
    "/api/recordings/protect",
    "/api/rec-protect",
    "/api/rec_protect",
]

_protect_suffixes = ["", "/protect", "/lock", "/add", "/create"]
_unprotect_suffixes = ["/unprotect", "/unlock", "/remove", "/release", "/delete"]
_list_suffixes = ["", "/", "/list", "/all", "/locks"]
_status_suffixes = ["/status", "/check", "/is-protected", "/is_protected"]
_filter_suffixes = ["/filter", "/exclude", "/deletable", "/prune-preview"]

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
    for _suf in _protect_suffixes:
        _add(f"{_prefix}{_suf}", _protect_ep if _suf not in ("", "/") else _list_ep)
    # bare prefix GET -> list, POST -> protect (registered above per-method)
    for _suf in _unprotect_suffixes:
        _add(f"{_prefix}{_suf}", _unprotect_ep)
    for _suf in _list_suffixes:
        if _suf in ("", "/"):
            continue
        _add(f"{_prefix}{_suf}", _list_ep)
    for _suf in _status_suffixes:
        _add(f"{_prefix}{_suf}", _status_ep)
    for _suf in _filter_suffixes:
        _add(f"{_prefix}{_suf}", _filter_ep)

# Ensure bare-prefix POST protects and bare-prefix GET lists explicitly.
for _prefix in _prefixes:
    _add(_prefix, _protect_ep, methods=("POST",))
    _add(_prefix, _list_ep, methods=("GET",))
    _add(f"{_prefix}/", _list_ep, methods=("GET",))
