"""Snapshot timelapse builder service.

Build a timelapse video from still snapshot images (``data/snapshots`` store)
using ffmpeg's concat demuxer. Jobs are tracked in memory with the same
defensive alias style as ``app.services.timelapse`` and
``app.services.digest_video``.

This module is the dedicated snapshot-to-mp4 path: unlike the generic
timelapse builder (arbitrary dir/glob) and the daily digest compiler
(concat of recorded video segments), it discovers snapshots from the shared
snapshots store with camera/date/event filtering and renders them to mp4.
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
from typing import Any, Dict, List, Optional, Union

try:
    from app.services import snapshots as snapshots_service
except Exception:  # noqa: BLE001
    snapshots_service = None  # type: ignore[assignment]

try:
    from app.core.config import DATA_DIR
except Exception:  # noqa: BLE001
    DATA_DIR = Path("data")  # type: ignore[assignment]

FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg"
FFMPEG_BIN = FFMPEG_PATH
DEFAULT_SNAP_TIMELAPSE_DIRNAME = "snap_timelapses"
DEFAULT_FPS = 30.0
DEFAULT_FACTOR = 1.0
MIN_FPS = 1.0
MAX_FPS = 120.0

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
SUPPORTED_EXTS = SUPPORTED_IMAGE_EXTS
IMAGE_EXTS = SUPPORTED_IMAGE_EXTS

SNAP_TIMELAPSE_JOBS: Dict[str, Dict[str, Any]] = {}
snap_timelapse_jobs = SNAP_TIMELAPSE_JOBS
jobs = SNAP_TIMELAPSE_JOBS
JOB_STORE = SNAP_TIMELAPSE_JOBS
JOBS = SNAP_TIMELAPSE_JOBS
SNAP_JOBS = SNAP_TIMELAPSE_JOBS

_jobs_lock = threading.Lock()

_EVENT_RE = re.compile(r"event[_-]?(\d+)", re.IGNORECASE)


def _default_snapshot_dir(override: Any = None) -> Path:
    if override is not None:
        p = Path(str(override)).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return p
    try:
        if snapshots_service is not None and hasattr(snapshots_service, "_snapshot_dir"):
            return snapshots_service._snapshot_dir()  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001
        pass
    try:
        if snapshots_service is not None and getattr(snapshots_service, "SNAPSHOT_DIR", None) is not None:
            p = Path(str(snapshots_service.SNAPSHOT_DIR)).expanduser()
            try:
                p.mkdir(parents=True, exist_ok=True)
            except Exception:  # noqa: BLE001
                pass
            return p
    except Exception:  # noqa: BLE001
        pass
    try:
        base = Path(str(DATA_DIR)).expanduser()
    except Exception:  # noqa: BLE001
        base = Path("data")
    d = base / "snapshots"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


def _default_snap_timelapse_dir(override: Any = None) -> Path:
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
    d = base / DEFAULT_SNAP_TIMELAPSE_DIRNAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


SNAP_TIMELAPSE_DIR = _default_snap_timelapse_dir()
SNAPSHOT_TIMELAPSE_DIR = SNAP_TIMELAPSE_DIR
TIMELAPSE_OUTPUT_DIR = SNAP_TIMELAPSE_DIR
OUTPUT_DIR = SNAP_TIMELAPSE_DIR


def _safe_stem(name: Any) -> str:
    s = str(name if name is not None else "snap_timelapse")
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
    s = s.strip("_") or "snap_timelapse"
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


def parse_snap_date(value: Any) -> Optional[date]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    txt = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(txt).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_event_id(name: str) -> Optional[int]:
    try:
        m = _EVENT_RE.search(str(name))
        if m:
            return int(m.group(1))
    except (TypeError, ValueError):
        pass
    return None


def _resolve_snapshots(value: Any, kwargs: Dict[str, Any]) -> Any:
    if value is not None:
        return value
    for key in ("snapshots", "snapshot", "snaps", "source", "source_path", "sourcePath",
                "input", "input_path", "inputPath", "images", "image_dir", "imageDir",
                "dir", "directory", "folder", "glob", "pattern", "path", "file",
                "files", "src", "filename", "snapshot_dir"):
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


def _resolve_camera(value: Any, kwargs: Dict[str, Any]) -> Any:
    if value is not None:
        return value
    for key in ("camera", "camera_id", "cameraId", "cameraID", "cam", "cam_id",
                "camera_name", "cameraName", "device_id", "deviceId"):
        if kwargs.get(key) is not None:
            return kwargs.get(key)
    return None


def _resolve_snap_date(value: Any, kwargs: Dict[str, Any]) -> Any:
    if value is not None:
        return value
    for key in ("snap_date", "snapDate", "date", "day", "snapshot_date", "snapshotDate",
                "for_date", "on_date", "target_date", "targetDate"):
        if kwargs.get(key) is not None:
            return kwargs.get(key)
    return None


def validate_snap_timelapse_params(
    snapshots: Any = None,
    output: Any = None,
    fps: Any = None,
    factor: Any = None,
    width: Any = None,
    camera: Any = None,
    snap_date: Any = None,
    event_id: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Validate snapshot timelapse inputs. Returns normalized dict or raises ValueError."""
    src = _resolve_snapshots(snapshots, kwargs)
    out = _resolve_output(output, kwargs)
    cam = _resolve_camera(camera, kwargs)
    raw_date = _resolve_snap_date(snap_date, kwargs)
    if event_id is None:
        for key in ("event", "event_pk", "eid"):
            if kwargs.get(key) is not None:
                event_id = kwargs.get(key)
                break

    if fps is None:
        for key in ("frame_rate", "frameRate", "framerate", "output_fps", "out_fps", "rate"):
            if kwargs.get(key) is not None:
                fps = kwargs.get(key)
                break
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

    day: Optional[date] = None
    if raw_date is not None:
        day = parse_snap_date(raw_date)
        if day is None:
            raise ValueError(f"unrecognized date: {raw_date!r}")

    want_event: Optional[int] = None
    if event_id is not None and str(event_id).strip() not in ("", "*", "all"):
        try:
            want_event = int(str(event_id).strip())
        except (TypeError, ValueError):
            raise ValueError(f"unrecognized event_id: {event_id!r}")

    cams: List[str] = []
    if cam is not None:
        if isinstance(cam, (list, tuple)):
            cams = [str(c) for c in cam if str(c).strip()]
        else:
            s = str(cam).strip()
            if s:
                cams = [p.strip() for p in s.replace(";", ",").split(",") if p.strip()]

    if isinstance(src, (list, tuple)):
        if not src:
            raise ValueError("snapshots must not be empty")
        norm_src: Any = [str(p) for p in src]
    elif src is not None:
        norm_src = str(src)
    else:
        norm_src = None

    if norm_src is None and not cams and day is None and want_event is None:
        # Allow bare snapshot-dir scans: fall back to the shared snapshots dir.
        norm_src = str(_default_snapshot_dir(kwargs.get("snapshot_dir", kwargs.get("snapshots_dir"))))

    return {
        "snapshots": norm_src,
        "source": norm_src,
        "source_path": norm_src,
        "inputs": norm_src,
        "output": Path(str(out)).expanduser() if out is not None else None,
        "output_path": Path(str(out)).expanduser() if out is not None else None,
        "fps": float(fps_val),
        "frame_rate": float(fps_val),
        "factor": float(factor_val),
        "timelapse_factor": float(factor_val),
        "speed": float(factor_val),
        "width": w,
        "camera": cams[0] if len(cams) == 1 else (cams if cams else None),
        "cameras": cams,
        "camera_id": cams[0] if len(cams) == 1 else (cams if cams else None),
        "date": day.isoformat() if day else None,
        "snap_date": day.isoformat() if day else None,
        "snapshot_date": day.isoformat() if day else None,
        "day": day.isoformat() if day else None,
        "snap_day": day,
        "event_id": want_event,
        "event": want_event,
    }


