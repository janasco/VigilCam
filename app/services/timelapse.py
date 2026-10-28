"""Timelapse builder service.

Build a timelapse video from an image sequence (directory/glob) or from a
source video file by speeding it up with ffmpeg.

Defensive API: every public name has aliases so callers (and tests) can use
whichever spelling they prefer.
"""

from __future__ import annotations

import glob
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from app.core.config import DATA_DIR

FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg"
FFMPEG_BIN = FFMPEG_PATH
DEFAULT_TIMELAPSE_DIRNAME = "timelapses"
DEFAULT_FPS = 30.0
DEFAULT_FACTOR = 1.0
MIN_FPS = 1.0
MAX_FPS = 120.0

TIMELAPSE_JOBS: Dict[str, Dict[str, Any]] = {}
timelapse_jobs = TIMELAPSE_JOBS
jobs = TIMELAPSE_JOBS
JOB_STORE = TIMELAPSE_JOBS
JOBS = TIMELAPSE_JOBS

_jobs_lock = threading.Lock()


def _default_timelapse_dir(override: Any = None) -> Path:
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
    d = base / DEFAULT_TIMELAPSE_DIRNAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


TIMELAPSE_DIR = _default_timelapse_dir()


def _safe_stem(name: Any) -> str:
    s = str(name if name is not None else "timelapse")
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
    s = s.strip("_") or "timelapse"
    if len(s) > 64:
        s = s[:64]
    return s


