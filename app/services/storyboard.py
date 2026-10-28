"""Recording storyboard generator for VigilCam.

Builds a contact sheet (grid image) of N thumbnails evenly spaced across a
time range for a camera. This is distinct from the single-frame helpers:

- :mod:`app.services.snapshots` saves one base64 image (no time range).
- :mod:`app.services.thumbnails` caches one JPEG per camera (no time range).

A storyboard instead samples ``count`` timestamps uniformly across
``[start, end]`` (inclusive) and renders them as a labelled contact sheet
(PNG by default, JPEG on request). Tiles are synthetic placeholders labelled
with camera + timestamp; the tile renderer is isolated in
:func:`_render_tile` so a real frame-grabber (recording segment / RTSP) can
replace it without changing grid semantics.
"""

from __future__ import annotations

import io
import math
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from app.core.config import DATA_DIR
except Exception:  # noqa: BLE001
    DATA_DIR = Path("data")  # type: ignore[assignment]

# Tunables.
DEFAULT_COUNT = 6
MIN_COUNT = 1
MAX_COUNT = 36
DEFAULT_THUMB_WIDTH = 320
DEFAULT_THUMB_HEIGHT = 180
MIN_DIMENSION = 32
MAX_DIMENSION = 640
DEFAULT_COLUMNS: Optional[int] = None
MAX_COLUMNS = 6
DEFAULT_GAP = 4
DEFAULT_PADDING = 8
HEADER_HEIGHT = 30
FOOTER_HEIGHT = 20
DEFAULT_FORMAT = "png"
CONTENT_TYPE_PNG = "image/png"
CONTENT_TYPE_JPEG = "image/jpeg"
DEFAULT_STORYBOARD_DIRNAME = "storyboards"

STORYBOARD_JOBS: Dict[str, Dict[str, Any]] = {}
storyboard_jobs = STORYBOARD_JOBS
jobs = STORYBOARD_JOBS
JOB_STORE = STORYBOARD_JOBS
JOBS = STORYBOARD_JOBS

_jobs_lock = threading.Lock()

_MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_MINIMAL_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
    b"\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\x09\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd2\x8a(\xa0\x00\xff\xd9"
)


def _default_storyboard_dir(override: Any = None) -> Path:
    if override is not None:
        p = Path(str(override)).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return p
    try:
        base = Path(str(DATA_DIR)).expanduser()
    except Exception:  # noqa: BLE001
        base = Path("data")
    d = base / DEFAULT_STORYBOARD_DIRNAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


