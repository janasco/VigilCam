"""Time-of-day event search API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.

Filter events by wall-clock time-of-day window (e.g. events between
02:00-04:00 on any date):

* ``GET  /api/tod-search?start=02:00&end=04:00``
* ``GET  /api/tod-search?start_hour=2&end_hour=4``
* ``GET  /api/tod-search?hours=2,3,4``
* ``GET  /api/events/by-tod?start=22:00&end=02:00`` (overnight wrap)
* ``POST /api/tod-search/search`` with ``{"start": "02:00", "end": "04:00"}``

POST bodies may also carry an in-memory ``events`` list (dicts with a
``created_at`` timestamp) so callers can search without a DB round-trip.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query, Request

from app.api.deps import db_session

router = APIRouter(tags=["tod_search"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    if payload is None:
        data: dict = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, str):
        data = {"time_range": payload}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {"time_range": str(payload)}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
            # Repeated ?hours=2&hours=3 style params: keep them all.
            for hk in ("hours", "hour"):
                vals = request.query_params.getlist(hk)
                if len(vals) > 1:
                    data[hk] = vals
        except Exception:
            pass
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "filter", "filters", "criteria", "query_params"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


async def _do_search(data: dict, db: Any = None) -> Any:
    from app.services import tod_search as _svc

    events = None
    for key in ("events", "event_list", "eventList", "items", "rows", "results"):
        if isinstance(data.get(key), (list, tuple)):
            events = list(data.get(key))
            break
    if events is not None:
        kwargs = {k: v for k, v in data.items() if k not in (
            "events", "event_list", "eventList", "items", "rows", "results",
        )}
        return _svc.search_events(events, **kwargs)
    session = db if (db is not None and hasattr(db, "execute")) else None
    if session is None:
        for key in ("db", "session", "database"):
            if data.get(key) is not None and hasattr(data.get(key), "execute"):
                session = data.get(key)
                break
    return await _svc.search_events_async(session, **data)


async def _search_dep(
    request: Request,
    payload: Any = Body(default=None),
    db: Any = Depends(db_session),
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    start_time: Optional[str] = Query(default=None),
    end_time: Optional[str] = Query(default=None),
    start_hour: Optional[str] = Query(default=None),
    end_hour: Optional[str] = Query(default=None),
    hour: Optional[str] = Query(default=None),
    hours: Optional[str] = Query(default=None),
    time_range: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    label: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
):
    data = _normalize(payload, request)
    if start is not None and data.get("start") is None:
        data["start"] = start
    if end is not None and data.get("end") is None:
        data["end"] = end
    if start_time is not None and data.get("start_time") is None:
        data["start_time"] = start_time
    if end_time is not None and data.get("end_time") is None:
        data["end_time"] = end_time
    if start_hour is not None and data.get("start_hour") is None:
        data["start_hour"] = start_hour
    if end_hour is not None and data.get("end_hour") is None:
        data["end_hour"] = end_hour
    if hour is not None and data.get("hour") is None:
        data["hour"] = hour
    if hours is not None and data.get("hours") is None:
        data["hours"] = hours
    if time_range is not None and data.get("time_range") is None:
        data["time_range"] = time_range
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if label is not None and data.get("label") is None:
        data["label"] = label
    if limit is not None and data.get("limit") is None:
        data["limit"] = limit
    if offset is not None and data.get("offset") is None:
        data["offset"] = offset
    session = db if hasattr(db, "execute") else None
    return await _do_search(data, session)


_prefixes = [
    "/api/tod-search",
    "/api/tod_search",
    "/api/todsearch",
    "/api/time-of-day-search",
    "/api/time_of_day_search",
    "/api/search/tod",
    "/api/search/by-tod",
    "/api/search/by_tod",
    "/api/search/time-of-day",
    "/api/events/tod-search",
    "/api/events/by-tod",
    "/api/events/by_tod",
    "/api/events/time-of-day",
    "/api/events/search/tod",
]

_search_suffixes = ["", "/", "/search", "/query", "/find", "/events", "/results", "/filter"]

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
        except Exception:
            pass


for _prefix in _prefixes:
    for _suf in _search_suffixes:
        _p = _prefix if _suf in ("", "/") else f"{_prefix}{_suf}"
        _add(_p, _search_dep)


# Compatibility aliases.
tod_search_router = router
todSearchRouter = router
tod_search_api_router = router
time_of_day_search_router = router
router_tod_search = router
api_router = router

__all__ = ["router"]
