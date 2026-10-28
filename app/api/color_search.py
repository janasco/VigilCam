"""Dominant-color event search API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.

Search events by snapshot dominant color (``red car`` etc. via snapshot
histograms computed in :mod:`app.services.color_search`):

* ``GET  /api/color-search?color=red&limit=20``
* ``POST /api/color-search/search`` with ``{"color": "red"}``
* ``GET  /api/events/by-color?color=blue``
* ``POST /api/color-search/analyze`` with ``{"image_base64": ...}``
* ``GET  /api/color-search/colors`` lists supported colors

POST bodies may also carry an in-memory ``events`` list (dicts with
``snapshot_path`` / ``image_base64`` / ``color`` / ``tags``) so callers
can search without a DB round-trip.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query, Request

from app.api.deps import db_session

router = APIRouter(tags=["color_search"])


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
        data = {"color": payload}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {"color": str(payload)}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
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
    from app.services import color_search as _svc
    # in-memory events take precedence when provided
    events = None
    for key in ("events", "event_list", "eventList", "items", "rows", "results"):
        if isinstance(data.get(key), (list, tuple)):
            events = list(data.get(key))
            break
    if events is not None:
        return _svc.search_events(events, **data)
    session = db if (db is not None and hasattr(db, "execute")) else None
    if session is None:
        for key in ("db", "session", "database"):
            if data.get(key) is not None and hasattr(data.get(key), "execute"):
                session = data.get(key)
                break
    return await _svc.search_events_async(session, **data)


async def _do_analyze(data: dict) -> Any:
    from app.services import color_search as _svc
    img = None
    for key in ("image_base64", "image", "img", "data", "bytes", "snapshot",
                "frame", "picture", "photo", "path", "snapshot_path", "file"):
        if data.get(key) is not None:
            img = data.get(key)
            break
    if img is None and isinstance(data.get("event"), dict):
        img = data.get("event")
    bins = data.get("bins", data.get("histogram_bins", 8))
    return _svc.analyze_image(img, bins=bins)


async def _do_colors() -> Any:
    from app.services import color_search as _svc
    colors = list(_svc.SUPPORTED_COLORS)
    return {
        "colors": colors,
        "colours": colors,
        "supported_colors": colors,
        "count": len(colors),
        "prototypes": {k: list(v) for k, v in _svc.COLOR_RGB.items()},
    }


async def _search_dep(
    request: Request,
    payload: Any = Body(default=None),
    db: Any = Depends(db_session),
    color: Optional[str] = Query(default=None),
    colour: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    label: Optional[str] = Query(default=None),
    threshold: Optional[float] = Query(default=None),
):
    data = _normalize(payload, request)
    if color is not None and data.get("color") is None:
        data["color"] = color
    if colour is not None and data.get("color") is None and data.get("colour") is None:
        data["colour"] = colour
    if limit is not None and data.get("limit") is None:
        data["limit"] = limit
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if label is not None and data.get("label") is None:
        data["label"] = label
    if threshold is not None and data.get("threshold") is None:
        data["threshold"] = threshold
    session = db if hasattr(db, "execute") else None
    return await _do_search(data, session)


async def _analyze_dep(request: Request, payload: Any = Body(default=None)):
    data = _normalize(payload, request)
    return await _do_analyze(data)


async def _colors_dep():
    return await _do_colors()


_prefixes = [
    "/api/color-search",
    "/api/color_search",
    "/api/colorsearch",
    "/api/search/color",
    "/api/search/by-color",
    "/api/search/by_color",
    "/api/events/color-search",
    "/api/events/color_search",
    "/api/events/by-color",
    "/api/events/by_color",
    "/api/events/search/color",
    "/api/events/color",
]

_search_suffixes = ["", "/", "/search", "/query", "/find", "/events", "/results"]
_analyze_suffixes = ["/analyze", "/analyse", "/detect", "/classify"]
_colors_suffixes = ["/colors", "/colours", "/supported-colors", "/supported_colours", "/names"]

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
    for _suf in _analyze_suffixes:
        _add(f"{_prefix}{_suf}", _analyze_dep)
    for _suf in _colors_suffixes:
        _add(f"{_prefix}{_suf}", _colors_dep, methods=("GET",))


# Compatibility aliases.
color_search_router = router
colorSearchRouter = router
color_search_api_router = router
colour_search_router = router
router_color_search = router
api_router = router

__all__ = ["router"]
