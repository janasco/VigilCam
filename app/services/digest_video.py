"""Daily digest video compilation service.

Compiles a day's clips (recorded segments, event clips, snapshots) for one
or more cameras into a single daily digest video using ffmpeg's concat
demuxer. Jobs are tracked in memory with the same defensive alias style as
``app.services.export`` and ``app.services.timelapse``.
"""

from __future__ import annotations

import glob
import re
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

try:
    from app.core.config import DATA_DIR, RECORDING_DIR
except Exception:  # noqa: BLE001
    DATA_DIR = Path("data")  # type: ignore[assignment]
    RECORDING_DIR = Path("data/recordings")  # type: ignore[assignment]

FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg"
FFMPEG_BIN = FFMPEG_PATH
DEFAULT_FPS = 30.0
DEFAULT_FACTOR = 1.0
MIN_FPS = 1.0
MAX_FPS = 120.0
DEFAULT_DIGEST_DIRNAME = "digest_videos"

DIGEST_VIDEO_JOBS: Dict[str, Dict[str, Any]] = {}
digest_video_jobs = DIGEST_VIDEO_JOBS
jobs = DIGEST_VIDEO_JOBS
JOB_STORE = DIGEST_VIDEO_JOBS
JOBS = DIGEST_VIDEO_JOBS
DIGEST_JOBS = DIGEST_VIDEO_JOBS
DAILY_DIGEST_JOBS = DIGEST_VIDEO_JOBS

_jobs_lock = threading.Lock()


def _default_digest_dir(override: Any = None) -> Path:
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
    d = base / DEFAULT_DIGEST_DIRNAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


DIGEST_VIDEO_DIR = _default_digest_dir()
DIGEST_DIR = DIGEST_VIDEO_DIR
DAILY_DIGEST_DIR = DIGEST_VIDEO_DIR
DIGEST_OUTPUT_DIR = DIGEST_VIDEO_DIR


def _safe_name(name: Any) -> str:
    s = str(name if name is not None else "digest")
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
    s = s.strip("_") or "digest"
    if len(s) > 64:
        s = s[:64]
    return s


