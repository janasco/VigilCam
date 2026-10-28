"""Audit prune / retention API router.

Exposes :mod:`app.services.audit_prune` over HTTP under a broad set of
alias prefixes.  Auto-registered by ``app.main`` (it imports every
``app.api`` module and includes ``router``), so no ``app/main.py`` edit
is required.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["audit-prune"])


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

    # Coerce numerics that arrive as strings.
    for key in (
        "retention_days", "max_age_days", "max_age", "retention", "age_days",
        "days", "older_than_days", "max_file_age_days", "audit_retention_days",
        "max_entries", "max_count", "max_records", "keep", "keep_count",
        "limit", "count", "size", "n", "max", "top",
    ):
        if data.get(key) is not None and isinstance(data.get(key), str):
            s = str(data.get(key)).strip()
            try:
                data[key] = float(s) if "." in s else int(s)
            except (TypeError, ValueError):
                pass
    for key in ("dry_run", "dryRun", "dry", "simulate", "preview", "preview_only"):
        if data.get(key) is not None and isinstance(data.get(key), str):
            s = str(data.get(key)).strip().lower()
            if s in ("1", "true", "yes", "y", "on"):
                data[key] = True
            elif s in ("0", "false", "no", "n", "off"):
                data[key] = False
    return data


async def _do_prune(data: dict) -> Any:
    from app.services.audit_prune import prune as _prune

    return _prune(**dict(data or {}))


async def _do_preview(data: dict) -> Any:
    from app.services.audit_prune import prune as _prune

    payload = dict(data or {})
    payload["dry_run"] = True
    return _prune(**payload)


async def _do_status(data: dict) -> Any:
    from app.services.audit_prune import get_policy, select_for_deletion
    from app.services.audit import _default_store

    policy = get_policy(**dict(data or {}))
    try:
        total = len(_default_store.read_all())
    except Exception:  # noqa: BLE001
        total = 0
    try:
        candidates = select_for_deletion(**dict(data or {}))
        candidate_count = len(candidates)
    except Exception:  # noqa: BLE001
        candidate_count = 0
    out = dict(policy)
    out.update(
        {
            "status": "ok",
            "ok": True,
            "total": total,
            "total_entries": total,
            "total_count": total,
            "candidate_count": candidate_count,
            "candidates_count": candidate_count,
            "selected_count": candidate_count,
        }
    )
    return out


async def _prune_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    retention_days: Optional[float] = Query(default=None),
    max_age_days: Optional[float] = Query(default=None),
    max_age: Optional[float] = Query(default=None),
    days: Optional[float] = Query(default=None),
    older_than_days: Optional[float] = Query(default=None),
    max_entries: Optional[int] = Query(default=None),
    max_count: Optional[int] = Query(default=None),
    keep: Optional[int] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    before: Optional[str] = Query(default=None),
    cutoff: Optional[str] = Query(default=None),
    dry_run: Optional[bool] = Query(default=None),
    preview: Optional[bool] = Query(default=None),
):
    data = _normalize(payload, request)
    if retention_days is not None and data.get("retention_days") is None:
        data["retention_days"] = retention_days
    if max_age_days is not None and data.get("max_age_days") is None:
        data["max_age_days"] = max_age_days
    if max_age is not None and data.get("max_age") is None:
        data["max_age"] = max_age
    if days is not None and data.get("days") is None:
        data["days"] = days
    if older_than_days is not None and data.get("older_than_days") is None:
        data["older_than_days"] = older_than_days
    if max_entries is not None and data.get("max_entries") is None:
        data["max_entries"] = max_entries
    if max_count is not None and data.get("max_count") is None:
        data["max_count"] = max_count
    if keep is not None and data.get("keep") is None:
        data["keep"] = keep
    if limit is not None and data.get("limit") is None:
        data["limit"] = limit
    if before is not None and data.get("before") is None:
        data["before"] = before
    if cutoff is not None and data.get("cutoff") is None:
        data["cutoff"] = cutoff
    if dry_run is not None and data.get("dry_run") is None:
        data["dry_run"] = bool(dry_run)
    if preview is not None and data.get("dry_run") is None:
        data["dry_run"] = bool(preview)
    return await _do_prune(data)


async def _preview_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    return await _do_preview(_normalize(payload, request))


async def _status_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    return await _do_status(_normalize(payload, request))


_PRUNE_PATHS = [
    "/api/audit/prune",
    "/api/audit/prune/",
    "/api/audit/prune/run",
    "/api/audit-prune",
    "/api/audit-prune/",
    "/api/audit_prune",
    "/api/audit_prune/",
    "/api/audit/retention",
    "/api/audit/retention/prune",
    "/api/audit/cleanup",
    "/api/audit/cleanup/run",
    "/api/audit-retention/prune",
    "/api/audit-cleanup/prune",
    "/api/prune/audit",
    "/api/retention/audit",
    "/api/cleanup/audit",
]
_PREVIEW_PATHS = [
    "/api/audit/prune/preview",
    "/api/audit/prune/dry-run",
    "/api/audit/prune/dry_run",
    "/api/audit-prune/preview",
    "/api/audit_prune/preview",
    "/api/audit/retention/preview",
    "/api/audit/cleanup/preview",
]
_STATUS_PATHS = [
    "/api/audit/prune/status",
    "/api/audit/prune/policy",
    "/api/audit/prune/count",
    "/api/audit-prune/status",
    "/api/audit_prune/status",
    "/api/audit/retention/status",
    "/api/audit/cleanup/status",
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


for _p in _PRUNE_PATHS:
    _add(_p, _prune_all, methods=("GET", "POST", "DELETE"))
for _p in _PREVIEW_PATHS:
    _add(_p, _preview_all, methods=("GET", "POST"))
for _p in _STATUS_PATHS:
    _add(_p, _status_all, methods=("GET", "POST"))


# Compatibility aliases.
audit_prune_router = router
router_audit_prune = router
prune_router = router
audit_retention_router = router
audit_cleanup_router = router
audit_api_router = router
api_router = router

__all__ = ["router"]
