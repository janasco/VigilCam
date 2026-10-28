"""Heatmap PNG image renderer for VigilCam.

Pure-Python PIL renderer that turns heatmap aggregation data
(see :mod:`app.services.heatmap`) into a PNG image.

The aggregation service returns data only (dicts of per-hour /
per-camera counts); this module is the single place that renders
those counts as pixels. No other heatmap module renders images.
"""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional, Tuple

CONTENT_TYPE_PNG = "image/png"

DEFAULT_CELL_WIDTH = 28
DEFAULT_CELL_HEIGHT = 26
MIN_CELL = 8
MAX_CELL = 96
LABEL_WIDTH = 88
HEADER_HEIGHT = 28
FOOTER_HEIGHT = 18
MAX_CAMERAS = 64
MAX_HOURS = 24

# Minimal 1x1 transparent PNG used only if PIL is unavailable.
_MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        iv = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, iv))


def _value_to_color(value: int, vmax: int) -> Tuple[int, int, int]:
    """Blue (cold) -> yellow -> red (hot) gradient."""
    try:
        v = max(0, int(value))
    except (TypeError, ValueError):
        v = 0
    try:
        m = max(0, int(vmax))
    except (TypeError, ValueError):
        m = 0
    if m <= 0 or v <= 0:
        return (24, 28, 44)
    t = min(1.0, float(v) / float(m))
    # dark-blue -> teal -> yellow -> red
    if t < 0.5:
        k = t / 0.5
        r = int(37 + (40 - 37) * k)
        g = int(90 + (190 - 90) * k)
        b = int(150 + (110 - 150) * k)
    else:
        k = (t - 0.5) / 0.5
        r = int(40 + (220 - 40) * k)
        g = int(190 + (60 - 190) * k)
        b = int(110 + (50 - 110) * k)
    return (r, g, b)


def _hour_count(data: Dict[str, Any], hour: int) -> int:
    for key in ("per_hour", "per_hour_counts", "hour_counts", "by_hour"):
        bucket = data.get(key)
        if isinstance(bucket, dict) and str(hour) in bucket:
            try:
                return max(0, int(bucket[str(hour)]))
            except (TypeError, ValueError):
                pass
    hourly = data.get("hourly") or data.get("hours") or data.get("buckets")
    if isinstance(hourly, (list, tuple)):
        for item in hourly:
            if isinstance(item, dict):
                try:
                    h = int(item.get("hour", item.get("h", -1)))
                except (TypeError, ValueError):
                    continue
                if h == hour:
                    try:
                        return max(0, int(item.get("count", item.get("total", 0))))
                    except (TypeError, ValueError):
                        return 0
            elif isinstance(item, (int, float)) and len(hourly) == 24:
                try:
                    return max(0, int(hourly[hour]))
                except (TypeError, ValueError, IndexError):
                    return 0
    if isinstance(hourly, (list, tuple)) and len(hourly) == 24:
        try:
            return max(0, int(hourly[hour]))
        except (TypeError, ValueError, IndexError):
            return 0
    return 0


