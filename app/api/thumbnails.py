from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response

from app.services import thumbnails as thumbnails_service

router = APIRouter(prefix="/api/thumbnails", tags=["thumbnails"])


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
        wi = int(width) if width is not None else 320
    except (TypeError, ValueError):
        wi = 320
    try:
        hi = int(height) if height is not None else 240
    except (TypeError, ValueError):
        hi = 240
    wi = max(16, min(1280, wi))
    hi = max(16, min(1280, hi))
    return wi, hi


def _etag_for(data: bytes, updated_at: float) -> str:
    return '"{}-{}"'.format(len(data), int(float(updated_at) * 1000))


@router.get("")
@router.get("/")
async def list_thumbnails_cache(
    ttl: Optional[float] = Query(default=None),
):
    items = thumbnails_service.list_thumbnails(ttl=ttl if ttl is not None else thumbnails_service.DEFAULT_TTL_SECONDS)
    return {"thumbnails": items, "items": items, "count": len(items), "total": len(items)}


@router.get("/{camera_id}/info")
async def thumbnail_info(
    camera_id: Any,
    ttl: Optional[float] = Query(default=None),
):
    ttl_f = float(ttl) if ttl is not None else thumbnails_service.DEFAULT_TTL_SECONDS
    info = thumbnails_service.get_info(camera_id, ttl=ttl_f)
    if info is None:
        # Not cached yet: generate so info is available, report MISS.
        _, info = thumbnails_service.get_thumbnail_with_meta(camera_id, ttl=ttl_f)
        info = dict(info)
        info["cached"] = False
        info["cache"] = "MISS"
        return info
    info = dict(info)
    info["cached"] = True
    info["cache"] = "HIT" if info.get("fresh") else "STALE"
    return info


@router.get("/{camera_id}")
async def get_thumbnail(
    camera_id: Any,
    ttl: Optional[float] = Query(default=None),
    refresh: Optional[bool] = Query(default=None),
    force: Optional[bool] = Query(default=None),
    width: Optional[int] = Query(default=None),
    height: Optional[int] = Query(default=None),
    w: Optional[int] = Query(default=None),
    h: Optional[int] = Query(default=None),
):
    do_refresh = _parse_refresh(refresh, force=force)
    ttl_f = float(ttl) if ttl is not None else thumbnails_service.DEFAULT_TTL_SECONDS
    wi, hi = _coerce_dims(width, height, w, h)
    data, meta = thumbnails_service.get_thumbnail_with_meta(
        camera_id, ttl=ttl_f, refresh=do_refresh, width=wi, height=hi,
    )
    headers = {
        "X-Cache": str(meta.get("cache", "HIT")),
        "Cache-Control": "public, max-age={}".format(int(max(0, ttl_f))),
        "ETag": _etag_for(data, float(meta.get("updated_at", 0.0))),
        "X-Thumbnail-Age": str(round(float(meta.get("age_seconds", 0.0)), 3)),
    }
    return Response(content=data, media_type="image/jpeg", headers=headers)


# Alias with explicit extension (e.g. /api/thumbnails/1.jpg).
@router.get("/{camera_id}.jpg")
@router.get("/{camera_id}.jpeg")
async def get_thumbnail_jpg_alias(
    camera_id: Any,
    ttl: Optional[float] = Query(default=None),
    refresh: Optional[bool] = Query(default=None),
    width: Optional[int] = Query(default=None),
    height: Optional[int] = Query(default=None),
):
    return await get_thumbnail(camera_id, ttl=ttl, refresh=refresh, width=width, height=height)


@router.post("/{camera_id}/refresh")
async def refresh_thumbnail(
    camera_id: Any,
    width: Optional[int] = Query(default=None),
    height: Optional[int] = Query(default=None),
    ttl: Optional[float] = Query(default=None),
):
    ttl_f = float(ttl) if ttl is not None else thumbnails_service.DEFAULT_TTL_SECONDS
    wi, hi = _coerce_dims(width, height)
    data, meta = thumbnails_service.get_thumbnail_with_meta(
        camera_id, ttl=ttl_f, refresh=True, width=wi, height=hi,
    )
    out = dict(meta)
    out.update({"refreshed": True, "ok": True, "success": True, "cache": "MISS"})
    return JSONResponse(content=out)


@router.delete("/{camera_id}")
async def invalidate_thumbnail(camera_id: Any):
    removed = thumbnails_service.invalidate(camera_id)
    return {"ok": True, "success": True, "invalidated": bool(removed), "removed": bool(removed)}


@router.delete("")
@router.delete("/")
async def clear_thumbnails_cache():
    thumbnails_service.clear()
    return {"ok": True, "success": True, "cleared": True}
