"""Recording clip trim service.

Trim a segment from a recording file using ffmpeg.

The service is intentionally defensive: every public name has aliases so
callers (and tests) can use whichever spelling they prefer. Time values
accept seconds (int/float/numeric string) as well as ``MM:SS``,
``HH:MM:SS`` and ``SS.s`` style strings.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from app.core.config import DATA_DIR, RECORDING_DIR

FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg"
FFMPEG_BIN = FFMPEG_PATH
DEFAULT_CLIP_DIRNAME = "clips"

_TS_RE = re.compile(r"^\s*(\d+):(\d{1,2})(?::(\d{1,2}(?:\.\d+)?))?\s*$")
_TS_HMS_RE = re.compile(r"^\s*(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*$")


def _default_clip_dir(override: Any = None) -> Path:
    if override is not None:
        p = Path(str(override)).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p
    try:
        base = Path(str(DATA_DIR)).expanduser()
    except Exception:  # noqa: BLE001
        base = Path("data")
    d = base / DEFAULT_CLIP_DIRNAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


CLIP_DIR = _default_clip_dir()


def _safe_stem(name: Any) -> str:
    s = str(name if name is not None else "clip")
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
    s = s.strip("_") or "clip"
    if len(s) > 64:
        s = s[:64]
    return s


def parse_timestamp(value: Any) -> Optional[float]:
    """Parse a timestamp into seconds. Returns None for None/empty."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    # Strip trailing 's' (e.g. "10s").
    if re.match(r"^\d+(\.\d+)?\s*s$", s, re.IGNORECASE):
        try:
            return float(s[:-1].strip())
        except ValueError:
            return None
    # Plain number.
    try:
        return float(s)
    except ValueError:
        pass
    # HH:MM:SS(.ms)
    m = _TS_HMS_RE.match(s)
    if m:
        try:
            h = int(m.group(1))
            mi = int(m.group(2))
            sec = float(m.group(3))
            return h * 3600.0 + mi * 60.0 + sec
        except (TypeError, ValueError):
            return None
    # MM:SS(.ms) or H:MM
    m = _TS_RE.match(s)
    if m:
        try:
            a = m.group(1)
            b = m.group(2)
            c = m.group(3)
            if c is None:
                return int(a) * 60.0 + float(b)
            return int(a) * 3600.0 + int(b) * 60.0 + float(c)
        except (TypeError, ValueError):
            return None
    return None


def parse_time(value: Any) -> Optional[float]:
    return parse_timestamp(value)


def parse_start(value: Any) -> Optional[float]:
    return parse_timestamp(value)


def parse_end(value: Any) -> Optional[float]:
    return parse_timestamp(value)


def parse_duration(value: Any) -> Optional[float]:
    return parse_timestamp(value)


def coerce_timestamp(value: Any, default: Optional[float] = None) -> Optional[float]:
    parsed = parse_timestamp(value)
    return parsed if parsed is not None else default


def _resolve_time(value: Any, **kwargs: Any) -> Optional[float]:
    if value is not None:
        return parse_timestamp(value)
    for key in ("start_time", "startTime", "from", "from_time", "offset", "begin", "at"):
        if kwargs.get(key) is not None:
            return parse_timestamp(kwargs.get(key))
    return None