def extract_matrix(data: Any) -> Tuple[List[str], List[List[int]]]:
    """Return (camera_labels, rows) where each row has 24 hourly counts."""
    if data is None:
        return (["all"], [[0] * 24])
    if isinstance(data, (list, tuple)) and data and all(
        isinstance(x, (int, float)) for x in data
    ):
        counts = [max(0, int(x)) for x in list(data)[:24]]
        while len(counts) < 24:
            counts.append(0)
        return (["all"], [counts])
    if not isinstance(data, dict):
        return (["all"], [[0] * 24])

    for key in ("per_camera_per_hour", "camera_hour_matrix", "matrix", "heatmap"):
        mat = data.get(key)
        if isinstance(mat, dict) and mat:
            labels = sorted(mat.keys(), key=lambda k: (0, int(k)) if str(k).lstrip("-").isdigit() else (1, str(k)))
            labels = [str(k) for k in labels][:MAX_CAMERAS]
            rows: List[List[int]] = []
            for lab in labels:
                cell = mat.get(lab)
                if not isinstance(cell, dict):
                    rows.append([0] * 24)
                    continue
                row = []
                for h in range(24):
                    v = cell.get(str(h), cell.get(h, 0))
                    try:
                        row.append(max(0, int(v)))
                    except (TypeError, ValueError):
                        row.append(0)
                rows.append(row)
            if rows:
                return (labels, rows)

    cells = data.get("cells")
    if isinstance(cells, (list, tuple)) and cells:
        agg: Dict[str, Dict[int, int]] = {}
        for item in cells:
            if not isinstance(item, dict):
                continue
            cam = item.get("camera_id", item.get("camera", "unknown"))
            try:
                h = int(item.get("hour", -1))
            except (TypeError, ValueError):
                continue
            if h < 0 or h > 23:
                continue
            try:
                c = max(0, int(item.get("count", item.get("total", 0))))
            except (TypeError, ValueError):
                c = 0
            lab = str(cam)
            agg.setdefault(lab, {}).setdefault(h, 0)
            agg[lab][h] = max(agg[lab][h], c)
        if agg:
            labels = sorted(agg.keys(), key=lambda k: (0, int(k)) if str(k).lstrip("-").isdigit() else (1, str(k)))[:MAX_CAMERAS]
            rows = [[int(agg[lab].get(h, 0)) for h in range(24)] for lab in labels]
            return (labels, rows)

    # Single-row fallback from per-hour buckets.
    row = [_hour_count(data, h) for h in range(24)]
    label = "all"
    per_camera = data.get("per_camera") or data.get("camera_counts") or data.get("by_camera")
    if isinstance(per_camera, dict) and len(per_camera) == 1:
        label = str(next(iter(per_camera.keys())))
    return ([label], [row])