def validate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return validate_snap_timelapse_params(*args, **kwargs)


def validate_params(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return validate_snap_timelapse_params(*args, **kwargs)


def _is_image_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in SUPPORTED_IMAGE_EXTS


def find_snapshots(
    snapshots: Any = None,
    snapshot_dir: Any = None,
    camera: Any = None,
    snap_date: Any = None,
    event_id: Any = None,
    pattern: Any = None,
    limit: Any = None,
    **kwargs: Any,
) -> List[Path]:
    """Resolve snapshot image files for an explicit list/glob/dir or a store scan.

    When no explicit ``snapshots`` source is given, scans the shared snapshots
    directory (or ``snapshot_dir`` override) and filters by camera substring,
    date (mtime or ``YYYYMMDD`` filename prefix / ISO date), event id, and
    optional glob/substring pattern.
    """
    if snapshots is None:
        snapshots = _resolve_snapshots(None, kwargs)
    if snapshot_dir is None:
        for key in ("snapshots_dir", "snapshotsDir", "directory", "dir", "folder", "root"):
            if kwargs.get(key) is not None:
                snapshot_dir = kwargs.get(key)
                break
    if camera is None:
        camera = _resolve_camera(None, kwargs)
    if snap_date is None:
        snap_date = _resolve_snap_date(None, kwargs)
    if event_id is None:
        for key in ("event", "event_pk", "eid"):
            if kwargs.get(key) is not None:
                event_id = kwargs.get(key)
                break
    if pattern is None:
        for key in ("glob", "filter", "search", "q", "query", "match"):
            if kwargs.get(key) is not None:
                pattern = kwargs.get(key)
                break
    if limit is None:
        for key in ("n", "count", "size", "max", "top", "per_page"):
            if kwargs.get(key) is not None:
                limit = kwargs.get(key)
                break

    if snapshots is not None:
        if isinstance(snapshots, (list, tuple)):
            out: List[Path] = []
            for item in snapshots:
                s = str(item)
                if any(ch in s for ch in ("*", "?", "[")):
                    out.extend(Path(f) for f in sorted(glob.glob(s)) if Path(f).is_file() and Path(f).suffix.lower() in SUPPORTED_IMAGE_EXTS)
                else:
                    p = Path(s).expanduser()
                    if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGE_EXTS:
                        out.append(p)
                    elif p.is_dir():
                        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.bmp",
                                    "*.JPG", "*.JPEG", "*.PNG", "*.WEBP"):
                            out.extend(f for f in sorted(p.glob(ext)) if f.is_file())
            return sorted(set(out))
        s = str(snapshots)
        if any(ch in s for ch in ("*", "?", "[")):
            return sorted(Path(f) for f in glob.glob(s) if Path(f).is_file() and Path(f).suffix.lower() in SUPPORTED_IMAGE_EXTS)
        p = Path(s).expanduser()
        if p.is_file():
            return [p] if p.suffix.lower() in SUPPORTED_IMAGE_EXTS else []
        if p.is_dir():
            files: List[Path] = []
            for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.bmp",
                        "*.JPG", "*.JPEG", "*.PNG", "*.WEBP"):
                files.extend(p.glob(ext))
            return sorted(set(f for f in files if f.is_file()))
        # Explicit filesystem-looking path that exists nowhere: do not fall
        # through to a store scan (which could pick up unrelated snapshots
        # and mask the missing source). Bare names without path separators
        # are treated as camera/pattern filters and fall through below.
        if s.startswith(("~", "/", ".")) or "/" in s or "\\" in s or p.suffix != "":
            return []

    target = _default_snapshot_dir(snapshot_dir)
    try:
        if not target.exists() or not target.is_dir():
            return []
        entries = [p for p in target.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGE_EXTS]
    except OSError:
        return []

    day = parse_snap_date(snap_date) if snap_date is not None else None
    want_event: Optional[int] = None
    if event_id is not None and str(event_id).strip() not in ("", "*", "all"):
        try:
            want_event = int(str(event_id).strip())
        except (TypeError, ValueError):
            want_event = None

    needles: List[str] = []
    if camera is not None:
        if isinstance(camera, (list, tuple)):
            needles = [str(c).strip().lower() for c in camera if str(c).strip()]
        else:
            needles = [p.strip().lower() for p in str(camera).replace(";", ",").split(",") if p.strip()]

    items: List[Path] = []
    for p in entries:
        if want_event is not None and _parse_event_id(p.name) != want_event:
            continue
        if needles and not any(n in p.name.lower() for n in needles):
            continue
        if day is not None:
            prefix = day.strftime("%Y%m%d")
            iso = day.isoformat()
            name_hit = prefix in p.name or iso in p.name
            if not name_hit:
                try:
                    mtime_day = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).date()
                except OSError:
                    continue
                if mtime_day != day:
                    continue
        if pattern is not None and str(pattern).strip() not in ("", "*", "all"):
            pat = str(pattern).strip()
            import fnmatch as _fn

            if any(ch in pat for ch in ("*", "?", "[")):
                if not _fn.fnmatch(p.name, pat):
                    continue
            elif pat.lower() not in p.name.lower():
                continue
        items.append(p)

    def _sort_key(p: Path):
        try:
            return (float(p.stat().st_mtime), p.name)
        except OSError:
            return (0.0, p.name)

    items.sort(key=_sort_key)
    if limit is not None:
        try:
            n = int(float(str(limit).strip()))
            if n >= 0:
                items = items[:n]
        except (TypeError, ValueError):
            pass
    return items


