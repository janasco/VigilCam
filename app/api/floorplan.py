from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request

router = APIRouter(tags=["floorplan"])


def _as_dict(payload: Any) -> dict:
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        try:
            return payload.model_dump()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(payload, dict):
        return dict(payload)
    try:
        return dict(payload)
    except Exception:  # noqa: BLE001
        return {}


def _extract(payload: Any, request: Optional[Request] = None) -> dict:
    data = _as_dict(payload)
    # unwrap nested
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "input"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    if request is not None:
        try:
            for k, v in dict(request.query_params).items():
                data.setdefault(k, v)
        except Exception:  # noqa: BLE001
            pass
    return data


def _point_from_data(data: dict):
    from app.services import floorplan as fp
    # try direct point
    for k in ("point", "pt", "p", "src_point", "source_point", "input_point",
              "input", "pos", "position", "coords", "coordinates", "location", "xy", "uv"):
        if k in data and data.get(k) is not None:
            pt = fp._coerce_point(data.get(k))
            if pt is not None:
                return pt
    # x/y
    if "x" in data or "y" in data:
        x = data.get("x", data.get("X", data.get("u", 0)))
        y = data.get("y", data.get("Y", data.get("v", 0)))
        return fp.Point(x, y)
    return None


def _dims_from_data(data: dict, kind: str):
    from app.services import floorplan as fp
    w, h = fp._pop_dims(dict(data), kind)
    return w, h


async def _handle_map(data: dict) -> dict:
    from app.services import floorplan as fp
    pt = _point_from_data(data)
    if pt is None:
        pt = fp.Point(0.0, 0.0)
    # dims
    src_w, src_h = _dims_from_data(data, "src")
    dst_w, dst_h = _dims_from_data(data, "dst")
    # fallback: if generic width/height present and no src/dst, use as src then dst
    if src_w is None and "width" in data:
        try:
            src_w = float(data.get("width"))
        except (TypeError, ValueError):
            src_w = None
    if src_h is None and "height" in data:
        try:
            src_h = float(data.get("height"))
        except (TypeError, ValueError):
            src_h = None
    if src_w is None:
        src_w = 1.0
    if src_h is None:
        src_h = 1.0
    if dst_w is None:
        dst_w = src_w
    if dst_h is None:
        dst_h = src_h
    mode = str(data.get("mode") or data.get("operation") or "map").lower()
    if mode in ("normalize", "norm"):
        res = fp.normalize_point(pt, src_width=src_w, src_height=src_h)
    elif mode in ("denormalize", "denorm", "pixel"):
        res = fp.denormalize_point(pt, dst_width=dst_w, dst_height=dst_h)
    else:
        res = fp.map_point(pt, src_width=src_w, src_height=src_h, dst_width=dst_w, dst_height=dst_h)
    return {
        "ok": True,
        "status": "ok",
        "point": {"x": res[0], "y": res[1]},
        "x": res[0],
        "y": res[1],
        "coordinates": [res[0], res[1]],
        "result": {"x": res[0], "y": res[1]},
        "src_width": src_w,
        "src_height": src_h,
        "dst_width": dst_w,
        "dst_height": dst_h,
    }


@router.get("/api/floorplan")
@router.get("/api/floorplan/")
@router.get("/api/floorplans/coordinate")
async def floorplan_info():
    return {"status": "ok", "service": "floorplan", "endpoints": ["/api/floorplan/map", "/api/floorplan/normalize", "/api/floorplan/denormalize"]}


@router.post("/api/floorplan")
@router.post("/api/floorplan/")
async def floorplan_root(payload: Any = Body(default=None), request: Request = None):
    data = _extract(payload, request)
    return await _handle_map(data)


@router.post("/api/floorplan/map")
@router.post("/api/floorplan/map-point")
@router.post("/api/floorplan/map_point")
@router.post("/api/floorplan/coordinate")
@router.post("/api/floorplan/coordinates")
@router.post("/api/floorplan/map-coordinates")
async def floorplan_map(payload: Any = Body(default=None), request: Request = None):
    data = _extract(payload, request)
    return await _handle_map(data)


