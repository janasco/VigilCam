"""Protection expiry API router.

Adds time-based auto-unlock on top of ``app.api.rec_protect`` without
editing ``app/main.py``: ``app.main`` auto-imports every ``app.api``
module and includes ``router``, so defining ``router`` here is enough.

Endpoints (alias-rich, GET+POST like ``rec_protect``):

* ``.../expiry/protect`` -- lock with ``expires_in_days`` / ``ttl_*`` /
  ``expires_at`` (permanent when omitted and no default policy set)
* ``.../expiry/sweep`` -- auto-unlock all expired locks
* ``.../expiry/status`` -- expiry-aware status for one segment
* ``.../expiry/list`` / ``.../expiry/expired`` / ``.../expiry/expiring``
* ``.../expiry/policy`` -- get/set the default expiry policy
* ``.../expiry/extend`` -- extend a lock; ``.../expiry/clear-expiry``
  makes a lock permanent again
"""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["protect_expiry"])


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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "segment", "protection", "policy"):
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
    # Coerce numerics that arrive as query strings.
    for key in (
        "expires_in_days", "expiry_days", "ttl_days", "days",
        "ttl", "expires_in", "ttl_seconds", "expires_in_seconds",
        "ttl_hours", "expires_in_hours", "ttl_minutes", "expires_in_minutes",
        "extra_days", "extend_by_days", "within_days",
        "default_expires_in_days", "max_expires_in_days",
    ):
        if data.get(key) is not None and isinstance(data.get(key), str):
            s = str(data.get(key)).strip()
            try:
                data[key] = float(s) if "." in s else int(s)
            except (TypeError, ValueError):
                pass
    for key in ("dry_run", "dryRun", "dry", "auto_sweep", "autoSweep"):
        if data.get(key) is not None and isinstance(data.get(key), str):
            s = str(data.get(key)).strip().lower()
            if s in ("1", "true", "yes", "y", "on"):
                data[key] = True
            elif s in ("0", "false", "no", "n", "off"):
                data[key] = False
    return data


def _svc():
    from app.services import protect_expiry as mod

    return mod


async def _do_protect(data: dict) -> dict:
    mod = _svc()
    out = mod.protect_with_expiry(
        data.get("path"),
        reason=data.get("reason"),
        storage_root=data.get("storage_root"),
        **{k: v for k, v in data.items() if k not in ("path", "reason", "storage_root")},
    )
    out.setdefault("ok", True)
    out.setdefault("success", True)
    return out


async def _do_sweep(data: dict) -> dict:
    mod = _svc()
    return mod.sweep_expired(storage_root=data.get("storage_root"), dry_run=data.get("dry_run", data.get("dryRun", False)))


async def _do_status(data: dict) -> dict:
    mod = _svc()
    return mod.status_for(data.get("path"), storage_root=data.get("storage_root"))


async def _do_list(data: dict) -> dict:
    mod = _svc()
    items = mod.list_protected(
        storage_root=data.get("storage_root"),
        include_expired=data.get("include_expired", data.get("includeExpired", True)),
    )
    paths = [(i.get("path") if isinstance(i, dict) else str(i)) for i in items]
    expired = [i for i in items if isinstance(i, dict) and i.get("expired")]
    return {
        "protected": items,
        "items": items,
        "segments": items,
        "locks": items,
        "paths": paths,
        "files": paths,
        "expired": expired,
        "expired_count": len(expired),
        "count": len(items),
        "protected_count": len(items),
        "ok": True,
        "success": True,
    }


async def _do_expired(data: dict) -> dict:
    mod = _svc()
    items = mod.list_expired(storage_root=data.get("storage_root"))
    paths = [(i.get("path") if isinstance(i, dict) else str(i)) for i in items]
    return {
        "expired": items,
        "items": items,
        "paths": paths,
        "files": paths,
        "count": len(items),
        "expired_count": len(items),
        "ok": True,
        "success": True,
    }


async def _do_expiring(data: dict) -> dict:
    mod = _svc()
    within = data.get("within_days", data.get("withinDays", 7))
    items = mod.list_expiring(within, storage_root=data.get("storage_root"))
    paths = [(i.get("path") if isinstance(i, dict) else str(i)) for i in items]
    return {
        "expiring": items,
        "items": items,
        "paths": paths,
        "files": paths,
        "count": len(items),
        "expiring_count": len(items),
        "within_days": within,
        "ok": True,
        "success": True,
    }


async def _do_policy_get(data: dict) -> dict:
    mod = _svc()
    return mod.get_policy()


async def _do_policy_set(data: dict) -> dict:
    mod = _svc()
    return mod.set_policy(dict(data))


async def _do_extend(data: dict) -> dict:
    mod = _svc()
    path = data.get("path")
    rest = {k: v for k, v in data.items() if k not in ("path", "storage_root")}
    return mod.extend_protection(path, storage_root=data.get("storage_root"), **rest)


async def _do_clear_expiry(data: dict) -> dict:
    mod = _svc()
    return mod.clear_expiry(data.get("path"), storage_root=data.get("storage_root"))


async def _do_set_expiry(data: dict) -> dict:
    mod = _svc()
    path = data.get("path")
    rest = {k: v for k, v in data.items() if k not in ("path", "storage_root")}
    return mod.set_expiry(path, storage_root=data.get("storage_root"), **rest)


