"""Line-crossing object counter API router (per direction / camera / line)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["object_counter"])


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


def _get_counter():
    from app.services import object_counter as _svc
    return _svc.default_counter


def _filter_kwargs(data: dict) -> dict:
    out: dict = {}
    if data.get("camera_id") is not None:
        out["camera_id"] = data.get("camera_id")
    elif data.get("camera") is not None:
        out["camera_id"] = data.get("camera")
    if data.get("line_id") is not None:
        out["line_id"] = data.get("line_id")
    elif data.get("line") is not None and not isinstance(data.get("line"), (list, tuple, dict)):
        out["line_id"] = data.get("line")
    return out


async def _counts_dep(
    request: Request,
    payload: Any = Body(default=None),
    camera_id: Optional[str] = Query(default=None),
    line_id: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if camera_id is not None and data.get("camera_id") is None and data.get("camera") is None:
        data["camera_id"] = camera_id
    if line_id is not None and data.get("line_id") is None:
        data["line_id"] = line_id
    svc = _get_counter()
    return svc.get_counts(**_filter_kwargs(data))


async def _counts_camera_dep(
    camera_id: Any,
    request: Request,
    payload: Any = Body(default=None),
    line_id: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    try:
        data["camera_id"] = int(camera_id)
    except (TypeError, ValueError):
        data["camera_id"] = str(camera_id)
    if line_id is not None and data.get("line_id") is None:
        data["line_id"] = line_id
    svc = _get_counter()
    return svc.get_counts(**_filter_kwargs(data))


async def _lines_get_dep(request: Request, camera_id: Optional[str] = Query(default=None)):
    data = _normalize(None, request)
    if camera_id is not None:
        data["camera_id"] = camera_id
    svc = _get_counter()
    cam = data.get("camera_id", data.get("camera"))
    lines = svc.get_lines(camera_id=cam)
    return {"status": "ok", "ok": True, "lines": lines, "count": len(lines), "total": len(lines)}


async def _lines_post_dep(request: Request, payload: Any = Body(default=None)):
    data = _normalize(payload, request)
    svc = _get_counter()
    # batch?
    batch = None
    for k in ("lines", "counting_lines", "items"):
        if isinstance(data.get(k), (list, tuple)):
            batch = data.get(k)
            break
    if batch is not None:
        created = svc.define_lines(batch)
        return {"status": "ok", "ok": True,
                "lines": [c.to_dict() for c in created],
                "count": len(created), "total": len(created)}
    line = svc.define_line(data)
    return {"status": "ok", "ok": True, "line": line.to_dict(), "lines": [line.to_dict()]}


async def _update_dep(request: Request, payload: Any = Body(default=None)):
    data = _normalize(payload, request)
    svc = _get_counter()
    # batch of detections?
    batch = None
    for k in ("detections", "tracks", "observations", "positions", "points", "items", "events"):
        if isinstance(data.get(k), (list, tuple)):
            batch = data.get(k)
            break
    if isinstance(payload, list):
        batch = payload
    if batch is not None:
        defaults = {k: v for k, v in data.items() if k in ("camera_id", "camera", "line_id", "line")}
        events = svc.process_detections(batch, **defaults)
        counts = svc.get_counts()
        return {"status": "ok", "ok": True, "events": events,
                "event": events[-1] if events else None,
                "crossing": events[-1] if events else None,
                "count": len(events), "total": len(events), "counts": counts}
    event = svc.update(data)
    counts = svc.get_counts()
    # scoped counts for the event camera when available
    scoped = None
    try:
        cam = data.get("camera_id", data.get("camera"))
        if cam is not None:
            scoped = svc.get_counts(camera_id=cam)
    except Exception:
        scoped = None
    return {"status": "ok", "ok": True, "event": event, "crossing": event,
            "crossed": event is not None, "events": [event] if event else [],
            "counts": scoped if scoped is not None else counts,
            "total_counts": counts}


async def _update_camera_dep(camera_id: Any, request: Request, payload: Any = Body(default=None)):
    data = _normalize(payload, request)
    try:
        data.setdefault("camera_id", int(camera_id))
    except (TypeError, ValueError):
        data.setdefault("camera_id", str(camera_id))
    return await _update_dep(request, data)


async def _reset_dep(request: Request, payload: Any = Body(default=None),
                     camera_id: Optional[str] = Query(default=None),
                     line_id: Optional[str] = Query(default=None)):
    data = _normalize(payload, request)
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if line_id is not None and data.get("line_id") is None:
        data["line_id"] = line_id
    svc = _get_counter()
    return svc.reset(camera_id=data.get("camera_id", data.get("camera")),
                     line_id=data.get("line_id", data.get("line")))


_prefixes = [
    "/api/object_counter",
    "/api/object-counter",
    "/api/objectcounter",
    "/api/object_counting",
    "/api/object-counting",
    "/api/counters/objects",
    "/api/analytics/object_counter",
]

_counts_suffixes = ["", "/", "/counts", "/summary", "/totals", "/stats", "/count"]
_lines_suffixes = ["/lines", "/line"]
_update_suffixes = ["/update", "/track", "/observe", "/feed", "/process", "/ingest", "/position", "/event"]
_reset_suffixes = ["/reset", "/clear"]

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
    for _suf in _counts_suffixes:
        _p = _prefix if _suf in ("", "/") else f"{_prefix}{_suf}"
        _add(_p, _counts_dep)
    _add(f"{_prefix}/lines", _lines_get_dep, methods=("GET",))
    for _suf in _lines_suffixes:
        _add(f"{_prefix}{_suf}", _lines_post_dep, methods=("POST", "PUT"))
    for _suf in _update_suffixes:
        _add(f"{_prefix}{_suf}", _update_dep, methods=("POST", "PUT"))
    for _suf in _reset_suffixes:
        _add(f"{_prefix}{_suf}", _reset_dep, methods=("POST", "DELETE"))
    for _cam_base in (f"{_prefix}/cameras/{{camera_id}}", f"{_prefix}/camera/{{camera_id}}"):
        _add(_cam_base, _counts_camera_dep)
        _add(f"{_cam_base}/counts", _counts_camera_dep)
        _add(f"{_cam_base}/summary", _counts_camera_dep)
        _add(f"{_cam_base}/update", _update_camera_dep, methods=("POST", "PUT"))
        _add(f"{_cam_base}/track", _update_camera_dep, methods=("POST", "PUT"))


# Compatibility aliases.
object_counter_router = router
router_object_counter = router
object_counting_router = router
line_counter_router = router
object_counter_api_router = router

__all__ = ["router"]