def list_snapshots(*args: Any, **kwargs: Any) -> List[Path]:
    if args and kwargs.get("snapshots") is None and kwargs.get("source") is None:
        return find_snapshots(snapshots=args[0], **kwargs)
    return find_snapshots(*args, **kwargs)


def discover_snapshots(*args: Any, **kwargs: Any) -> List[Path]:
    return find_snapshots(*args, **kwargs)


def collect_snapshots(*args: Any, **kwargs: Any) -> List[Path]:
    return find_snapshots(*args, **kwargs)


def _default_output_path(snapshots: Any, snap_dir: Any = None) -> Path:
    d = _default_snap_timelapse_dir(snap_dir)
    if isinstance(snapshots, (list, tuple)) and snapshots:
        stem = _safe_stem(Path(str(snapshots[0])).stem)
    elif snapshots is not None:
        try:
            stem = _safe_stem(Path(str(snapshots)).stem)
        except Exception:  # noqa: BLE001
            stem = "snapshots"
    else:
        stem = "snapshots"
    return d / f"{stem}_snap_timelapse_{uuid.uuid4().hex}.mp4"


def build_ffmpeg_command(
    snapshots: Any = None,
    output: Any = None,
    fps: Any = DEFAULT_FPS,
    factor: Any = DEFAULT_FACTOR,
    width: Any = None,
    overwrite: bool = True,
    ffmpeg: Optional[str] = None,
    **kwargs: Any,
) -> List[str]:
    """Build the ffmpeg concat command rendering snapshots to mp4."""
    if snapshots is None:
        snapshots = _resolve_snapshots(None, kwargs)
    if output is None:
        output = _resolve_output(None, kwargs)
    camera = kwargs.pop("camera", kwargs.pop("camera_id", None))
    snap_date = kwargs.pop("snap_date", kwargs.pop("snapshot_date", kwargs.pop("date", kwargs.pop("day", None))))
    event_id = kwargs.pop("event_id", kwargs.pop("event", None))
    normalized = validate_snap_timelapse_params(
        snapshots, output=output, fps=fps, factor=factor, width=width,
        camera=camera, snap_date=snap_date, event_id=event_id,
        **kwargs,
    )
    src = normalized["snapshots"]
    out = normalized["output"]
    if out is None:
        out = _default_output_path(src, kwargs.get("snap_timelapse_dir", kwargs.get("output_dir")))
    fps_f = float(normalized["fps"])
    factor_f = float(normalized["factor"])
    w = normalized["width"]

    resolved = find_snapshots(
        snapshots=src,
        snapshot_dir=kwargs.get("snapshot_dir", kwargs.get("snapshots_dir")),
        camera=normalized.get("cameras") or normalized.get("camera"),
        snap_date=normalized.get("snap_day") or normalized.get("date"),
        event_id=normalized.get("event_id"),
        pattern=kwargs.get("pattern", kwargs.get("glob")),
    )
    if not resolved:
        raise FileNotFoundError("No snapshots found for timelapse")

    binary = ffmpeg or kwargs.get("ffmpeg_path") or kwargs.get("binary") or FFMPEG_PATH
    out_path = Path(str(out)).expanduser()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    list_file = out_path.parent / (out_path.stem + "_snap_inputs.txt")
    with open(list_file, "w", encoding="utf-8") as fh:
        for p in resolved:
            fh.write(f"file '{p}'\n")

    cmd: List[str] = [str(binary)]
    cmd.append("-y" if overwrite else "-n")
    cmd += ["-hide_banner", "-loglevel", "error"]
    cmd += ["-framerate", str(fps_f), "-f", "concat", "-safe", "0", "-i", str(list_file)]

    vf_parts: List[str] = []
    if w is not None:
        vf_parts.append(f"scale={int(w)}:-2")
    if factor_f != 1.0:
        vf_parts.append(f"setpts=PTS/{factor_f}")
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    cmd += ["-an", "-r", str(fps_f), "-c:v", "libx264", "-preset", "fast",
            "-pix_fmt", "yuv420p", str(out_path)]
    return cmd


