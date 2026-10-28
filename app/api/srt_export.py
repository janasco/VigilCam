"""SRT subtitle export router (event sidecar for video exports).

Mounted without a prefix so it serves ``/api/export/srt*``,
``/api/events/*.srt`` and ``/api/srt-export*`` paths.  The main app
auto-registers any ``app.api`` module exposing ``router``, so no edit to
``app/main.py`` is required and no template change is needed.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.api.deps import db_session
from app.services.srt_export import (
    build_srt,
    export_srt,
    fetch_events_for_srt,
    srt_filename,
)

router = APIRouter(tags=["srt-export"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
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


def _pick(data: dict, *keys: str) -> Any:
    for k in keys:
        if data.get(k) is not None:
            return data.get(k)
    return None


async def _build_from_data(data: dict, db: Any) -> str:
    events = _pick(data, "events", "items", "rows", "event_list")
    camera_id = _pick(data, "camera_id", "camera_ids", "camera", "cameras", "cam", "cameraId", "cam_id")
    camera_name = _pick(data, "camera_name", "cameraName")
    start = _pick(data, "start", "start_time", "startTime", "from_", "from", "since", "after", "range_start", "begin")
    end = _pick(data, "end", "end_time", "endTime", "to", "until", "before", "range_end", "finish")
    label = _pick(data, "label", "labels", "kind", "kinds", "type", "object")
    duration = _pick(data, "duration", "duration_seconds", "cue_duration", "default_duration")
    limit = _pick(data, "limit", "n", "count", "size", "max", "max_cues")
    anchor = _pick(data, "anchor", "base", "base_time")
    session = db if hasattr(db, "execute") else None
    if isinstance(events, dict):
        events = [events]
    if isinstance(events, (list, tuple)) and len(list(events)) > 0:
        # In-memory path (works without DB).
        extra = {}
        if camera_name is not None:
            extra["camera_name"] = camera_name
        if anchor is not None:
            extra["anchor"] = anchor
        return build_srt(
            list(events),
            start=start,
            end=end,
            camera_id=camera_id,
            label=label,
            duration=duration,
            limit=limit,
            **extra,
        )
    return await export_srt(
        session,
        start=start,
        end=end,
        camera_id=camera_id,
        camera_name=camera_name,
        label=label,
        duration=duration,
        limit=limit,
        anchor=anchor,
    )


def _srt_response(text: str, filename: str) -> Response:
    return Response(
        content=text,
        media_type="application/x-subrip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _srt_handler(
    request: Request,
    payload: Any = Body(default=None),
    db: Any = Depends(db_session),
    camera_id: Optional[str] = Query(default=None),
    camera_name: Optional[str] = Query(default=None),
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    label: Optional[str] = Query(default=None),
    duration: Optional[float] = Query(default=None),
    limit: Optional[int] = Query(default=None),
):
    data = _normalize(payload, request)
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if camera_name is not None and data.get("camera_name") is None:
        data["camera_name"] = camera_name
    if start is not None and data.get("start") is None:
        data["start"] = start
    if end is not None and data.get("end") is None:
        data["end"] = end
    if label is not None and data.get("label") is None:
        data["label"] = label
    if duration is not None and data.get("duration") is None:
        data["duration"] = duration
    if limit is not None and data.get("limit") is None:
        data["limit"] = limit
    # Path hint for filename (e.g. /api/events/export.srt).
    try:
        data.setdefault("_path_hint", str(request.url.path))
    except Exception:  # noqa: BLE001
        pass
    text = await _build_from_data(data, db)
    filename = srt_filename(
        camera_name=data.get("camera_name") or data.get("camera_id"),
        start=data.get("start"),
        end=data.get("end"),
    )
    # Honour ?format=json debug escape hatch without breaking default SRT.
    try:
        fmt = str(data.get("format", data.get("fmt", ""))).lower()
    except Exception:  # noqa: BLE001
        fmt = ""
    if fmt == "json":
        cues = text.strip().split("\n\n") if text.strip() else []
        return JSONResponse({"srt": text, "cues": len(cues), "filename": filename})
    return _srt_response(text, filename)


async def _srt_preview_handler(
    request: Request,
    payload: Any = Body(default=None),
    db: Any = Depends(db_session),
):
    data = _normalize(payload, request)
    text = await _build_from_data(data, db)
    stripped = text.strip()
    cues = stripped.split("\n\n") if stripped else []
    return JSONResponse({"cues": len(cues), "srt": text})


async def _srt_health_handler():
    return {"status": "ok", "format": "srt"}


_SRT_PATHS = [
    "/api/export/srt",
    "/api/export/srt/",
    "/api/exports/srt",
    "/api/exports/srt/",
    "/api/export/events.srt",
    "/api/exports/events.srt",
    "/api/events/srt",
    "/api/events/srt/",
    "/api/events/export.srt",
    "/api/events/export_srt",
    "/api/event/srt",
    "/api/srt/export",
    "/api/srt/exports",
    "/api/srt-export",
    "/api/srt-export/",
    "/api/srt_export",
    "/api/srt_exports",
    "/api/subtitles/export",
    "/api/subtitles/srt",
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


for _p in _SRT_PATHS:
    _add(_p, _srt_handler)

_add("/api/export/srt/preview", _srt_preview_handler)
_add("/api/events/srt/preview", _srt_preview_handler)
_add("/api/srt-export/preview", _srt_preview_handler)
_add("/api/export/srt/health", _srt_health_handler, methods=("GET",))
_add("/api/events/srt/health", _srt_health_handler, methods=("GET",))
_add("/api/srt-export/health", _srt_health_handler, methods=("GET",))


# Compatibility aliases.
srt_export_router = router
api_router = router
srt_api_router = router
subtitle_export_router = router
subtitles_router = router

__all__ = ["router"]
