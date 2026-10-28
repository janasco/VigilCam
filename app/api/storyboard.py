"""Recording storyboard API router.

Serves ``N thumbnails across a time range as a contact sheet`` for a camera:

- ``GET /api/storyboard/preview`` (and ``GET /api/storyboard/{camera_id}``)
  returns the contact sheet image directly (PNG by default, JPEG on request).
- ``POST /api/storyboard`` (plus ``/build`` / ``/create`` / ``/generate`` /
  ``/render`` aliases) validates the time range, renders the sheet, persists
  it under ``data/storyboards/`` and returns JSON metadata with per-tile
  timestamps.

Registered automatically by the ``app.api`` auto-loader in
:mod:`app.main` (no manual wiring needed, do not edit ``app/main.py`` for
this router).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import Response

from app.services import storyboard as storyboard_service

router = APIRouter(prefix="/api/storyboard", tags=["storyboard"])
plural_router = APIRouter(prefix="/api/storyboards", tags=["storyboard"])
builder_router = APIRouter(prefix="/api/storyboard-builder", tags=["storyboard"])
# Alias kept for compatibility with code expecting `underscore_router`.
underscore_router = router


def _normalize(payload: Any, request: Any = None) -> dict:
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


def _pick(data: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if data.get(k) is not None:
            return data.get(k)
    return default


def _image_response(payload: bytes, meta: dict) -> Response:
    content_type = str(meta.get("content_type", "image/png"))
    headers = {
        "X-Storyboard-Camera": str(meta.get("camera", "")),
        "X-Storyboard-Count": str(meta.get("count", "")),
        "X-Storyboard-Columns": str(meta.get("columns", "")),
        "X-Storyboard-Rows": str(meta.get("rows", "")),
        "Cache-Control": "public, max-age=10",
    }
    return Response(content=payload, media_type=content_type, headers=headers)


def _do_image(data: dict) -> Response:
    camera = _pick(
        data, "camera", "camera_id", "cameraId", "cameraID", "cam", "cam_id",
        "camera_name", "cameraName", "device_id", "deviceId",
    )
    start = _pick(
        data, "start", "start_time", "startTime", "from", "from_time",
        "fromTime", "begin", "since", "after", "range_start", "rangeStart",
        "start_ts", "startTs", "t0",
    )
    end = _pick(
        data, "end", "end_time", "endTime", "to", "to_time", "toTime",
        "until", "before", "range_end", "rangeEnd", "finish",
        "end_ts", "endTs", "t1",
    )
    count = _pick(
        data, "count", "n", "num", "num_thumbnails", "numThumbnails",
        "tiles", "frames", "num_frames", "numFrames", "thumbnails",
        "num_tiles", "numTiles", "limit",
    )
    thumb_width = _pick(
        data, "thumb_width", "thumbWidth", "tile_width", "tileWidth",
        "thumbnail_width", "width", "w", "tw",
    )
    thumb_height = _pick(
        data, "thumb_height", "thumbHeight", "tile_height", "tileHeight",
        "thumbnail_height", "height", "h", "th",
    )
    columns = _pick(
        data, "columns", "cols", "per_row", "perRow", "num_columns",
        "numColumns", "grid_columns", "columns_count",
    )
    fmt = _pick(
        data, "format", "fmt", "image_format", "ext", "extension",
        "output_format", "mime",
    )
    try:
        payload, meta = storyboard_service.build_storyboard_with_meta(
            camera, start, end, count,
            thumb_width=thumb_width, thumb_height=thumb_height,
            columns=columns, format=fmt,
            gap=data.get("gap", data.get("spacing")),
            padding=data.get("padding", data.get("pad")),
            title=data.get("title", data.get("heading")),
            quality=data.get("quality", data.get("jpeg_quality")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _image_response(payload, meta)


def _do_build(data: dict) -> dict:
    camera = _pick(
        data, "camera", "camera_id", "cameraId", "cameraID", "cam", "cam_id",
        "camera_name", "cameraName", "device_id", "deviceId",
    )
    start = _pick(
        data, "start", "start_time", "startTime", "from", "from_time",
        "fromTime", "begin", "since", "after", "range_start", "rangeStart",
        "start_ts", "startTs", "t0",
    )
    end = _pick(
        data, "end", "end_time", "endTime", "to", "to_time", "toTime",
        "until", "before", "range_end", "rangeEnd", "finish",
        "end_ts", "endTs", "t1",
    )
    count = _pick(
        data, "count", "n", "num", "num_thumbnails", "numThumbnails",
        "tiles", "frames", "num_frames", "numFrames", "thumbnails",
        "num_tiles", "numTiles", "limit",
    )
    thumb_width = _pick(
        data, "thumb_width", "thumbWidth", "tile_width", "tileWidth",
        "thumbnail_width", "width", "w", "tw",
    )
    thumb_height = _pick(
        data, "thumb_height", "thumbHeight", "tile_height", "tileHeight",
        "thumbnail_height", "height", "h", "th",
    )
    columns = _pick(
        data, "columns", "cols", "per_row", "perRow", "num_columns",
        "numColumns", "grid_columns", "columns_count",
    )
    fmt = _pick(
        data, "format", "fmt", "image_format", "ext", "extension",
        "output_format", "mime",
    )
    output = _pick(
        data, "output", "output_path", "outputPath", "dest", "destination",
        "target", "out", "outfile", "save_to", "save_as", "path", "file",
    )
    try:
        normalized = storyboard_service.validate_storyboard_params(
            camera, start=start, end=end, count=count,
            thumb_width=thumb_width, thumb_height=thumb_height,
            columns=columns, format=fmt,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        out_path = storyboard_service.build_storyboard_file(
            normalized["camera"], normalized["start"], normalized["end"],
            normalized["count"], output=output,
            thumb_width=normalized["thumb_width"],
            thumb_height=normalized["thumb_height"],
            columns=normalized["columns"],
            format=normalized.get("format"),
            gap=data.get("gap", data.get("spacing")),
            padding=data.get("padding", data.get("pad")),
            title=data.get("title", data.get("heading")),
            quality=data.get("quality", data.get("jpeg_quality")),
        )
        # Re-render bytes to report exact size/content-type (cheap, cached logic).
        payload, meta = storyboard_service.build_storyboard_with_meta(
            normalized["camera"], normalized["start"], normalized["end"],
            normalized["count"],
            thumb_width=normalized["thumb_width"],
            thumb_height=normalized["thumb_height"],
            columns=normalized["columns"],
            format=normalized.get("format"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "ok": True,
        "success": True,
        "status": "completed",
        "camera": meta.get("camera"),
        "camera_id": meta.get("camera"),
        "camera_name": meta.get("camera"),
        "start": meta.get("start"),
        "start_time": meta.get("start"),
        "start_iso": meta.get("start_iso"),
        "end": meta.get("end"),
        "end_time": meta.get("end"),
        "end_iso": meta.get("end_iso"),
        "count": meta.get("count"),
        "n": meta.get("count"),
        "num_thumbnails": meta.get("count"),
        "tiles": meta.get("count"),
        "frames": meta.get("count"),
        "timestamps": meta.get("timestamps"),
        "timestamp_isos": meta.get("timestamp_isos"),
        "frame_timestamps": meta.get("timestamps"),
        "thumb_width": meta.get("thumb_width"),
        "thumb_height": meta.get("thumb_height"),
        "columns": meta.get("columns"),
        "cols": meta.get("columns"),
        "rows": meta.get("rows"),
        "width": meta.get("width"),
        "height": meta.get("height"),
        "format": meta.get("format"),
        "content_type": meta.get("content_type"),
        "size": meta.get("size"),
        "size_bytes": meta.get("size_bytes"),
        "output_path": str(out_path),
        "output": str(out_path),
        "path": str(out_path),
    }


def _register(r: APIRouter) -> None:
    @r.get("")
    @r.get("/")
    async def storyboard_info():
        return {
            "service": "storyboard",
            "status": "ok",
            "ok": True,
            "success": True,
            "usage": "GET /preview?camera=&start=&end=&count= or POST with {camera, start, end, count}",
            "endpoints": [
                "GET /preview",
                "GET /{camera_id}",
                "POST /build",
                "POST /",
                "GET /status",
            ],
        }

    @r.get("/status")
    async def storyboard_status():
        return {"status": "ok", "service": "storyboard", "ok": True, "success": True}

    @r.get("/health")
    async def storyboard_health():
        return {"status": "ok", "service": "storyboard", "ok": True, "success": True}

    @r.get("/info")
    async def storyboard_info_alias():
        return await storyboard_info()

    @r.get("/preview")
    @r.get("/preview.png")
    async def storyboard_preview(
        request: Request,
        payload: Any = Body(default=None),
        camera: Any = Query(default=None),
        camera_id: Any = Query(default=None),
        start: Any = Query(default=None),
        start_time: Any = Query(default=None),
        end: Any = Query(default=None),
        end_time: Any = Query(default=None),
        count: Any = Query(default=None),
        n: Any = Query(default=None),
        thumb_width: Any = Query(default=None),
        thumb_height: Any = Query(default=None),
        width: Any = Query(default=None),
        height: Any = Query(default=None),
        columns: Any = Query(default=None),
        cols: Any = Query(default=None),
        format: Optional[str] = Query(default=None),  # noqa: A002
    ):
        data = _normalize(payload, request)
        for k, v in (
            ("camera", camera), ("camera_id", camera_id),
            ("start", start), ("start_time", start_time),
            ("end", end), ("end_time", end_time),
            ("count", count), ("n", n),
            ("thumb_width", thumb_width), ("thumb_height", thumb_height),
            ("width", width), ("height", height),
            ("columns", columns), ("cols", cols),
            ("format", format),
        ):
            if v is not None and data.get(k) is None:
                data[k] = v
        return _do_image(data)

    @r.post("")
    @r.post("/")
    @r.post("/build")
    @r.post("/create")
    @r.post("/generate")
    @r.post("/render")
    async def storyboard_build_post(
        request: Request,
        payload: Any = Body(default=None),
        camera: Any = Query(default=None),
        camera_id: Any = Query(default=None),
        start: Any = Query(default=None),
        start_time: Any = Query(default=None),
        end: Any = Query(default=None),
        end_time: Any = Query(default=None),
        count: Any = Query(default=None),
        n: Any = Query(default=None),
        thumb_width: Any = Query(default=None),
        thumb_height: Any = Query(default=None),
        width: Any = Query(default=None),
        height: Any = Query(default=None),
        columns: Any = Query(default=None),
        cols: Any = Query(default=None),
        format: Optional[str] = Query(default=None),  # noqa: A002
        output: Any = Query(default=None),
        output_path: Any = Query(default=None),
    ):
        data = _normalize(payload, request)
        for k, v in (
            ("camera", camera), ("camera_id", camera_id),
            ("start", start), ("start_time", start_time),
            ("end", end), ("end_time", end_time),
            ("count", count), ("n", n),
            ("thumb_width", thumb_width), ("thumb_height", thumb_height),
            ("width", width), ("height", height),
            ("columns", columns), ("cols", cols),
            ("format", format), ("output", output),
            ("output_path", output_path),
        ):
            if v is not None and data.get(k) is None:
                data[k] = v
        return _do_build(data)

    @r.get("/{camera_id}.png")
    @r.get("/{camera_id}.jpg")
    @r.get("/{camera_id}.jpeg")
    async def storyboard_camera_image_alias(
        camera_id: Any,
        request: Request,
        start: Any = Query(default=None),
        start_time: Any = Query(default=None),
        end: Any = Query(default=None),
        end_time: Any = Query(default=None),
        count: Any = Query(default=None),
        n: Any = Query(default=None),
        thumb_width: Any = Query(default=None),
        thumb_height: Any = Query(default=None),
        width: Any = Query(default=None),
        height: Any = Query(default=None),
        columns: Any = Query(default=None),
        cols: Any = Query(default=None),
        format: Optional[str] = Query(default=None),  # noqa: A002
    ):
        data = _normalize(None, request)
        data["camera"] = camera_id
        for k, v in (
            ("start", start), ("start_time", start_time),
            ("end", end), ("end_time", end_time),
            ("count", count), ("n", n),
            ("thumb_width", thumb_width), ("thumb_height", thumb_height),
            ("width", width), ("height", height),
            ("columns", columns), ("cols", cols),
            ("format", format),
        ):
            if v is not None and data.get(k) is None:
                data[k] = v
        return _do_image(data)

    @r.get("/{camera_id}")
    async def storyboard_camera_image(
        camera_id: Any,
        request: Request,
        start: Any = Query(default=None),
        start_time: Any = Query(default=None),
        end: Any = Query(default=None),
        end_time: Any = Query(default=None),
        count: Any = Query(default=None),
        n: Any = Query(default=None),
        thumb_width: Any = Query(default=None),
        thumb_height: Any = Query(default=None),
        width: Any = Query(default=None),
        height: Any = Query(default=None),
        columns: Any = Query(default=None),
        cols: Any = Query(default=None),
        format: Optional[str] = Query(default=None),  # noqa: A002
    ):
        data = _normalize(None, request)
        data["camera"] = camera_id
        for k, v in (
            ("start", start), ("start_time", start_time),
            ("end", end), ("end_time", end_time),
            ("count", count), ("n", n),
            ("thumb_width", thumb_width), ("thumb_height", thumb_height),
            ("width", width), ("height", height),
            ("columns", columns), ("cols", cols),
            ("format", format),
        ):
            if v is not None and data.get(k) is None:
                data[k] = v
        return _do_image(data)


_register(router)
_register(plural_router)
_register(builder_router)


# Self-register so no edit to app/main.py is required. The auto-loader in
# main.py also picks up `router`, so this is belt-and-braces.
try:
    from app.main import app as _main_app

    for _candidate in (router, plural_router, builder_router):
        try:
            _ids = getattr(_main_app, "_registered_router_ids", set())
            if id(_candidate) not in _ids:
                _main_app.include_router(_candidate)
                _main_app._registered_router_ids = _ids | {id(_candidate)}
        except Exception:  # noqa: BLE001
            pass
except Exception:  # noqa: BLE001
    pass