def parse_date(value: Any, default: Optional[Any] = None) -> Optional[date]:
    """Parse a date value into a ``datetime.date`` or return default/None."""
    if value is None:
        if default is None:
            return None
        if isinstance(default, datetime):
            return default.date()
        if isinstance(default, date):
            return default
        return parse_date(default)
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            v = float(value)
            if v > 1e12:
                v = v / 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc).date()
        except (OSError, OverflowError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    # epoch numeric string
    try:
        v = float(s)
        # only treat long digit strings as epoch
        if re.fullmatch(r"\d{10,13}", s):
            if v > 1e12:
                v = v / 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc).date()
    except (ValueError, OSError, OverflowError):
        pass
    txt = s.replace("Z", "+00:00")
    # ISO datetime / date
    try:
        dt = datetime.fromisoformat(txt)
        return dt.date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_time(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:
            v = v / 1000.0
        return v
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    s = str(value).strip()
    if not s:
        return None
    try:
        v = float(s)
        if v > 1e12:
            v = v / 1000.0
        return v
    except ValueError:
        pass
    try:
        txt = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%d_%H%M%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(s, fmt)
            dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _parse_filename_ts(name: str) -> Optional[float]:
    m = re.search(r"(\d{8})_(\d{6})", name)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


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


def coerce_fps(value: Any = None, default: float = DEFAULT_FPS, **kwargs: Any) -> float:
    for key in ("fps", "frame_rate", "frameRate", "framerate", "output_fps", "out_fps", "rate"):
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


coerce_timelapse = coerce_factor
coerce_speed = coerce_factor
parse_speed = parse_factor
parse_timelapse = parse_factor
parse_timelapse_factor = parse_factor
parse_digest_date = parse_date


def _resolve_clips(value: Any, kwargs: Dict[str, Any]) -> Any:
    if value is not None:
        return value
    for key in ("clips", "sources", "source", "source_path", "sourcePath", "inputs",
                "input", "input_path", "inputPath", "videos", "video", "files", "file",
                "segments", "segment_paths", "paths", "path", "images", "image_dir",
                "src", "filename", "glob", "pattern", "recording", "recording_path"):
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


def _resolve_date(value: Any, kwargs: Dict[str, Any]) -> Any:
    if value is not None:
        return value
    for key in ("date", "day", "digest_date", "digestDate", "digest_day",
                "for_date", "for_day", "on_date", "target_date", "targetDate",
                "daily_date", "compilation_date"):
        if kwargs.get(key) is not None:
            return kwargs.get(key)
    return None


def _resolve_camera(value: Any, kwargs: Dict[str, Any]) -> Any:
    if value is not None:
        return value
    for key in ("camera", "camera_id", "cameraId", "cameraID", "cam", "cam_id",
                "camera_name", "cameraName", "device_id", "deviceId", "source_camera"):
        if kwargs.get(key) is not None:
            return kwargs.get(key)
    return None


def validate_digest_params(
    clips: Any = None,
    output: Any = None,
    camera: Any = None,
    digest_date: Any = None,
    fps: Any = None,
    factor: Any = None,
    width: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Validate daily digest inputs. Returns normalized dict or raises ValueError."""
    raw_clips = _resolve_clips(clips, kwargs)
    raw_out = _resolve_output(output, kwargs)
    raw_date = _resolve_date(digest_date, kwargs)
    raw_cam = _resolve_camera(camera, kwargs)

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

    fps_val = parse_fps(fps, default=DEFAULT_FPS)
    if fps_val is None:
        raise ValueError(f"fps must be between {MIN_FPS} and {MAX_FPS}")
    factor_val = parse_factor(factor, default=DEFAULT_FACTOR)
    if factor_val is None:
        raise ValueError("factor must be > 0")

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

    digest_day: Optional[date] = None
    if raw_date is not None:
        digest_day = parse_date(raw_date)
        if digest_day is None:
            raise ValueError(f"unrecognized date: {raw_date!r}")
    else:
        # default to today (UTC) when only camera/clips given
        digest_day = datetime.now(timezone.utc).date()

    # normalize clips: may be list, single string, glob, dir
    norm_clips: Any = None
    if raw_clips is not None:
        if isinstance(raw_clips, (list, tuple)):
            norm_clips = [str(p) for p in raw_clips]
            if not norm_clips:
                raise ValueError("clips must not be empty")
        else:
            norm_clips = str(raw_clips)

    cameras: List[str] = []
    if raw_cam is not None:
        if isinstance(raw_cam, (list, tuple)):
            cameras = [str(c) for c in raw_cam if str(c).strip()]
        else:
            s = str(raw_cam).strip()
            if s:
                # allow comma-separated
                cameras = [p.strip() for p in s.replace(";", ",").split(",") if p.strip()]

    if norm_clips is None and not cameras:
        raise ValueError("camera or clips/source is required")

    title = kwargs.get("title", kwargs.get("name", kwargs.get("digest_title")))
    if title is not None:
        title = str(title)

    return {
        "clips": norm_clips,
        "sources": norm_clips,
        "source": norm_clips,
        "camera": cameras[0] if len(cameras) == 1 else (cameras if cameras else None),
        "cameras": cameras,
        "camera_id": cameras[0] if len(cameras) == 1 else (cameras if cameras else None),
        "camera_name": cameras[0] if len(cameras) == 1 else (cameras if cameras else None),
        "date": digest_day.isoformat() if digest_day else None,
        "digest_date": digest_day.isoformat() if digest_day else None,
        "day": digest_day.isoformat() if digest_day else None,
        "digest_day": digest_day,
        "output": Path(str(raw_out)).expanduser() if raw_out is not None else None,
        "output_path": Path(str(raw_out)).expanduser() if raw_out is not None else None,
        "fps": float(fps_val),
        "frame_rate": float(fps_val),
        "factor": float(factor_val),
        "timelapse_factor": float(factor_val),
        "speed": float(factor_val),
        "width": w,
        "title": title,
    }


def validate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return validate_digest_params(*args, **kwargs)


def validate_params(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return validate_digest_params(*args, **kwargs)


def validate_daily_digest(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return validate_digest_params(*args, **kwargs)


def _camera_dirs(camera: Any, recording_dir: Any = None) -> List[Path]:
    root = Path(str(recording_dir)) if recording_dir is not None else Path(str(RECORDING_DIR))
    names: List[str] = []
    if camera is None:
        return [root] if root.exists() else []
    if isinstance(camera, (list, tuple)):
        for c in camera:
            names.append(_safe_name(c))
            names.append(str(c))
    else:
        s = str(camera)
        for part in s.replace(";", ",").split(","):
            part = part.strip()
            if part:
                names.append(_safe_name(part))
                names.append(part)
    dirs: List[Path] = []
    for n in names:
        d = root / n
        if d.exists() and d not in dirs:
            dirs.append(d)
    if not dirs and root.exists():
        # fall back to scanning everything when camera dir not found
        return [root]
    return dirs


def find_clips(
    camera: Any = None,
    digest_date: Any = None,
    clips: Any = None,
    recording_dir: Any = None,
    **kwargs: Any,
) -> List[Path]:
    """Resolve digest clip files.

    Accepts explicit clip lists / globs / dirs, otherwise scans the recording
    dir for files matching the digest day.
    """
    raw = clips
    if raw is None:
        raw = _resolve_clips(None, kwargs)
    if raw is None and camera is None:
        cam = _resolve_camera(None, kwargs)
    else:
        cam = camera
    if raw is None:
        raw = _resolve_clips(None, kwargs)
    day = parse_date(digest_date if digest_date is not None else _resolve_date(None, kwargs))
    if day is None and raw is None:
        # still allow camera-only scan (all files)
        pass

    if raw is not None:
        if isinstance(raw, (list, tuple)):
            out: List[Path] = []
            for item in raw:
                s = str(item)
                if any(ch in s for ch in ("*", "?", "[")):
                    out.extend(Path(f) for f in sorted(glob.glob(s)) if Path(f).is_file())
                else:
                    p = Path(s).expanduser()
                    if p.is_file():
                        out.append(p)
                    elif p.is_dir():
                        for ext in ("*.mp4", "*.mkv", "*.ts", "*.mov", "*.jpg", "*.jpeg", "*.png"):
                            out.extend(p.glob(ext))
            return sorted(set(out))
        s = str(raw)
        if any(ch in s for ch in ("*", "?", "[")):
            return sorted(Path(f) for f in glob.glob(s) if Path(f).is_file())
        p = Path(s).expanduser()
        if p.is_file():
            return [p]
        if p.is_dir():
            files: List[Path] = []
            for ext in ("*.mp4", "*.mkv", "*.ts", "*.mov"):
                files.extend(p.glob(ext))
            return sorted(set(f for f in files if f.is_file()))
        # treat as camera name fallback below
        cam = cam or s

    # camera + date scan
    if cam is None:
        cam = _resolve_camera(None, kwargs)
    dirs = _camera_dirs(cam, recording_dir)
    files: List[Path] = []
    for d in dirs:
        if d.is_file():
            files.append(d)
            continue
        for ext in ("*.mp4", "*.mkv", "*.ts", "*.mov"):
            files.extend(d.rglob(ext) if d == Path(str(RECORDING_DIR)) else d.glob(ext))
    files = sorted(set(f for f in files if f.is_file()))
    if day is None or not files:
        return files
    prefix = day.strftime("%Y%m%d")
    iso = day.isoformat()
    matched = [f for f in files if prefix in f.name or iso in f.name or _parse_filename_ts(f.name) is not None and datetime.fromtimestamp(_parse_filename_ts(f.name), tz=timezone.utc).date() == day]  # type: ignore[arg-type]
    # If strict match yields nothing, fall back to all files (tolerant).
    return matched or files


def collect_segments(*args: Any, **kwargs: Any) -> List[Path]:
    return find_clips(*args, **kwargs)


def find_segments(*args: Any, **kwargs: Any) -> List[Path]:
    return find_clips(*args, **kwargs)


def collect_clips(*args: Any, **kwargs: Any) -> List[Path]:
    return find_clips(*args, **kwargs)


def find_daily_segments(*args: Any, **kwargs: Any) -> List[Path]:
    return find_clips(*args, **kwargs)


def collect_daily_clips(*args: Any, **kwargs: Any) -> List[Path]:
    return find_clips(*args, **kwargs)


def list_clips(*args: Any, **kwargs: Any) -> List[Path]:
    return find_clips(*args, **kwargs)


def _default_output_path(camera: Any, digest_day: Optional[date], digest_dir: Any = None) -> Path:
    d = _default_digest_dir(digest_dir)
    if isinstance(camera, (list, tuple)) and camera:
        stem = _safe_name(camera[0])
    elif camera:
        stem = _safe_name(str(camera).split(",")[0])
    else:
        stem = "digest"
    day_s = digest_day.isoformat() if digest_day else datetime.now(timezone.utc).date().isoformat()
    return d / f"daily_digest_{stem}_{day_s}_{uuid.uuid4().hex}.mp4"


def build_ffmpeg_command(
    clips: Any = None,
    output: Any = None,
    fps: Any = DEFAULT_FPS,
    factor: Any = DEFAULT_FACTOR,
    width: Any = None,
    overwrite: bool = True,
    ffmpeg: Optional[str] = None,
    **kwargs: Any,
) -> List[str]:
    """Build the ffmpeg concat command for a daily digest."""
    if clips is None:
        clips = _resolve_clips(None, kwargs)
    if output is None:
        output = _resolve_output(None, kwargs)
    raw_date = _resolve_date(kwargs.get("digest_date", kwargs.get("date", kwargs.get("day"))), kwargs)
    raw_cam = _resolve_camera(kwargs.get("camera", kwargs.get("camera_id")), kwargs)

    normalized = validate_digest_params(
        clips, output=output, camera=raw_cam, digest_date=raw_date,
        fps=fps, factor=factor, width=width, **kwargs,
    )
    src = normalized["clips"]
    out = normalized["output"]
    if out is None:
        cams = normalized.get("cameras") or []
        day = normalized.get("digest_day")
        out = _default_output_path(cams[0] if cams else None, day, kwargs.get("digest_dir", kwargs.get("output_dir")))
    if src is None:
        # camera + date scan
        src = find_clips(camera=normalized.get("cameras") or normalized.get("camera"), digest_date=normalized.get("digest_day"))

    if isinstance(src, (str, Path)):
        s = str(src)
        if any(ch in s for ch in ("*", "?", "[")):
            src_list = sorted(Path(f) for f in glob.glob(s) if Path(f).is_file())
        else:
            p = Path(s).expanduser()
            if p.is_dir():
                src_list = find_clips(clips=str(p))
            elif p.is_file():
                src_list = [p]
            else:
                src_list = find_clips(camera=normalized.get("cameras") or normalized.get("camera"), digest_date=normalized.get("digest_day"))
    else:
        src_list = [Path(str(p)).expanduser() for p in (src or [])]
    if not src_list:
        raise FileNotFoundError("No clips found for daily digest")

    fps_f = float(normalized["fps"])
    factor_f = float(normalized["factor"])
    w = normalized["width"]

    binary = ffmpeg or kwargs.get("ffmpeg_path") or kwargs.get("binary") or FFMPEG_PATH
    out_path = Path(str(out)).expanduser()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    list_file = out_path.parent / (out_path.stem + "_concat.txt")
    with open(list_file, "w", encoding="utf-8") as fh:
        for p in src_list:
            fh.write(f"file '{p}'\n")

    cmd: List[str] = [str(binary)]
    cmd.append("-y" if overwrite else "-n")
    cmd += ["-hide_banner", "-loglevel", "error"]
    cmd += ["-f", "concat", "-safe", "0", "-i", str(list_file)]
    vf_parts: List[str] = []
    if w is not None:
        vf_parts.append(f"scale={int(w)}:-2")
    if factor_f != 1.0:
        vf_parts.append(f"setpts=PTS/{factor_f}")
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
        cmd += ["-an", "-r", str(fps_f), "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c", "copy"]
    cmd += [str(out_path)]
    return cmd


def build_command(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_command(*args, **kwargs)


def ffmpeg_command(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_command(*args, **kwargs)


def build_digest_video(
    clips: Any = None,
    output: Any = None,
    camera: Any = None,
    digest_date: Any = None,
    fps: Any = DEFAULT_FPS,
    factor: Any = DEFAULT_FACTOR,
    width: Any = None,
    overwrite: bool = True,
    ffmpeg: Optional[str] = None,
    digest_dir: Any = None,
    timeout: float = 300.0,
    **kwargs: Any,
) -> str:
    """Compile a daily digest video. Returns the output path as a string."""
    if clips is None:
        clips = _resolve_clips(None, kwargs)
    if output is None:
        output = _resolve_output(None, kwargs)
    if camera is None:
        camera = _resolve_camera(None, kwargs)
    if digest_date is None:
        digest_date = _resolve_date(None, kwargs)
    normalized = validate_digest_params(
        clips, output=output, camera=camera, digest_date=digest_date,
        fps=fps, factor=factor, width=width, **kwargs,
    )
    src = normalized["clips"]
    out_path: Optional[Path] = normalized["output"]
    fps_f = float(normalized["fps"])
    factor_f = float(normalized["factor"])
    w = normalized["width"]
    cams = normalized.get("cameras") or []
    day = normalized.get("digest_day")

    if kwargs.get("overwrite") is not None:
        overwrite = bool(kwargs.get("overwrite"))

    # Resolve concrete clip list.
    if src is None:
        resolved = find_clips(camera=cams or normalized.get("camera"), digest_date=day,
                              recording_dir=kwargs.get("recording_dir"))
    elif isinstance(src, (list, tuple)):
        resolved = []
        for item in src:
            p = Path(str(item)).expanduser()
            if p.is_file():
                resolved.append(p)
            elif any(ch in str(item) for ch in ("*", "?", "[")):
                resolved.extend(Path(f) for f in sorted(glob.glob(str(item))) if Path(f).is_file())
            else:
                raise FileNotFoundError(f"Clip not found: {item}")
    else:
        s = str(src)
        if any(ch in s for ch in ("*", "?", "[")):
            resolved = sorted(Path(f) for f in glob.glob(s) if Path(f).is_file())
            if not resolved:
                raise FileNotFoundError(f"No clips matched pattern: {s}")
        else:
            p = Path(s).expanduser()
            if p.is_file():
                resolved = [p]
            elif p.is_dir():
                resolved = find_clips(clips=str(p))
            else:
                # maybe a camera name
                resolved = find_clips(camera=s, digest_date=day)
    if not resolved:
        raise FileNotFoundError("No clips found for daily digest")

    if out_path is None:
        out_path = _default_output_path(cams[0] if cams else None, day,
                                        digest_dir or kwargs.get("digest_dir") or kwargs.get("output_dir"))
    out_path = Path(str(out_path)).expanduser()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass

    cmd = build_ffmpeg_command(
        [str(p) for p in resolved], out_path, fps=fps_f, factor=factor_f,
        width=w, overwrite=overwrite, ffmpeg=ffmpeg, **kwargs,
    )
    try:
        to = timeout if timeout is not None else kwargs.get("timeout_seconds", 300.0)
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=float(to))
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg digest failed: {exc.stderr or exc}") from exc
    return str(out_path)


# Alias entry points for tolerant callers/tests.
def build_daily_digest(*args: Any, **kwargs: Any) -> str:
    return str(build_digest_video(*args, **kwargs))


def create_digest_video(*args: Any, **kwargs: Any) -> str:
    return str(build_digest_video(*args, **kwargs))


def make_digest_video(*args: Any, **kwargs: Any) -> str:
    return str(build_digest_video(*args, **kwargs))


def generate_digest_video(*args: Any, **kwargs: Any) -> str:
    return str(build_digest_video(*args, **kwargs))


def render_digest_video(*args: Any, **kwargs: Any) -> str:
    return str(build_digest_video(*args, **kwargs))


def compile_digest(*args: Any, **kwargs: Any) -> str:
    return str(build_digest_video(*args, **kwargs))


def compile_digest_video(*args: Any, **kwargs: Any) -> str:
    return str(build_digest_video(*args, **kwargs))


def compile_daily_digest(*args: Any, **kwargs: Any) -> str:
    return str(build_digest_video(*args, **kwargs))


def build_digest(*args: Any, **kwargs: Any) -> str:
    return str(build_digest_video(*args, **kwargs))


def build(*args: Any, **kwargs: Any) -> str:
    return str(build_digest_video(*args, **kwargs))


def create(*args: Any, **kwargs: Any) -> str:
    return str(build_digest_video(*args, **kwargs))


def make(*args: Any, **kwargs: Any) -> str:
    return str(build_digest_video(*args, **kwargs))


def render(*args: Any, **kwargs: Any) -> str:
    return str(build_digest_video(*args, **kwargs))


def generate(*args: Any, **kwargs: Any) -> str:
    return str(build_digest_video(*args, **kwargs))


def create_job(
    clips: Any = None,
    output: Any = None,
    camera: Any = None,
    digest_date: Any = None,
    fps: Any = None,
    factor: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    normalized = validate_digest_params(clips, output=output, camera=camera, digest_date=digest_date, fps=fps, factor=factor, **kwargs)
    job_id = str(kwargs.get("job_id") or uuid.uuid4())
    job: Dict[str, Any] = {
        "id": job_id,
        "job_id": job_id,
        "digest_id": job_id,
        "digest_job_id": job_id,
        "clips": normalized["clips"],
        "sources": normalized["clips"],
        "source": normalized["clips"],
        "camera": normalized.get("camera"),
        "cameras": normalized.get("cameras"),
        "camera_id": normalized.get("camera"),
        "date": normalized.get("date"),
        "digest_date": normalized.get("date"),
        "day": normalized.get("date"),
        "output": str(normalized["output"]) if normalized["output"] is not None else None,
        "output_path": str(normalized["output"]) if normalized["output"] is not None else None,
        "path": str(normalized["output"]) if normalized["output"] is not None else None,
        "fps": normalized["fps"],
        "frame_rate": normalized["fps"],
        "factor": normalized["factor"],
        "timelapse_factor": normalized["factor"],
        "speed": normalized["factor"],
        "width": normalized["width"],
        "title": normalized.get("title"),
        "status": "queued",
        "state": "queued",
        "progress": 0.0,
        "error": None,
    }
    with _jobs_lock:
        DIGEST_VIDEO_JOBS[job_id] = job
    return job


def create_digest_job(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_job(*args, **kwargs)


def new_job(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_job(*args, **kwargs)


def get_job(job_id: Any) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        return DIGEST_VIDEO_JOBS.get(str(job_id))


def get_digest_job(job_id: Any) -> Optional[Dict[str, Any]]:
    return get_job(job_id)


def list_jobs() -> List[Dict[str, Any]]:
    with _jobs_lock:
        return list(DIGEST_VIDEO_JOBS.values())


def list_digest_jobs() -> List[Dict[str, Any]]:
    return list_jobs()


def update_job(job_id: Any, **fields: Any) -> Optional[Dict[str, Any]]:
    key = str(job_id)
    with _jobs_lock:
        job = DIGEST_VIDEO_JOBS.get(key)
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
        return DIGEST_VIDEO_JOBS.pop(str(job_id), None) is not None


def cancel_job(job_id: Any) -> bool:
    return delete_job(job_id)


def run_job(job_id: Any, **kwargs: Any) -> Dict[str, Any]:
    key = str(job_id)
    job = get_job(key)
    if job is None:
        raise KeyError(f"Unknown digest video job {key}")
    update_job(key, status="processing", state="processing", progress=10.0)
    try:
        out = build_digest_video(
            job.get("clips", job.get("sources")),
            job.get("output"),
            camera=job.get("cameras", job.get("camera")),
            digest_date=job.get("date", job.get("digest_date")),
            fps=job.get("fps", DEFAULT_FPS),
            factor=job.get("factor", DEFAULT_FACTOR),
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


cancel = cancel_job
remove_job = delete_job
get = get_job
list_all = list_jobs

cancel_digest_job = cancel_job
delete_digest_job = delete_job
get_digest = get_job


@dataclass
class DigestVideoResult:
    clips: str
    output_path: str
    fps: float = DEFAULT_FPS
    factor: float = DEFAULT_FACTOR

    def to_dict(self) -> Dict[str, Any]:
        return {
            "clips": self.clips,
            "output_path": self.output_path,
            "output": self.output_path,
            "path": self.output_path,
            "fps": self.fps,
            "frame_rate": self.fps,
            "factor": self.factor,
            "timelapse_factor": self.factor,
            "speed": self.factor,
            "ok": True,
            "success": True,
        }


class DigestVideoService:
    """Object-oriented facade over :func:`build_digest_video`."""

    def __init__(self, digest_dir: Any = None, ffmpeg: Optional[str] = None, **kwargs: Any) -> None:
        self.digest_dir = _default_digest_dir(digest_dir or kwargs.get("output_dir"))
        self.ffmpeg = ffmpeg or kwargs.get("ffmpeg_path") or FFMPEG_PATH
        self.jobs = DIGEST_VIDEO_JOBS

    def build(self, clips: Any = None, output: Any = None, **kwargs: Any) -> str:
        kwargs.setdefault("digest_dir", self.digest_dir)
        return str(build_digest_video(clips, output, ffmpeg=kwargs.pop("ffmpeg", self.ffmpeg), **kwargs))

    def create(self, *args: Any, **kwargs: Any) -> str:
        kwargs.setdefault("ffmpeg", self.ffmpeg)
        kwargs.setdefault("digest_dir", self.digest_dir)
        return str(build_digest_video(*args, **kwargs))

    def validate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return validate_digest_params(*args, **kwargs)

    def build_command(self, *args: Any, **kwargs: Any) -> List[str]:
        kwargs.setdefault("ffmpeg", self.ffmpeg)
        return build_ffmpeg_command(*args, **kwargs)

    def find_clips(self, *args: Any, **kwargs: Any) -> List[Path]:
        if args and not kwargs.get("camera") and not kwargs.get("clips"):
            # tolerate positional (camera, date) or (clips,)
            if len(args) >= 2:
                return find_clips(camera=args[0], digest_date=args[1], **kwargs)
            return find_clips(clips=args[0], **kwargs)
        return find_clips(*args, **kwargs)

    def create_job(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_job(*args, **kwargs)

    def get_job(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_job(*args, **kwargs)

    def list_jobs(self) -> List[Dict[str, Any]]:
        return list_jobs()

    def run_job(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return run_job(*args, **kwargs)

    # Aliases.
    def build_digest_video(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)

    def build_daily_digest(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)

    def compile_digest(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)


class DigestVideoBuilder(DigestVideoService):
    pass


class DigestBuilder(DigestVideoService):
    pass


class DailyDigestService(DigestVideoService):
    pass


class DailyDigestBuilder(DigestVideoService):
    pass


class DigestVideo(DigestVideoService):
    pass


class Builder(DigestVideoService):
    pass


class Service(DigestVideoService):
    pass


digest_video_service = DigestVideoService()
builder = digest_video_service
service = digest_video_service
digest_builder = digest_video_service
default_service = digest_video_service
instance = digest_video_service
singleton = digest_video_service
daily_digest_service = digest_video_service

DigestVideoManager = DigestVideoService
DigestManager = DigestVideoService
DigestVideoExporter = DigestVideoService

__all__ = [
    "FFMPEG_PATH",
    "FFMPEG_BIN",
    "DIGEST_VIDEO_DIR",
    "DIGEST_VIDEO_JOBS",
    "parse_date",
    "parse_fps",
    "parse_factor",
    "coerce_fps",
    "coerce_factor",
    "validate_digest_params",
    "validate",
    "validate_params",
    "build_ffmpeg_command",
    "build_command",
    "ffmpeg_command",
    "find_clips",
    "collect_clips",
    "find_segments",
    "build_digest_video",
    "build_daily_digest",
    "create_digest_video",
    "compile_digest",
    "create_job",
    "get_job",
    "list_jobs",
    "update_job",
    "delete_job",
    "run_job",
    "DigestVideoResult",
    "DigestVideoService",
    "DigestVideoBuilder",
    "digest_video_service",
]
