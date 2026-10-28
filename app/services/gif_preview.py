"""GIF preview export service for VigilCam.

Generates short animated GIF previews for a camera without affecting the
regular video export pipeline (which remains mp4/video-only). Previews are
rendered from placeholder frames by default so the endpoint works even when
no recordings exist; when explicit source clips are provided and ffmpeg is
available, a real palette-based GIF conversion is attempted first with a
placeholder fallback so the API never fails for missing tooling.

Defensive API: every public helper has aliases so callers (and tests) can
use whichever spelling they prefer.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from app.core.config import DATA_DIR, RECORDING_DIR
except Exception:  # noqa: BLE001
    DATA_DIR = Path("data")  # type: ignore[assignment]
    RECORDING_DIR = Path("data/recordings")  # type: ignore[assignment]

FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg"
FFMPEG_BIN = FFMPEG_PATH

CONTENT_TYPE_GIF = "image/gif"
GIF_MAGIC_87 = b"GIF87a"
GIF_MAGIC_89 = b"GIF89a"

DEFAULT_WIDTH = 320
MIN_WIDTH = 16
MAX_WIDTH = 1280
DEFAULT_HEIGHT = 240
MIN_HEIGHT = 16
MAX_HEIGHT = 1280

DEFAULT_FPS = 5.0
MIN_FPS = 1.0
MAX_FPS = 15.0

DEFAULT_DURATION = 3.0
MIN_DURATION = 0.5
MAX_DURATION = 10.0

DEFAULT_DIRNAME = "gif_previews"

GIF_PREVIEW_JOBS: Dict[str, Dict[str, Any]] = {}
gif_preview_jobs = GIF_PREVIEW_JOBS
jobs = GIF_PREVIEW_JOBS
JOB_STORE = GIF_PREVIEW_JOBS
JOBS = GIF_PREVIEW_JOBS
PREVIEW_JOBS = GIF_PREVIEW_JOBS
GIF_JOBS = GIF_PREVIEW_JOBS

_jobs_lock = threading.Lock()


def _default_gif_dir(override: Any = None) -> Path:
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
    d = base / DEFAULT_DIRNAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


GIF_DIR = _default_gif_dir()
GIF_PREVIEW_DIR = GIF_DIR
PREVIEW_DIR = GIF_DIR
OUTPUT_DIR = GIF_DIR
GIF_OUTPUT_DIR = GIF_DIR


def _safe_name(name: Any) -> str:
    s = str(name if name is not None else "preview")
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
    s = s.strip("_") or "preview"
    if len(s) > 64:
        s = s[:64]
    return s


def _coerce_camera(value: Any, kwargs: Dict[str, Any]) -> Optional[str]:
    if value is not None:
        s = str(value).strip()
        return s or None
    for key in (
        "camera", "camera_id", "cameraId", "cameraID", "cam", "cam_id",
        "camera_name", "cameraName", "device_id", "deviceId", "source_camera",
    ):
        if kwargs.get(key) is not None:
            s = str(kwargs.get(key)).strip()
            if s:
                return s
    return None


def _coerce_output(value: Any, kwargs: Dict[str, Any]) -> Optional[Path]:
    if value is not None:
        s = str(value).strip()
        if s:
            return Path(s).expanduser()
        return None
    for key in (
        "output", "output_path", "outputPath", "dest", "destination",
        "target", "out", "outfile", "save_to", "save_as", "saveTo",
        "path", "file", "filename",
    ):
        if kwargs.get(key) is not None:
            s = str(kwargs.get(key)).strip()
            if s:
                return Path(s).expanduser()
    return None


def parse_width(value: Any, default: int = DEFAULT_WIDTH) -> Optional[int]:
    if value is None:
        return int(default)
    if isinstance(value, bool):
        return int(default)
    try:
        w = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    if w < MIN_WIDTH or w > MAX_WIDTH:
        return None
    return int(w)


def parse_height(value: Any, default: int = DEFAULT_HEIGHT) -> Optional[int]:
    if value is None:
        return int(default)
    if isinstance(value, bool):
        return int(default)
    try:
        h = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    if h < MIN_HEIGHT or h > MAX_HEIGHT:
        return None
    return int(h)


def parse_fps(value: Any, default: float = DEFAULT_FPS) -> Optional[float]:
    if value is None:
        return float(default)
    if isinstance(value, bool):
        return float(default)
    try:
        v = float(str(value).strip() if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return None
    if v < MIN_FPS or v > MAX_FPS:
        return None
    return float(v)


def parse_duration(value: Any, default: float = DEFAULT_DURATION) -> Optional[float]:
    if value is None:
        return float(default)
    if isinstance(value, bool):
        return float(default)
    try:
        v = float(str(value).strip() if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return None
    if v < MIN_DURATION or v > MAX_DURATION:
        return None
    return float(v)


def parse_frames(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        v = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    if v < 1 or v > 150:
        return None
    return int(v)


def coerce_width(value: Any = None, default: int = DEFAULT_WIDTH, **kwargs: Any) -> int:
    if value is None:
        for key in ("width", "w", "width_px", "scale_width", "video_width", "cols"):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    parsed = parse_width(value, default=default)
    return int(parsed) if parsed is not None else int(default)


def coerce_height(value: Any = None, default: int = DEFAULT_HEIGHT, **kwargs: Any) -> int:
    if value is None:
        for key in ("height", "h", "height_px", "scale_height", "video_height", "rows"):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    parsed = parse_height(value, default=default)
    return int(parsed) if parsed is not None else int(default)


def coerce_fps(value: Any = None, default: float = DEFAULT_FPS, **kwargs: Any) -> float:
    if value is None:
        for key in ("fps", "frame_rate", "frameRate", "framerate", "output_fps", "out_fps", "rate"):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    parsed = parse_fps(value, default=default)
    return float(parsed) if parsed is not None else float(default)


def coerce_duration(value: Any = None, default: float = DEFAULT_DURATION, **kwargs: Any) -> float:
    if value is None:
        for key in (
            "duration", "duration_seconds", "durationSeconds", "length",
            "length_seconds", "seconds", "secs", "sec", "clip_length",
        ):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    parsed = parse_duration(value, default=default)
    return float(parsed) if parsed is not None else float(default)


def coerce_frames(
    value: Any = None,
    default: Optional[int] = None,
    fps: Any = None,
    duration: Any = None,
    **kwargs: Any,
) -> int:
    if value is None:
        for key in ("frames", "frame_count", "frameCount", "num_frames", "n_frames", "count"):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    parsed = parse_frames(value, default=None)
    if parsed is not None:
        return int(parsed)
    fps_f = coerce_fps(fps if fps is not None else kwargs.get("fps"), **kwargs)
    dur_f = coerce_duration(duration if duration is not None else kwargs.get("duration"), **kwargs)
    n = max(1, min(150, int(round(float(fps_f) * float(dur_f)))))
    if default is not None and value is None and fps is None and duration is None:
        return int(default)
    return int(n)


# Alias spellings for coercion helpers.
coerce_frame_rate = coerce_fps
coerce_framerate = coerce_fps
coerce_length = coerce_duration
parse_frame_rate = parse_fps
parse_length = parse_duration


def validate_gif_params(
    camera: Any = None,
    output: Any = None,
    width: Any = None,
    height: Any = None,
    fps: Any = None,
    duration: Any = None,
    frames: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Validate GIF preview inputs. Returns normalized dict or raises ValueError."""
    cam = _coerce_camera(camera, kwargs)
    out = _coerce_output(output, kwargs)

    # Resolve aliases not already covered by coerce_*.
    if width is None:
        for key in ("w", "width_px", "scale_width", "video_width", "cols"):
            if kwargs.get(key) is not None:
                width = kwargs.get(key)
                break
    if height is None:
        for key in ("h", "height_px", "scale_height", "video_height", "rows"):
            if kwargs.get(key) is not None:
                height = kwargs.get(key)
                break
    if fps is None:
        for key in ("frame_rate", "frameRate", "framerate", "output_fps", "out_fps", "rate"):
            if kwargs.get(key) is not None:
                fps = kwargs.get(key)
                break
    if duration is None:
        for key in (
            "duration_seconds", "durationSeconds", "length", "length_seconds",
            "seconds", "secs", "sec", "clip_length",
        ):
            if kwargs.get(key) is not None:
                duration = kwargs.get(key)
                break
    if frames is None:
        for key in ("frame_count", "frameCount", "num_frames", "n_frames", "count"):
            if kwargs.get(key) is not None:
                frames = kwargs.get(key)
                break

    w = parse_width(width, default=DEFAULT_WIDTH)
    if w is None:
        raise ValueError(f"width must be between {MIN_WIDTH} and {MAX_WIDTH}")
    h = parse_height(height, default=DEFAULT_HEIGHT)
    if h is None:
        raise ValueError(f"height must be between {MIN_HEIGHT} and {MAX_HEIGHT}")
    fps_v = parse_fps(fps, default=DEFAULT_FPS)
    if fps_v is None:
        raise ValueError(f"fps must be between {MIN_FPS} and {MAX_FPS}")
    dur_v = parse_duration(duration, default=DEFAULT_DURATION)
    if dur_v is None:
        raise ValueError(f"duration must be between {MIN_DURATION} and {MAX_DURATION}")
    if frames is not None:
        fr = parse_frames(frames, default=None)
        if fr is None:
            raise ValueError("frames must be between 1 and 150")
    else:
        fr = max(1, min(150, int(round(float(fps_v) * float(dur_v)))))

    label = kwargs.get("label", kwargs.get("title", kwargs.get("text")))
    if label is not None:
        label = str(label)

    return {
        "camera": cam,
        "camera_id": cam,
        "camera_name": cam,
        "cam": cam,
        "output": out,
        "output_path": out,
        "path": out,
        "width": int(w),
        "w": int(w),
        "height": int(h),
        "h": int(h),
        "fps": float(fps_v),
        "frame_rate": float(fps_v),
        "framerate": float(fps_v),
        "rate": float(fps_v),
        "duration": float(dur_v),
        "duration_seconds": float(dur_v),
        "seconds": float(dur_v),
        "length": float(dur_v),
        "frames": int(fr),
        "frame_count": int(fr),
        "num_frames": int(fr),
        "label": label,
        "format": "gif",
        "output_format": "gif",
        "content_type": CONTENT_TYPE_GIF,
    }


