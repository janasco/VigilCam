"""Animated heatmap GIF renderer for VigilCam.

Companion to :mod:`app.services.heatmap_image` (which renders a single
static PNG). This module renders the same heatmap aggregation data as an
animated ``image/gif`` that sweeps across the 24 hour columns so the UI
can show activity building up over the day.

Pure-Python PIL renderer. Never raises on bad input: returns a valid
(possibly empty) GIF.
"""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional, Tuple

CONTENT_TYPE_GIF = "image/gif"

DEFAULT_CELL_WIDTH = 28
DEFAULT_CELL_HEIGHT = 26
MIN_CELL = 8
MAX_CELL = 96
LABEL_WIDTH = 88
HEADER_HEIGHT = 28
FOOTER_HEIGHT = 18
MAX_CAMERAS = 64
MAX_HOURS = 24

DEFAULT_FRAMES = 8
MIN_FRAMES = 2
MAX_FRAMES = 24

DEFAULT_FPS = 4.0
MIN_FPS = 1.0
MAX_FPS = 15.0
DEFAULT_DURATION_MS = 180

# Minimal 1x1 transparent GIF (GIF89a) used only if PIL is unavailable.
_MINIMAL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00"
    b"\x01\x00\x00\x02\x02\x44\x01\x00\x3b"
)


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        iv = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, iv))


def _clamp_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        fv = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, fv))


def _value_to_color(value: int, vmax: int) -> Tuple[int, int, int]:
    """Blue (cold) -> yellow -> red (hot) gradient (mirrors heatmap_image)."""
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


def _dim(color: Tuple[int, int, int], factor: float = 0.25) -> Tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * factor))) for c in color)  # type: ignore[return-value]


def extract_matrix(data: Any) -> Tuple[List[str], List[List[int]]]:
    """Return (camera_labels, rows) where each row has 24 hourly counts.

    Delegates to :mod:`app.services.heatmap_image` so both renderers share
    the exact same matrix semantics; falls back to a local zero matrix if
    that module is unavailable.
    """
    try:
        from app.services import heatmap_image as _img

        return _img.extract_matrix(data)
    except Exception:  # noqa: BLE001
        pass
    if isinstance(data, dict):
        for key in ("per_camera_per_hour", "camera_hour_matrix", "matrix", "heatmap"):
            mat = data.get(key)
            if isinstance(mat, dict) and mat:
                labels = sorted(mat.keys(), key=lambda k: str(k))[:MAX_CAMERAS]
                rows: List[List[int]] = []
                for lab in labels:
                    cell = mat.get(lab)
                    row = []
                    for h in range(24):
                        try:
                            v = cell.get(str(h), cell.get(h, 0)) if isinstance(cell, dict) else 0
                            row.append(max(0, int(v)))
                        except (TypeError, ValueError, AttributeError):
                            row.append(0)
                    rows.append(row)
                if rows:
                    return ([str(k) for k in labels], rows)
    if isinstance(data, dict):
        row = []
        per_hour = data.get("per_hour") or data.get("per_hour_counts") or {}
        if isinstance(per_hour, dict):
            for h in range(24):
                try:
                    row.append(max(0, int(per_hour.get(str(h), 0))))
                except (TypeError, ValueError):
                    row.append(0)
        else:
            row = [0] * 24
        return (["all"], [row])
    return (["all"], [[0] * 24])


def _coerce_frames(value: Any, kwargs: Dict[str, Any]) -> int:
    if value is None:
        for key in ("frames", "frame_count", "frameCount", "num_frames", "n_frames", "count", "nframes"):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    if value is None:
        return DEFAULT_FRAMES
    return _clamp_int(value, DEFAULT_FRAMES, MIN_FRAMES, MAX_FRAMES)


def _coerce_fps(value: Any, kwargs: Dict[str, Any]) -> float:
    if value is None:
        for key in ("fps", "frame_rate", "frameRate", "framerate", "rate", "speed"):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    if value is None:
        return DEFAULT_FPS
    return _clamp_float(value, DEFAULT_FPS, MIN_FPS, MAX_FPS)