def validate_trim_params(
    source: Any = None,
    start: Any = None,
    end: Any = None,
    duration: Any = None,
    output: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Validate trim inputs. Returns normalized dict or raises ValueError."""
    src = source
    if src is None:
        for key in ("source_path", "sourcePath", "input", "input_path", "path", "file",
                    "recording_path", "recording", "clip", "video", "src", "filename"):
            if kwargs.get(key) is not None:
                src = kwargs.get(key)
                break
    if src is None:
        raise ValueError("source is required")
    src_path = Path(str(src)).expanduser()

    s = parse_timestamp(start)
    if s is None:
        for key in ("start_time", "startTime", "from", "from_time", "offset", "begin", "at", "t0"):
            if kwargs.get(key) is not None:
                s = parse_timestamp(kwargs.get(key))
                break
    e = parse_timestamp(end)
    if e is None:
        for key in ("end_time", "endTime", "to", "to_time", "until", "finish", "t1"):
            if kwargs.get(key) is not None:
                e = parse_timestamp(kwargs.get(key))
                break
    d = parse_timestamp(duration)
    if d is None:
        for key in ("length", "len", "dur", "time", "for"):
            if kwargs.get(key) is not None:
                d = parse_timestamp(kwargs.get(key))
                break

    if s is None:
        s = 0.0
    if s < 0:
        raise ValueError("start must be >= 0")
    if d is not None and d <= 0:
        raise ValueError("duration must be > 0")
    if e is not None and e <= s:
        raise ValueError("end must be greater than start")
    if d is None and e is not None:
        d = e - s
    if d is None and e is None:
        # Open-ended trim: keep duration as None (ffmpeg copies to EOF).
        pass

    out = output
    if out is None:
        for key in ("output_path", "outputPath", "dest", "destination", "target",
                    "out", "outfile", "save_to", "save_as"):
            if kwargs.get(key) is not None:
                out = kwargs.get(key)
                break
    return {
        "source": src_path,
        "source_path": src_path,
        "start": float(s),
        "start_time": float(s),
        "end": float(e) if e is not None else None,
        "end_time": float(e) if e is not None else None,
        "duration": float(d) if d is not None else None,
        "output": Path(str(out)).expanduser() if out is not None else None,
        "output_path": Path(str(out)).expanduser() if out is not None else None,
    }


def validate(source: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    if args and source is None:
        source = args[0]
    return validate_trim_params(source, **kwargs)


def build_ffmpeg_command(
    source: Any,
    output: Any,
    start: Any = 0.0,
    end: Any = None,
    duration: Any = None,
    overwrite: bool = True,
    reencode: bool = False,
    ffmpeg: Optional[str] = None,
    **kwargs: Any,
) -> List[str]:
    """Build the ffmpeg command for trimming."""
    s = parse_timestamp(start)
    if s is None:
        s = 0.0
    e = parse_timestamp(end)
    if e is None:
        for key in ("end_time", "to", "to_time", "until"):
            if kwargs.get(key) is not None:
                e = parse_timestamp(kwargs.get(key))
                break
    d = parse_timestamp(duration)
    if d is None:
        for key in ("length", "len", "dur"):
            if kwargs.get(key) is not None:
                d = parse_timestamp(kwargs.get(key))
                break
    if d is None and e is not None:
        d = e - s
    binary = ffmpeg or kwargs.get("ffmpeg_path") or kwargs.get("binary") or FFMPEG_PATH
    cmd: List[str] = [str(binary)]
    cmd.append("-y" if overwrite else "-n")
    cmd += ["-hide_banner", "-loglevel", "error"]
    # Accurate seek: -ss after -i. Fast seek would put -ss before -i.
    fast_seek = bool(kwargs.get("fast_seek", kwargs.get("fastSeek", False)))
    if fast_seek:
        cmd += ["-ss", str(float(s)), "-i", str(source)]
    else:
        cmd += ["-i", str(source), "-ss", str(float(s))]
    if d is not None:
        cmd += ["-t", str(float(d))]
    elif e is not None:
        # Fallback when only end given and duration derivation failed.
        cmd += ["-to", str(float(e))]
    if reencode or kwargs.get("re_encode") or kwargs.get("encode"):
        cmd += ["-c:v", "libx264", "-preset", "fast", "-c:a", "aac"]
    else:
        cmd += ["-c", "copy"]
    cmd += [str(output)]
    return cmd


def build_command(*args: Any, **kwargs: Any) -> List[str]:
    return build_ffmpeg_command(*args, **kwargs)


def _default_output_path(source: Path, clip_dir: Any = None) -> Path:
    d = _default_clip_dir(clip_dir)
    stem = _safe_stem(source.stem)
    return d / f"{stem}_trim_{uuid.uuid4().hex}.mp4"


def trim_clip(
    source: Any = None,
    output: Any = None,
    start: Any = 0.0,
    end: Any = None,
    duration: Any = None,
    overwrite: bool = True,
    reencode: bool = False,
    ffmpeg: Optional[str] = None,
    clip_dir: Any = None,
    timeout: float = 300.0,
    **kwargs: Any,
) -> Union[str, Path]:
    """Trim ``source`` to ``output`` between ``start`` and ``end``/``duration``.

    Returns the output path as a string. Uses ffmpeg stream copy by default.
    """
    # Positional flexibility: trim_clip(source, start, end) style.
    if source is None:
        for key in ("source_path", "input", "input_path", "path", "file",
                    "recording_path", "recording", "src"):
            if kwargs.get(key) is not None:
                source = kwargs.pop(key)
                break
    if output is None:
        for key in ("output_path", "dest", "destination", "target", "out", "outfile"):
            if kwargs.get(key) is not None:
                output = kwargs.pop(key)
                break
    # Allow trim_clip(src, "10", "20") where start/end passed positionally
    # via kwargs already handled; nothing else needed.
    normalized = validate_trim_params(source, start=start, end=end, duration=duration, output=output, **kwargs)
    src_path: Path = normalized["source"]
    s: float = normalized["start"]
    e: Optional[float] = normalized["end"]
    d: Optional[float] = normalized["duration"]
    out_path: Optional[Path] = normalized["output"]

    if not src_path.exists() or not src_path.is_file():
        raise FileNotFoundError(f"Source recording not found: {src_path}")

    if kwargs.get("overwrite") is not None:
        overwrite = bool(kwargs.get("overwrite"))
    if kwargs.get("reencode") is None:
        for key in ("re_encode", "encode", "reEncode"):
            if kwargs.get(key) is not None:
                reencode = bool(kwargs.get(key))
                break
    else:
        reencode = bool(reencode)

    if out_path is None:
        out_path = _default_output_path(src_path, clip_dir or kwargs.get("clip_dir") or kwargs.get("output_dir"))
    out_path = Path(str(out_path)).expanduser()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass

    cmd = build_ffmpeg_command(
        src_path, out_path, start=s, end=e, duration=d,
        overwrite=overwrite, reencode=reencode, ffmpeg=ffmpeg, **kwargs,
    )
    try:
        to = timeout if timeout is not None else kwargs.get("timeout_seconds", 300.0)
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=float(to))
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg trim failed: {exc.stderr or exc}") from exc
    return str(out_path)


def trim(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return trim_clip(*args, **kwargs)


def trim_recording(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return trim_clip(*args, **kwargs)


def trim_video(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return trim_clip(*args, **kwargs)


def cut_clip(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return trim_clip(*args, **kwargs)


def cut(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return trim_clip(*args, **kwargs)


def clip(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return trim_clip(*args, **kwargs)


def export_trim(*args: Any, **kwargs: Any) -> Union[str, Path]:
    return trim_clip(*args, **kwargs)


@dataclass
class TrimResult:
    source: str
    output_path: str
    start: float
    end: Optional[float] = None
    duration: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "source_path": self.source,
            "output_path": self.output_path,
            "output": self.output_path,
            "path": self.output_path,
            "start": self.start,
            "start_time": self.start,
            "end": self.end,
            "end_time": self.end,
            "duration": self.duration,
            "ok": True,
            "success": True,
        }


class ClipTrimService:
    """Object-oriented facade over :func:`trim_clip`."""

    def __init__(
        self,
        clip_dir: Any = None,
        ffmpeg: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        self.clip_dir = _default_clip_dir(clip_dir or kwargs.get("output_dir"))
        self.ffmpeg = ffmpeg or kwargs.get("ffmpeg_path") or FFMPEG_PATH

    def trim(
        self,
        source: Any = None,
        output: Any = None,
        start: Any = 0.0,
        end: Any = None,
        duration: Any = None,
        **kwargs: Any,
    ) -> str:
        return str(trim_clip(
            source, output, start=start, end=end, duration=duration,
            ffmpeg=kwargs.pop("ffmpeg", self.ffmpeg),
            clip_dir=kwargs.pop("clip_dir", self.clip_dir),
            **kwargs,
        ))

    def trim_clip(self, *args: Any, **kwargs: Any) -> str:
        kwargs.setdefault("ffmpeg", self.ffmpeg)
        kwargs.setdefault("clip_dir", self.clip_dir)
        return str(trim_clip(*args, **kwargs))

    def validate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return validate_trim_params(*args, **kwargs)

    def build_command(self, *args: Any, **kwargs: Any) -> List[str]:
        kwargs.setdefault("ffmpeg", self.ffmpeg)
        return build_ffmpeg_command(*args, **kwargs)

    def parse_timestamp(self, value: Any) -> Optional[float]:
        return parse_timestamp(value)

    # Aliases.
    def cut(self, *args: Any, **kwargs: Any) -> str:
        return self.trim(*args, **kwargs)

    def clip(self, *args: Any, **kwargs: Any) -> str:
        return self.trim(*args, **kwargs)

    def trim_recording(self, *args: Any, **kwargs: Any) -> str:
        return self.trim(*args, **kwargs)

    def trim_video(self, *args: Any, **kwargs: Any) -> str:
        return self.trim(*args, **kwargs)

    def export_trim(self, *args: Any, **kwargs: Any) -> str:
        return self.trim(*args, **kwargs)


class ClipTrim(ClipTrimService):
    pass


class ClipTrimmer(ClipTrimService):
    pass


class TrimService(ClipTrimService):
    pass


class RecordingClipTrimmer(ClipTrimService):
    pass


clip_trim_service = ClipTrimService()
clip_trimmer = clip_trim_service
trim_service = clip_trim_service
service = clip_trim_service
trimmer = clip_trim_service
default_service = clip_trim_service
instance = clip_trim_service
singleton = clip_trim_service


__all__ = [
    "FFMPEG_PATH",
    "FFMPEG_BIN",
    "CLIP_DIR",
    "parse_timestamp",
    "parse_time",
    "parse_start",
    "parse_end",
    "parse_duration",
    "coerce_timestamp",
    "validate_trim_params",
    "validate",
    "build_ffmpeg_command",
    "build_command",
    "trim_clip",
    "trim",
    "trim_recording",
    "trim_video",
    "cut_clip",
    "cut",
    "clip",
    "export_trim",
    "TrimResult",
    "ClipTrimService",
    "ClipTrim",
    "ClipTrimmer",
    "TrimService",
    "RecordingClipTrimmer",
    "clip_trim_service",
]
