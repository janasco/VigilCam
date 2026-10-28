"""Saved-search execution API router (run a saved filter -> events).

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.

This router is intentionally *complementary* to
:mod:`app.api.saved_filters`:

* ``app/api/saved_filters.py`` = CRUD for named filters + ``/query`` which
  only echoes the stored criteria.
* this module = *execution*: resolve the stored criteria and return the
  matching events from the event store (``GET /api/events`` semantics).

Primary namespace (new, no clash with saved_filters)::

    GET  /api/saved-search/{filter_id}/run
    POST /api/saved-search/{filter_id}/run
    GET  /api/saved-search/{filter_id}/execute  (alias)
    GET  /api/saved-search/{filter_id}/events   (alias)
    GET  /api/saved-search/{filter_id}/results  (alias)
    POST /api/saved-search/run                  (body: filter_id/name + overrides)

Legacy convenience (same execution, mounted under the saved-filters
prefixes only for ``/run|/execute|/events|/results`` suffixes, which the
CRUD router does not define)::

    GET  /api/saved-filters/{filter_id}/run
    POST /api/saved-filters/{filter_id}/execute
    ...
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services import saved_filters as _saved_filters
from app.services import saved_search as _svc

router = APIRouter(tags=["saved_search"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"name": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"name": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {"name": str(payload)}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass
    for nested_key in ("saved_search", "savedSearch", "saved_filter", "savedFilter",
                       "filter", "filters", "payload", "options", "config",
                       "params", "data", "body", "request", "fields", "criteria",
                       "query", "overrides", "override"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _coerce_int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _resolve_id(raw: Any) -> Any:
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            raise HTTPException(status_code=400, detail="filter_id is required")
        nid = _coerce_int_or_none(s)
        return nid if nid is not None else s
    if raw is None or isinstance(raw, bool):
        raise HTTPException(status_code=400, detail="filter_id is required")
    return raw


def _extract_ref(data: Dict[str, Any]) -> Any:
    for key in ("filter_id", "filterId", "saved_filter_id", "savedFilterId",
                "id", "uuid", "key", "filter", "saved_filter", "savedFilter",
                "filter_name", "filterName", "name"):
        if key in data and data.get(key) is not None:
            return data.get(key)
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in ("filter_id", "filterid", "id", "name", "filter_name", "filtername"):
        if key in lower and lower[key] is not None:
            return lower[key]
    return None


async def _do_run(filter_ref: Any, request: Request | None,
                  payload: Any, db: AsyncSession | None) -> Dict[str, Any]:
    data = _normalize(payload, request)
    ref = filter_ref
    if ref is None:
        ref = _extract_ref(data)
    if ref is None:
        raise HTTPException(status_code=400, detail="filter_id (or name) is required")
    ref = _resolve_id(ref) if not isinstance(ref, dict) else ref
    record = _saved_filters.get_filter(ref) if not isinstance(ref, dict) else ref
    if record is None:
        raise HTTPException(status_code=404, detail="Saved filter not found")
    # Overrides = everything except the filter identity itself.
    identity = {"filter_id", "filterid", "saved_filter_id", "savedfilterid",
                "id", "uuid", "key", "filter", "saved_filter", "savedfilter",
                "filter_name", "filtername", "name"}
    overrides = {k: v for k, v in data.items()
                 if k.lower() not in identity and v is not None}
    # Explicit in-memory event list (lets callers run without a DB).
    events_override = None
    for ek in ("events", "event_list", "eventList", "items", "results"):
        if ek in data and isinstance(data.get(ek), list):
            events_override = data.get(ek)
            overrides.pop(ek, None)
            break
    criteria = _svc.resolve_criteria(record.get("id"), **overrides) \
        if isinstance(record, dict) and record.get("id") is not None \
        else _svc.resolve_criteria(record, **overrides)
    if events_override is not None:
        result = _svc.execute(record.get("id") if isinstance(record, dict) else ref,
                              events=list(events_override), **overrides)
        return result
    if db is None:
        matched = _svc.filter_event_list([], criteria)
        return {
            "filter": record,
            "criteria": criteria,
            "query": criteria,
            "params": criteria,
            "count": len(matched),
            "total": len(matched),
            "events": matched,
            "results": matched,
        }
    try:
        result = await _svc.execute_async(
            record.get("id") if isinstance(record, dict) and record.get("id") is not None else ref,
            db=db, **overrides,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Saved filter not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


async def _do_run_all(request: Request | None, payload: Any,
                      db: AsyncSession | None) -> Dict[str, Any]:
    data = _normalize(payload, request)
    refs: Any = None
    for key in ("filter_ids", "filterIds", "ids", "filters", "names", "filter_names"):
        if key in data and data.get(key) is not None:
            refs = data.get(key)
            break
    if refs is None:
        single = _extract_ref(data)
        refs = [single] if single is not None else None
    overrides = {k: v for k, v in data.items()
                 if k.lower() not in {"filter_ids", "filterids", "ids", "filters",
                                      "names", "filter_names", "events", "event_list"} and v is not None}
    events_override = data.get("events") if isinstance(data.get("events"), list) else None
    if db is None:
        return _svc.run_all(refs, events=events_override or [], **overrides)
    return await _svc.run_all_async(refs, events=events_override, db=db, **overrides)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base + "/{filter_id}/run", summary="Execute saved search -> events")
    async def _run_get(filter_id: str, request: Request, db: AsyncSession = Depends(db_session)):
        return await _do_run(filter_id, request, None, db)

    @r.get(base + "/{filter_id}/run/", include_in_schema=False)
    async def _run_get_slash(filter_id: str, request: Request, db: AsyncSession = Depends(db_session)):
        return await _do_run(filter_id, request, None, db)

    @r.post(base + "/{filter_id}/run", summary="Execute saved search -> events")
    async def _run_post(filter_id: str, request: Request,
                        payload: Any = Body(default=None),
                        db: AsyncSession = Depends(db_session)):
        return await _do_run(filter_id, request, payload, db)

    @r.post(base + "/{filter_id}/run/", include_in_schema=False)
    async def _run_post_slash(filter_id: str, request: Request,
                              payload: Any = Body(default=None),
                              db: AsyncSession = Depends(db_session)):
        return await _do_run(filter_id, request, payload, db)

    for suffix in ("execute", "events", "results", "search"):
        @r.get(base + "/{filter_id}/" + suffix, include_in_schema=False)
        async def _alias_get(filter_id: str, request: Request,
                             db: AsyncSession = Depends(db_session),
                             _s=suffix):
            return await _do_run(filter_id, request, None, db)

        @r.post(base + "/{filter_id}/" + suffix, include_in_schema=False)
        async def _alias_post(filter_id: str, request: Request,
                              payload: Any = Body(default=None),
                              db: AsyncSession = Depends(db_session)):
            return await _do_run(filter_id, request, payload, db)

    @r.get(base + "/run", summary="Execute saved search (filter_id via query)")
    async def _run_query(request: Request, db: AsyncSession = Depends(db_session)):
        return await _do_run(None, request, None, db)

    @r.post(base, status_code=status.HTTP_200_OK, summary="Execute saved search (body)")
    async def _run_body(request: Request, payload: Any = Body(default=None),
                        db: AsyncSession = Depends(db_session)):
        data = _normalize(payload, request)
        ref = _extract_ref(data)
        if ref is None:
            # No identity -> run all (scheduled-runner convenience).
            return await _do_run_all(request, payload, db)
        return await _do_run(ref, request, payload, db)

    @r.post(base + "/", include_in_schema=False)
    async def _run_body_slash(request: Request, payload: Any = Body(default=None),
                              db: AsyncSession = Depends(db_session)):
        return await _run_body(request, payload, db)

    @r.post(base + "/run", include_in_schema=False)
    async def _run_named(request: Request, payload: Any = Body(default=None),
                         db: AsyncSession = Depends(db_session)):
        data = _normalize(payload, request)
        ref = _extract_ref(data)
        if ref is None:
            return await _do_run_all(request, payload, db)
        return await _do_run(ref, request, payload, db)

    @r.post(base + "/execute", include_in_schema=False)
    async def _exec_named(request: Request, payload: Any = Body(default=None),
                          db: AsyncSession = Depends(db_session)):
        return await _run_named(request, payload, db)

    @r.post(base + "/run-all", include_in_schema=False)
    async def _run_all_post(request: Request, payload: Any = Body(default=None),
                            db: AsyncSession = Depends(db_session)):
        return await _do_run_all(request, payload, db)

    @r.get(base + "/run-all", include_in_schema=False)
    async def _run_all_get(request: Request, db: AsyncSession = Depends(db_session)):
        return await _do_run_all(request, None, db)


_BASES = (
    "/api/saved-search",
    "/api/saved_search",
    "/api/saved-searches",
    "/api/saved_searches",
    "/api/event-search",
    "/api/search/saved",
)

_seen: set = set()
for _base in _BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen:
        continue
    _seen.add(_norm)
    _register(router, _norm)


def _register_legacy(r: APIRouter, base: str) -> None:
    """Execution-only suffixes under existing saved-filters prefixes."""
    for suffix in ("run", "execute", "events", "results"):
        @r.get(base + "/{filter_id}/" + suffix, include_in_schema=False)
        async def _leg_get(filter_id: str, request: Request,
                           db: AsyncSession = Depends(db_session)):
            return await _do_run(filter_id, request, None, db)

        @r.post(base + "/{filter_id}/" + suffix, include_in_schema=False)
        async def _leg_post(filter_id: str, request: Request,
                            payload: Any = Body(default=None),
                            db: AsyncSession = Depends(db_session)):
            return await _do_run(filter_id, request, payload, db)


_LEGACY_BASES = (
    "/api/saved-filters",
    "/api/saved_filters",
    "/api/event-filters",
    "/api/event_filters",
)
for _base in _LEGACY_BASES:
    _register_legacy(router, _base.rstrip("/") or "/")


# Compatibility aliases.
saved_search_router = router
savedSearchRouter = router
saved_search_api_router = router
saved_searches_router = router
event_search_router = router
api_router = router

__all__ = ["router"]
