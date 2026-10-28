"""Combined mosaic grid image endpoints.

Renders per-camera thumbnails (see :mod:`app.api.thumbnails`) into one grid
JPEG via :mod:`app.services.mosaic`. Registered automatically by the
``app.api`` auto-loader in :mod:`app.main` (no manual wiring needed).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response

from app.services import mosaic as mosaic_service

router = APIRouter(prefix="/api/mosaic", tags=["mosaic"])


def _parse_cameras(
    cameras: Any = None,
    camera_ids: Any = None,
    ids: Any = None,
    cams: Any = None,
) -> list[str]:
    for candidate in (cameras, camera_ids, ids, cams):
        if candidate is not None and str(candidate).strip() != "":
            return mosaic_service.resolve_camera_ids(candidate)
    return []


def _coerce_tile(value: Any, default: int) -> int:
    try:
        v = int(value) if value is not None else default
    except (TypeError, ValueError):
        v = default
    return max(16, min(1280, v))


def _render(
    cameras: Any = None,
    cols: Any = None,
    tile_width: Any = None,
    tile_height: Any = None,
    quality: Any = None,
    w: Any = None,
    h: Any = None,
) -> Response:
    ids = _parse_cameras(cameras)
    if not ids:
        ids = mosaic_service.default_camera_ids()
    tw = _coerce_tile(tile_width if tile_width is not None else w, mosaic_service.DEFAULT_TILE_WIDTH)
    th = _coerce_tile(tile_height if tile_height is not None else h, mosaic_service.DEFAULT_TILE_HEIGHT)
    data, meta = mosaic_service.build_mosaic_with_meta(
        ids, tile_width=tw, tile_height=th, cols=cols, quality=quality,
    )
    headers = {
        "X-Mosaic-Count": str(meta.get("count", len(ids))),
        "X-Mosaic-Grid": str(meta.get("grid", "")),
        "Cache-Control": "public, max-age=10",
    }
    return Response(content=data, media_type="image/jpeg", headers=headers)


@router.get("/info")
async def mosaic_info(
    cameras: Optional[str] = Query(default=None),
    camera_ids: Optional[str] = Query(default=None, alias="camera_ids"),
    ids: Optional[str] = Query(default=None),
    cols: Optional[int] = Query(default=None),
    tile_width: Optional[int] = Query(default=None),
    tile_height: Optional[int] = Query(default=None),
    w: Optional[int] = Query(default=None),
    h: Optional[int] = Query(default=None),
):
    ids_list = _parse_cameras(cameras, camera_ids, ids)
    if not ids_list:
        ids_list = mosaic_service.default_camera_ids()
    tw = _coerce_tile(tile_width if tile_width is not None else w, mosaic_service.DEFAULT_TILE_WIDTH)
    th = _coerce_tile(tile_height if tile_height is not None else h, mosaic_service.DEFAULT_TILE_HEIGHT)
    info = mosaic_service.get_info(ids_list, cols=cols, tile_width=tw, tile_height=th)
    info.update({"ok": True, "success": True})
    return JSONResponse(content=info)


@router.get("")
@router.get("/")
@router.get("/grid")
async def get_mosaic(
    cameras: Optional[str] = Query(default=None),
    camera_ids: Optional[str] = Query(default=None, alias="camera_ids"),
    ids: Optional[str] = Query(default=None),
    cols: Optional[int] = Query(default=None),
    tile_width: Optional[int] = Query(default=None),
    tile_height: Optional[int] = Query(default=None),
    w: Optional[int] = Query(default=None),
    h: Optional[int] = Query(default=None),
    quality: Optional[int] = Query(default=None),
):
    raw = cameras if cameras is not None else (camera_ids if camera_ids is not None else ids)
    return _render(raw, cols=cols, tile_width=tile_width, tile_height=tile_height, quality=quality, w=w, h=h)


@router.get("/grid.jpg")
@router.get("/mosaic.jpg")
async def get_mosaic_jpg_alias(
    cameras: Optional[str] = Query(default=None),
    cols: Optional[int] = Query(default=None),
    tile_width: Optional[int] = Query(default=None),
    tile_height: Optional[int] = Query(default=None),
    w: Optional[int] = Query(default=None),
    h: Optional[int] = Query(default=None),
    quality: Optional[int] = Query(default=None),
):
    return _render(cameras, cols=cols, tile_width=tile_width, tile_height=tile_height, quality=quality, w=w, h=h)