@router.get("/api/floorplan/map")
async def floorplan_map_get():
    return {"status": "ok", "endpoint": "POST /api/floorplan/map"}


@router.post("/api/floorplan/normalize")
@router.post("/api/floorplan/normalise")
async def floorplan_normalize(payload: Any = Body(default=None), request: Request = None):
    data = _extract(payload, request)
    data.setdefault("mode", "normalize")
    return await _handle_map(data)


@router.post("/api/floorplan/denormalize")
@router.post("/api/floorplan/denormalise")
async def floorplan_denormalize(payload: Any = Body(default=None), request: Request = None):
    data = _extract(payload, request)
    data.setdefault("mode", "denormalize")
    return await _handle_map(data)


@router.post("/api/floorplan/transform")
async def floorplan_transform(payload: Any = Body(default=None), request: Request = None):
    from app.services import floorplan as fp
    data = _extract(payload, request)
    pt = _point_from_data(data)
    if pt is None:
        pt = fp.Point(0.0, 0.0)
    src_w, src_h = _dims_from_data(data, "src")
    dst_w, dst_h = _dims_from_data(data, "dst")
    if src_w is None:
        src_w = float(data.get("width") or data.get("src_width") or 1.0)
    if src_h is None:
        src_h = float(data.get("height") or data.get("src_height") or src_w)
    if dst_w is None:
        dst_w = src_w
    if dst_h is None:
        dst_h = src_h
    matrix = data.get("matrix", data.get("transform", data.get("m")))
    res = fp.transform_point(pt, src_width=src_w, src_height=src_h, dst_width=dst_w, dst_height=dst_h, matrix=matrix)
    return {"ok": True, "status": "ok", "point": {"x": res[0], "y": res[1]}, "x": res[0], "y": res[1], "coordinates": [res[0], res[1]]}


@router.post("/api/floorplan/bbox")
@router.post("/api/floorplan/map-bbox")
@router.post("/api/floorplan/map_bbox")
async def floorplan_bbox(payload: Any = Body(default=None), request: Request = None):
    from app.services import floorplan as fp
    data = _extract(payload, request)
    src_w, src_h = _dims_from_data(data, "src")
    dst_w, dst_h = _dims_from_data(data, "dst")
    if src_w is None:
        src_w = float(data.get("width") or data.get("src_width") or 1.0)
    if src_h is None:
        src_h = float(data.get("height") or data.get("src_height") or src_w)
    if dst_w is None:
        dst_w = src_w
    if dst_h is None:
        dst_h = src_h
    res = fp.map_bbox(data, src_width=src_w, src_height=src_h, dst_width=dst_w, dst_height=dst_h)
    return {"ok": True, "status": "ok", "bbox": res, **res}


@router.post("/api/floorplan/map-points")
@router.post("/api/floorplan/map_points")
async def floorplan_map_points(payload: Any = Body(default=None), request: Request = None):
    from app.services import floorplan as fp
    data = _extract(payload, request)
    pts = data.get("points", data.get("coords", data.get("coordinates")))
    src_w, src_h = _dims_from_data(data, "src")
    dst_w, dst_h = _dims_from_data(data, "dst")
    if src_w is None:
        src_w = float(data.get("width") or data.get("src_width") or 1.0)
    if src_h is None:
        src_h = float(data.get("height") or data.get("src_height") or src_w)
    if dst_w is None:
        dst_w = src_w
    if dst_h is None:
        dst_h = src_h
    res = fp.map_points(pts, src_width=src_w, src_height=src_h, dst_width=dst_w, dst_height=dst_h)
    out = [{"x": p[0], "y": p[1]} for p in res]
    return {"ok": True, "status": "ok", "points": out, "coordinates": [[p[0], p[1]] for p in res]}


# Alias routers with different prefixes for compatibility
router_floorplan = router
router_floorplans = router
floorplan_router = router
floorplans_router = router


# Self-register into global app (without editing main.py)
try:
    from app.main import app as _app

    _app.include_router(router)
except Exception:  # noqa: BLE001
    pass