async def _protect_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    path: Optional[str] = Query(default=None),
    reason: Optional[str] = Query(default=None),
    storage_root: Optional[str] = Query(default=None),
    expires_in_days: Optional[float] = Query(default=None),
    ttl_days: Optional[float] = Query(default=None),
    expires_at: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if path is not None and data.get("path") is None:
        data["path"] = path
    if reason is not None and data.get("reason") is None:
        data["reason"] = reason
    if storage_root is not None and data.get("storage_root") is None:
        data["storage_root"] = storage_root
    if expires_in_days is not None and data.get("expires_in_days") is None:
        data["expires_in_days"] = expires_in_days
    if ttl_days is not None and data.get("ttl_days") is None:
        data["ttl_days"] = ttl_days
    if expires_at is not None and data.get("expires_at") is None:
        data["expires_at"] = expires_at
    return await _do_protect(data)


async def _sweep_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    storage_root: Optional[str] = Query(default=None),
    dry_run: Optional[bool] = Query(default=None),
):
    data = _normalize(payload, request)
    if storage_root is not None and data.get("storage_root") is None:
        data["storage_root"] = storage_root
    if dry_run is not None and data.get("dry_run") is None:
        data["dry_run"] = bool(dry_run)
    return await _do_sweep(data)


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


async def _list_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    storage_root: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if storage_root is not None and data.get("storage_root") is None:
        data["storage_root"] = storage_root
    return await _do_list(data)


async def _expired_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    storage_root: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if storage_root is not None and data.get("storage_root") is None:
        data["storage_root"] = storage_root
    return await _do_expired(data)


async def _expiring_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    storage_root: Optional[str] = Query(default=None),
    within_days: Optional[float] = Query(default=None),
):
    data = _normalize(payload, request)
    if storage_root is not None and data.get("storage_root") is None:
        data["storage_root"] = storage_root
    if within_days is not None and data.get("within_days") is None:
        data["within_days"] = within_days
    return await _do_expiring(data)


async def _policy_get_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    data = _normalize(payload, request)
    return await _do_policy_get(data)


async def _policy_set_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    default_expires_in_days: Optional[float] = Query(default=None),
    max_expires_in_days: Optional[float] = Query(default=None),
):
    data = _normalize(payload, request)
    if default_expires_in_days is not None and data.get("default_expires_in_days") is None:
        data["default_expires_in_days"] = default_expires_in_days
    if max_expires_in_days is not None and data.get("max_expires_in_days") is None:
        data["max_expires_in_days"] = max_expires_in_days
    return await _do_policy_set(data)


async def _extend_ep(
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
    return await _do_extend(data)


async def _clear_expiry_ep(
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
    return await _do_clear_expiry(data)


async def _set_expiry_ep(
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
    return await _do_set_expiry(data)


_prefixes = [
    "/api/recordings/protection/expiry",
    "/api/recording-protection/expiry",
    "/api/recording_protection/expiry",
    "/api/recordings/protect/expiry",
    "/api/rec-protect/expiry",
    "/api/rec_protect/expiry",
    "/api/protection/expiry",
    "/api/protect-expiry",
    "/api/protect_expiry",
]

_protect_suffixes = ["", "/protect", "/lock", "/add", "/create"]
_sweep_suffixes = ["/sweep", "/unlock-expired", "/unlock_expired", "/auto-unlock", "/auto_unlock", "/prune", "/cleanup", "/expire", "/release-expired"]
_status_suffixes = ["/status", "/check", "/is-protected", "/is_protected"]
_list_suffixes = ["/list", "/all", "/locks"]
_expired_suffixes = ["/expired", "/expired-list"]
_expiring_suffixes = ["/expiring", "/expiring-soon", "/expiring_soon"]
_policy_get_suffixes = ["/policy", "/config", "/policy/get"]
_policy_set_suffixes = ["/policy/set", "/policy/update", "/config/set"]
_extend_suffixes = ["/extend", "/renew"]
_clear_suffixes = ["/clear-expiry", "/clear_expiry", "/make-permanent", "/unexpire"]
_set_suffixes = ["/set", "/set-expiry", "/set_expiry"]

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
    for _suf in _sweep_suffixes:
        _add(f"{_prefix}{_suf}", _sweep_ep)
    for _suf in _status_suffixes:
        _add(f"{_prefix}{_suf}", _status_ep)
    for _suf in _list_suffixes:
        if _suf in ("", "/"):
            continue
        _add(f"{_prefix}{_suf}", _list_ep)
    for _suf in _expired_suffixes:
        _add(f"{_prefix}{_suf}", _expired_ep)
    for _suf in _expiring_suffixes:
        _add(f"{_prefix}{_suf}", _expiring_ep)
    for _suf in _extend_suffixes:
        _add(f"{_prefix}{_suf}", _extend_ep)
    for _suf in _clear_suffixes:
        _add(f"{_prefix}{_suf}", _clear_expiry_ep)
    for _suf in _set_suffixes:
        _add(f"{_prefix}{_suf}", _set_expiry_ep)

# Policy GET vs POST split on the shared /policy path.
for _prefix in _prefixes:
    _add(f"{_prefix}/policy", _policy_get_ep, methods=("GET",))
    _add(f"{_prefix}/policy", _policy_set_ep, methods=("POST", "PUT", "PATCH"))
    _add(f"{_prefix}/config", _policy_get_ep, methods=("GET",))
    _add(f"{_prefix}/config", _policy_set_ep, methods=("POST", "PUT", "PATCH"))
    for _suf in _policy_get_suffixes:
        if _suf in ("/policy", "/config"):
            continue
        _add(f"{_prefix}{_suf}", _policy_get_ep)
    for _suf in _policy_set_suffixes:
        _add(f"{_prefix}{_suf}", _policy_set_ep)
    # Bare prefix: GET -> list, POST -> protect.
    _add(_prefix, _protect_ep, methods=("POST",))
    _add(_prefix, _list_ep, methods=("GET",))
    _add(f"{_prefix}/", _list_ep, methods=("GET",))

# Compatibility aliases.
protect_expiry_router = router
expiry_router = router
protection_expiry_router = router
api_router = router

__all__ = ["router"]