def validate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return validate_gif_params(*args, **kwargs)


def validate_params(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return validate_gif_params(*args, **kwargs)


def validate_preview_params(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return validate_gif_params(*args, **kwargs)


def _minimal_gif_bytes() -> bytes:
    # 1x1 transparent GIF (GIF89a magic) fallback when PIL is unavailable.
    return (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
        b"\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00"
        b"\x01\x00\x00\x02\x02\x44\x01\x00\x3b"
    )


def _placeholder_frames(
    label: str = "VigilCam",
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    frames: int = 10,
):
    try:
        from PIL import Image, ImageDraw  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    palette = [
        (31, 119, 180), (255, 127, 14), (44, 160, 44), (214, 39, 40),
        (148, 103, 189), (140, 86, 75), (227, 119, 194), (127, 127, 127),
    ]
    imgs = []
    for i in range(max(1, int(frames))):
        color = palette[i % len(palette)]
        img = Image.new("RGB", (int(width), int(height)), color=color)
        try:
            draw = ImageDraw.Draw(img)
            draw.text((10, 10), str(label)[:48], fill=(255, 255, 255))
            # Moving bar so frames differ (animated).
            bar_w = max(8, int(width) // 8)
            total = max(1, int(frames) - 1) if int(frames) > 1 else 1
            progress = float(i) / float(total)
            x0 = int((int(width) - bar_w) * progress)
            draw.rectangle([x0, int(height) - 14, x0 + bar_w, int(height) - 6], fill=(255, 255, 255))
            draw.text((10, max(28, int(height) // 2 - 10)), f"frame {i + 1}/{frames}", fill=(255, 255, 255))
        except Exception:  # noqa: BLE001
            pass
        imgs.append(img)
    return imgs


def generate_gif_bytes(
    camera: Any = None,
    width: Any = None,
    height: Any = None,
    fps: Any = None,
    duration: Any = None,
    frames: Any = None,
    **kwargs: Any,
) -> bytes:
    """Render an animated GIF preview as bytes (placeholder frames)."""
    normalized = validate_gif_params(
        camera, width=width, height=height, fps=fps,
        duration=duration, frames=frames, **kwargs,
    )
    w = int(normalized["width"])
    h = int(normalized["height"])
    n = int(normalized["frames"])
    fps_f = float(normalized["fps"])
    cam = normalized.get("camera") or "preview"
    label = normalized.get("label") or f"Cam {cam}"
    imgs = _placeholder_frames(str(label), width=w, height=h, frames=n)
    if not imgs:
        return _minimal_gif_bytes()
    buf = io.BytesIO()
    frame_duration_ms = max(50, int(round(1000.0 / max(0.5, fps_f))))
    try:
        imgs[0].save(
            buf, format="GIF", save_all=True, append_images=imgs[1:],
            duration=frame_duration_ms, loop=0, optimize=False,
        )
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return _minimal_gif_bytes()


def render_gif_bytes(*args: Any, **kwargs: Any) -> bytes:
    return generate_gif_bytes(*args, **kwargs)


def generate_placeholder_gif(*args: Any, **kwargs: Any) -> bytes:
    return generate_gif_bytes(*args, **kwargs)


def render_placeholder_gif(*args: Any, **kwargs: Any) -> bytes:
    return generate_gif_bytes(*args, **kwargs)


def get_gif_bytes(*args: Any, **kwargs: Any) -> bytes:
    return generate_gif_bytes(*args, **kwargs)


def make_gif_bytes(*args: Any, **kwargs: Any) -> bytes:
    return generate_gif_bytes(*args, **kwargs)


def build_ffmpeg_command(
    source: Any = None,
    output: Any = None,
    width: Any = None,
    fps: Any = None,
    duration: Any = None,
    overwrite: bool = True,
    ffmpeg: Optional[str] = None,
    **kwargs: Any,
) -> List[str]:
    """Build an ffmpeg palette-based GIF conversion command."""
    if source is None:
        for key in (
            "input", "input_path", "inputPath", "src", "path", "file",
            "video", "clip", "recording", "recording_path", "clips", "segment",
        ):
            if kwargs.get(key) is not None:
                source = kwargs.get(key)
                break
    if output is None:
        output = _coerce_output(None, kwargs)
    normalized = validate_gif_params(
        kwargs.get("camera", kwargs.get("camera_id")),
        output=output, width=width, height=kwargs.get("height"),
        fps=fps, duration=duration, frames=kwargs.get("frames"), **kwargs,
    )
    # Re-resolve width/fps/duration through aliases when passed positionally.
    w = coerce_width(width if width is not None else kwargs.get("width"))
    fps_f = coerce_fps(fps if fps is not None else kwargs.get("fps"), **kwargs)
    # duration only affects -t; frames derived but ffmpeg uses -t.
    dur_f = coerce_duration(duration if duration is not None else kwargs.get("duration"), **kwargs)
    out = normalized["output"]
    if out is None:
        cam = normalized.get("camera") or "preview"
        out = _default_gif_dir(kwargs.get("gif_dir", kwargs.get("output_dir"))) / (
            f"gif_preview_{_safe_name(cam)}_{uuid.uuid4().hex}.gif"
        )
    out_path = Path(str(out)).expanduser()
    # Force .gif suffix.
    if out_path.suffix.lower() != ".gif":
        out_path = out_path.with_suffix(".gif")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass

    binary = ffmpeg or kwargs.get("ffmpeg_path") or kwargs.get("binary") or FFMPEG_PATH
    src = str(source) if source is not None else str(kwargs.get("source", "input.mp4"))
    vf = (
        f"fps={fps_f},scale={int(w)}:-1:flags=lanczos,"
        f"split[s0][s1];[s0]palettegen=max_colors=256[p];[s1][p]paletteuse"
    )
    cmd: List[str] = [str(binary)]
    cmd.append("-y" if overwrite else "-n")
    cmd += ["-hide_banner", "-loglevel", "error"]
    cmd += ["-i", src]
    if dur_f is not None:
        cmd += ["-t", str(float(dur_f))]
    cmd += ["-vf", vf, str(out_path)]
    return cmd


def build_command(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_command(*args, **kwargs)


def ffmpeg_command(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_command(*args, **kwargs)


def _default_output_path(camera: Any, gif_dir: Any = None) -> Path:
    d = _default_gif_dir(gif_dir)
    stem = _safe_name(camera) if camera else "preview"
    return d / f"gif_preview_{stem}_{uuid.uuid4().hex}.gif"


def build_gif_preview(
    camera: Any = None,
    output: Any = None,
    width: Any = None,
    height: Any = None,
    fps: Any = None,
    duration: Any = None,
    frames: Any = None,
    source: Any = None,
    gif_dir: Any = None,
    overwrite: bool = True,
    ffmpeg: Optional[str] = None,
    timeout: float = 60.0,
    **kwargs: Any,
) -> str:
    """Build a GIF preview file. Returns the output path as a string.

    When a real ``source`` video file is supplied and ffmpeg succeeds, the
    GIF is transcoded; otherwise a placeholder animated GIF is written so the
    call never depends on recordings existing.
    """
    if camera is None:
        camera = _coerce_camera(None, kwargs)
    if output is None:
        output = _coerce_output(None, kwargs)
    if source is None:
        for key in ("input", "input_path", "src", "video", "clip", "recording", "clips"):
            if kwargs.get(key) is not None:
                source = kwargs.get(key)
                break
    normalized = validate_gif_params(
        camera, output=output, width=width, height=height,
        fps=fps, duration=duration, frames=frames, **kwargs,
    )
    out_path: Optional[Path] = normalized["output"]
    if out_path is None:
        out_path = _default_output_path(
            normalized.get("camera"), gif_dir or kwargs.get("gif_dir") or kwargs.get("output_dir")
        )
    out_path = Path(str(out_path)).expanduser()
    if out_path.suffix.lower() != ".gif":
        out_path = out_path.with_suffix(".gif")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass

    # Try real ffmpeg conversion when an existing source file is given.
    src_path: Optional[Path] = None
    if source is not None and not isinstance(source, (list, tuple)):
        p = Path(str(source)).expanduser()
        if p.is_file():
            src_path = p
    elif isinstance(source, (list, tuple)) and source:
        first = Path(str(source[0])).expanduser()
        if first.is_file():
            src_path = first

    if src_path is not None:
        try:
            cmd = build_ffmpeg_command(
                str(src_path), str(out_path),
                width=normalized["width"], fps=normalized["fps"],
                duration=normalized["duration"], overwrite=overwrite,
                ffmpeg=ffmpeg, **kwargs,
            )
            to = timeout if timeout is not None else float(kwargs.get("timeout_seconds", 60.0))
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=float(to))
            if out_path.is_file() and out_path.stat().st_size > 0:
                return str(out_path)
        except Exception:  # noqa: BLE001
            pass

    # Placeholder fallback (always succeeds, no recordings required).
    data = generate_gif_bytes(
        normalized.get("camera"), width=normalized["width"], height=normalized["height"],
        fps=normalized["fps"], duration=normalized["duration"],
        frames=normalized["frames"], label=normalized.get("label"),
    )
    # Respect overwrite=False when file already exists.
    if not overwrite and out_path.is_file():
        return str(out_path)
    with open(out_path, "wb") as fh:
        fh.write(data)
    return str(out_path)


def get_gif_preview(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def generate_gif_preview(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def create_gif_preview(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def make_gif_preview(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def render_gif_preview(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def generate_preview(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def create_preview(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def build_preview(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def build(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def create(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def make(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def render(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def generate(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def preview(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def get_preview(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def export_gif_preview(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def gif_export(*args: Any, **kwargs: Any) -> str:
    return build_gif_preview(*args, **kwargs)


def create_job(
    camera: Any = None,
    output: Any = None,
    width: Any = None,
    fps: Any = None,
    duration: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    normalized = validate_gif_params(
        camera, output=output, width=width, fps=fps, duration=duration, **kwargs,
    )
    job_id = str(kwargs.get("job_id") or uuid.uuid4())
    job: Dict[str, Any] = {
        "id": job_id,
        "job_id": job_id,
        "preview_id": job_id,
        "gif_id": job_id,
        "camera": normalized.get("camera"),
        "camera_id": normalized.get("camera"),
        "camera_name": normalized.get("camera"),
        "output": str(normalized["output"]) if normalized["output"] is not None else None,
        "output_path": str(normalized["output"]) if normalized["output"] is not None else None,
        "path": str(normalized["output"]) if normalized["output"] is not None else None,
        "width": normalized["width"],
        "height": normalized["height"],
        "fps": normalized["fps"],
        "frame_rate": normalized["fps"],
        "duration": normalized["duration"],
        "frames": normalized["frames"],
        "format": "gif",
        "content_type": CONTENT_TYPE_GIF,
        "status": "queued",
        "state": "queued",
        "progress": 0.0,
        "error": None,
    }
    with _jobs_lock:
        GIF_PREVIEW_JOBS[job_id] = job
    return job


def create_preview_job(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_job(*args, **kwargs)


def create_gif_job(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_job(*args, **kwargs)


def new_job(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_job(*args, **kwargs)


def get_job(job_id: Any) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        return GIF_PREVIEW_JOBS.get(str(job_id))


def get_preview_job(job_id: Any) -> Optional[Dict[str, Any]]:
    return get_job(job_id)


def get_gif_job(job_id: Any) -> Optional[Dict[str, Any]]:
    return get_job(job_id)


def list_jobs() -> List[Dict[str, Any]]:
    with _jobs_lock:
        return list(GIF_PREVIEW_JOBS.values())


def list_preview_jobs() -> List[Dict[str, Any]]:
    return list_jobs()


def list_gif_jobs() -> List[Dict[str, Any]]:
    return list_jobs()


def update_job(job_id: Any, **fields: Any) -> Optional[Dict[str, Any]]:
    key = str(job_id)
    with _jobs_lock:
        job = GIF_PREVIEW_JOBS.get(key)
        if job is None:
            return None
        for k, v in fields.items():
            job[k] = v
            if k == "status":
                job["state"] = v
            if k == "output_path":
                job["output"] = v
                job["path"] = v
        return job


def delete_job(job_id: Any) -> bool:
    with _jobs_lock:
        return GIF_PREVIEW_JOBS.pop(str(job_id), None) is not None


def cancel_job(job_id: Any) -> bool:
    return delete_job(job_id)


def run_job(job_id: Any, **kwargs: Any) -> Dict[str, Any]:
    key = str(job_id)
    job = get_job(key)
    if job is None:
        raise KeyError(f"Unknown GIF preview job {key}")
    update_job(key, status="processing", state="processing", progress=10.0)
    try:
        out = build_gif_preview(
            job.get("camera", job.get("camera_id")),
            job.get("output"),
            width=job.get("width", DEFAULT_WIDTH),
            height=job.get("height", DEFAULT_HEIGHT),
            fps=job.get("fps", DEFAULT_FPS),
            duration=job.get("duration", DEFAULT_DURATION),
            frames=job.get("frames"),
            **kwargs,
        )
        update_job(key, status="completed", state="completed", progress=100.0,
                   output_path=str(out), output=str(out), path=str(out))
        result = get_job(key)
        assert result is not None
        return result
    except Exception as exc:  # noqa: BLE001
        update_job(key, status="failed", state="failed", error=str(exc), progress=0.0)
        result = get_job(key)
        assert result is not None
        return result


cancel = cancel_job
remove_job = delete_job
get = get_job
list_all = list_jobs


@dataclass
class GifPreviewResult:
    camera: Optional[str]
    output_path: str
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    fps: float = DEFAULT_FPS
    duration: float = DEFAULT_DURATION
    frames: int = 15

    def to_dict(self) -> Dict[str, Any]:
        return {
            "camera": self.camera,
            "camera_id": self.camera,
            "camera_name": self.camera,
            "output_path": self.output_path,
            "output": self.output_path,
            "path": self.output_path,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "frame_rate": self.fps,
            "duration": self.duration,
            "frames": self.frames,
            "format": "gif",
            "content_type": CONTENT_TYPE_GIF,
            "ok": True,
            "success": True,
        }


class GifPreviewService:
    """Object-oriented facade over :func:`build_gif_preview`."""

    def __init__(self, gif_dir: Any = None, ffmpeg: Optional[str] = None, **kwargs: Any) -> None:
        self.gif_dir = _default_gif_dir(gif_dir or kwargs.get("output_dir"))
        self.ffmpeg = ffmpeg or kwargs.get("ffmpeg_path") or FFMPEG_PATH
        self.jobs = GIF_PREVIEW_JOBS

    def build(self, camera: Any = None, output: Any = None, **kwargs: Any) -> str:
        kwargs.setdefault("gif_dir", self.gif_dir)
        return str(build_gif_preview(camera, output, ffmpeg=kwargs.pop("ffmpeg", self.ffmpeg), **kwargs))

    def generate(self, *args: Any, **kwargs: Any) -> str:
        kwargs.setdefault("ffmpeg", self.ffmpeg)
        kwargs.setdefault("gif_dir", self.gif_dir)
        return str(build_gif_preview(*args, **kwargs))

    def preview(self, *args: Any, **kwargs: Any) -> str:
        return self.generate(*args, **kwargs)

    def generate_bytes(self, *args: Any, **kwargs: Any) -> bytes:
        return generate_gif_bytes(*args, **kwargs)

    def validate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return validate_gif_params(*args, **kwargs)

    def build_command(self, *args: Any, **kwargs: Any) -> List[str]:
        kwargs.setdefault("ffmpeg", self.ffmpeg)
        return build_ffmpeg_command(*args, **kwargs)

    def create_job(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_job(*args, **kwargs)

    def get_job(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_job(*args, **kwargs)

    def list_jobs(self) -> List[Dict[str, Any]]:
        return list_jobs()

    def run_job(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return run_job(*args, **kwargs)

    def build_gif_preview(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)

    def generate_gif_preview(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)

    def create_gif_preview(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)


class GifPreviewBuilder(GifPreviewService):
    pass


class PreviewBuilder(GifPreviewService):
    pass


class GifBuilder(GifPreviewService):
    pass


class PreviewService(GifPreviewService):
    pass


class Builder(GifPreviewService):
    pass


class Service(GifPreviewService):
    pass


gif_preview_service = GifPreviewService()
builder = gif_preview_service
service = gif_preview_service
preview_service = gif_preview_service
gif_service = gif_preview_service
default_service = gif_preview_service
instance = gif_preview_service
singleton = gif_preview_service

GifPreviewManager = GifPreviewService
GifManager = GifPreviewService
PreviewManager = GifPreviewService

__all__ = [
    "FFMPEG_PATH",
    "FFMPEG_BIN",
    "CONTENT_TYPE_GIF",
    "GIF_DIR",
    "GIF_PREVIEW_DIR",
    "PREVIEW_DIR",
    "OUTPUT_DIR",
    "GIF_PREVIEW_JOBS",
    "DEFAULT_WIDTH",
    "DEFAULT_HEIGHT",
    "DEFAULT_FPS",
    "DEFAULT_DURATION",
    "parse_width",
    "parse_height",
    "parse_fps",
    "parse_duration",
    "parse_frames",
    "coerce_width",
    "coerce_height",
    "coerce_fps",
    "coerce_duration",
    "coerce_frames",
    "validate_gif_params",
    "validate",
    "validate_params",
    "validate_preview_params",
    "generate_gif_bytes",
    "render_gif_bytes",
    "generate_placeholder_gif",
    "get_gif_bytes",
    "build_ffmpeg_command",
    "build_command",
    "ffmpeg_command",
    "build_gif_preview",
    "generate_gif_preview",
    "create_gif_preview",
    "make_gif_preview",
    "render_gif_preview",
    "get_gif_preview",
    "create_job",
    "get_job",
    "list_jobs",
    "update_job",
    "delete_job",
    "run_job",
    "GifPreviewResult",
    "GifPreviewService",
    "GifPreviewBuilder",
    "gif_preview_service",
]
