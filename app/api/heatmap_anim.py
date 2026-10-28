"""Animated heatmap GIF API router.

Serves ``image/gif`` animated renders of the event heatmap aggregation
(see :mod:`app.services.heatmap`). Registered automatically by the
``app.api`` auto-loader in :mod:`app.main` (no manual wiring needed,
do not edit ``app/main.py`` for this router).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import Response

from app.api.deps import db_session

router = APIRouter(tags=["heatmap-anim"])


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
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "filter", "filters"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _has_heatmap_payload(data: dict) -> bool:
    for key in ("per_camera_per_hour", "camera_hour_matrix", "matrix", "heatmap", "per_hour", "hourly", "cells"):
        if isinstance(data.get(key), (dict, list)):
            return True
    return False


async def _do_gif(data: dict, db: Any = None) -> Response:
    from app.services import heatmap as _svc
    from app.services import heatmap_anim as _anim

    heatmap_data: dict
    if _has_heatmap_payload(data):
        heatmap_data = dict(data)
    else:
        kwargs = dict(data)
        if db is not None:
            kwargs.setdefault("db", db)
        result = await _svc.get_heatmap(**kwargs)
        heatmap_data = result if isinstance(result, dict) else {}

    gif = _anim.render_heatmap_gif(
        heatmap_data,
        frames=data.get("frames", data.get("n_frames", data.get("frame_count"))),
        fps=data.get("fps", data.get("frame_rate")),
        cell_width=data.get("cell_width", data.get("cell_w", data.get("cw"))),
        cell_height=data.get("cell_height", data.get("cell_h", data.get("ch"))),
        width=data.get("width", data.get("w")),
        height=data.get("height", data.get("h")),
        title=data.get("title", data.get("heading")),
    )
    try:
        total = int(heatmap_data.get("total", heatmap_data.get("count", 0)))
    except (TypeError, ValueError):
        total = 0
    headers = {
        "X-Heatmap-Total": str(total),
        "Cache-Control": "public, max-age=10",
    }
    return Response(content=gif, media_type="image/gif", headers=headers)


async def _heatmap_anim_dep(
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
    width: Optional[int] = Query(default=None),
    height: Optional[int] = Query(default=None),
    cell_width: Optional[int] = Query(default=None),
    cell_height: Optional[int] = Query(default=None),
    frames: Optional[int] = Query(default=None),
    fps: Optional[float] = Query(default=None),
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
    if width is not None and data.get("width") is None:
        data["width"] = width
    if height is not None and data.get("height") is None:
        data["height"] = height
    if cell_width is not None and data.get("cell_width") is None:
        data["cell_width"] = cell_width
    if cell_height is not None and data.get("cell_height") is None:
        data["cell_height"] = cell_height
    if frames is not None and data.get("frames") is None:
        data["frames"] = frames
    if fps is not None and data.get("fps") is None:
        data["fps"] = fps
    session = db if hasattr(db, "execute") else None
    return await _do_gif(data, session)


_paths = [
    "/api/heatmap/anim",
    "/api/heatmap/anim.gif",
    "/api/heatmap-anim",
    "/api/heatmap-anim.gif",
    "/api/heatmaps/anim",
    "/api/events/heatmap/anim",
    "/api/analytics/heatmap/anim",
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


for _p in _paths:
    _add(_p, _heatmap_anim_dep)


# Compatibility aliases.
heatmap_anim_router = router
router_heatmap_anim = router
heatmap_gif_router = router
heatmap_animation_router = router
heatmap_anim_api_router = router

__all__ = ["router"]
