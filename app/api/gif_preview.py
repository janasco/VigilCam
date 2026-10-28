"""GIF preview export API router.

Dedicated GIF preview endpoints (``image/gif``) separate from the regular
video export pipeline (``POST /api/export`` → mp4). Auto-registered by
``app.main`` (it imports every ``app.api`` module and includes ``router``),
so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request, HTTPException
from fastapi.responses import JSONResponse, Response

router = APIRouter(tags=["gif-preview"])


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
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    # Camera aliases.
    if data.get("camera") is None:
        for k in ("camera_id", "cameraId", "cameraID", "cam", "cam_id",
                  "camera_name", "cameraName", "device_id", "deviceId"):
            if data.get(k) is not None:
                data["camera"] = data.get(k)
                break
    if data.get("width") is None:
        for k in ("w", "width_px", "scale_width", "video_width", "cols"):
            if data.get(k) is not None:
                data["width"] = data.get(k)
                break
    if data.get("height") is None:
        for k in ("h", "height_px", "scale_height", "video_height", "rows"):
            if data.get(k) is not None:
                data["height"] = data.get(k)
                break
    if data.get("fps") is None:
        for k in ("frame_rate", "frameRate", "framerate", "output_fps", "out_fps", "rate"):
            if data.get(k) is not None:
                data["fps"] = data.get(k)
                break
    if data.get("duration") is None:
        for k in ("duration_seconds", "durationSeconds", "length", "length_seconds",
                  "seconds", "secs", "sec", "clip_length"):
            if data.get(k) is not None:
                data["duration"] = data.get(k)
                break
    if data.get("frames") is None:
        for k in ("frame_count", "frameCount", "num_frames", "n_frames", "count"):
            if data.get(k) is not None:
                data["frames"] = data.get(k)
                break
    return data


def _pick(data: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if data.get(k) is not None:
            return data.get(k)
    return default


def _clean_q(value: Any) -> Any:
    """Return None for unresolved FastAPI Query() defaults (internal calls)."""
    if value is None:
        return None
    try:
        from fastapi.params import Param as _Param  # type: ignore

        if isinstance(value, _Param):
            return None
    except Exception:  # noqa: BLE001
        pass
    if value.__class__.__name__ in ("Query", "Param"):
        return None
    return value


async def _preview_bytes(
    camera_id: Any = None,
    width: Any = Query(default=None),
    height: Any = Query(default=None),
    fps: Any = Query(default=None),
    duration: Any = Query(default=None),
    frames: Any = Query(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    from app.services import gif_preview as svc

    width = _clean_q(width)
    height = _clean_q(height)
    fps = _clean_q(fps)
    duration = _clean_q(duration)
    frames = _clean_q(frames)
    data = _normalize(None, request)
    cam = camera_id if camera_id is not None else _pick(data, "camera", "camera_id", "cam")
    if cam is None:
        cam = "preview"
    w = width if width is not None else data.get("width")
    h = height if height is not None else data.get("height")
    f = fps if fps is not None else data.get("fps")
    d = duration if duration is not None else data.get("duration")
    n = frames if frames is not None else data.get("frames")
    try:
        payload = svc.generate_gif_bytes(cam, width=w, height=h, fps=f, duration=d, frames=n)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(content=payload, media_type=svc.CONTENT_TYPE_GIF)


async def _preview_info(
    camera_id: Any = None,
    width: Any = Query(default=None),
    height: Any = Query(default=None),
    fps: Any = Query(default=None),
    duration: Any = Query(default=None),
    frames: Any = Query(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    from app.services import gif_preview as svc

    width = _clean_q(width)
    height = _clean_q(height)
    fps = _clean_q(fps)
    duration = _clean_q(duration)
    frames = _clean_q(frames)
    data = _normalize(None, request)
    cam = camera_id if camera_id is not None else _pick(data, "camera", "camera_id", "cam")
    w = width if width is not None else data.get("width")
    h = height if height is not None else data.get("height")
    f = fps if fps is not None else data.get("fps")
    d = duration if duration is not None else data.get("duration")
    n = frames if frames is not None else data.get("frames")
    try:
        normalized = svc.validate_gif_params(cam, width=w, height=h, fps=f, duration=d, frames=n)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    body = dict(normalized)
    body.update({
        "ok": True,
        "success": True,
        "status": "ok",
        "format": "gif",
        "output_format": "gif",
        "content_type": svc.CONTENT_TYPE_GIF,
        "media_type": svc.CONTENT_TYPE_GIF,
    })
    if body.get("output") is not None:
        body["output"] = str(body["output"])
    if body.get("output_path") is not None:
        body["output_path"] = str(body["output_path"])
    return JSONResponse(content=body)


async def _build_preview(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    camera: Any = Query(default=None),
    camera_id: Any = Query(default=None),
    width: Any = Query(default=None),
    height: Any = Query(default=None),
    fps: Any = Query(default=None),
    duration: Any = Query(default=None),
    frames: Any = Query(default=None),
    output: Any = Query(default=None),
):
    from app.services import gif_preview as svc

    camera = _clean_q(camera)
    camera_id = _clean_q(camera_id)
    width = _clean_q(width)
    height = _clean_q(height)
    fps = _clean_q(fps)
    duration = _clean_q(duration)
    frames = _clean_q(frames)
    output = _clean_q(output)
    data = _normalize(payload, request)
    if camera is not None and data.get("camera") is None:
        data["camera"] = camera
    if camera_id is not None and data.get("camera") is None:
        data["camera"] = camera_id
    if width is not None and data.get("width") is None:
        data["width"] = width
    if height is not None and data.get("height") is None:
        data["height"] = height
    if fps is not None and data.get("fps") is None:
        data["fps"] = fps
    if duration is not None and data.get("duration") is None:
        data["duration"] = duration
    if frames is not None and data.get("frames") is None:
        data["frames"] = frames
    if output is not None and data.get("output") is None:
        data["output"] = output
    cam = _pick(data, "camera", "camera_id", "cam", "camera_name")
    try:
        normalized = svc.validate_gif_params(
            cam, output=_pick(data, "output", "output_path", "dest", "target", "out"),
            width=_pick(data, "width", "w"), height=_pick(data, "height", "h"),
            fps=_pick(data, "fps", "frame_rate", "framerate"),
            duration=_pick(data, "duration", "duration_seconds", "length", "seconds"),
            frames=_pick(data, "frames", "frame_count", "num_frames"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        out = svc.build_gif_preview(
            normalized.get("camera"), normalized.get("output"),
            width=normalized["width"], height=normalized["height"],
            fps=normalized["fps"], duration=normalized["duration"],
            frames=normalized["frames"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "ok": True,
        "success": True,
        "status": "completed",
        "camera": normalized.get("camera"),
        "camera_id": normalized.get("camera"),
        "camera_name": normalized.get("camera"),
        "output_path": str(out),
        "output": str(out),
        "path": str(out),
        "width": normalized["width"],
        "height": normalized["height"],
        "fps": normalized["fps"],
        "frame_rate": normalized["fps"],
        "duration": normalized["duration"],
        "frames": normalized["frames"],
        "format": "gif",
        "output_format": "gif",
        "content_type": svc.CONTENT_TYPE_GIF,
        "media_type": svc.CONTENT_TYPE_GIF,
    }


async def _build_preview_for_camera(
    camera_id: Any,
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    width: Any = Query(default=None),
    height: Any = Query(default=None),
    fps: Any = Query(default=None),
    duration: Any = Query(default=None),
    frames: Any = Query(default=None),
    output: Any = Query(default=None),
):
    width = _clean_q(width)
    height = _clean_q(height)
    fps = _clean_q(fps)
    duration = _clean_q(duration)
    frames = _clean_q(frames)
    output = _clean_q(output)
    data = _normalize(payload, request)
    data.setdefault("camera", camera_id)
    if width is not None and data.get("width") is None:
        data["width"] = width
    if height is not None and data.get("height") is None:
        data["height"] = height
    if fps is not None and data.get("fps") is None:
        data["fps"] = fps
    if duration is not None and data.get("duration") is None:
        data["duration"] = duration
    if frames is not None and data.get("frames") is None:
        data["frames"] = frames
    if output is not None and data.get("output") is None:
        data["output"] = output
    return await _build_preview(payload=data, request=None)


async def _health() -> dict:
    return {"status": "ok", "service": "gif-preview", "ok": True, "success": True,
            "format": "gif", "content_type": "image/gif"}


async def _info() -> dict:
    from app.services import gif_preview as svc

    return {
        "service": "gif-preview",
        "status": "ok",
        "ok": True,
        "success": True,
        "format": "gif",
        "output_format": "gif",
        "content_type": svc.CONTENT_TYPE_GIF,
        "default_width": svc.DEFAULT_WIDTH,
        "default_fps": svc.DEFAULT_FPS,
        "default_duration": svc.DEFAULT_DURATION,
        "usage": "GET /{camera_id} -> image/gif | POST with {camera?, width?, fps?, duration?}",
        "endpoints": ["GET /{camera_id}", "GET /{camera_id}/info", "POST /", "POST /build"],
    }


_prefixes = [
    "/api/gif-preview",
    "/api/gif_preview",
    "/api/gif/preview",
    "/api/preview/gif",
    "/api/gif",
    "/api/export/gif-preview",
    "/api/export/gif_preview",
    "/api/export/gif",
]

_registered: set = set()
_op_counter = {"n": 0}


def _add(path: str, handler, methods=("GET",)):
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


for _prefix in _prefixes:
    _add(f"{_prefix}/health", _health, methods=("GET",))
    _add(f"{_prefix}/status", _health, methods=("GET",))
    _add(f"{_prefix}", _info, methods=("GET",))
    _add(f"{_prefix}/", _info, methods=("GET",))
    _add(f"{_prefix}/info", _info, methods=("GET",))
    _add(_prefix, _build_preview, methods=("POST",))
    _add(f"{_prefix}/", _build_preview, methods=("POST",))
    _add(f"{_prefix}/build", _build_preview, methods=("POST",))
    _add(f"{_prefix}/generate", _build_preview, methods=("POST",))
    _add(f"{_prefix}/render", _build_preview, methods=("POST",))
    _add(f"{_prefix}/create", _build_preview, methods=("POST",))
    _add(f"{_prefix}/{{camera_id}}", _preview_bytes, methods=("GET",))
    _add(f"{_prefix}/{{camera_id}}/info", _preview_info, methods=("GET",))
    _add(f"{_prefix}/camera/{{camera_id}}", _preview_bytes, methods=("GET",))
    _add(f"{_prefix}/cameras/{{camera_id}}", _preview_bytes, methods=("GET",))
    _add(f"{_prefix}/{{camera_id}}/build", _build_preview_for_camera, methods=("POST",))
    _add(f"{_prefix}/{{camera_id}}/generate", _build_preview_for_camera, methods=("POST",))
    _add(f"{_prefix}/{{camera_id}}", _build_preview_for_camera, methods=("POST",))


# Compatibility aliases.
gif_preview_router = router
router_gif_preview = router
preview_router = router
preview_gif_router = router
gif_router = router
gif_preview_api_router = router

__all__ = ["router"]