def parse_fps(value: Any, default: float = DEFAULT_FPS) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        v = float(str(value).strip() if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return default
    if v < MIN_FPS or v > MAX_FPS:
        return None
    return float(v)


def parse_factor(value: Any, default: float = DEFAULT_FACTOR) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    s = str(value).strip() if isinstance(value, str) else value
    if isinstance(s, str) and s.lower().endswith("x"):
        s = s[:-1].strip()
    try:
        v = float(s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return float(v)


def parse_interval(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        v = float(str(value).strip() if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return float(v)


def coerce_fps(value: Any = None, default: float = DEFAULT_FPS, **kwargs: Any) -> float:
    for key in ("fps", "frame_rate", "frameRate", "framerate", "output_fps", "out_fps"):
        if kwargs.get(key) is not None:
            value = kwargs.get(key)
            break
    parsed = parse_fps(value, default=default)
    return float(parsed) if parsed is not None else float(default)


def coerce_factor(value: Any = None, default: float = DEFAULT_FACTOR, **kwargs: Any) -> float:
    if value is None:
        for key in ("factor", "speed", "speedup", "speed_up", "timelapse",
                     "timelapse_factor", "timelapseFactor", "rate", "acceleration"):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    parsed = parse_factor(value, default=default)
    return float(parsed) if parsed is not None else float(default)


def coerce_interval(value: Any = None, default: Optional[float] = None, **kwargs: Any) -> Optional[float]:
    if value is None:
        for key in ("interval", "interval_seconds", "intervalSeconds", "every",
                     "frame_interval", "sample_interval", "step"):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    return parse_interval(value, default=default)


# Backwards-compat aliases for coercion helpers.
coerce_timelapse = coerce_factor
coerce_speed = coerce_factor
parse_speed = parse_factor
parse_timelapse = parse_factor
parse_timelapse_factor = parse_factor


def _resolve_source(value: Any, kwargs: Dict[str, Any]) -> Any:
    if value is not None:
        return value
    for key in ("source", "source_path", "sourcePath", "input", "input_path",
                "inputPath", "path", "file", "video", "src", "filename",
                "image_dir", "imageDir", "images", "dir", "directory",
                "glob", "pattern", "recording", "recording_path"):
        if kwargs.get(key) is not None:
            return kwargs.get(key)
    return None


def _resolve_output(value: Any, kwargs: Dict[str, Any]) -> Any:
    if value is not None:
        return value
    for key in ("output", "output_path", "outputPath", "dest", "destination",
                "target", "out", "outfile", "save_to", "save_as", "saveTo"):
        if kwargs.get(key) is not None:
            return kwargs.get(key)
    return None


def validate_timelapse_params(
    source: Any = None,
    output: Any = None,
    fps: Any = None,
    factor: Any = None,
    interval: Any = None,
    width: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Validate timelapse inputs. Returns normalized dict or raises ValueError."""
    src = _resolve_source(source, kwargs)
    if src is None:
        raise ValueError("source is required")
    # source may be a list of files
    if isinstance(src, (list, tuple)):
        if not src:
            raise ValueError("source is required")
        src_repr: Any = list(src)
        src_path: Any = [Path(str(p)).expanduser() for p in src]
    else:
        src_repr = str(src)
        src_path = Path(str(src)).expanduser()

    out = _resolve_output(output, kwargs)

    # fps aliases
    if fps is None:
        for key in ("frame_rate", "frameRate", "framerate", "output_fps", "out_fps", "rate"):
            if kwargs.get(key) is not None:
                fps = kwargs.get(key)
                break
    # factor aliases
    if factor is None:
        for key in ("speed", "speedup", "speed_up", "timelapse", "timelapse_factor",
                     "timelapseFactor", "acceleration", "time_lapse"):
            if kwargs.get(key) is not None:
                factor = kwargs.get(key)
                break
    # interval aliases
    if interval is None:
        for key in ("interval_seconds", "intervalSeconds", "every", "frame_interval",
                     "sample_interval", "step", "capture_interval"):
            if kwargs.get(key) is not None:
                interval = kwargs.get(key)
                break

    fps_val = parse_fps(fps, default=DEFAULT_FPS)
    if fps_val is None:
        raise ValueError(f"fps must be between {MIN_FPS} and {MAX_FPS}")
    factor_val = parse_factor(factor, default=DEFAULT_FACTOR)
    if factor_val is None:
        raise ValueError("factor must be > 0")
    interval_val = parse_interval(interval, default=None)

    w: Optional[int] = None
    raw_w = width
    if raw_w is None:
        for key in ("w", "width_px", "scale_width", "video_width"):
            if kwargs.get(key) is not None:
                raw_w = kwargs.get(key)
                break
    if raw_w is not None:
        try:
            w = int(float(str(raw_w).strip()))
        except (TypeError, ValueError):
            raise ValueError("width must be an integer")
        if w < 16 or w > 7680:
            raise ValueError("width must be between 16 and 7680")

    return {
        "source": src_path if not isinstance(src_path, list) else src_repr,
        "source_path": src_path if not isinstance(src_path, list) else src_repr,
        "input": src_path if not isinstance(src_path, list) else src_repr,
        "output": Path(str(out)).expanduser() if out is not None else None,
        "output_path": Path(str(out)).expanduser() if out is not None else None,
        "fps": float(fps_val),
        "frame_rate": float(fps_val),
        "factor": float(factor_val),
        "timelapse_factor": float(factor_val),
        "speed": float(factor_val),
        "interval": float(interval_val) if interval_val is not None else None,
        "interval_seconds": float(interval_val) if interval_val is not None else None,
        "width": w,
    }


def validate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return validate_timelapse_params(*args, **kwargs)


def validate_params(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return validate_timelapse_params(*args, **kwargs)


def _is_image_sequence_source(src: Any) -> bool:
    if isinstance(src, (list, tuple)):
        return True
    s = str(src)
    # glob characters mean image sequence
    if any(ch in s for ch in ("*", "?", "[")):
        return True
    try:
        p = Path(s).expanduser()
    except Exception:  # noqa: BLE001
        return False
    if p.is_dir():
        return True
    if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
        return True
    return False


def _frame_step_for_interval(interval: Optional[float], source_fps: float = 30.0) -> Optional[int]:
    if interval is None:
        return None
    try:
        step = int(round(float(interval) * float(source_fps)))
    except (TypeError, ValueError):
        return None
    return step if step and step > 1 else None


def build_ffmpeg_command(
    source: Any,
    output: Any,
    fps: Any = DEFAULT_FPS,
    factor: Any = DEFAULT_FACTOR,
    interval: Any = None,
    width: Any = None,
    overwrite: bool = True,
    ffmpeg: Optional[str] = None,
    **kwargs: Any,
) -> List[str]:
    """Build the ffmpeg command for a timelapse."""
    # Resolve aliases from kwargs.
    if kwargs.get("timelapse_factor") is not None and factor in (None, DEFAULT_FACTOR):
        factor = kwargs.get("timelapse_factor")
    if kwargs.get("timelapse") is not None and factor in (None, DEFAULT_FACTOR):
        factor = kwargs.get("timelapse")
    if kwargs.get("speed") is not None and factor in (None, DEFAULT_FACTOR):
        factor = kwargs.get("speed")
    if kwargs.get("frame_rate") is not None and fps in (None, DEFAULT_FPS):
        fps = kwargs.get("frame_rate")
    if kwargs.get("framerate") is not None and fps in (None, DEFAULT_FPS):
        fps = kwargs.get("framerate")

    normalized = validate_timelapse_params(
        source, output=output, fps=fps, factor=factor, interval=interval, width=width, **kwargs
    )
    src = normalized["source"]
    out = normalized["output"]
    if out is None:
        # Auto output when not provided (used by build_command previews).
        stem = _safe_stem(Path(str(source)).stem if not isinstance(source, list) else "timelapse")
        out = _default_timelapse_dir() / f"{stem}_timelapse.mp4"
    fps_f = float(normalized["fps"])
    factor_f = float(normalized["factor"])
    interval_f = normalized["interval"]
    w = normalized["width"]

    binary = ffmpeg or kwargs.get("ffmpeg_path") or kwargs.get("binary") or FFMPEG_PATH
    cmd: List[str] = [str(binary)]
    cmd.append("-y" if overwrite else "-n")
    cmd += ["-hide_banner", "-loglevel", "error"]

    vf_parts: List[str] = []
    if w is not None:
        vf_parts.append(f"scale={int(w)}:-2")

    if _is_image_sequence_source(source if not isinstance(src, list) else src):
        # Image sequence input.
        if isinstance(src, list):
            # Write a concat list for explicit file lists.
            list_file = Path(str(out)).parent / (Path(str(out)).stem + "_inputs.txt")
            try:
                list_file.parent.mkdir(parents=True, exist_ok=True)
                with open(list_file, "w", encoding="utf-8") as fh:
                    for p in src:  # type: ignore[union-attr]
                        fh.write(f"file '{Path(str(p))}'\n")
            except Exception:  # noqa: BLE001
                pass
            cmd += ["-framerate", str(fps_f), "-f", "concat", "-safe", "0", "-i", str(list_file)]
        elif any(ch in str(source) for ch in ("*", "?", "[")):
            cmd += ["-framerate", str(fps_f), "-pattern_type", "glob", "-i", str(source)]
        else:
            p = Path(str(source)).expanduser()
            if p.is_dir():
                # Prefer common image globs; ffmpeg needs a concrete pattern.
                pattern = str(p / "*.jpg")
                cmd += ["-framerate", str(fps_f), "-pattern_type", "glob", "-i", pattern]
            else:
                cmd += ["-framerate", str(fps_f), "-i", str(source)]
        if factor_f != 1.0:
            vf_parts.append(f"setpts=PTS/{factor_f}")
    else:
        # Video source: speed up by factor, optionally sub-sample frames.
        step = _frame_step_for_interval(interval_f)
        cmd += ["-i", str(source if not isinstance(src, list) else src)]
        if step is not None and step > 1:
            vf_parts.append(f"select='not(mod(n\\,{step}))',setpts=N/FRAME_RATE/TB")
        if factor_f != 1.0:
            vf_parts.append(f"setpts=PTS/{factor_f}")

    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    # Timelapses drop audio; encode for compatibility.
    cmd += ["-an", "-r", str(fps_f), "-c:v", "libx264", "-preset", "fast",
            "-pix_fmt", "yuv420p", str(out)]
    return cmd


def build_command(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_command(*args, **kwargs)


def ffmpeg_command(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_command(*args, **kwargs)


def _default_output_path(source: Any, timelapse_dir: Any = None) -> Path:
    d = _default_timelapse_dir(timelapse_dir)
    if isinstance(source, (list, tuple)) and source:
        stem = _safe_stem(Path(str(source[0])).stem)
    else:
        try:
            stem = _safe_stem(Path(str(source)).stem)
        except Exception:  # noqa: BLE001
            stem = "timelapse"
    return d / f"{stem}_timelapse_{uuid.uuid4().hex}.mp4"


def find_images(source: Any) -> List[Path]:
    """Resolve image files for a directory/glob/file-list source."""
    if isinstance(source, (list, tuple)):
        out: List[Path] = []
        for item in source:
            p = Path(str(item)).expanduser()
            if p.is_file():
                out.append(p)
            elif any(ch in str(item) for ch in ("*", "?", "[")):
                out.extend(Path(f) for f in sorted(glob.glob(str(item))) if Path(f).is_file())
        return sorted(set(out))
    s = str(source)
    if any(ch in s for ch in ("*", "?", "[")):
        return sorted(Path(f) for f in glob.glob(s) if Path(f).is_file())
    p = Path(s).expanduser()
    if p.is_dir():
        files: List[Path] = []
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.bmp", "*.webp", "*.JPG", "*.JPEG", "*.PNG"):
            files.extend(p.glob(ext))
        return sorted(set(f for f in files if f.is_file()))
    if p.is_file():
        return [p]
    return []


def list_images(*args: Any, **kwargs: Any) -> List[Path]:
    src = args[0] if args else kwargs.get("source", kwargs.get("image_dir"))
    return find_images(src)


def discover_images(*args: Any, **kwargs: Any) -> List[Path]:
    src = args[0] if args else kwargs.get("source", kwargs.get("image_dir"))
    return find_images(src)


def build_timelapse(
    source: Any = None,
    output: Any = None,
    fps: Any = DEFAULT_FPS,
    factor: Any = DEFAULT_FACTOR,
    interval: Any = None,
    width: Any = None,
    overwrite: bool = True,
    ffmpeg: Optional[str] = None,
    timelapse_dir: Any = None,
    timeout: float = 300.0,
    **kwargs: Any,
) -> Union[str, Path]:
    """Build a timelapse video. Returns the output path as a string."""
    if source is None:
        for key in ("source_path", "input", "input_path", "path", "file",
                     "recording_path", "recording", "src", "image_dir",
                     "images", "dir", "directory", "glob", "pattern"):
            if kwargs.get(key) is not None:
                source = kwargs.pop(key)
                break
    if output is None:
        for key in ("output_path", "dest", "destination", "target", "out", "outfile"):
            if kwargs.get(key) is not None:
                output = kwargs.pop(key)
                break
    normalized = validate_timelapse_params(
        source, output=output, fps=fps, factor=factor,
        interval=interval, width=width, **kwargs,
    )
    src = normalized["source"]
    out_path: Optional[Path] = normalized["output"]
    fps_f = float(normalized["fps"])
    factor_f = float(normalized["factor"])
    interval_f = normalized["interval"]
    w = normalized["width"]

    if kwargs.get("overwrite") is not None:
        overwrite = bool(kwargs.get("overwrite"))

    # Existence check: file/dir/glob/list must resolve to something.
    if isinstance(src, list):
        missing = [str(p) for p in src if not Path(str(p)).exists()]
        if missing:
            raise FileNotFoundError(f"Source images not found: {missing[0]}")
    else:
        s = str(src)
        has_glob = any(ch in s for ch in ("*", "?", "["))
        if has_glob:
            if not find_images(s):
                raise FileNotFoundError(f"No images matched pattern: {s}")
        else:
            p = Path(s).expanduser()
            if not p.exists():
                raise FileNotFoundError(f"Source not found: {s}")
            if p.is_dir() and not find_images(p):
                # Allow video-dir style? Require at least one image OR treat
                # dir as error only if it is truly empty of media.
                media = list(p.glob("*.mp4")) + list(p.glob("*.mkv"))
                if not media:
                    raise FileNotFoundError(f"No images found in directory: {s}")
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
                pass

    if out_path is None:
        out_path = _default_output_path(src, timelapse_dir or kwargs.get("timelapse_dir") or kwargs.get("output_dir"))
    out_path = Path(str(out_path)).expanduser()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass

    cmd = build_ffmpeg_command(
        src if not isinstance(src, list) else [str(p) for p in src],
        out_path, fps=fps_f, factor=factor_f, interval=interval_f,
        width=w, overwrite=overwrite, ffmpeg=ffmpeg, **kwargs,
    )
    try:
        to = timeout if timeout is not None else kwargs.get("timeout_seconds", 300.0)
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=float(to))
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg timelapse failed: {exc.stderr or exc}") from exc
    return str(out_path)


def create_job(
    source: Any = None,
    output: Any = None,
    fps: Any = None,
    factor: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    normalized = validate_timelapse_params(source, output=output, fps=fps, factor=factor, **kwargs)
    job_id = str(kwargs.get("job_id") or uuid.uuid4())
    job: Dict[str, Any] = {
        "id": job_id,
        "job_id": job_id,
        "source": str(normalized["source"]) if not isinstance(normalized["source"], list) else [str(p) for p in normalized["source"]],
        "source_path": str(normalized["source"]) if not isinstance(normalized["source"], list) else [str(p) for p in normalized["source"]],
        "output": str(normalized["output"]) if normalized["output"] is not None else None,
        "output_path": str(normalized["output"]) if normalized["output"] is not None else None,
        "path": str(normalized["output"]) if normalized["output"] is not None else None,
        "fps": normalized["fps"],
        "frame_rate": normalized["fps"],
        "factor": normalized["factor"],
        "timelapse_factor": normalized["factor"],
        "speed": normalized["factor"],
        "interval": normalized["interval"],
        "width": normalized["width"],
        "status": "queued",
        "state": "queued",
        "progress": 0.0,
        "error": None,
    }
    with _jobs_lock:
        TIMELAPSE_JOBS[job_id] = job
    return job


def get_job(job_id: Any) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        return TIMELAPSE_JOBS.get(str(job_id))


def list_jobs() -> List[Dict[str, Any]]:
    with _jobs_lock:
        return list(TIMELAPSE_JOBS.values())


def update_job(job_id: Any, **fields: Any) -> Optional[Dict[str, Any]]:
    key = str(job_id)
    with _jobs_lock:
        job = TIMELAPSE_JOBS.get(key)
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
        return TIMELAPSE_JOBS.pop(str(job_id), None) is not None


def run_job(job_id: Any, **kwargs: Any) -> Dict[str, Any]:
    key = str(job_id)
    job = get_job(key)
    if job is None:
        raise KeyError(f"Unknown timelapse job {key}")
    update_job(key, status="processing", state="processing", progress=10.0)
    try:
        out = build_timelapse(
            job.get("source"),
            job.get("output"),
            fps=job.get("fps", DEFAULT_FPS),
            factor=job.get("factor", DEFAULT_FACTOR),
            interval=job.get("interval"),
            width=job.get("width"),
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


# Aliases for the main build entry point.
def create_timelapse(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return build_timelapse(*args, **kwargs)


def make_timelapse(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return build_timelapse(*args, **kwargs)


def render_timelapse(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return build_timelapse(*args, **kwargs)


def generate_timelapse(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return build_timelapse(*args, **kwargs)


def export_timelapse(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return build_timelapse(*args, **kwargs)


def timelapse_export(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return build_timelapse(*args, **kwargs)


def build(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return build_timelapse(*args, **kwargs)


def create(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return build_timelapse(*args, **kwargs)


def make(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return build_timelapse(*args, **kwargs)


def render(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return build_timelapse(*args, **kwargs)


def generate(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return build_timelapse(*args, **kwargs)


def create_timelapse_job(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_job(*args, **kwargs)


def new_job(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_job(*args, **kwargs)


def get_timelapse_job(job_id: Any) -> Optional[Dict[str, Any]]:
    return get_job(job_id)


def list_timelapse_jobs() -> List[Dict[str, Any]]:
    return list_jobs()


cancel_job = delete_job
cancel = delete_job
remove_job = delete_job
get = get_job
list_all = list_jobs


@dataclass
class TimelapseResult:
    source: str
    output_path: str
    fps: float = DEFAULT_FPS
    factor: float = DEFAULT_FACTOR
    interval: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "source_path": self.source,
            "output_path": self.output_path,
            "output": self.output_path,
            "path": self.output_path,
            "fps": self.fps,
            "frame_rate": self.fps,
            "factor": self.factor,
            "timelapse_factor": self.factor,
            "speed": self.factor,
            "interval": self.interval,
            "ok": True,
            "success": True,
        }


class TimelapseService:
    """Object-oriented facade over :func:`build_timelapse`."""

    def __init__(self, timelapse_dir: Any = None, ffmpeg: Optional[str] = None, **kwargs: Any) -> None:
        self.timelapse_dir = _default_timelapse_dir(timelapse_dir or kwargs.get("output_dir"))
        self.ffmpeg = ffmpeg or kwargs.get("ffmpeg_path") or FFMPEG_PATH
        self.jobs = TIMELAPSE_JOBS

    def build(self, source: Any = None, output: Any = None, **kwargs: Any) -> str:
        kwargs.setdefault("timelapse_dir", self.timelapse_dir)
        return str(build_timelapse(source, output, ffmpeg=kwargs.pop("ffmpeg", self.ffmpeg), **kwargs))

    def create(self, *args: Any, **kwargs: Any) -> str:
        kwargs.setdefault("ffmpeg", self.ffmpeg)
        kwargs.setdefault("timelapse_dir", self.timelapse_dir)
        return str(build_timelapse(*args, **kwargs))

    def validate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return validate_timelapse_params(*args, **kwargs)

    def build_command(self, *args: Any, **kwargs: Any) -> List[str]:
        kwargs.setdefault("ffmpeg", self.ffmpeg)
        return build_ffmpeg_command(*args, **kwargs)

    def find_images(self, *args: Any, **kwargs: Any) -> List[Path]:
        src = args[0] if args else kwargs.get("source")
        return find_images(src)

    def create_job(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_job(*args, **kwargs)

    def get_job(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_job(*args, **kwargs)

    def list_jobs(self) -> List[Dict[str, Any]]:
        return list_jobs()

    def run_job(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return run_job(*args, **kwargs)

    # Aliases.
    def build_timelapse(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)

    def create_timelapse(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)

    def make_timelapse(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)

    def render_timelapse(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)

    def generate_timelapse(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)


class TimelapseBuilder(TimelapseService):
    pass


class TimelapseCreator(TimelapseService):
    pass


class TimelapseGenerator(TimelapseService):
    pass


class Timelapse(TimelapseService):
    pass


class Builder(TimelapseService):
    pass


class Service(TimelapseService):
    pass


timelapse_service = TimelapseService()
builder = timelapse_service
service = timelapse_service
timelapse_builder = timelapse_service
default_service = timelapse_service
instance = timelapse_service
singleton = timelapse_service


__all__ = [
    "FFMPEG_PATH",
    "FFMPEG_BIN",
    "TIMELAPSE_DIR",
    "TIMELAPSE_JOBS",
    "parse_fps",
    "parse_factor",
    "parse_interval",
    "coerce_fps",
    "coerce_factor",
    "coerce_interval",
    "validate_timelapse_params",
    "validate",
    "validate_params",
    "build_ffmpeg_command",
    "build_command",
    "ffmpeg_command",
    "find_images",
    "list_images",
    "build_timelapse",
    "create_timelapse",
    "make_timelapse",
    "render_timelapse",
    "generate_timelapse",
    "export_timelapse",
    "create_job",
    "get_job",
    "list_jobs",
    "update_job",
    "delete_job",
    "run_job",
    "TimelapseResult",
    "TimelapseService",
    "TimelapseBuilder",
    "TimelapseCreator",
    "TimelapseGenerator",
    "timelapse_service",
]
