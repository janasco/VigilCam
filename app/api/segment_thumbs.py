"""Recording segment thumbnail API router.

Per-segment JPEG thumbnails (``GET .../thumb``) backed by
:mod:`app.services.segment_thumbs`. This is separate from live frames
(``GET /api/thumbnails/{camera_id}``), which are one cached JPEG per
camera with no segment argument.

Auto-registered by the ``app.api`` pkgutil auto-loader in
:mod:`app.main` (it imports every ``app.api`` module and includes
``router``), so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse, Response

from app.services import segment_thumbs as seg_service

router = APIRouter(tags=["segment-thumbs"])

# Compatibility aliases.
segment_thumbs_router = router
segment_thumb_router = router
segment_thumbnails_router = router
api_router = router


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
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "settings", "filter", "filters"):
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


def _clean_q(value: Any) -> Any:
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


def _parse_refresh(value: Any = None, **kwargs: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on", "force"}:
        return True
    for alias in ("force", "force_refresh", "no_cache", "nocache", "fresh", "reload"):
        v = kwargs.get(alias)
        if v is True or (isinstance(v, str) and v.strip().lower() in {"1", "true", "yes", "on"}):
            return True
    return False


def _coerce_dims(width: Any, height: Any, w: Any = None, h: Any = None) -> tuple[int, int]:
    width = width if width is not None else w
    height = height if height is not None else h
    try:
        wi = int(width) if width is not None else seg_service.DEFAULT_WIDTH
    except (TypeError, ValueError):
        wi = seg_service.DEFAULT_WIDTH
    try:
        hi = int(height) if height is not None else seg_service.DEFAULT_HEIGHT
    except (TypeError, ValueError):
        hi = seg_service.DEFAULT_HEIGHT
    wi = max(seg_service.MIN_DIMENSION, min(seg_service.MAX_DIMENSION, wi))
    hi = max(seg_service.MIN_DIMENSION, min(seg_service.MAX_DIMENSION, hi))
    return wi, hi


def _etag_for(data: bytes, updated_at: float) -> str:
    return '"{}-{}"'.format(len(data), int(float(updated_at) * 1000))


def _image_response(payload: bytes, meta: dict) -> Response:
    headers = {
        "X-Cache": str(meta.get("cache", "HIT")),
        "X-Segment-Path": str(meta.get("segment_path") or meta.get("segment") or ""),
        "X-Segment-Exists": str(bool(meta.get("exists", False))).lower(),
        "Cache-Control": "public, max-age={}".format(int(max(0, float(meta.get("ttl_seconds", 60))))),
        "ETag": _etag_for(payload, float(meta.get("updated_at", 0.0))),
        "X-Thumbnail-Age": str(round(float(meta.get("age_seconds", 0.0)), 3)),
    }
    if meta.get("recording_id") is not None:
        headers["X-Recording-Id"] = str(meta.get("recording_id"))
    return Response(content=payload, media_type="image/jpeg", headers=headers)


def _resolve_image_kwargs(data: dict) -> dict:
    segment = _pick(
        data, "segment", "segment_path", "segmentPath", "segment_file",
        "path", "file_path", "filePath", "file", "filepath",
        "recording_path", "recordingPath", "clip", "video", "src",
        "source", "source_path", "sourcePath", "filename", "file_name",
        "name", "segment_name",
    )
    recording_id = _pick(
        data, "recording_id", "recordingId", "recordingID", "rec_id", "recId",
    )
    camera = _pick(
        data, "camera", "camera_id", "cameraId", "cameraID", "cam", "cam_id",
        "camera_name", "cameraName", "device_id", "deviceId",
    )
    timestamp = _pick(
        data, "timestamp", "time", "event_time", "eventTime", "start",
        "start_time", "startTime", "at",
    )
    width = _pick(data, "width", "w", "width_px", "thumb_width", "tile_width", "tw")
    height = _pick(data, "height", "h", "height_px", "thumb_height", "tile_height", "th")
    offset = _pick(
        data, "offset", "offset_seconds", "offsetSeconds", "seek", "position",
        "at_seconds", "thumb_offset", "frame_offset", "ss", "time_offset", "t",
    )
    ttl = _pick(data, "ttl", "ttl_seconds", "max_age", "cache_ttl")
    refresh = _parse_refresh(
        _pick(data, "refresh", "force", "force_refresh", "no_cache", "nocache", "fresh", "reload"),
        force=data.get("force"), force_refresh=data.get("force_refresh"),
    )
    recording_dir = _pick(
        data, "recording_dir", "recordingDir", "recordings_dir", "recordingsDir",
        "storage_root", "storageRoot", "root", "directory", "dir", "folder",
    )
    return {
        "segment": segment,
        "recording_id": recording_id,
        "camera": camera,
        "timestamp": timestamp,
        "width": width,
        "height": height,
        "offset": offset,
        "ttl": ttl,
        "refresh": refresh,
        "recording_dir": recording_dir,
        "raw": data,
    }


async def _do_image(data: dict, db: Any = None) -> Response:
    from fastapi import HTTPException

    p = _resolve_image_kwargs(data)
    wi, hi = _coerce_dims(p["width"], p["height"])
    ttl_f = float(p["ttl"]) if p["ttl"] is not None else seg_service.DEFAULT_TTL_SECONDS
    try:
        ttl_f = float(ttl_f)
    except (TypeError, ValueError):
        ttl_f = float(seg_service.DEFAULT_TTL_SECONDS)
    off_raw = p["offset"]
    # Recording-id path (DB-backed when a session is available).
    if p["recording_id"] is not None and p["segment"] is None and p["camera"] is None:
        try:
            rid = int(p["recording_id"])  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid recording_id")
        kwargs: dict[str, Any] = {"width": wi, "height": hi, "ttl": ttl_f, "refresh": p["refresh"]}
        if off_raw is not None:
            kwargs["offset"] = off_raw
        if p["recording_dir"] is not None:
            kwargs["recording_dir"] = p["recording_dir"]
        if db is not None:
            payload, meta = await seg_service.get_thumbnail_for_recording_async(
                rid, db=db, **kwargs)
        else:
            payload, meta = seg_service.get_segment_thumbnail_with_meta(
                f"recording:{int(rid)}", recording_id=int(rid), **kwargs)
        return _image_response(payload, meta)
    # Segment / camera+time path.
    target = p["segment"]
    if target is None and p["camera"] is not None:
        target = seg_service.resolve_segment(
            camera=p["camera"], timestamp=p["timestamp"],
            recording_dir=p["recording_dir"]) or f"{p['camera']}@{p['timestamp']}" if p["timestamp"] is not None else str(p["camera"])
        # resolve_segment already searched; fall back to pseudo-key.
        if target is None:
            target = str(p["camera"])
    if target is None:
        raise HTTPException(status_code=400, detail="segment is required")
    kwargs = {"width": wi, "height": hi, "ttl": ttl_f, "refresh": p["refresh"]}
    if off_raw is not None:
        kwargs["offset"] = off_raw
    if p["recording_dir"] is not None:
        kwargs["recording_dir"] = p["recording_dir"]
    if p["recording_id"] is not None:
        kwargs["recording_id"] = p["recording_id"]
    if p["camera"] is not None:
        kwargs.setdefault("camera", p["camera"])
    try:
        payload, meta = seg_service.get_segment_thumbnail_with_meta(target, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _image_response(payload, meta)


async def _do_info(data: dict, db: Any = None) -> dict:
    from fastapi import HTTPException

    p = _resolve_image_kwargs(data)
    wi, hi = _coerce_dims(p["width"], p["height"])
    ttl_f = float(p["ttl"]) if p["ttl"] is not None else seg_service.DEFAULT_TTL_SECONDS
    try:
        ttl_f = float(ttl_f)
    except (TypeError, ValueError):
        ttl_f = float(seg_service.DEFAULT_TTL_SECONDS)
    if p["recording_id"] is not None and p["segment"] is None and p["camera"] is None:
        try:
            rid = int(p["recording_id"])  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid recording_id")
        kwargs: dict[str, Any] = {"width": wi, "height": hi, "ttl": ttl_f}
        if p["offset"] is not None:
            kwargs["offset"] = p["offset"]
        if p["recording_dir"] is not None:
            kwargs["recording_dir"] = p["recording_dir"]
        if db is not None:
            _payload, meta = await seg_service.get_thumbnail_for_recording_async(
                rid, db=db, **kwargs)
        else:
            _payload, meta = seg_service.get_segment_thumbnail_with_meta(
                f"recording:{int(rid)}", recording_id=int(rid), **kwargs)
        out = dict(meta)
        out.update({"cached": True, "cache": meta.get("cache", "HIT")})
        return out
    target = p["segment"]
    if target is None and p["camera"] is not None:
        target = seg_service.resolve_segment(
            camera=p["camera"], timestamp=p["timestamp"],
            recording_dir=p["recording_dir"])
        if target is None:
            target = str(p["camera"])
    if target is None:
        raise HTTPException(status_code=400, detail="segment is required")
    kwargs = {"width": wi, "height": hi, "ttl": ttl_f}
    if p["offset"] is not None:
        kwargs["offset"] = p["offset"]
    if p["recording_dir"] is not None:
        kwargs["recording_dir"] = p["recording_dir"]
    info = seg_service.get_info(target, **kwargs)
    if info is None:
        _payload, meta = seg_service.get_segment_thumbnail_with_meta(target, **kwargs)
        info = dict(meta)
        info["cached"] = False
        info["cache"] = "MISS"
    else:
        info = dict(info)
        info["cached"] = True
        info["cache"] = "HIT" if info.get("fresh") else "STALE"
    return info


# ---- list / stats ------------------------------------------------------------

async def _list_cache(
    ttl: Any = Query(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    data = _normalize(None, request)
    ttl_v = _clean_q(ttl)
    if ttl_v is None:
        ttl_v = data.get("ttl", data.get("ttl_seconds"))
    items = seg_service.list_thumbnails(
        ttl=float(ttl_v) if ttl_v is not None else seg_service.DEFAULT_TTL_SECONDS)
    return {"thumbnails": items, "items": items, "count": len(items), "total": len(items),
            "segments": items}


async def _list_segments(
    camera: Any = Query(default=None),
    recording_dir: Any = Query(default=None),
    limit: Any = Query(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    data = _normalize(None, request)
    cam = _clean_q(camera)
    if cam is None:
        cam = _pick(data, "camera", "camera_id", "camera_name", "cam")
    rdir = _clean_q(recording_dir)
    if rdir is None:
        rdir = _pick(data, "recording_dir", "recordings_dir", "storage_root")
    lim = _clean_q(limit)
    if lim is None:
        lim = _pick(data, "limit", "n", "count")
    items = seg_service.list_segments(camera=cam, recording_dir=rdir, limit=lim)
    return {"segments": items, "items": items, "count": len(items), "total": len(items)}


async def _status() -> dict:
    stats = seg_service.get_stats()
    return {"status": "ok", "ok": True, "success": True, "service": "segment-thumbs", **stats}


# ---- image handlers ----------------------------------------------------------

async def _image_query(
    segment: Any = Query(default=None),
    recording_id: Any = Query(default=None),
    camera: Any = Query(default=None),
    timestamp: Any = Query(default=None),
    width: Any = Query(default=None),
    height: Any = Query(default=None),
    offset: Any = Query(default=None),
    ttl: Any = Query(default=None),
    refresh: Any = Query(default=None),
    recording_dir: Any = Query(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    data = _normalize(None, request)
    for k, v in (("segment", _clean_q(segment)), ("recording_id", _clean_q(recording_id)),
                 ("camera", _clean_q(camera)), ("timestamp", _clean_q(timestamp)),
                 ("width", _clean_q(width)), ("height", _clean_q(height)),
                 ("offset", _clean_q(offset)), ("ttl", _clean_q(ttl)),
                 ("refresh", _clean_q(refresh)), ("recording_dir", _clean_q(recording_dir))):
        if v is not None and data.get(k) is None:
            data[k] = v
    # db session for recording_id resolution (best effort).
    db = None
    try:
        from app.api.deps import db_session as _db_sess  # noqa: E402
        from app.core.database import get_session as _get_session  # noqa: E402
        _gen = _get_session()
        try:
            db = await _gen.__anext__()
        except Exception:  # noqa: BLE001
            db = None
    except Exception:  # noqa: BLE001
        db = None
    try:
        return await _do_image(data, db=db)
    finally:
        try:
            if db is not None and hasattr(db, "close"):
                await db.close()
        except Exception:  # noqa: BLE001
            pass


async def _info_query(
    segment: Any = Query(default=None),
    recording_id: Any = Query(default=None),
    camera: Any = Query(default=None),
    timestamp: Any = Query(default=None),
    width: Any = Query(default=None),
    height: Any = Query(default=None),
    offset: Any = Query(default=None),
    ttl: Any = Query(default=None),
    recording_dir: Any = Query(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    data = _normalize(None, request)
    for k, v in (("segment", _clean_q(segment)), ("recording_id", _clean_q(recording_id)),
                 ("camera", _clean_q(camera)), ("timestamp", _clean_q(timestamp)),
                 ("width", _clean_q(width)), ("height", _clean_q(height)),
                 ("offset", _clean_q(offset)), ("ttl", _clean_q(ttl)),
                 ("recording_dir", _clean_q(recording_dir))):
        if v is not None and data.get(k) is None:
            data[k] = v
    return await _do_info(data, db=None)


async def _image_for_file(
    seg_path: str,
    width: Any = Query(default=None),
    height: Any = Query(default=None),
    offset: Any = Query(default=None),
    ttl: Any = Query(default=None),
    refresh: Any = Query(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    data = _normalize(None, request)
    data["segment"] = seg_path
    for k, v in (("width", _clean_q(width)), ("height", _clean_q(height)),
                 ("offset", _clean_q(offset)), ("ttl", _clean_q(ttl)),
                 ("refresh", _clean_q(refresh))):
        if v is not None and data.get(k) is None:
            data[k] = v
    return await _do_image(data, db=None)


async def _image_for_recording(
    recording_id: int,
    width: Any = Query(default=None),
    height: Any = Query(default=None),
    offset: Any = Query(default=None),
    ttl: Any = Query(default=None),
    refresh: Any = Query(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    from fastapi import Depends as _Depends  # noqa: E402

    _ = _Depends  # keep import surface familiar
    data = _normalize(None, request)
    data["recording_id"] = recording_id
    for k, v in (("width", _clean_q(width)), ("height", _clean_q(height)),
                 ("offset", _clean_q(offset)), ("ttl", _clean_q(ttl)),
                 ("refresh", _clean_q(refresh))):
        if v is not None and data.get(k) is None:
            data[k] = v
    # Resolve via DB session directly (dependency-free to allow :path routes).
    db = None
    try:
        from app.core.database import get_session as _get_session  # noqa: E402
        _gen = _get_session()
        try:
            db = await _gen.__anext__()
        except Exception:  # noqa: BLE001
            db = None
    except Exception:  # noqa: BLE001
        db = None
    try:
        return await _do_image(data, db=db)
    finally:
        try:
            if db is not None and hasattr(db, "close"):
                await db.close()
        except Exception:  # noqa: BLE001
            pass


async def _image_for_segment_name(
    segment_name: str,
    width: Any = Query(default=None),
    height: Any = Query(default=None),
    offset: Any = Query(default=None),
    ttl: Any = Query(default=None),
    refresh: Any = Query(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    data = _normalize(None, request)
    data["segment"] = segment_name
    for k, v in (("width", _clean_q(width)), ("height", _clean_q(height)),
                 ("offset", _clean_q(offset)), ("ttl", _clean_q(ttl)),
                 ("refresh", _clean_q(refresh))):
        if v is not None and data.get(k) is None:
            data[k] = v
    return await _do_image(data, db=None)


async def _refresh(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    from fastapi import HTTPException

    data = _normalize(payload, request)
    p = _resolve_image_kwargs(data)
    target = p["segment"]
    if target is None and p["recording_id"] is not None:
        target = f"recording:{int(p['recording_id'])}"  # type: ignore[arg-type]
    if target is None and p["camera"] is not None:
        target = str(p["camera"])
    if target is None:
        raise HTTPException(status_code=400, detail="segment is required")
    wi, hi = _coerce_dims(p["width"], p["height"])
    kwargs: dict[str, Any] = {"width": wi, "height": hi, "refresh": True}
    if p["offset"] is not None:
        kwargs["offset"] = p["offset"]
    if p["recording_dir"] is not None:
        kwargs["recording_dir"] = p["recording_dir"]
    try:
        _payload, meta = seg_service.get_segment_thumbnail_with_meta(target, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    out = dict(meta)
    out.update({"refreshed": True, "ok": True, "success": True, "cache": "MISS"})
    return JSONResponse(content=out)


async def _invalidate_one(seg_path: str):
    ok = seg_service.invalidate(seg_path)
    return {"ok": True, "success": True, "invalidated": bool(ok), "removed": bool(ok)}


async def _clear_all():
    seg_service.clear()
    return {"ok": True, "success": True, "cleared": True}


# ---- route registration ------------------------------------------------------

router.get("/api/segment-thumbs")(_list_cache)
router.get("/api/segment-thumbs/")(_list_cache)
router.get("/api/segment_thumbs")(_list_cache)
router.get("/api/segment_thumbs/")(_list_cache)
router.get("/api/segments/thumbs")(_list_cache)
router.get("/api/segments/thumbs/")(_list_cache)

router.get("/api/segment-thumbs/segments")(_list_segments)
router.get("/api/segment_thumbs/segments")(_list_segments)
router.get("/api/segments/list")(_list_segments)

router.get("/api/segment-thumbs/status")(_status)
router.get("/api/segment-thumbs/health")(_status)
router.get("/api/segment_thumbs/status")(_status)
router.get("/api/segment_thumbs/health")(_status)
router.get("/api/segments/thumbs/status")(_status)

router.get("/api/segment-thumbs/info")(_info_query)
router.get("/api/segment_thumbs/info")(_info_query)
router.get("/api/segments/thumbs/info")(_info_query)

router.get("/api/segment-thumbs/image")(_image_query)
router.get("/api/segment-thumbs/thumb")(_image_query)
router.get("/api/segment-thumbs/thumbnail")(_image_query)
router.get("/api/segment_thumbs/image")(_image_query)
router.get("/api/segment_thumbs/thumb")(_image_query)
router.get("/api/segment_thumbs/thumbnail")(_image_query)
router.get("/api/segments/thumbs/image")(_image_query)
router.get("/api/segments/thumb")(_image_query)
router.get("/api/segments/thumbnail")(_image_query)

router.get("/api/segment-thumbs/file/{seg_path:path}")(_image_for_file)
router.get("/api/segment_thumbs/file/{seg_path:path}")(_image_for_file)

router.get("/api/recordings/{recording_id}/thumb")(_image_for_recording)
router.get("/api/recordings/{recording_id}/thumbnail")(_image_for_recording)
router.get("/api/recordings/{recording_id}/thumb.jpg")(_image_for_recording)

router.get("/api/segments/{segment_name}/thumb")(_image_for_segment_name)
router.get("/api/segments/{segment_name}/thumbnail")(_image_for_segment_name)

router.post("/api/segment-thumbs/refresh")(_refresh)
router.post("/api/segment_thumbs/refresh")(_refresh)

router.delete("/api/segment-thumbs/file/{seg_path:path}")(_invalidate_one)
router.delete("/api/segment-thumbs")(_clear_all)
router.delete("/api/segment-thumbs/")(_clear_all)
router.delete("/api/segment_thumbs")(_clear_all)
router.delete("/api/segment_thumbs/")(_clear_all)

__all__ = ["router"]