def build_command(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_command(*args, **kwargs)


def ffmpeg_command(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_command(*args, **kwargs)


def build_snap_timelapse(
    snapshots: Any = None,
    output: Any = None,
    fps: Any = DEFAULT_FPS,
    factor: Any = DEFAULT_FACTOR,
    width: Any = None,
    overwrite: bool = True,
    ffmpeg: Optional[str] = None,
    snap_timelapse_dir: Any = None,
    timeout: float = 300.0,
    **kwargs: Any,
) -> str:
    """Render snapshots to a timelapse mp4. Returns the output path as a string."""
    if snapshots is None:
        for key in ("source", "source_path", "input", "input_path", "path", "file",
                     "files", "src", "image_dir", "images", "snapshot_dir"):
            if kwargs.get(key) is not None:
                snapshots = kwargs.pop(key)
                break
    if output is None:
        for key in ("output_path", "dest", "destination", "target", "out", "outfile"):
            if kwargs.get(key) is not None:
                output = kwargs.pop(key)
                break
    camera = kwargs.pop("camera", kwargs.pop("camera_id", None))
    snap_date = kwargs.pop("snap_date", kwargs.pop("snapshot_date", kwargs.pop("date", kwargs.pop("day", None))))
    event_id = kwargs.pop("event_id", kwargs.pop("event", None))
    normalized = validate_snap_timelapse_params(
        snapshots, output=output, fps=fps, factor=factor, width=width,
        camera=camera, snap_date=snap_date, event_id=event_id,
        **kwargs,
    )
    src = normalized["snapshots"]
    out_path: Optional[Path] = normalized["output"]
    fps_f = float(normalized["fps"])
    factor_f = float(normalized["factor"])
    w = normalized["width"]

    if kwargs.get("overwrite") is not None:
        overwrite = bool(kwargs.get("overwrite"))

    # Existence check for explicit file lists: fail fast on missing files
    # instead of silently dropping them.
    if isinstance(src, (list, tuple)):
        missing = [str(p) for p in src
                   if not any(ch in str(p) for ch in ("*", "?", "["))
                   and not Path(str(p)).expanduser().exists()]
        if missing:
            raise FileNotFoundError(f"Snapshot images not found: {missing[0]}")

    resolved = find_snapshots(
        snapshots=src,
        snapshot_dir=kwargs.get("snapshot_dir", kwargs.get("snapshots_dir")),
        camera=normalized.get("cameras") or normalized.get("camera"),
        snap_date=normalized.get("snap_day") or normalized.get("date"),
        event_id=normalized.get("event_id"),
        pattern=kwargs.get("pattern", kwargs.get("glob")),
    )
    if not resolved:
        if isinstance(src, (list, tuple)):
            raise FileNotFoundError(f"Snapshot images not found: {src[0] if src else src}")
        raise FileNotFoundError(f"No snapshots found for timelapse: {src}")

    if out_path is None:
        out_path = _default_output_path(
            [str(p) for p in resolved],
            snap_timelapse_dir or kwargs.get("snap_timelapse_dir") or kwargs.get("output_dir"),
        )
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
        raise RuntimeError(f"ffmpeg snapshot timelapse failed: {exc.stderr or exc}") from exc
    return str(out_path)


def create_job(
    snapshots: Any = None,
    output: Any = None,
    fps: Any = None,
    factor: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    normalized = validate_snap_timelapse_params(snapshots, output=output, fps=fps, factor=factor, **kwargs)
    job_id = str(kwargs.get("job_id") or uuid.uuid4())
    job: Dict[str, Any] = {
        "id": job_id,
        "job_id": job_id,
        "snap_job_id": job_id,
        "snap_timelapse_job_id": job_id,
        "snapshots": normalized["snapshots"] if not isinstance(normalized["snapshots"], list) else list(normalized["snapshots"]),
        "source": normalized["snapshots"] if not isinstance(normalized["snapshots"], list) else list(normalized["snapshots"]),
        "source_path": normalized["snapshots"] if not isinstance(normalized["snapshots"], list) else list(normalized["snapshots"]),
        "output": str(normalized["output"]) if normalized["output"] is not None else None,
        "output_path": str(normalized["output"]) if normalized["output"] is not None else None,
        "path": str(normalized["output"]) if normalized["output"] is not None else None,
        "fps": normalized["fps"],
        "frame_rate": normalized["fps"],
        "factor": normalized["factor"],
        "timelapse_factor": normalized["factor"],
        "speed": normalized["factor"],
        "width": normalized["width"],
        "camera": normalized.get("camera"),
        "cameras": normalized.get("cameras"),
        "date": normalized.get("date"),
        "snap_date": normalized.get("date"),
        "event_id": normalized.get("event_id"),
        "status": "queued",
        "state": "queued",
        "progress": 0.0,
        "error": None,
    }
    with _jobs_lock:
        SNAP_TIMELAPSE_JOBS[job_id] = job
    return job


def get_job(job_id: Any) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        return SNAP_TIMELAPSE_JOBS.get(str(job_id))


def list_jobs() -> List[Dict[str, Any]]:
    with _jobs_lock:
        return list(SNAP_TIMELAPSE_JOBS.values())


def update_job(job_id: Any, **fields: Any) -> Optional[Dict[str, Any]]:
    key = str(job_id)
    with _jobs_lock:
        job = SNAP_TIMELAPSE_JOBS.get(key)
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
        return SNAP_TIMELAPSE_JOBS.pop(str(job_id), None) is not None


def run_job(job_id: Any, **kwargs: Any) -> Dict[str, Any]:
    key = str(job_id)
    job = get_job(key)
    if job is None:
        raise KeyError(f"Unknown snapshot timelapse job {key}")
    update_job(key, status="processing", state="processing", progress=10.0)
    try:
        for _k in ("camera", "camera_id", "cameras", "snap_date", "snapshot_date",
                   "date", "day", "event_id", "event"):
            kwargs.pop(_k, None)
        out = build_snap_timelapse(
            job.get("snapshots", job.get("source")),
            job.get("output"),
            fps=job.get("fps", DEFAULT_FPS),
            factor=job.get("factor", DEFAULT_FACTOR),
            width=job.get("width"),
            camera=job.get("cameras", job.get("camera")),
            snap_date=job.get("date", job.get("snap_date")),
            event_id=job.get("event_id"),
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
def create_snap_timelapse(*args: Any, **kwargs: Any) -> str:
    return str(build_snap_timelapse(*args, **kwargs))


def make_snap_timelapse(*args: Any, **kwargs: Any) -> str:
    return str(build_snap_timelapse(*args, **kwargs))


def render_snap_timelapse(*args: Any, **kwargs: Any) -> str:
    return str(build_snap_timelapse(*args, **kwargs))


def generate_snap_timelapse(*args: Any, **kwargs: Any) -> str:
    return str(build_snap_timelapse(*args, **kwargs))


def export_snap_timelapse(*args: Any, **kwargs: Any) -> str:
    return str(build_snap_timelapse(*args, **kwargs))


def build_snapshot_timelapse(*args: Any, **kwargs: Any) -> str:
    return str(build_snap_timelapse(*args, **kwargs))


def create_snapshot_timelapse(*args: Any, **kwargs: Any) -> str:
    return str(build_snap_timelapse(*args, **kwargs))


def build(*args: Any, **kwargs: Any) -> str:
    return str(build_snap_timelapse(*args, **kwargs))


def create(*args: Any, **kwargs: Any) -> str:
    return str(build_snap_timelapse(*args, **kwargs))


def make(*args: Any, **kwargs: Any) -> str:
    return str(build_snap_timelapse(*args, **kwargs))


def render(*args: Any, **kwargs: Any) -> str:
    return str(build_snap_timelapse(*args, **kwargs))


def generate(*args: Any, **kwargs: Any) -> str:
    return str(build_snap_timelapse(*args, **kwargs))


def create_snap_timelapse_job(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_job(*args, **kwargs)


def new_job(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_job(*args, **kwargs)


def get_snap_timelapse_job(job_id: Any) -> Optional[Dict[str, Any]]:
    return get_job(job_id)


def list_snap_timelapse_jobs() -> List[Dict[str, Any]]:
    return list_jobs()


cancel_job = delete_job
cancel = delete_job
remove_job = delete_job
get = get_job
list_all = list_jobs


@dataclass
class SnapTimelapseResult:
    source: str
    output_path: str
    fps: float = DEFAULT_FPS
    factor: float = DEFAULT_FACTOR

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
            "ok": True,
            "success": True,
        }


class SnapTimelapseService:
    """Object-oriented facade over :func:`build_snap_timelapse`."""

    def __init__(self, snap_timelapse_dir: Any = None, ffmpeg: Optional[str] = None, **kwargs: Any) -> None:
        self.snap_timelapse_dir = _default_snap_timelapse_dir(snap_timelapse_dir or kwargs.get("output_dir"))
        self.snapshot_dir = _default_snapshot_dir(kwargs.get("snapshot_dir", kwargs.get("snapshots_dir")))
        self.ffmpeg = ffmpeg or kwargs.get("ffmpeg_path") or FFMPEG_PATH
        self.jobs = SNAP_TIMELAPSE_JOBS

    def build(self, snapshots: Any = None, output: Any = None, **kwargs: Any) -> str:
        kwargs.setdefault("snap_timelapse_dir", self.snap_timelapse_dir)
        return str(build_snap_timelapse(snapshots, output, ffmpeg=kwargs.pop("ffmpeg", self.ffmpeg), **kwargs))

    def create(self, *args: Any, **kwargs: Any) -> str:
        kwargs.setdefault("ffmpeg", self.ffmpeg)
        kwargs.setdefault("snap_timelapse_dir", self.snap_timelapse_dir)
        return str(build_snap_timelapse(*args, **kwargs))

    def validate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return validate_snap_timelapse_params(*args, **kwargs)

    def build_command(self, *args: Any, **kwargs: Any) -> List[str]:
        kwargs.setdefault("ffmpeg", self.ffmpeg)
        return build_ffmpeg_command(*args, **kwargs)

    def find_snapshots(self, *args: Any, **kwargs: Any) -> List[Path]:
        if args and not kwargs.get("snapshots") and not kwargs.get("source"):
            if len(args) >= 1:
                return find_snapshots(snapshots=args[0], **kwargs)
        return find_snapshots(*args, **kwargs)

    def create_job(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_job(*args, **kwargs)

    def get_job(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_job(*args, **kwargs)

    def list_jobs(self) -> List[Dict[str, Any]]:
        return list_jobs()

    def run_job(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return run_job(*args, **kwargs)

    # Aliases.
    def build_snap_timelapse(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)

    def build_snapshot_timelapse(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)

    def create_snap_timelapse(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)

    def render_snap_timelapse(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)


class SnapTimelapseBuilder(SnapTimelapseService):
    pass


class SnapTimelapseCreator(SnapTimelapseService):
    pass


class SnapTimelapseGenerator(SnapTimelapseService):
    pass


class SnapshotTimelapseService(SnapTimelapseService):
    pass


class SnapshotTimelapseBuilder(SnapTimelapseService):
    pass


class SnapTimelapse(SnapTimelapseService):
    pass


class Builder(SnapTimelapseService):
    pass


class Service(SnapTimelapseService):
    pass


snap_timelapse_service = SnapTimelapseService()
builder = snap_timelapse_service
service = snap_timelapse_service
snap_builder = snap_timelapse_service
default_service = snap_timelapse_service
instance = snap_timelapse_service
singleton = snap_timelapse_service

SnapTimelapseManager = SnapTimelapseService
SnapshotTimelapseManager = SnapTimelapseService

find_images = find_snapshots
list_images = find_snapshots
discover_images = find_snapshots
build_snapshot_timelapse_alias = build_snapshot_timelapse
build_timelapse = build_snap_timelapse

__all__ = [
    "FFMPEG_PATH",
    "FFMPEG_BIN",
    "SNAP_TIMELAPSE_DIR",
    "SNAP_TIMELAPSE_JOBS",
    "SUPPORTED_IMAGE_EXTS",
    "parse_fps",
    "parse_factor",
    "parse_snap_date",
    "coerce_fps",
    "coerce_factor",
    "validate_snap_timelapse_params",
    "validate",
    "validate_params",
    "build_ffmpeg_command",
    "build_command",
    "ffmpeg_command",
    "find_snapshots",
    "list_snapshots",
    "discover_snapshots",
    "collect_snapshots",
    "build_snap_timelapse",
    "create_snap_timelapse",
    "make_snap_timelapse",
    "render_snap_timelapse",
    "generate_snap_timelapse",
    "export_snap_timelapse",
    "build_snapshot_timelapse",
    "create_snapshot_timelapse",
    "create_job",
    "get_job",
    "list_jobs",
    "update_job",
    "delete_job",
    "run_job",
    "SnapTimelapseResult",
    "SnapTimelapseService",
    "SnapTimelapseBuilder",
    "SnapshotTimelapseService",
    "snap_timelapse_service",
]
