"""Auto-GIF on alert API router.

Dedicated alert-GIF endpoints (``image/gif``) separate from the regular
video export pipeline (``POST /api/export`` -> mp4) and from the
on-demand GIF preview endpoints (``/api/gif-preview``).  Auto-registered
by ``app.main`` (it imports every ``app.api`` module and includes
``router``), so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request, HTTPException
from fastapi.responses import JSONResponse, Response

router = APIRouter(tags=["alert-gif"])


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
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "alert", "event", "notification"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    # Camera aliases.
    if data.get("camera") is None:
        for k in ("camera_id", "cameraId", "cameraID", "cam", "cam_id",
                  "camera_name", "cameraName", "device_id", "deviceId",
                  "target", "source", "stream"):
            if data.get(k) is not None:
                data["camera"] = data.get(k)
                break
    # Alert-id aliases.
    if data.get("alert_id") is None:
        for k in ("alertId", "alertID", "id", "event_id", "eventId",
                  "incident_id", "incidentId", "notification_id"):
            if data.get(k) is not None:
                data["alert_id"] = data.get(k)
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


async def _health() -> dict:
    return {"status": "ok", "service": "alert-gif", "ok": True, "success": True,
            "format": "gif", "content_type": "image/gif"}


async def _info() -> dict:
    from app.services import alert_gif as svc

    cfg = svc.get_config()
    return {
        "service": "alert-gif",
        "status": "enabled" if cfg.get("enabled") else "disabled",
        "ok": True,
        "success": True,
        "format": "gif",
        "output_format": "gif",
        "content_type": svc.CONTENT_TYPE_GIF,
        "media_type": svc.CONTENT_TYPE_GIF,
        "enabled": bool(cfg.get("enabled")),
        "auto_gif": bool(cfg.get("auto_gif", True)),
        "default_width": svc.DEFAULT_WIDTH,
        "default_fps": svc.DEFAULT_FPS,
        "default_duration": svc.DEFAULT_DURATION,
        "usage": "POST with {alert_id?, camera?, width?, fps?, duration?} auto-builds a GIF",
        "endpoints": ["GET /{alert_id} -> image/gif", "POST / {alert+camera} -> auto GIF", "POST /build", "POST /handle"],
    }


async def _config_get(request: Request = None) -> dict:  # type: ignore[assignment]
    from app.services import alert_gif as svc

    data = _normalize(None, request)
    cfg = svc.get_config(**data)
    cfg.setdefault("ok", True)
    cfg.setdefault("success", True)
    cfg.setdefault("service", "alert-gif")
    return cfg


async def _config_post(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    from app.services import alert_gif as svc

    data = _normalize(payload, request)
    try:
        cfg = svc.configure(**data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    cfg = dict(cfg)
    cfg.setdefault("ok", True)
    cfg.setdefault("success", True)
    cfg.setdefault("service", "alert-gif")
    return cfg


async def _build_alert(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    camera: Any = Query(default=None),
    camera_id: Any = Query(default=None),
    alert_id: Any = Query(default=None),
    width: Any = Query(default=None),
    height: Any = Query(default=None),
    fps: Any = Query(default=None),
    duration: Any = Query(default=None),
    frames: Any = Query(default=None),
    output: Any = Query(default=None),
):
    from app.services import alert_gif as svc

    camera = _clean_q(camera)
    camera_id = _clean_q(camera_id)
    alert_id = _clean_q(alert_id)
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
    if alert_id is not None and data.get("alert_id") is None:
        data["alert_id"] = alert_id
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
    aid = _pick(data, "alert_id", "alertId", "id", "event_id")
    try:
        out = svc.build_alert_gif(
            cam, _pick(data, "output", "output_path", "dest", "target", "out"),
            alert_id=aid,
            width=_pick(data, "width", "w"), height=_pick(data, "height", "h"),
            fps=_pick(data, "fps", "frame_rate", "framerate"),
            duration=_pick(data, "duration", "duration_seconds", "length", "seconds"),
            frames=_pick(data, "frames", "frame_count", "num_frames"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    rec = svc.get_alert_gif(aid) if aid is not None else None
    body = dict(rec) if rec else {}
    body.update({
        "ok": True,
        "success": True,
        "status": "completed",
        "camera": cam,
        "camera_id": cam,
        "camera_name": cam,
        "alert_id": str(aid) if aid is not None else body.get("alert_id"),
        "id": str(aid) if aid is not None else body.get("id"),
        "output_path": str(out),
        "output": str(out),
        "path": str(out),
        "gif_path": str(out),
        "format": "gif",
        "output_format": "gif",
        "content_type": svc.CONTENT_TYPE_GIF,
        "media_type": svc.CONTENT_TYPE_GIF,
    })
    return body


async def _handle_alert(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    camera: Any = Query(default=None),
    camera_id: Any = Query(default=None),
    alert_id: Any = Query(default=None),
    width: Any = Query(default=None),
    fps: Any = Query(default=None),
    duration: Any = Query(default=None),
):
    from app.services import alert_gif as svc

    camera = _clean_q(camera)
    camera_id = _clean_q(camera_id)
    alert_id = _clean_q(alert_id)
    width = _clean_q(width)
    fps = _clean_q(fps)
    duration = _clean_q(duration)
    data = _normalize(payload, request)
    if camera is not None and data.get("camera") is None:
        data["camera"] = camera
    if camera_id is not None and data.get("camera") is None:
        data["camera"] = camera_id
    if alert_id is not None and data.get("alert_id") is None:
        data["alert_id"] = alert_id
    if width is not None and data.get("width") is None:
        data["width"] = width
    if fps is not None and data.get("fps") is None:
        data["fps"] = fps
    if duration is not None and data.get("duration") is None:
        data["duration"] = duration
    try:
        result = svc.handle_alert(**data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not isinstance(result, dict):
        result = {"result": result, "ok": True, "success": True}
    result.setdefault("ok", True)
    result.setdefault("success", True)
    result.setdefault("format", "gif")
    return result


async def _bytes_for_alert(
    alert_id: Any,
    width: Any = Query(default=None),
    height: Any = Query(default=None),
    fps: Any = Query(default=None),
    duration: Any = Query(default=None),
    frames: Any = Query(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    from app.services import alert_gif as svc

    width = _clean_q(width)
    height = _clean_q(height)
    fps = _clean_q(fps)
    duration = _clean_q(duration)
    frames = _clean_q(frames)
    data = _normalize(None, request)
    w = width if width is not None else data.get("width")
    h = height if height is not None else data.get("height")
    f = fps if fps is not None else data.get("fps")
    d = duration if duration is not None else data.get("duration")
    n = frames if frames is not None else data.get("frames")
    rec = svc.get_alert_gif(alert_id)
    if rec is not None and rec.get("gif_path"):
        try:
            from pathlib import Path as _Path

            p = _Path(str(rec["gif_path"]))
            if p.is_file() and p.stat().st_size > 0:
                return Response(content=p.read_bytes(), media_type=svc.CONTENT_TYPE_GIF)
        except Exception:  # noqa: BLE001
            pass
    # Fall back to rendering bytes for the stored camera (or alert id as label).
    try:
        cam = rec.get("camera") if rec else None
        payload = svc.generate_alert_gif_bytes(cam or alert_id, width=w, height=h, fps=f, duration=d, frames=n)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(content=payload, media_type=svc.CONTENT_TYPE_GIF)


async def _info_for_alert(alert_id: Any):
    from app.services import alert_gif as svc

    rec = svc.get_alert_gif(alert_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Unknown alert {alert_id}")
    body = dict(rec)
    body.setdefault("ok", True)
    body.setdefault("success", True)
    body.setdefault("format", "gif")
    return JSONResponse(content=body)


async def _bytes_for_camera(
    camera_id: Any,
    width: Any = Query(default=None),
    height: Any = Query(default=None),
    fps: Any = Query(default=None),
    duration: Any = Query(default=None),
    frames: Any = Query(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    from app.services import alert_gif as svc

    width = _clean_q(width)
    height = _clean_q(height)
    fps = _clean_q(fps)
    duration = _clean_q(duration)
    frames = _clean_q(frames)
    data = _normalize(None, request)
    w = width if width is not None else data.get("width")
    h = height if height is not None else data.get("height")
    f = fps if fps is not None else data.get("fps")
    d = duration if duration is not None else data.get("duration")
    n = frames if frames is not None else data.get("frames")
    try:
        payload = svc.generate_alert_gif_bytes(camera_id, width=w, height=h, fps=f, duration=d, frames=n)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(content=payload, media_type=svc.CONTENT_TYPE_GIF)


async def _list() -> dict:
    from app.services import alert_gif as svc

    items = svc.list_alert_gifs()
    return {"ok": True, "success": True, "count": len(items), "items": items,
            "format": "gif", "content_type": svc.CONTENT_TYPE_GIF}


_prefixes = [
    "/api/alert_gif",
    "/api/alert-gif",
    "/api/alerts/gif",
    "/api/gif/alert",
    "/api/alerts/alert_gif",
    "/api/alerts/alert-gif",
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
    _add(f"{_prefix}/config", _config_get, methods=("GET",))
    _add(f"{_prefix}/config", _config_post, methods=("POST",))
    _add(f"{_prefix}/configure", _config_post, methods=("POST",))
    _add(f"{_prefix}/list", _list, methods=("GET",))
    _add(_prefix, _handle_alert, methods=("POST",))
    _add(f"{_prefix}/", _handle_alert, methods=("POST",))
    _add(f"{_prefix}/build", _build_alert, methods=("POST",))
    _add(f"{_prefix}/generate", _build_alert, methods=("POST",))
    _add(f"{_prefix}/render", _build_alert, methods=("POST",))
    _add(f"{_prefix}/create", _build_alert, methods=("POST",))
    _add(f"{_prefix}/handle", _handle_alert, methods=("POST",))
    _add(f"{_prefix}/alert", _handle_alert, methods=("POST",))
    _add(f"{_prefix}/trigger", _handle_alert, methods=("POST",))
    _add(f"{_prefix}/camera/{{camera_id}}", _bytes_for_camera, methods=("GET",))
    _add(f"{_prefix}/cameras/{{camera_id}}", _bytes_for_camera, methods=("GET",))
    _add(f"{_prefix}/{{alert_id}}/info", _info_for_alert, methods=("GET",))
    _add(f"{_prefix}/{{alert_id}}", _bytes_for_alert, methods=("GET",))


# Compatibility aliases.
alert_gif_router = router
router_alert_gif = router
alert_router = router
gif_alert_router = router
alert_gif_api_router = router

__all__ = ["router"]
