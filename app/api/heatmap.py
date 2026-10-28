"""Event heatmap API router (per-hour / per-camera counts)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query, Request

from app.api.deps import db_session

router = APIRouter(tags=["heatmap"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "filter", "filters"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


async def _do_heatmap(data: dict, db: Any = None) -> Any:
    from app.services import heatmap as _svc

    kwargs = dict(data)
    if db is not None:
        kwargs.setdefault("db", db)
    return await _svc.get_heatmap(**kwargs)


async def _heatmap_all_dep(
    request: Request,
    payload: Any = Body(default=None),
    db: Any = Depends(db_session),
    camera_id: Optional[str] = Query(default=None),
    camera_ids: Optional[str] = Query(default=None),
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    hours: Optional[float] = Query(default=None),
    days: Optional[float] = Query(default=None),
    label: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
):
    data = _normalize(payload, request)
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if camera_ids is not None and data.get("camera_ids") is None:
        data["camera_ids"] = camera_ids
    if start is not None and data.get("start") is None:
        data["start"] = start
    if end is not None and data.get("end") is None:
        data["end"] = end
    if hours is not None and data.get("hours") is None:
        data["hours"] = hours
    if days is not None and data.get("days") is None:
        data["days"] = days
    if label is not None and data.get("label") is None:
        data["label"] = label
    if limit is not None and data.get("limit") is None:
        data["limit"] = limit
    session = db if hasattr(db, "execute") else None
    return await _do_heatmap(data, session)


async def _heatmap_camera_dep(
    camera_id: Any,
    request: Request,
    payload: Any = Body(default=None),
    db: Any = Depends(db_session),
):
    data = _normalize(payload, request)
    try:
        data.setdefault("camera_id", int(camera_id))
    except (TypeError, ValueError):
        if data.get("camera_name") is None:
            data["camera_name"] = str(camera_id)
        data.setdefault("camera_id", camera_id)
    session = db if hasattr(db, "execute") else None
    return await _do_heatmap(data, session)


_prefixes = [
    "/api/heatmap",
    "/api/heatmaps",
    "/api/event_heatmap",
    "/api/event-heatmap",
    "/api/events/heatmap",
    "/api/events/heatmaps",
    "/api/analytics/heatmap",
    "/api/analytics/heatmaps",
]

_suffixes = [
    "",
    "/",
    "/summary",
    "/hourly",
    "/per-hour",
    "/per_hour",
    "/by-hour",
    "/by_hour",
    "/hours",
    "/per-camera",
    "/per_camera",
    "/by-camera",
    "/by_camera",
    "/cameras",
    "/matrix",
    "/counts",
]

_camera_suffixes = [
    "",
    "/summary",
    "/hourly",
    "/counts",
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
        except Exception:
            pass


for _prefix in _prefixes:
    for _suf in _suffixes:
        _p = _prefix if _suf in ("", "/") else f"{_prefix}{_suf}"
        _add(_p, _heatmap_all_dep)
    for _suf in _camera_suffixes:
        _base1 = f"{_prefix}/cameras/{{camera_id}}" + (_suf or "")
        _base2 = f"{_prefix}/camera/{{camera_id}}" + (_suf or "")
        for _p in (_base1, _base2):
            _add(_p, _heatmap_camera_dep)


# Compatibility aliases.
heatmap_router = router
router_heatmap = router
heatmaps_router = router
event_heatmap_router = router
heatmap_api_router = router

__all__ = ["router"]
