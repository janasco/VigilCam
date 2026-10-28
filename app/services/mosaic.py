"""Mosaic grid image service for VigilCam.

Combines per-camera thumbnails (see :mod:`app.services.thumbnails`) into a
single grid JPEG. Thumbnails cache single frames; MJPEG streams singles;
this module is the only place that produces a combined mosaic image.
"""

from __future__ import annotations

import io
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services import thumbnails as thumbnails_service

DEFAULT_TILE_WIDTH = 320
DEFAULT_TILE_HEIGHT = 240
MIN_DIMENSION = 16
MAX_DIMENSION = 1280
DEFAULT_QUALITY = 75
DEFAULT_FALLBACK_CAMERAS = ("1", "2", "3", "4")

CONTENT_TYPE_JPEG = "image/jpeg"


def _coerce_tile(value: Any, default: int) -> int:
    try:
        v = int(value) if value is not None else default
    except (TypeError, ValueError):
        v = default
    return max(MIN_DIMENSION, min(MAX_DIMENSION, v))


def _coerce_quality(value: Any = None, **kwargs: Any) -> int:
    if value is None:
        for alias in ("q", "jpeg_quality", "jpg_quality"):
            if kwargs.get(alias) is not None:
                value = kwargs.get(alias)
                break
    try:
        q = int(value) if value is not None else DEFAULT_QUALITY
    except (TypeError, ValueError):
        q = DEFAULT_QUALITY
    return max(10, min(95, q))


def _coerce_cols(value: Any = None, **kwargs: Any) -> Optional[int]:
    if value is None:
        for alias in ("columns", "ncols", "cols_count", "grid_cols"):
            if kwargs.get(alias) is not None:
                value = kwargs.get(alias)
                break
    if value is None:
        return None
    try:
        c = int(value)
    except (TypeError, ValueError):
        return None
    if c <= 0:
        return None
    return max(1, min(8, c))


def resolve_camera_ids(camera_ids: Any = None, **kwargs: Any) -> List[str]:
    """Normalize caller-supplied camera ids to a list of non-empty strings."""
    raw: Any = camera_ids
    if raw is None:
        for alias in ("cameras", "camera", "ids", "cams", "cam_ids", "id"):
            if kwargs.get(alias) is not None:
                raw = kwargs.get(alias)
                break
    if raw is None:
        return []
    if isinstance(raw, (int, float)):
        raw = [str(raw)]
    elif isinstance(raw, str):
        # Accept "1,2,3" / "1 2 3" / "1|2|3".
        cleaned = raw.replace("|", ",").replace(";", ",")
        parts: List[str] = []
        for chunk in cleaned.split(","):
            for token in chunk.split():
                token = token.strip()
                if token:
                    parts.append(token)
        raw = parts
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        raw = list(raw)
    else:
        raw = [raw]
    out: List[str] = []
    for item in raw:  # type: ignore[union-attr]
        s = str(item).strip()
        if not s:
            continue
        low = s.lower()
        for suffix in (".jpg", ".jpeg", ".png"):
            if low.endswith(suffix):
                s = s[: -len(suffix)].strip()
                break
        if s and s not in out:
            out.append(s)
        if len(out) >= 64:
            break
    return out


def default_camera_ids(limit: int = 4) -> List[str]:
    """Ids used when the caller does not specify any cameras."""
    try:
        cached = thumbnails_service.list_thumbnails()
        keys = [str(i.get("camera_id", "")).strip() for i in cached]
        keys = [k for k in keys if k]
        if keys:
            return sorted(set(keys))[: max(1, min(64, int(limit)))]
    except Exception:  # noqa: BLE001
        pass
    n = max(1, min(64, int(limit) if limit else 4))
    base = list(DEFAULT_FALLBACK_CAMERAS)
    while len(base) < n:
        base.append(str(len(base) + 1))
    return base[:n]


def grid_dimensions(count: int, cols: Any = None, **kwargs: Any) -> Tuple[int, int]:
    """Return (cols, rows) for ``count`` tiles."""
    n = max(1, int(count))
    c = _coerce_cols(cols, **kwargs)
    if c is None:
        c = int(math.ceil(math.sqrt(n)))
    c = max(1, min(max(1, n), c))
    rows = int(math.ceil(n / c))
    return c, rows


def _load_tile_image(data: bytes, width: int, height: int, label: str = ""):
    from PIL import Image, ImageDraw  # type: ignore

    try:
        img = Image.open(io.BytesIO(bytes(data))).convert("RGB")
        if img.size != (width, height):
            img = img.resize((width, height))
        return img
    except Exception:  # noqa: BLE001
        img = Image.new("RGB", (width, height), color=(20, 20, 30))
        try:
            draw = ImageDraw.Draw(img)
            draw.text((8, 8), str(label)[:24], fill=(255, 255, 255))
        except Exception:  # noqa: BLE001
            pass
        return img


