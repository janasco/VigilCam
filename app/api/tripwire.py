"""Virtual tripwire API router (per-camera trip lines + crossing evaluation)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query, Request

from app.api.deps import db_session

router = APIRouter(tags=["tripwire"])


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


_PREFIXES = [
    "/api/tripwire",
    "/api/tripwires",
    "/api/trip_wire",
    "/api/trip_wires",
    "/api/virtual_tripwire",
]


async def _list_dep(
    request: Request,
    camera_id: Optional[str] = Query(default=None),
    db: Any = Depends(db_session),
):
    from app.services import tripwire as _svc

    data = _normalize(None, request)
    if camera_id is not None:
        data["camera_id"] = camera_id
    if "camera_id" not in data and "camera" not in data:
        return _svc.list_trip_lines()
    cid = data.get("camera_id", data.get("camera", "default"))
    return _svc.get_trip_lines(cid)


async def _define_dep(
    request: Request,
    payload: Any = Body(default=None),
    db: Any = Depends(db_session),
):
    from app.services import tripwire as _svc

    data = _normalize(payload, request)
    return _svc.define_trip_line(data)


async def _evaluate_dep(
    request: Request,
    payload: Any = Body(default=None),
    db: Any = Depends(db_session),
):
    from app.services import tripwire as _svc

    data = _normalize(payload, request)
    camera_id = data.get("camera_id", data.get("camera", "default"))
    points = None
    for key in ("points", "track", "positions", "path", "trajectory", "centroids"):
        if data.get(key) is not None:
            points = data.get(key)
            break
    # Single-step form: {"prev": ..., "curr": ..., "line_id": ...}
    if points is None and (data.get("prev") is not None or data.get("curr") is not None):
        line_id = data.get("line_id", data.get("id", data.get("line")))
        lines = _svc.get_trip_lines(camera_id)
        if line_id is not None:
            lines = [ln for ln in lines if ln.get("id") == str(line_id)]
        if not lines:
            return {"events": [], "crossed": False, "count": 0, "camera_id": str(camera_id)}
        res = _svc.evaluate_crossing(lines[0], data.get("prev"), data.get("curr"), camera_id)
        event = {**res, "events": [res] if res.get("crossed") else []}
        event["count"] = 1 if res.get("crossed") else 0
        return event
    return _svc.evaluate_track(camera_id, points, **{k: v for k, v in data.items() if k not in ("camera_id", "camera", "points", "track", "positions", "path", "trajectory", "centroids")})


async def _remove_dep(
    request: Request,
    payload: Any = Body(default=None),
    db: Any = Depends(db_session),
):
    from app.services import tripwire as _svc

    data = _normalize(payload, request)
    camera_id = data.get("camera_id", data.get("camera", "default"))
    line_id = data.get("line_id", data.get("id", data.get("line")))
    if line_id is None:
        # No line specified -> clear camera lines.
        return _svc.clear_trip_lines(camera_id)
    return _svc.remove_trip_line(camera_id, line_id)


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


for _prefix in _PREFIXES:
    _add(_prefix, _list_dep, methods=("GET",))
    _add(_prefix + "/", _list_dep, methods=("GET",))
    _add(_prefix + "/lines", _list_dep, methods=("GET",))
    _add(_prefix + "/list", _list_dep, methods=("GET",))
    _add(_prefix, _define_dep, methods=("POST",))
    _add(_prefix + "/", _define_dep, methods=("POST",))
    _add(_prefix + "/lines", _define_dep, methods=("POST",))
    _add(_prefix + "/define", _define_dep, methods=("POST",))
    _add(_prefix + "/evaluate", _evaluate_dep, methods=("GET", "POST"))
    _add(_prefix + "/crossings", _evaluate_dep, methods=("GET", "POST"))
    _add(_prefix + "/check", _evaluate_dep, methods=("GET", "POST"))
    _add(_prefix + "/remove", _remove_dep, methods=("POST",))
    _add(_prefix + "/clear", _remove_dep, methods=("POST",))


# Compatibility aliases.
tripwire_router = router
router_tripwire = router
tripwires_router = router
tripwire_api_router = router

__all__ = ["router"]
