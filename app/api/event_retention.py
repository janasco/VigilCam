"""Event retention / quota API router.

Exposes :mod:`app.services.event_retention` over HTTP with max age/count
per-camera quotas and dry-run support.  Auto-registered by ``app.main``
(it imports every ``app.api`` module and includes ``router``), so no
``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session

router = APIRouter(tags=["event-retention"])


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
    elif isinstance(payload, (list, tuple)):
        data = {"events": list(payload)}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    vals = request.query_params.getlist(k)
                    data[k] = vals[0] if len(vals) == 1 else vals
        except Exception:  # noqa: BLE001
            pass

    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "policy", "settings", "filter", "filters", "quota"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # Coerce numerics that arrive as strings.
    for key in (
        "retention_days", "max_age_days", "max_age", "retention", "age_days",
        "days", "older_than_days", "max_file_age_days", "event_retention_days",
        "ttl_days", "max_entries", "max_count", "max_records", "max_events",
        "keep", "keep_count", "limit", "count", "size", "max", "quota",
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


async def _do_prune(data: dict, db: AsyncSession) -> Any:
    from app.services import event_retention as svc

    # Inline events (no DB needed) — useful for isolated router tests.
    inline = None
    for k in ("events", "entries", "items", "rows", "event_list"):
        if isinstance(data.get(k), (list, tuple)) and data.get(k):
            inline = list(data.get(k))
            break
    if inline is not None:
        payload = dict(data)
        # operate on a copy so the request echo is not mutated unexpectedly
        working = [dict(e) if isinstance(e, dict) else e for e in inline]
        return svc.prune(entries=working, **{kk: vv for kk, vv in payload.items() if kk not in ("events", "entries", "items", "rows", "event_list")})
    return await svc.prune_async(db=db, **dict(data or {}))


async def _do_preview(data: dict, db: AsyncSession) -> Any:
    payload = dict(data or {})
    payload["dry_run"] = True
    return await _do_prune(payload, db)


async def _do_status(data: dict, db: AsyncSession) -> Any:
    from app.services import event_retention as svc

    return await svc.get_status_async(db=db, **dict(data or {}))


async def _prune_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = None,  # type: ignore[assignment]
    retention_days: Optional[float] = Query(default=None),
    max_age_days: Optional[float] = Query(default=None),
    max_age: Optional[float] = Query(default=None),
    days: Optional[float] = Query(default=None),
    older_than_days: Optional[float] = Query(default=None),
    max_entries: Optional[int] = Query(default=None),
    max_count: Optional[int] = Query(default=None),
    keep: Optional[int] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    camera_id: Optional[int] = Query(default=None),
    camera_ids: Optional[str] = Query(default=None),
    before: Optional[str] = Query(default=None),
    cutoff: Optional[str] = Query(default=None),
    dry_run: Optional[bool] = Query(default=None),
    preview: Optional[bool] = Query(default=None),
):
    from fastapi import Depends as _Depends

    _ = _Depends
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
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if camera_ids is not None and data.get("camera_ids") is None:
        data["camera_ids"] = camera_ids
    if before is not None and data.get("before") is None:
        data["before"] = before
    if cutoff is not None and data.get("cutoff") is None:
        data["cutoff"] = cutoff
    if dry_run is not None and data.get("dry_run") is None:
        data["dry_run"] = bool(dry_run)
    if preview is not None and data.get("dry_run") is None:
        data["dry_run"] = bool(preview)
    # Resolve db when called via FastAPI DI; direct calls pass db explicitly.
    if db is None:
        async for _s in db_session():
            db = _s
            break
    return await _do_prune(data, db)


async def _preview_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = None,  # type: ignore[assignment]
):
    data = _normalize(payload, request)
    if db is None:
        async for _s in db_session():
            db = _s
            break
    return await _do_preview(data, db)


async def _status_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = None,  # type: ignore[assignment]
):
    data = _normalize(payload, request)
    if db is None:
        async for _s in db_session():
            db = _s
            break
    return await _do_status(data, db)


# Build real route handlers with explicit Depends to satisfy FastAPI DI.
from fastapi import Depends as _Depends  # noqa: E402


async def _handle_prune(
    request: Request,
    db: AsyncSession = _Depends(db_session),
    payload: Any = Body(default=None),
    retention_days: Optional[float] = Query(default=None),
    max_age_days: Optional[float] = Query(default=None),
    max_age: Optional[float] = Query(default=None),
    days: Optional[float] = Query(default=None),
    older_than_days: Optional[float] = Query(default=None),
    max_entries: Optional[int] = Query(default=None),
    max_count: Optional[int] = Query(default=None),
    keep: Optional[int] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    camera_id: Optional[int] = Query(default=None),
    camera_ids: Optional[str] = Query(default=None),
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
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if camera_ids is not None and data.get("camera_ids") is None:
        data["camera_ids"] = camera_ids
    if before is not None and data.get("before") is None:
        data["before"] = before
    if cutoff is not None and data.get("cutoff") is None:
        data["cutoff"] = cutoff
    if dry_run is not None and data.get("dry_run") is None:
        data["dry_run"] = bool(dry_run)
    if preview is not None and data.get("dry_run") is None:
        data["dry_run"] = bool(preview)
    return await _do_prune(data, db)


async def _handle_preview(
    request: Request,
    db: AsyncSession = _Depends(db_session),
    payload: Any = Body(default=None),
):
    return await _do_preview(_normalize(payload, request), db)


async def _handle_status(
    request: Request,
    db: AsyncSession = _Depends(db_session),
    payload: Any = Body(default=None),
):
    return await _do_status(_normalize(payload, request), db)


_PRUNE_PATHS = [
    "/api/events/retention",
    "/api/events/retention/",
    "/api/events/retention/prune",
    "/api/events/retention/run",
    "/api/events/retention/enforce",
    "/api/events/retention/cleanup",
    "/api/events/retention/delete",
    "/api/events/prune",
    "/api/events/cleanup",
    "/api/events/quota",
    "/api/event-retention",
    "/api/event-retention/",
    "/api/event-retention/prune",
    "/api/event-retention/run",
    "/api/event-retention/enforce",
    "/api/event_retention",
    "/api/event_retention/",
    "/api/event_retention/prune",
    "/api/event_retention/run",
    "/api/event_retention/enforce",
    "/api/eventretention",
    "/api/eventretention/prune",
    "/api/retention/events",
    "/api/retention/events/prune",
    "/api/events/retention/quota",
]
_PREVIEW_PATHS = [
    "/api/events/retention/preview",
    "/api/events/retention/dry-run",
    "/api/events/retention/dry_run",
    "/api/events/prune/preview",
    "/api/event-retention/preview",
    "/api/event_retention/preview",
    "/api/eventretention/preview",
    "/api/retention/events/preview",
]
_STATUS_PATHS = [
    "/api/events/retention/status",
    "/api/events/retention/policy",
    "/api/events/retention/count",
    "/api/events/retention/quota/status",
    "/api/event-retention/status",
    "/api/event_retention/status",
    "/api/eventretention/status",
    "/api/retention/events/status",
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
    _add(_p, _handle_prune, methods=("GET", "POST", "DELETE"))
for _p in _PREVIEW_PATHS:
    _add(_p, _handle_preview, methods=("GET", "POST"))
for _p in _STATUS_PATHS:
    _add(_p, _handle_status, methods=("GET", "POST"))


# Compatibility aliases.
event_retention_router = router
router_event_retention = router
retention_router = router
event_prune_router = router
event_cleanup_router = router
event_quota_router = router
events_retention_router = router
api_router = router

__all__ = ["router"]