def build_mosaic_with_meta(
    camera_ids: Any = None,
    tile_width: Any = None,
    tile_height: Any = None,
    cols: Any = None,
    quality: Any = None,
    label_tiles: bool = True,
    **kwargs: Any,
) -> Tuple[bytes, Dict[str, Any]]:
    """Build a grid JPEG from per-camera thumbnails.

    Returns ``(jpeg_bytes, meta)`` where meta describes the grid layout.
    """
    ids = resolve_camera_ids(camera_ids, **kwargs)
    if not ids:
        try:
            limit_raw = kwargs.get("limit", kwargs.get("count", kwargs.get("default_count", 4)))
            limit = int(limit_raw) if limit_raw is not None else 4
        except (TypeError, ValueError):
            limit = 4
        ids = default_camera_ids(limit=limit)
    if not ids:
        raise ValueError("at least one camera id is required")

    tw = _coerce_tile(
        tile_width if tile_width is not None else kwargs.get("tile_w", kwargs.get("w", kwargs.get("width"))),
        DEFAULT_TILE_WIDTH,
    )
    th = _coerce_tile(
        tile_height if tile_height is not None else kwargs.get("tile_h", kwargs.get("h", kwargs.get("height"))),
        DEFAULT_TILE_HEIGHT,
    )
    q = _coerce_quality(quality, **kwargs)
    ncols, nrows = grid_dimensions(len(ids), cols, **kwargs)

    thumbs: List[bytes] = []
    for cid in ids:
        try:
            thumbs.append(bytes(thumbnails_service.get_thumbnail(cid, width=tw, height=th)))
        except Exception:  # noqa: BLE001
            thumbs.append(b"")

    try:
        from PIL import Image, ImageDraw  # type: ignore

        mosaic = Image.new("RGB", (ncols * tw, nrows * th), color=(10, 10, 15))
        draw = ImageDraw.Draw(mosaic)
        for idx, cid in enumerate(ids):
            r, c = divmod(idx, ncols)
            tile = _load_tile_image(thumbs[idx], tw, th, label="Cam {}".format(cid))
            mosaic.paste(tile, (c * tw, r * th))
            if label_tiles:
                try:
                    draw.text((c * tw + 8, r * th + 8), "Cam {}".format(cid)[:24], fill=(255, 255, 255))
                except Exception:  # noqa: BLE001
                    pass
        buf = io.BytesIO()
        mosaic.save(buf, format="JPEG", quality=q)
        data = buf.getvalue()
    except ImportError:
        # PIL unavailable: fall back to the first thumbnail bytes.
        data = thumbs[0] if thumbs and thumbs[0][:2] == b"\xff\xd8" else thumbnails_service.get_thumbnail(ids[0], width=tw, height=th)

    meta: Dict[str, Any] = {
        "camera_ids": list(ids),
        "cameras": list(ids),
        "count": len(ids),
        "cols": ncols,
        "rows": nrows,
        "grid": "{}x{}".format(ncols, nrows),
        "tile_width": tw,
        "tile_height": th,
        "width": ncols * tw,
        "height": nrows * th,
        "quality": q,
        "size_bytes": len(data),
        "content_type": CONTENT_TYPE_JPEG,
    }
    return bytes(data), meta


def build_mosaic(camera_ids: Any = None, **kwargs: Any) -> bytes:
    """Return mosaic grid JPEG bytes for the given cameras."""
    data, _meta = build_mosaic_with_meta(camera_ids, **kwargs)
    return data


def get_info(camera_ids: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Describe the mosaic layout without rendering pixels."""
    ids = resolve_camera_ids(camera_ids, **kwargs)
    if not ids:
        ids = default_camera_ids()
    tw = _coerce_tile(kwargs.get("tile_width", kwargs.get("width", DEFAULT_TILE_WIDTH)), DEFAULT_TILE_WIDTH)
    th = _coerce_tile(kwargs.get("tile_height", kwargs.get("height", DEFAULT_TILE_HEIGHT)), DEFAULT_TILE_HEIGHT)
    ncols, nrows = grid_dimensions(len(ids), cols=kwargs.get("cols"))
    return {
        "camera_ids": list(ids),
        "cameras": list(ids),
        "count": len(ids),
        "cols": ncols,
        "rows": nrows,
        "grid": "{}x{}".format(ncols, nrows),
        "tile_width": tw,
        "tile_height": th,
        "width": ncols * tw,
        "height": nrows * th,
        "content_type": CONTENT_TYPE_JPEG,
    }


# ---- Compatibility aliases (repo convention: many names, same behaviour) ----
get_mosaic = build_mosaic
fetch_mosaic = build_mosaic
mosaic = build_mosaic
grid_image = build_mosaic
mosaic_image = build_mosaic
combined_thumbnail = build_mosaic
grid = build_mosaic
build_grid = build_mosaic

get_mosaic_with_meta = build_mosaic_with_meta
mosaic_with_meta = build_mosaic_with_meta

info = get_info
mosaic_info = get_info


__all__ = [
    "DEFAULT_TILE_WIDTH",
    "DEFAULT_TILE_HEIGHT",
    "DEFAULT_QUALITY",
    "CONTENT_TYPE_JPEG",
    "resolve_camera_ids",
    "default_camera_ids",
    "grid_dimensions",
    "build_mosaic",
    "build_mosaic_with_meta",
    "get_info",
]