def render_heatmap_png(
    data: Any = None,
    cell_width: Any = None,
    cell_height: Any = None,
    width: Any = None,
    height: Any = None,
    title: Any = None,
    **kwargs: Any,
) -> bytes:
    """Render heatmap aggregation ``data`` to PNG bytes.

    Never raises on bad input: returns a valid (possibly empty) PNG.
    """
    if data is None:
        for alias in ("heatmap", "matrix", "heatmap_data", "payload"):
            if kwargs.get(alias) is not None:
                data = kwargs.get(alias)
                break
    if cell_width is None:
        for alias in ("cell_w", "cw", "cellwidth", "tile_width", "tw"):
            if kwargs.get(alias) is not None:
                cell_width = kwargs.get(alias)
                break
    if cell_height is None:
        for alias in ("cell_h", "ch", "cellheight", "tile_height", "th"):
            if kwargs.get(alias) is not None:
                cell_height = kwargs.get(alias)
                break
    # Explicit total width/height derive cell size for the 24-column grid.
    cw = _clamp_int(cell_width if cell_width is not None else DEFAULT_CELL_WIDTH, DEFAULT_CELL_WIDTH, MIN_CELL, MAX_CELL)
    ch = _clamp_int(cell_height if cell_height is not None else DEFAULT_CELL_HEIGHT, DEFAULT_CELL_HEIGHT, MIN_CELL, MAX_CELL)
    if width is not None:
        try:
            total_w = int(width)
            cw = _clamp_int((total_w - LABEL_WIDTH) // MAX_HOURS, DEFAULT_CELL_WIDTH, MIN_CELL, MAX_CELL)
        except (TypeError, ValueError):
            pass
    if height is not None:
        try:
            total_h = int(height)
            # height hint only shrinks/grows cells when row count is known
            _hint = total_h  # applied below after row count known
        except (TypeError, ValueError):
            _hint = None  # type: ignore[assignment]
    else:
        _hint = None  # type: ignore[assignment]

    labels, rows = extract_matrix(data)
    if not rows:
        labels, rows = (["all"], [[0] * 24])
    n_rows = min(len(rows), MAX_CAMERAS)
    labels = list(labels[:n_rows])
    rows = [list(r[:24]) + [0] * max(0, 24 - len(r[:24])) for r in rows[:n_rows]]

    vmax = 0
    for r in rows:
        for v in r:
            if v > vmax:
                vmax = v

    if _hint is not None:
        try:
            avail = int(_hint) - HEADER_HEIGHT - FOOTER_HEIGHT
            if avail > 0 and n_rows > 0:
                ch = _clamp_int(avail // n_rows, ch, MIN_CELL, MAX_CELL)
        except (TypeError, ValueError):
            pass

    img_w = LABEL_WIDTH + 24 * cw + 12
    img_h = HEADER_HEIGHT + n_rows * ch + FOOTER_HEIGHT
    img_w = max(120, min(2048, img_w))
    img_h = max(60, min(2048, img_h))

    if title is None:
        title = kwargs.get("heading", kwargs.get("caption", "Heatmap (hour x camera)"))

    try:
        from PIL import Image, ImageDraw  # type: ignore
    except Exception:  # noqa: BLE001
        return bytes(_MINIMAL_PNG)

    try:
        img = Image.new("RGB", (img_w, img_h), color=(14, 16, 24))
        draw = ImageDraw.Draw(img)
        draw.text((8, 6), str(title)[:64], fill=(235, 235, 245))
        # Hour header labels (every 3h to stay legible on small cells).
        for h in range(24):
            if h % 3 == 0 or cw >= 22:
                x = LABEL_WIDTH + h * cw + 2
                draw.text((x, HEADER_HEIGHT - 14), str(h), fill=(160, 170, 190))
        for ri, lab in enumerate(labels):
            y0 = HEADER_HEIGHT + ri * ch
            draw.text((8, y0 + max(0, ch // 2 - 6)), str(lab)[:10], fill=(200, 205, 220))
            for h in range(24):
                x0 = LABEL_WIDTH + h * cw
                color = _value_to_color(rows[ri][h], vmax)
                draw.rectangle([x0, y0, x0 + cw - 1, y0 + ch - 1], fill=color, outline=(10, 12, 20))
        # Footer with max scale hint.
        draw.text((8, img_h - 14), "max={} total={}".format(vmax, sum(sum(r) for r in rows))[:64], fill=(140, 150, 170))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return bytes(_MINIMAL_PNG)


def render_from_events(events: Any = None, *args: Any, **kwargs: Any) -> bytes:
    """Aggregate ``events`` via heatmap service then render PNG."""
    from app.services import heatmap as _svc

    data = _svc.build_heatmap(events or [], *args, **kwargs)
    return render_heatmap_png(
        data,
        cell_width=kwargs.get("cell_width"),
        cell_height=kwargs.get("cell_height"),
        width=kwargs.get("width"),
        height=kwargs.get("height"),
        title=kwargs.get("title"),
    )


async def arender_from_events(events: Any = None, *args: Any, **kwargs: Any) -> bytes:
    from app.services import heatmap as _svc

    data = await _svc.get_heatmap(events, *args, **kwargs)
    return render_heatmap_png(data)


class HeatmapImageService:
    """Object-oriented facade over :func:`render_heatmap_png`."""

    def render(self, data: Any = None, **kwargs: Any) -> bytes:
        return render_heatmap_png(data, **kwargs)

    def from_events(self, events: Any = None, *args: Any, **kwargs: Any) -> bytes:
        return render_from_events(events, *args, **kwargs)

    def extract(self, data: Any = None) -> Tuple[List[str], List[List[int]]]:
        return extract_matrix(data)


# Compatibility aliases (repo convention: many names, same behaviour).
build_png = render_heatmap_png
to_png_bytes = render_heatmap_png
heatmap_to_png = render_heatmap_png
render_png = render_heatmap_png
render = render_heatmap_png
render_image = render_heatmap_png
build_heatmap_png = render_heatmap_png
build_image = render_heatmap_png
heatmap_png = render_heatmap_png
to_png = render_heatmap_png
get_png = render_heatmap_png
generate_png = render_heatmap_png

HeatmapImageRenderer = HeatmapImageService
HeatmapPngService = HeatmapImageService
heatmap_image_service = HeatmapImageService()
service = heatmap_image_service

__all__ = [
    "CONTENT_TYPE_PNG",
    "extract_matrix",
    "render_heatmap_png",
    "render_from_events",
    "arender_from_events",
    "HeatmapImageService",
    "heatmap_image_service",
]