def _frame_duration_ms(fps: float, kwargs: Dict[str, Any]) -> int:
    for key in ("duration_ms", "durationMs", "frame_duration_ms", "frame_duration", "delay", "delay_ms"):
        if kwargs.get(key) is not None:
            try:
                ms = int(float(kwargs.get(key)))
                return max(50, min(2000, ms))
            except (TypeError, ValueError):
                pass
    # Total-duration hint (seconds or ms) overrides per-frame fps derivation.
    for key in ("duration", "duration_seconds", "durationSeconds", "seconds", "total_ms", "total_duration_ms"):
        if kwargs.get(key) is not None:
            try:
                raw = float(kwargs.get(key))
            except (TypeError, ValueError):
                continue
            n_hint = kwargs.get("_frames_hint")
            try:
                n = max(1, int(n_hint)) if n_hint else DEFAULT_FRAMES
            except (TypeError, ValueError):
                n = DEFAULT_FRAMES
            # Heuristic: values > 30 are treated as milliseconds, else seconds.
            total_ms = int(raw * 1000.0) if raw <= 60 else int(raw)
            return max(50, min(2000, total_ms // max(1, n)))
    try:
        return max(50, int(round(1000.0 / max(0.5, float(fps)))))
    except (TypeError, ValueError):
        return DEFAULT_DURATION_MS


def _render_frame(
    draw_img_factory: Any,
    labels: List[str],
    rows: List[List[int]],
    vmax: int,
    revealed: int,
    frame_idx: int,
    n_frames: int,
    img_w: int,
    img_h: int,
    cw: int,
    ch: int,
    title: str,
) -> Any:
    """Build one PIL frame; hours >= revealed are dimmed (not-yet-swept)."""
    from PIL import Image, ImageDraw  # type: ignore

    img = Image.new("RGB", (img_w, img_h), color=(14, 16, 24))
    draw = ImageDraw.Draw(img)
    draw.text((8, 6), str(title)[:64], fill=(235, 235, 245))
    for h in range(24):
        if h % 3 == 0 or cw >= 22:
            x = LABEL_WIDTH + h * cw + 2
            # Highlight the current sweep head hour.
            fill = (255, 220, 120) if h == min(23, revealed - 1) else (160, 170, 190)
            draw.text((x, HEADER_HEIGHT - 14), str(h), fill=fill)
    for ri, lab in enumerate(labels):
        y0 = HEADER_HEIGHT + ri * ch
        draw.text((8, y0 + max(0, ch // 2 - 6)), str(lab)[:10], fill=(200, 205, 220))
        for h in range(24):
            x0 = LABEL_WIDTH + h * cw
            color = _value_to_color(rows[ri][h], vmax)
            if h >= revealed:
                color = _dim(color)
                outline = (30, 34, 48)
            elif h == revealed - 1:
                outline = (255, 220, 120)
            else:
                outline = (10, 12, 20)
            draw.rectangle([x0, y0, x0 + cw - 1, y0 + ch - 1], fill=color, outline=outline)
    # Footer: max/total plus sweep progress bar.
    total = sum(sum(r) for r in rows)
    draw.text((8, img_h - 14), "max={} total={}  t={}/24".format(vmax, total, min(24, revealed))[:64], fill=(140, 150, 170))
    # Progress bar along the bottom-right.
    bar_x0 = LABEL_WIDTH
    bar_x1 = img_w - 12
    bar_y = img_h - 6
    try:
        draw.rectangle([bar_x0, bar_y, bar_x1, bar_y + 3], fill=(30, 34, 48))
        frac = min(1.0, max(0.0, float(revealed) / 24.0))
        fill_x = int(bar_x0 + (bar_x1 - bar_x0) * frac)
        if fill_x > bar_x0:
            draw.rectangle([bar_x0, bar_y, fill_x, bar_y + 3], fill=(255, 200, 90))
        # Moving sweep line so even all-zero heatmaps animate visibly.
        sweep_x = int(bar_x0 + (bar_x1 - bar_x0) * (float(frame_idx + 1) / max(1, n_frames)))
        draw.rectangle([sweep_x - 1, HEADER_HEIGHT, sweep_x + 1, img_h - FOOTER_HEIGHT], fill=(255, 220, 120))
    except Exception:  # noqa: BLE001
        pass
    void = draw_img_factory  # keep signature stable for future factories
    return img


def render_heatmap_gif(
    data: Any = None,
    frames: Any = None,
    fps: Any = None,
    cell_width: Any = None,
    cell_height: Any = None,
    width: Any = None,
    height: Any = None,
    title: Any = None,
    **kwargs: Any,
) -> bytes:
    """Render heatmap aggregation ``data`` to animated GIF bytes.

    The animation sweeps hour 0 -> 23 across ``frames`` frames. Never
    raises on bad input: returns a valid (possibly empty) GIF.
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
    if frames is None:
        for alias in ("n_frames", "frame_count", "frameCount", "num_frames", "count", "nframes"):
            if kwargs.get(alias) is not None:
                frames = kwargs.get(alias)
                break
    if fps is None:
        for alias in ("frame_rate", "frameRate", "framerate", "rate", "speed"):
            if kwargs.get(alias) is not None:
                fps = kwargs.get(alias)
                break

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
            _hint_total_h = int(height)
        except (TypeError, ValueError):
            _hint_total_h = None  # type: ignore[assignment]
    else:
        _hint_total_h = None  # type: ignore[assignment]

    n_frames = _coerce_frames(frames, kwargs)
    fps_f = _coerce_fps(fps, kwargs)
    kwargs["_frames_hint"] = n_frames
    frame_ms = _frame_duration_ms(fps_f, kwargs)

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

    if _hint_total_h is not None:
        try:
            avail = int(_hint_total_h) - HEADER_HEIGHT - FOOTER_HEIGHT
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
        from PIL import Image  # noqa: F401  # type: ignore
    except Exception:  # noqa: BLE001
        return bytes(_MINIMAL_GIF)

    try:
        frames_list = []
        for i in range(n_frames):
            revealed = min(24, max(1, int(round(24.0 * float(i + 1) / float(n_frames)))))
            frames_list.append(
                _render_frame(None, labels, rows, vmax, revealed, i, n_frames, img_w, img_h, cw, ch, str(title))
            )
        buf = io.BytesIO()
        frames_list[0].save(
            buf,
            format="GIF",
            save_all=True,
            append_images=frames_list[1:],
            duration=frame_ms,
            loop=0,
            optimize=False,
        )
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return bytes(_MINIMAL_GIF)


def render_from_events(events: Any = None, *args: Any, **kwargs: Any) -> bytes:
    """Aggregate ``events`` via heatmap service then render animated GIF."""
    from app.services import heatmap as _svc

    data = _svc.build_heatmap(events or [], *args, **kwargs)
    return render_heatmap_gif(
        data,
        frames=kwargs.get("frames"),
        fps=kwargs.get("fps"),
        cell_width=kwargs.get("cell_width"),
        cell_height=kwargs.get("cell_height"),
        width=kwargs.get("width"),
        height=kwargs.get("height"),
        title=kwargs.get("title"),
    )


async def arender_from_events(events: Any = None, *args: Any, **kwargs: Any) -> bytes:
    from app.services import heatmap as _svc

    data = await _svc.get_heatmap(events, *args, **kwargs)
    return render_heatmap_gif(data)


class HeatmapAnimService:
    """Object-oriented facade over :func:`render_heatmap_gif`."""

    def render(self, data: Any = None, **kwargs: Any) -> bytes:
        return render_heatmap_gif(data, **kwargs)

    def generate(self, data: Any = None, **kwargs: Any) -> bytes:
        return render_heatmap_gif(data, **kwargs)

    def from_events(self, events: Any = None, *args: Any, **kwargs: Any) -> bytes:
        return render_from_events(events, *args, **kwargs)

    def extract(self, data: Any = None) -> Tuple[List[str], List[List[int]]]:
        return extract_matrix(data)


# Compatibility aliases (repo convention: many names, same behaviour).
build_gif = render_heatmap_gif
to_gif_bytes = render_heatmap_gif
heatmap_to_gif = render_heatmap_gif
render_gif = render_heatmap_gif
render = render_heatmap_gif
render_image = render_heatmap_gif
render_animation = render_heatmap_gif
render_animated = render_heatmap_gif
build_heatmap_gif = render_heatmap_gif
build_image = render_heatmap_gif
build_animation = render_heatmap_gif
heatmap_gif = render_heatmap_gif
heatmap_anim = render_heatmap_gif
heatmap_animation = render_heatmap_gif
to_gif = render_heatmap_gif
get_gif = render_heatmap_gif
generate_gif = render_heatmap_gif
generate_heatmap_gif = render_heatmap_gif
generate_animation = render_heatmap_gif
get_heatmap_gif = render_heatmap_gif

HeatmapAnimRenderer = HeatmapAnimService
HeatmapGifService = HeatmapAnimService
HeatmapAnimationService = HeatmapAnimService
heatmap_anim_service = HeatmapAnimService()
service = heatmap_anim_service

__all__ = [
    "CONTENT_TYPE_GIF",
    "extract_matrix",
    "render_heatmap_gif",
    "render_from_events",
    "arender_from_events",
    "HeatmapAnimService",
    "heatmap_anim_service",
]