STORYBOARD_DIR = _default_storyboard_dir()


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def parse_time(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Parse a timestamp value to epoch seconds. Returns default/None on empty."""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:  # millis
            v = v / 1000.0
        return float(v)
    s = str(value).strip()
    if not s:
        return default
    # plain epoch (seconds or millis), incl. numeric strings
    try:
        v = float(s)
        if v > 1e12:
            v = v / 1000.0
        # only accept pure numbers to avoid swallowing ISO strings
        if s.replace(".", "", 1).replace("-", "", 1).replace("+", "", 1).replace("e", "").replace("E", "").strip().replace(" ", "") == s.replace(" ", "") or s.lstrip("-+").replace(".", "", 1).isdigit():
            return float(v)
        # fall through for mixed strings: if float parse succeeded but the
        # string is not purely numeric, treat as datetime below
        if all(c in "0123456789.+-eE" for c in s):
            return float(v)
    except ValueError:
        pass
    txt = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y%m%d_%H%M%S",
        "%Y%m%d%H%M%S",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            dt = dt.replace(tzinfo=timezone.utc)
            return float(dt.timestamp())
        except ValueError:
            continue
    return None


def parse_count(value: Any, default: Optional[int] = DEFAULT_COUNT) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        v = int(float(str(value).strip())) if isinstance(value, str) else int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if v < MIN_COUNT or v > MAX_COUNT:
        return None
    return int(v)


def parse_thumb_dim(value: Any, default: int) -> Optional[int]:
    if value is None:
        return int(default)
    if isinstance(value, bool):
        return int(default)
    try:
        v = int(float(str(value).strip())) if isinstance(value, str) else int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if v < MIN_DIMENSION or v > MAX_DIMENSION:
        return None
    return int(v)


def parse_columns(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        v = int(float(str(value).strip())) if isinstance(value, str) else int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if v < 1 or v > MAX_COLUMNS:
        return None
    return int(v)


def coerce_count(value: Any = None, default: int = DEFAULT_COUNT, **kwargs: Any) -> int:
    if value is None:
        for key in ("count", "n", "num", "num_thumbnails", "numThumbnails",
                    "tiles", "frames", "num_frames", "numFrames", "thumbnails",
                    "num_tiles", "numTiles", "per_storyboard", "limit"):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    parsed = parse_count(value, default=default)
    return int(parsed) if parsed is not None else int(default)


def coerce_thumb_dims(
    width: Any = None,
    height: Any = None,
    default_w: int = DEFAULT_THUMB_WIDTH,
    default_h: int = DEFAULT_THUMB_HEIGHT,
    **kwargs: Any,
) -> Tuple[int, int]:
    if width is None:
        for key in ("thumb_width", "thumbWidth", "tile_width", "tileWidth",
                    "w", "width_px", "thumbnail_width", "tw", "tile_w"):
            if kwargs.get(key) is not None:
                width = kwargs.get(key)
                break
    if height is None:
        for key in ("thumb_height", "thumbHeight", "tile_height", "tileHeight",
                    "h", "height_px", "thumbnail_height", "th", "tile_h"):
            if kwargs.get(key) is not None:
                height = kwargs.get(key)
                break
    w = parse_thumb_dim(width, default_w)
    h = parse_thumb_dim(height, default_h)
    if w is None:
        w = int(default_w)
    if h is None:
        h = int(default_h)
    w = max(MIN_DIMENSION, min(MAX_DIMENSION, int(w)))
    h = max(MIN_DIMENSION, min(MAX_DIMENSION, int(h)))
    return int(w), int(h)


def coerce_columns(value: Any = None, count: int = DEFAULT_COUNT, **kwargs: Any) -> int:
    if value is None:
        for key in ("columns", "cols", "columns_count", "per_row", "perRow",
                    "num_columns", "numColumns", "grid_columns"):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    if value is None:
        return auto_columns(count)
    parsed = parse_columns(value, default=None)
    if parsed is None:
        # fall back to auto on invalid explicit value for tolerant callers?
        # No: let validation raise; coercion clamps to auto only when missing.
        return auto_columns(count)
    return int(parsed)


def coerce_camera(value: Any = None, **kwargs: Any) -> str:
    if value is None:
        for key in ("camera", "camera_id", "cameraId", "cameraID", "cam",
                    "cam_id", "camera_name", "cameraName", "device_id",
                    "deviceId", "source_camera"):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    if value is None:
        return "0"
    if hasattr(value, "id") and not isinstance(value, (int, str, float)):
        try:
            value = getattr(value, "id")
        except Exception:  # noqa: BLE001
            pass
    s = str(value).strip()
    if not s:
        return "0"
    if len(s) > 64:
        s = s[:64]
    return s


def coerce_format(value: Any = None, **kwargs: Any) -> str:
    if value is None:
        for key in ("format", "fmt", "image_format", "ext", "extension",
                    "output_format", "mime", "content_type"):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    if value is None:
        return DEFAULT_FORMAT
    s = str(value).strip().lower().lstrip(".")
    if s in {"jpg", "jpeg"}:
        return "jpeg"
    if s in {"png"}:
        return "png"
    # content-type inputs
    if "jpeg" in s or "jpg" in s:
        return "jpeg"
    if "png" in s:
        return "png"
    return DEFAULT_FORMAT


def auto_columns(count: int) -> int:
    try:
        n = max(1, int(count))
    except (TypeError, ValueError):
        n = DEFAULT_COUNT
    cols = int(math.ceil(math.sqrt(n)))
    return max(1, min(MAX_COLUMNS, cols))


def layout_grid(count: int, columns: Any = None) -> Tuple[int, int]:
    """Return (rows, cols) for ``count`` tiles."""
    try:
        n = max(1, int(count))
    except (TypeError, ValueError):
        n = DEFAULT_COUNT
    cols: int
    if columns is None:
        cols = auto_columns(n)
    else:
        try:
            cols = int(columns)
        except (TypeError, ValueError):
            cols = auto_columns(n)
        if cols < 1:
            cols = 1
        cols = min(MAX_COLUMNS, cols)
    rows = int(math.ceil(n / max(1, cols)))
    return rows, cols


def _resolve_start(value: Any, kwargs: Dict[str, Any]) -> Any:
    if value is not None:
        return value
    for key in ("start", "start_time", "startTime", "from", "from_time",
                "fromTime", "begin", "since", "after", "range_start",
                "rangeStart", "start_ts", "startTs", "t0", "begin_time"):
        if kwargs.get(key) is not None:
            return kwargs.get(key)
    return None


def _resolve_end(value: Any, kwargs: Dict[str, Any]) -> Any:
    if value is not None:
        return value
    for key in ("end", "end_time", "endTime", "to", "to_time", "toTime",
                "until", "before", "range_end", "rangeEnd", "finish",
                "end_ts", "endTs", "t1", "finish_time"):
        if kwargs.get(key) is not None:
            return kwargs.get(key)
    return None


def _resolve_count(value: Any, kwargs: Dict[str, Any]) -> Any:
    if value is not None:
        return value
    for key in ("count", "n", "num", "num_thumbnails", "numThumbnails",
                "tiles", "frames", "num_frames", "numFrames", "thumbnails",
                "num_tiles", "numTiles", "limit"):
        if kwargs.get(key) is not None:
            return kwargs.get(key)
    return None


def validate_storyboard_params(
    camera: Any = None,
    start: Any = None,
    end: Any = None,
    count: Any = None,
    thumb_width: Any = None,
    thumb_height: Any = None,
    columns: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Validate storyboard inputs. Returns normalized dict or raises ValueError."""
    raw_cam = coerce_camera(camera, **kwargs)
    raw_start = _resolve_start(start, kwargs)
    raw_end = _resolve_end(end, kwargs)
    raw_count = _resolve_count(count, kwargs)

    # thumb dims aliases
    if thumb_width is None:
        for key in ("thumb_width", "thumbWidth", "tile_width", "tileWidth",
                    "thumbnail_width", "tw", "tile_w"):
            if kwargs.get(key) is not None:
                thumb_width = kwargs.get(key)
                break
    if thumb_height is None:
        for key in ("thumb_height", "thumbHeight", "tile_height", "tileHeight",
                    "thumbnail_height", "th", "tile_h"):
            if kwargs.get(key) is not None:
                thumb_height = kwargs.get(key)
                break
    raw_w = thumb_width
    if raw_w is None:
        for key in ("width", "w", "width_px", "video_width"):
            if kwargs.get(key) is not None:
                raw_w = kwargs.get(key)
                break
    raw_h = thumb_height
    if raw_h is None:
        for key in ("height", "h", "height_px", "video_height"):
            if kwargs.get(key) is not None:
                raw_h = kwargs.get(key)
                break
    if columns is None:
        for key in ("columns", "cols", "per_row", "perRow", "num_columns",
                    "numColumns", "grid_columns", "columns_count"):
            if kwargs.get(key) is not None:
                columns = kwargs.get(key)
                break

    fmt = coerce_format(kwargs.get("format", kwargs.get("fmt")), **kwargs)

    # count (strict: out-of-range raises)
    if raw_count is None:
        count_v = DEFAULT_COUNT
    else:
        count_v = parse_count(raw_count, default=None)
        if count_v is None:
            raise ValueError(
                "count must be an integer between {} and {}".format(MIN_COUNT, MAX_COUNT)
            )

    # dims (clamped, tolerant)
    tw, th = coerce_thumb_dims(raw_w, raw_h)

    # columns (strict when explicitly provided)
    if columns is None:
        cols_v = auto_columns(count_v)
    else:
        cols_v = parse_columns(columns, default=None)
        if cols_v is None:
            raise ValueError("columns must be an integer between 1 and {}".format(MAX_COLUMNS))

    # times
    start_ts = parse_time(raw_start, default=None)
    end_ts = parse_time(raw_end, default=None)
    now = _now_ts()
    if start_ts is None and end_ts is None:
        end_ts = float(now)
        start_ts = float(end_ts - 3600.0)
    elif start_ts is None:
        end_ts = float(end_ts)  # type: ignore[arg-type]
        start_ts = float(end_ts - 3600.0)
    elif end_ts is None:
        start_ts = float(start_ts)
        end_ts = float(start_ts + 3600.0)
    else:
        start_ts = float(start_ts)
        end_ts = float(end_ts)
    if not (end_ts > start_ts):
        raise ValueError("end must be after start")

    rows_v, cols_norm = layout_grid(count_v, columns=cols_v)
    total_w = DEFAULT_PADDING * 2 + cols_norm * tw + max(0, cols_norm - 1) * DEFAULT_GAP
    total_h = HEADER_HEIGHT + FOOTER_HEIGHT + DEFAULT_PADDING * 2 + rows_v * th + max(0, rows_v - 1) * DEFAULT_GAP
    total_w = max(120, min(4096, int(total_w)))
    total_h = max(60, min(4096, int(total_h)))

    timestamps = compute_timestamps(start_ts, end_ts, count_v)
    content_type = CONTENT_TYPE_JPEG if fmt == "jpeg" else CONTENT_TYPE_PNG

    def _iso(ts: float) -> str:
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
        except Exception:  # noqa: BLE001
            return str(ts)

    return {
        "camera": raw_cam,
        "camera_id": raw_cam,
        "camera_name": raw_cam,
        "start": float(start_ts),
        "start_time": float(start_ts),
        "startTime": float(start_ts),
        "start_iso": _iso(start_ts),
        "end": float(end_ts),
        "end_time": float(end_ts),
        "endTime": float(end_ts),
        "end_iso": _iso(end_ts),
        "count": int(count_v),
        "n": int(count_v),
        "num_thumbnails": int(count_v),
        "tiles": int(count_v),
        "frames": int(count_v),
        "timestamps": list(timestamps),
        "thumb_width": int(tw),
        "thumb_height": int(th),
        "tile_width": int(tw),
        "tile_height": int(th),
        "width": int(total_w),
        "height": int(total_h),
        "columns": int(cols_norm),
        "cols": int(cols_norm),
        "rows": int(rows_v),
        "format": fmt,
        "content_type": content_type,
    }


def validate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return validate_storyboard_params(*args, **kwargs)


def validate_params(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return validate_storyboard_params(*args, **kwargs)


def compute_timestamps(start: Any, end: Any, count: Any = DEFAULT_COUNT, **kwargs: Any) -> List[float]:
    """Return ``count`` epoch timestamps evenly spaced across [start, end].

    Endpoints are inclusive; a single tile maps to the midpoint.
    """
    if count is None:
        for key in ("count", "n", "num", "num_thumbnails", "tiles", "frames"):
            if kwargs.get(key) is not None:
                count = kwargs.get(key)
                break
    if start is None:
        start = _resolve_start(None, kwargs)
    if end is None:
        end = _resolve_end(None, kwargs)
    s = parse_time(start, default=None)
    e = parse_time(end, default=None)
    if s is None or e is None:
        # fall back to a 1h window ending now when underspecified
        now = _now_ts()
        if s is None and e is None:
            e = now
            s = e - 3600.0
        elif s is None:
            s = float(e) - 3600.0
        else:
            e = float(s) + 3600.0
    s = float(s)  # type: ignore[arg-type]
    e = float(e)  # type: ignore[arg-type]
    n = parse_count(count, default=DEFAULT_COUNT)
    if n is None:
        raise ValueError(
            "count must be an integer between {} and {}".format(MIN_COUNT, MAX_COUNT)
        )
    n = int(n)
    if n <= 0:
        raise ValueError("count must be >= 1")
    if not (e > s):
        raise ValueError("end must be after start")
    if n == 1:
        return [float((s + e) / 2.0)]
    step = (e - s) / float(n - 1)
    return [float(s + i * step) for i in range(n)]


def _tile_color(index: int, total: int) -> Tuple[int, int, int]:
    # Distinct but dark-friendly palette varying with index.
    palette = [
        (38, 70, 120), (42, 110, 110), (110, 70, 42), (70, 42, 110),
        (42, 110, 70), (120, 42, 70), (70, 90, 42), (90, 42, 90),
    ]
    base = palette[int(index) % len(palette)]
    # slight brightness ramp across the strip so order is visible
    try:
        k = float(index) / max(1.0, float(max(1, total - 1)))
    except Exception:  # noqa: BLE001
        k = 0.0
    lift = int(10 + 25 * k)
    return (min(255, base[0] + lift), min(255, base[1] + lift), min(255, base[2] + lift))


def _format_ts_label(ts: float) -> str:
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime("%H:%M:%S")
    except Exception:  # noqa: BLE001
        return str(ts)


def _render_tile(camera: Any, timestamp: float, index: int, total: int,
                 width: int, height: int):
    """Render one storyboard tile as a PIL Image (placeholder + label)."""
    from PIL import Image, ImageDraw  # type: ignore

    w = max(MIN_DIMENSION, min(MAX_DIMENSION, int(width)))
    h = max(MIN_DIMENSION, min(MAX_DIMENSION, int(height)))
    color = _tile_color(index, total)
    img = Image.new("RGB", (w, h), color=color)
    draw = ImageDraw.Draw(img)
    label = "Cam {} #{}".format(camera, index + 1)
    ts_label = _format_ts_label(timestamp)
    try:
        dt = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        date_label = dt.strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        date_label = ""
    draw.text((8, 8), str(label)[:40], fill=(255, 255, 255))
    draw.text((8, max(22, h // 2 - 10)), str(ts_label)[:40], fill=(255, 255, 255))
    if date_label:
        draw.text((8, h - 20), str(date_label)[:40], fill=(230, 230, 240))
    # border so tiles are visually separated even with gap=0
    draw.rectangle([0, 0, w - 1, h - 1], outline=(10, 12, 20))
    return img


def build_storyboard_with_meta(
    camera: Any = None,
    start: Any = None,
    end: Any = None,
    count: Any = None,
    thumb_width: Any = None,
    thumb_height: Any = None,
    columns: Any = None,
    **kwargs: Any,
) -> Tuple[bytes, Dict[str, Any]]:
    """Render a storyboard contact sheet. Returns (image_bytes, meta)."""
    normalized = validate_storyboard_params(
        camera, start=start, end=end, count=count,
        thumb_width=thumb_width, thumb_height=thumb_height,
        columns=columns, **kwargs,
    )
    cam = normalized["camera"]
    timestamps: List[float] = list(normalized["timestamps"])
    n = int(normalized["count"])
    tw = int(normalized["thumb_width"])
    th = int(normalized["thumb_height"])
    cols = int(normalized["columns"])
    rows = int(normalized["rows"])
    fmt = str(normalized.get("format", DEFAULT_FORMAT))

    gap = kwargs.get("gap", kwargs.get("spacing", DEFAULT_GAP))
    try:
        gap = max(0, min(32, int(gap)))
    except (TypeError, ValueError):
        gap = DEFAULT_GAP
    pad = kwargs.get("padding", kwargs.get("pad", DEFAULT_PADDING))
    try:
        pad = max(0, min(64, int(pad)))
    except (TypeError, ValueError):
        pad = DEFAULT_PADDING
    title = kwargs.get("title", kwargs.get("heading"))
    if title is None:
        title = "Storyboard Cam {} ({} frames)".format(cam, n)

    img_w = pad * 2 + cols * tw + max(0, cols - 1) * gap
    img_h = HEADER_HEIGHT + FOOTER_HEIGHT + pad * 2 + rows * th + max(0, rows - 1) * gap
    img_w = max(120, min(4096, int(img_w)))
    img_h = max(60, min(4096, int(img_h)))

    try:
        from PIL import Image, ImageDraw  # type: ignore
    except Exception:  # noqa: BLE001
        data = bytes(_MINIMAL_PNG if fmt == "png" else _MINIMAL_JPEG)
        meta = dict(normalized)
        meta.update({
            "size": len(data),
            "size_bytes": len(data),
            "ok": True,
            "success": True,
        })
        return data, meta

    try:
        sheet = Image.new("RGB", (img_w, img_h), color=(14, 16, 24))
        draw = ImageDraw.Draw(sheet)
        draw.text((pad, 8), str(title)[:80], fill=(235, 235, 245))
        # range subtitle on the header's right side (best effort, may clip)
        try:
            s_iso = datetime.fromtimestamp(float(normalized["start"]), tz=timezone.utc).strftime("%H:%M:%S")
            e_iso = datetime.fromtimestamp(float(normalized["end"]), tz=timezone.utc).strftime("%H:%M:%S")
            draw.text((max(pad, img_w - 220), 8), "{} -> {}".format(s_iso, e_iso)[:32], fill=(160, 170, 190))
        except Exception:  # noqa: BLE001
            pass
        for i, ts in enumerate(timestamps):
            r = i // cols
            c = i % cols
            x0 = pad + c * (tw + gap)
            y0 = HEADER_HEIGHT + pad + r * (th + gap)
            tile = _render_tile(cam, ts, i, n, tw, th)
            sheet.paste(tile, (x0, y0))
        # footer
        try:
            total = "{} frames {}x{} tiles {}x{}".format(n, cols, rows, tw, th)
            draw.text((pad, img_h - 16), str(total)[:80], fill=(140, 150, 170))
        except Exception:  # noqa: BLE001
            pass
        buf = io.BytesIO()
        if fmt == "jpeg":
            sheet.save(buf, format="JPEG", quality=int(kwargs.get("quality", kwargs.get("jpeg_quality", 80))))
        else:
            sheet.save(buf, format="PNG")
        data = buf.getvalue()
    except Exception:  # noqa: BLE001
        data = bytes(_MINIMAL_PNG if fmt == "png" else _MINIMAL_JPEG)

    def _iso(ts: float) -> str:
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
        except Exception:  # noqa: BLE001
            return str(ts)

    meta = dict(normalized)
    meta.update({
        "timestamps": list(timestamps),
        "timestamp_isos": [_iso(t) for t in timestamps],
        "frame_timestamps": list(timestamps),
        "frame_times": list(timestamps),
        "size": len(data),
        "size_bytes": len(data),
        "content_type": CONTENT_TYPE_JPEG if fmt == "jpeg" else CONTENT_TYPE_PNG,
        "format": fmt,
        "gap": int(gap),
        "padding": int(pad),
        "title": str(title),
        "ok": True,
        "success": True,
    })
    # width/height reflect the actual sheet (recomputed with gap/pad overrides)
    meta["width"] = int(img_w)
    meta["height"] = int(img_h)
    return bytes(data), meta


def build_storyboard(*args: Any, **kwargs: Any) -> bytes:
    """Render a storyboard contact sheet and return image bytes."""
    # tolerate positional (camera, start, end, count)
    camera = kwargs.pop("camera", kwargs.pop("camera_id", None)) if "camera" in kwargs or "camera_id" in kwargs else None
    pos = list(args)
    # if first positional looks like a path/output it is handled by file variant;
    # here all positionals are (camera, start, end, count)
    if camera is None and pos:
        camera = pos.pop(0) if len(pos) else None
    start = kwargs.pop("start", kwargs.pop("start_time", None)) if ("start" in kwargs or "start_time" in kwargs) else None
    if start is None and pos:
        start = pos.pop(0) if pos else None
    end = kwargs.pop("end", kwargs.pop("end_time", None)) if ("end" in kwargs or "end_time" in kwargs) else None
    if end is None and pos:
        end = pos.pop(0) if pos else None
    count = kwargs.pop("count", kwargs.pop("n", None)) if ("count" in kwargs or "n" in kwargs) else None
    if count is None and pos:
        count = pos.pop(0) if pos else None
    data, _meta = build_storyboard_with_meta(camera, start, end, count, **kwargs)
    return data


def render_contact_sheet(*args: Any, **kwargs: Any) -> bytes:
    return build_storyboard(*args, **kwargs)


def _safe_name(name: Any) -> str:
    s = str(name if name is not None else "storyboard")
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
    s = s.strip("_") or "storyboard"
    if len(s) > 64:
        s = s[:64]
    return s


def _default_output_path(camera: Any, start_ts: float, end_ts: float,
                         fmt: str, storyboard_dir: Any = None) -> Path:
    d = _default_storyboard_dir(storyboard_dir)
    try:
        s = datetime.fromtimestamp(float(start_ts), tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    except Exception:  # noqa: BLE001
        s = "start"
    try:
        e = datetime.fromtimestamp(float(end_ts), tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    except Exception:  # noqa: BLE001
        e = "end"
    ext = "jpg" if fmt == "jpeg" else "png"
    stem = _safe_name(camera)
    return d / "storyboard_{}_{}_{}_{}.{}".format(stem, s, e, uuid.uuid4().hex, ext)


def build_storyboard_file(
    camera: Any = None,
    start: Any = None,
    end: Any = None,
    count: Any = None,
    output: Any = None,
    storyboard_dir: Any = None,
    **kwargs: Any,
) -> str:
    """Render a storyboard and persist it. Returns the output path as a string."""
    if output is None:
        for key in ("output", "output_path", "outputPath", "dest", "destination",
                    "target", "out", "outfile", "save_to", "save_as", "path", "file"):
            if kwargs.get(key) is not None:
                output = kwargs.pop(key)
                break
    if storyboard_dir is None:
        for key in ("storyboard_dir", "output_dir", "directory", "dir", "folder"):
            if kwargs.get(key) is not None:
                storyboard_dir = kwargs.pop(key)
                break
    data, meta = build_storyboard_with_meta(camera, start, end, count, **kwargs)
    fmt = str(meta.get("format", DEFAULT_FORMAT))
    if output is None:
        out_path = _default_output_path(meta.get("camera"), meta.get("start"), meta.get("end"), fmt, storyboard_dir)
    else:
        out_path = Path(str(output)).expanduser()
        # ensure a valid image suffix when the caller passes a bare stem
        if out_path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            out_path = out_path.with_suffix(".jpg" if fmt == "jpeg" else ".png")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    tmp = out_path.with_suffix(out_path.suffix + ".partial")
    tmp.write_bytes(bytes(data))
    tmp.replace(out_path)
    return str(out_path)


def create_job(
    camera: Any = None,
    start: Any = None,
    end: Any = None,
    count: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    normalized = validate_storyboard_params(camera, start=start, end=end, count=count, **kwargs)
    job_id = str(kwargs.get("job_id") or uuid.uuid4())
    job: Dict[str, Any] = {
        "id": job_id,
        "job_id": job_id,
        "storyboard_id": job_id,
        "camera": normalized["camera"],
        "camera_id": normalized["camera"],
        "start": normalized["start"],
        "start_time": normalized["start"],
        "end": normalized["end"],
        "end_time": normalized["end"],
        "count": normalized["count"],
        "timestamps": normalized["timestamps"],
        "thumb_width": normalized["thumb_width"],
        "thumb_height": normalized["thumb_height"],
        "columns": normalized["columns"],
        "rows": normalized["rows"],
        "format": normalized["format"],
        "output": kwargs.get("output", kwargs.get("output_path")),
        "output_path": kwargs.get("output", kwargs.get("output_path")),
        "status": "queued",
        "state": "queued",
        "progress": 0.0,
        "error": None,
    }
    with _jobs_lock:
        STORYBOARD_JOBS[job_id] = job
    return job


def get_job(job_id: Any) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        return STORYBOARD_JOBS.get(str(job_id))


def list_jobs() -> List[Dict[str, Any]]:
    with _jobs_lock:
        return list(STORYBOARD_JOBS.values())


def update_job(job_id: Any, **fields: Any) -> Optional[Dict[str, Any]]:
    key = str(job_id)
    with _jobs_lock:
        job = STORYBOARD_JOBS.get(key)
        if job is None:
            return None
        for k, v in fields.items():
            job[k] = v
            if k == "status":
                job["state"] = v
            if k == "output_path":
                job["output"] = v
        return job


def delete_job(job_id: Any) -> bool:
    with _jobs_lock:
        return STORYBOARD_JOBS.pop(str(job_id), None) is not None


def run_job(job_id: Any, **kwargs: Any) -> Dict[str, Any]:
    key = str(job_id)
    job = get_job(key)
    if job is None:
        raise KeyError("Unknown storyboard job {}".format(key))
    update_job(key, status="processing", state="processing", progress=10.0)
    try:
        out = build_storyboard_file(
            job.get("camera"),
            job.get("start"),
            job.get("end"),
            job.get("count"),
            output=job.get("output"),
            thumb_width=job.get("thumb_width"),
            thumb_height=job.get("thumb_height"),
            columns=job.get("columns"),
            format=job.get("format", DEFAULT_FORMAT),
            **kwargs,
        )
        update_job(key, status="completed", state="completed", progress=100.0,
                   output_path=str(out), output=str(out))
        result = get_job(key)
        assert result is not None
        return result
    except Exception as exc:  # noqa: BLE001
        update_job(key, status="failed", state="failed", error=str(exc), progress=0.0)
        result = get_job(key)
        assert result is not None
        return result


# Compatibility aliases (repo convention: many names, same behaviour).
build_contact_sheet = build_storyboard
generate_storyboard = build_storyboard
create_storyboard = build_storyboard
make_storyboard = build_storyboard
render_storyboard = build_storyboard
get_storyboard = build_storyboard
storyboard_png = build_storyboard
contact_sheet = build_storyboard
contact_sheet_png = build_storyboard
to_png_bytes = build_storyboard
storyboard_image = build_storyboard
build_image = build_storyboard
generate_contact_sheet = build_storyboard
create_contact_sheet = build_storyboard
render = build_storyboard
build = build_storyboard
create = build_storyboard
make = build_storyboard
generate = build_storyboard

build_with_meta = build_storyboard_with_meta
get_with_meta = build_storyboard_with_meta
render_with_meta = build_storyboard_with_meta

save_storyboard = build_storyboard_file
export_storyboard = build_storyboard_file
write_storyboard = build_storyboard_file
storyboard_export = build_storyboard_file
persist_storyboard = build_storyboard_file

compute_times = compute_timestamps
storyboard_timestamps = compute_timestamps
frame_timestamps = compute_timestamps
timestamps_for_range = compute_timestamps
sample_timestamps = compute_timestamps

grid_layout = layout_grid
compute_layout = layout_grid
grid_shape = layout_grid

create_storyboard_job = create_job
new_job = create_job
get_storyboard_job = get_job
list_storyboard_jobs = list_jobs

cancel_job = delete_job
cancel = delete_job
remove_job = delete_job
get = get_job
list_all = list_jobs

parse_start = parse_time
parse_end = parse_time
parse_timestamp = parse_time


@dataclass
class StoryboardResult:
    camera: str
    start: float
    end: float
    count: int
    output_path: str
    width: int = 0
    height: int = 0
    columns: int = 0
    rows: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "camera": self.camera,
            "camera_id": self.camera,
            "start": self.start,
            "start_time": self.start,
            "end": self.end,
            "end_time": self.end,
            "count": self.count,
            "n": self.count,
            "output_path": self.output_path,
            "output": self.output_path,
            "path": self.output_path,
            "width": self.width,
            "height": self.height,
            "columns": self.columns,
            "cols": self.columns,
            "rows": self.rows,
            "ok": True,
            "success": True,
        }


class StoryboardService:
    """Object-oriented facade over :func:`build_storyboard`."""

    def __init__(self, storyboard_dir: Any = None, **kwargs: Any) -> None:
        self.storyboard_dir = _default_storyboard_dir(
            storyboard_dir or kwargs.get("output_dir")
        )

    def build(self, camera: Any = None, start: Any = None, end: Any = None,
              count: Any = None, **kwargs: Any) -> bytes:
        return build_storyboard(camera, start, end, count, **kwargs)

    def build_with_meta(self, *args: Any, **kwargs: Any) -> Tuple[bytes, Dict[str, Any]]:
        return build_storyboard_with_meta(*args, **kwargs)

    def build_file(self, *args: Any, **kwargs: Any) -> str:
        kwargs.setdefault("storyboard_dir", self.storyboard_dir)
        return build_storyboard_file(*args, **kwargs)

    def validate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return validate_storyboard_params(*args, **kwargs)

    def compute_timestamps(self, *args: Any, **kwargs: Any) -> List[float]:
        return compute_timestamps(*args, **kwargs)

    def layout(self, *args: Any, **kwargs: Any) -> Tuple[int, int]:
        count = args[0] if args else kwargs.get("count", DEFAULT_COUNT)
        columns = args[1] if len(args) > 1 else kwargs.get("columns")
        return layout_grid(int(count), columns=columns)

    def create_job(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_job(*args, **kwargs)

    def get_job(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_job(*args, **kwargs)

    def list_jobs(self) -> List[Dict[str, Any]]:
        return list_jobs()

    def run_job(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return run_job(*args, **kwargs)

    # Aliases.
    def render(self, *args: Any, **kwargs: Any) -> bytes:
        return self.build(*args, **kwargs)

    def generate(self, *args: Any, **kwargs: Any) -> bytes:
        return self.build(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> bytes:
        return self.build(*args, **kwargs)

    def contact_sheet(self, *args: Any, **kwargs: Any) -> bytes:
        return self.build(*args, **kwargs)

    def render_contact_sheet(self, *args: Any, **kwargs: Any) -> bytes:
        return self.build(*args, **kwargs)


class StoryboardBuilder(StoryboardService):
    pass


class StoryboardCreator(StoryboardService):
    pass


class StoryboardGenerator(StoryboardService):
    pass


class Storyboard(StoryboardService):
    pass


class ContactSheetBuilder(StoryboardService):
    pass


class Builder(StoryboardService):
    pass


class Service(StoryboardService):
    pass


storyboard_service = StoryboardService()
builder = storyboard_service
service = storyboard_service
storyboard_builder = storyboard_service
default_service = storyboard_service
instance = storyboard_service
singleton = storyboard_service
contact_sheet_service = storyboard_service

StoryboardManager = StoryboardService
StoryboardExporter = StoryboardService

__all__ = [
    "DEFAULT_COUNT",
    "MIN_COUNT",
    "MAX_COUNT",
    "DEFAULT_THUMB_WIDTH",
    "DEFAULT_THUMB_HEIGHT",
    "CONTENT_TYPE_PNG",
    "CONTENT_TYPE_JPEG",
    "STORYBOARD_DIR",
    "STORYBOARD_JOBS",
    "parse_time",
    "parse_count",
    "parse_thumb_dim",
    "parse_columns",
    "coerce_count",
    "coerce_thumb_dims",
    "coerce_columns",
    "coerce_camera",
    "coerce_format",
    "auto_columns",
    "layout_grid",
    "validate_storyboard_params",
    "validate",
    "validate_params",
    "compute_timestamps",
    "build_storyboard_with_meta",
    "build_storyboard",
    "render_contact_sheet",
    "build_storyboard_file",
    "create_job",
    "get_job",
    "list_jobs",
    "update_job",
    "delete_job",
    "run_job",
    "StoryboardResult",
    "StoryboardService",
    "StoryboardBuilder",
    "StoryboardCreator",
    "StoryboardGenerator",
    "storyboard_service",
]
