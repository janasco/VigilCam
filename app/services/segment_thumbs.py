"""Recording segment thumbnail service for VigilCam.

Per-segment JPEG thumbnails for recorded video segments. This is distinct
from the live-frame cache:

- :mod:`app.services.thumbnails` caches one JPEG per camera (live frame
  only, no segment argument, no recording path handling).
- This module caches one JPEG per *recording segment* (filesystem
  ``*.mp4`` segment or ``Recording`` DB row), keyed by segment path +
  dimensions + intra-segment offset.

Tiles are synthetic placeholders labelled with segment name + offset; the
frame extractor (:func:`_try_ffmpeg_frame`) is isolated so a real
frame-grabber can replace it without changing cache semantics.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from app.core.config import RECORDING_DIR as _DEFAULT_RECORDING_DIR  # type: ignore
except Exception:  # noqa: BLE001
    _DEFAULT_RECORDING_DIR = Path("data/recordings")  # type: ignore

# Tunables.
DEFAULT_TTL_SECONDS = 60.0
MAX_CACHE_ENTRIES = 256
DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 180
MIN_DIMENSION = 16
MAX_DIMENSION = 1280
DEFAULT_OFFSET_SECONDS = 1.0
MAX_OFFSET_SECONDS = 3600.0
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".ts", ".mov", ".m4v")

CONTENT_TYPE_JPEG = "image/jpeg"

_TS_RE = re.compile(r"(\d{8})_(\d{6})")

_LOCK = threading.Lock()
# cache key -> entry dict {data, content_type, updated_at, hits, width, height,
#                          segment, offset, exists}
_CACHE: Dict[str, Dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_segment(*args: Any, **kwargs: Any) -> str:
    value: Any = args[0] if args else None
    if value is None:
        for key in (
            "segment", "segment_path", "segmentPath", "segment_file",
            "path", "file_path", "filePath", "file", "filepath",
            "recording_path", "recordingPath", "recording", "clip",
            "video", "src", "source", "source_path", "sourcePath",
            "filename", "file_name", "name", "segment_name",
        ):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, dict):
        for key in (
            "segment_path", "path", "file_path", "filePath", "recording_path",
            "file", "segment", "src", "source",
        ):
            if value.get(key):
                value = value.get(key)
                break
        else:
            for key in ("recording_id", "recordingId", "segment_id", "segmentId", "id"):
                if value.get(key) is not None:
                    value = value.get(key)
                    break
    s = str(value).strip()
    if not s:
        return ""
    if len(s) > 1024:
        s = s[:1024]
    return s


def _coerce_recording_id(*args: Any, **kwargs: Any) -> Optional[int]:
    value: Any = args[0] if args else None
    if value is None:
        for key in (
            "recording_id", "recordingId", "recordingID", "rec_id", "recId",
            "recording", "rec",
        ):
            if kwargs.get(key) is not None:
                value = kwargs.get(key)
                break
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for key in ("recording_id", "recordingId", "id"):
            if value.get(key) is not None:
                value = value.get(key)
                break
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _coerce_ttl(*args: Any, **kwargs: Any) -> float:
    ttl: Any = args[0] if args else None
    for alias in ("ttl", "ttl_seconds", "ttl_secs", "max_age", "maxage",
                  "cache_ttl", "cache_ttl_seconds", "age", "expires_in"):
        if ttl is None and kwargs.get(alias) is not None:
            ttl = kwargs.get(alias)
    if ttl is None:
        return float(DEFAULT_TTL_SECONDS)
    try:
        return float(ttl)
    except (TypeError, ValueError):
        return float(DEFAULT_TTL_SECONDS)


def _coerce_dims(*args: Any, **kwargs: Any) -> Tuple[int, int]:
    width: Any = args[0] if len(args) > 0 else None
    height: Any = args[1] if len(args) > 1 else None
    # kwargs may also carry width/height under the same names (duplicate-safe
    # because this signature uses *args, not named params).
    if width is None and kwargs.get("width") is not None:
        width = kwargs.get("width")
    if height is None and kwargs.get("height") is not None:
        height = kwargs.get("height")
    if width is None:
        for alias in ("w", "cols", "width_px", "thumbnail_width", "thumb_width",
                      "tile_width", "tw", "video_width"):
            if kwargs.get(alias) is not None:
                width = kwargs.get(alias)
                break
    if height is None:
        for alias in ("h", "rows", "height_px", "thumbnail_height", "thumb_height",
                      "tile_height", "th", "video_height"):
            if kwargs.get(alias) is not None:
                height = kwargs.get(alias)
                break
    try:
        w = int(width) if width is not None else DEFAULT_WIDTH
    except (TypeError, ValueError):
        w = DEFAULT_WIDTH
    try:
        h = int(height) if height is not None else DEFAULT_HEIGHT
    except (TypeError, ValueError):
        h = DEFAULT_HEIGHT
    w = max(MIN_DIMENSION, min(MAX_DIMENSION, w))
    h = max(MIN_DIMENSION, min(MAX_DIMENSION, h))
    return w, h


def _coerce_offset(*args: Any, **kwargs: Any) -> float:
    value: Any = args[0] if args else None
    if value is None:
        for alias in ("offset", "offset_seconds", "offsetSeconds", "seek",
                      "position", "at_seconds", "thumb_offset", "frame_offset",
                      "ss", "time_offset", "t", "second"):
            if kwargs.get(alias) is not None:
                value = kwargs.get(alias)
                break
    if value is None:
        return float(DEFAULT_OFFSET_SECONDS)
    try:
        v = float(value)
    except (TypeError, ValueError):
        try:
            v = float(str(value).strip())
        except (TypeError, ValueError):
            return float(DEFAULT_OFFSET_SECONDS)
    if v < 0:
        v = 0.0
    if v > MAX_OFFSET_SECONDS:
        v = MAX_OFFSET_SECONDS
    return float(v)


def _default_recording_dir(override: Any = None) -> Path:
    if override is not None:
        try:
            return Path(str(override)).expanduser()
        except Exception:  # noqa: BLE001
            pass
    try:
        return Path(str(_DEFAULT_RECORDING_DIR)).expanduser()
    except Exception:  # noqa: BLE001
        return Path("data/recordings")


def _coerce_recording_dir(*args: Any, **kwargs: Any) -> Path:
    for a in args:
        if isinstance(a, (str, Path)) and str(a).strip():
            p = Path(str(a)).expanduser()
            if p.exists() and p.is_dir():
                return p
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    for key in (
        "recording_dir", "recordingDir", "recordings_dir", "recordingsDir",
        "storage_root", "storageRoot", "root", "base_dir", "directory",
        "dir", "folder", "location", "scan_path", "recording_root",
    ):
        if flat.get(key) is not None:
            try:
                return Path(str(flat.get(key))).expanduser()
            except Exception:  # noqa: BLE001
                continue
    return _default_recording_dir(flat.get("recording_dir"))


def _placeholder_jpeg(label: str = "segment", width: int = DEFAULT_WIDTH,
                      height: int = DEFAULT_HEIGHT) -> bytes:
    w, h = _coerce_dims(width, height)
    try:
        from PIL import Image, ImageDraw  # type: ignore

        img = Image.new("RGB", (w, h), color=(24, 32, 48))
        draw = ImageDraw.Draw(img)
        draw.text((10, max(10, h // 2 - 18)), str(label)[:48], fill=(255, 255, 255))
        draw.rectangle([0, 0, w - 1, h - 1], outline=(10, 12, 20))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
            b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
            b"\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd2\x8a(\xa0\x00\xff\xd9"
        )


def _try_ffmpeg_frame(segment_path: str, offset: float,
                      width: int, height: int) -> Optional[bytes]:
    """Best-effort single-frame extraction via ffmpeg. Returns None on any failure."""
    try:
        p = Path(str(segment_path))
        if not p.exists() or not p.is_file():
            return None
        if p.stat().st_size <= 0:
            return None
    except OSError:
        return None
    try:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(float(offset)),
            "-i", str(segment_path),
            "-frames:v", "1",
            "-s", f"{int(width)}x{int(height)}",
            "-q:v", "5",
            "-f", "mjpeg",
            "pipe:1",
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=10)
        out = bytes(proc.stdout or b"")
        if proc.returncode == 0 and len(out) > 2 and out[:2] == b"\xff\xd8":
            return out
        return None
    except Exception:  # noqa: BLE001
        return None


def _segment_display(segment: str) -> str:
    try:
        return Path(str(segment)).name[:40] or str(segment)[:40]
    except Exception:  # noqa: BLE001
        return str(segment)[:40]


def _capture_segment_frame(segment: str, width: int = DEFAULT_WIDTH,
                           height: int = DEFAULT_HEIGHT,
                           offset: float = DEFAULT_OFFSET_SECONDS) -> Tuple[bytes, bool]:
    """Return (jpeg_bytes, exists). Tries ffmpeg, falls back to placeholder."""
    w, h = _coerce_dims(width, height)
    off = _coerce_offset(offset)
    exists = False
    try:
        p = Path(str(segment))
        exists = p.exists() and p.is_file()
    except Exception:  # noqa: BLE001
        exists = False
    if exists:
        frame = _try_ffmpeg_frame(str(segment), off, w, h)
        if frame is not None:
            return bytes(frame), True
    label = f"{_segment_display(segment)} @{off:.1f}s"
    return _placeholder_jpeg(label, width=w, height=h), exists


def _cache_key(segment: str, width: int, height: int, offset: float) -> str:
    return f"{segment}|{int(width)}x{int(height)}|{float(offset):.3f}"


def _is_fresh(entry: Dict[str, Any], now: float, ttl: float) -> bool:
    try:
        age = float(now) - float(entry.get("updated_at", 0.0))
    except (TypeError, ValueError):
        return False
    try:
        ttl_f = float(ttl)
    except (TypeError, ValueError):
        ttl_f = float(DEFAULT_TTL_SECONDS)
    if ttl_f <= 0:
        return False
    # File mtime change invalidates even within TTL.
    try:
        seg = str(entry.get("segment", ""))
        if seg and not seg.startswith("recording:"):
            mtime = Path(seg).stat().st_mtime
            if float(mtime) != float(entry.get("mtime", mtime)):
                return False
    except OSError:
        pass
    except (TypeError, ValueError):
        pass
    return age < ttl_f


def _evict_if_needed() -> None:
    if len(_CACHE) <= MAX_CACHE_ENTRIES:
        return
    ordered = sorted(_CACHE.items(), key=lambda kv: float(kv[1].get("updated_at", 0.0)))
    overflow = len(_CACHE) - MAX_CACHE_ENTRIES
    for k, _ in ordered[:overflow]:
        _CACHE.pop(k, None)


def _entry_to_info(key: str, entry: Dict[str, Any], ttl: float | None = None) -> Dict[str, Any]:
    now = _now()
    data = entry.get("data", b"")
    ttl_f = float(ttl) if ttl is not None else float(DEFAULT_TTL_SECONDS)
    try:
        age = float(now) - float(entry.get("updated_at", 0.0))
    except (TypeError, ValueError):
        age = 0.0
    return {
        "segment": entry.get("segment", key.split("|")[0]),
        "segment_path": entry.get("segment", key.split("|")[0]),
        "path": entry.get("segment", key.split("|")[0]),
        "key": key,
        "id": key,
        "size": len(data) if isinstance(data, (bytes, bytearray)) else 0,
        "size_bytes": len(data) if isinstance(data, (bytes, bytearray)) else 0,
        "content_type": entry.get("content_type", CONTENT_TYPE_JPEG),
        "updated_at": float(entry.get("updated_at", 0.0)),
        "age_seconds": age,
        "ttl_seconds": ttl_f,
        "fresh": _is_fresh(entry, now, ttl_f),
        "hits": int(entry.get("hits", 0)),
        "width": entry.get("width", DEFAULT_WIDTH),
        "height": entry.get("height", DEFAULT_HEIGHT),
        "offset": entry.get("offset", DEFAULT_OFFSET_SECONDS),
        "exists": bool(entry.get("exists", False)),
    }


def _resolve_segment_string(*args: Any, **kwargs: Any) -> str:
    for a in args:
        if isinstance(a, (str, Path)) and str(a).strip():
            s = str(a).strip()
            # Heuristic: explicit segment refs (paths, filenames, recording: ids).
            low = s.lower()
            if "/" in s or "\\" in s or low.endswith(VIDEO_EXTENSIONS) or s.startswith("recording:"):
                return s
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    seg = _coerce_segment(flat.get("segment"), **flat)
    if seg:
        return seg
    # recording_id pseudo-key fallback.
    rid = _coerce_recording_id(flat.get("recording_id"), **flat)
    if rid is not None:
        return f"recording:{int(rid)}"
    # camera@time pseudo-key fallback (find nearest segment below if possible).
    cam = None
    for key in ("camera", "camera_id", "cameraId", "cam", "camera_name", "cameraName"):
        if flat.get(key) is not None:
            cam = flat.get(key)
            break
    ts = None
    for key in ("timestamp", "time", "event_time", "start", "start_time", "at"):
        if flat.get(key) is not None:
            ts = flat.get(key)
            break
    if cam is not None and ts is not None:
        return f"{cam}@{ts}"
    if cam is not None:
        # may resolve to a real file via list_segments below
        return str(cam)
    return ""


def list_segments(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """List recording segment files on disk (filesystem scan, no DB)."""
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "options", "request", "filter"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    cam = flat.get("camera", flat.get("camera_name",
                   flat.get("cameraName", flat.get("cam"))))
    if cam is None:
        for key in ("camera_id", "cameraId"):
            if isinstance(flat.get(key), str):
                cam = flat.get(key)
                break
    recording_dir = _coerce_recording_dir(*args, **kwargs)
    try:
        from app.services.event_segments import _list_segment_files as _scan  # type: ignore
        files = _scan(cam, recording_dir)
    except Exception:  # noqa: BLE001
        files = []
        try:
            root = recording_dir
            if root.exists():
                if cam is None:
                    for dirpath, _d, fns in os.walk(root):
                        for fn in fns:
                            if fn.lower().endswith(VIDEO_EXTENSIONS):
                                files.append(Path(dirpath) / fn)
                else:
                    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(cam))
                    for cand in (root / safe, root / str(cam)):
                        if cand.exists() and cand.is_dir():
                            for ext in ("*.mp4", "*.mkv", "*.ts", "*.mov", "*.m4v"):
                                files.extend(cand.glob(ext))
        except Exception:  # noqa: BLE001
            files = []
    out: List[Dict[str, Any]] = []
    for f in sorted(set(files)):
        try:
            st = f.stat()
            mtime = float(st.st_mtime)
            size = int(st.st_size)
        except OSError:
            mtime = None
            size = None
        ts: Optional[float] = None
        m = _TS_RE.search(f.name)
        if m:
            try:
                dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
                dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.timestamp()
            except ValueError:
                ts = None
        if ts is None:
            ts = mtime
        out.append({
            "segment": str(f),
            "segment_path": str(f),
            "path": str(f),
            "file_path": str(f),
            "filename": f.name,
            "name": f.name,
            "camera": f.parent.name if f.parent else None,
            "camera_name": f.parent.name if f.parent else None,
            "start": ts,
            "mtime": mtime,
            "size": size,
            "size_bytes": size,
        })
    limit = kwargs.get("limit", kwargs.get("n", kwargs.get("count")))
    if limit is not None:
        try:
            n = int(limit)
            if n >= 0:
                out = out[:n]
        except (TypeError, ValueError):
            pass
    return out


def resolve_segment(*args: Any, **kwargs: Any) -> Optional[str]:
    """Resolve a segment reference to a filesystem path or pseudo-key.

    Handles: filesystem path, bare filename (searches ``recording_dir``),
    ``recording:<id>`` pseudo-keys, and camera+timestamp nearest-match.
    Returns None when nothing can be resolved.
    """
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    raw = _resolve_segment_string(*args, **kwargs)
    if not raw:
        return None
    if raw.startswith("recording:"):
        return raw
    p = Path(str(raw))
    try:
        if p.exists() and p.is_file():
            return str(p)
    except Exception:  # noqa: BLE001
        pass
    # Bare filename: search under recording_dir.
    recording_dir = _coerce_recording_dir(*args, **kwargs)
    if "/" not in raw and "\\" not in raw:
        for seg in list_segments(recording_dir=recording_dir):
            if seg.get("filename") == raw or str(seg.get("path", "")).endswith(raw):
                return str(seg.get("path"))
    # camera@time pseudo-key: find nearest segment for camera.
    if "@" in raw and "/" not in raw and "\\" not in raw:
        cam_part, _, _ts_part = raw.partition("@")
        segs = list_segments(camera=cam_part, recording_dir=recording_dir)
        if segs:
            return str(segs[0].get("path"))
        return raw
    # camera name alone: first segment for that camera.
    segs = list_segments(camera=raw, recording_dir=recording_dir)
    if segs:
        return str(segs[0].get("path"))
    return raw


def get_segment_thumbnail_with_meta(segment: Any = None, *args: Any,
                                    ttl: Any = None, refresh: bool = False,
                                    width: Any = None, height: Any = None,
                                    offset: Any = None,
                                    **kwargs: Any) -> Tuple[bytes, Dict[str, Any]]:
    if segment is None:
        for alias in ("camera", "cam", "cam_id", "id", "cameraId"):
            if kwargs.get(alias) is not None and args:
                break
        segment = _resolve_segment_string(*args, **kwargs)
        if isinstance(segment, str) and not segment and args:
            for a in args:
                if isinstance(a, (str, Path)) and str(a).strip():
                    segment = str(a).strip()
                    break
    else:
        if isinstance(segment, dict):
            segment = _coerce_segment(None, **{**kwargs, "segment": segment})
        else:
            seg_from_kwargs = _coerce_segment(None, **kwargs)
            if seg_from_kwargs and str(segment).strip() in ("0", ""):
                segment = seg_from_kwargs
            else:
                segment = str(segment).strip() if not isinstance(segment, Path) else str(segment)
    if not segment:
        # recording_id fallback
        rid = _coerce_recording_id(None, **kwargs)
        if rid is not None:
            segment = f"recording:{int(rid)}"
    if not segment:
        raise ValueError("segment is required")
    # Resolve bare names / camera hints to real files when possible.
    resolved = resolve_segment(segment, **kwargs)
    key_segment = resolved or str(segment)
    ttl_f = _coerce_ttl(ttl, **kwargs)
    if refresh is False:
        for alias in ("force", "force_refresh", "no_cache", "nocache",
                      "fresh", "reload", "regenerate"):
            if kwargs.get(alias) is not None:
                refresh = bool(kwargs.get(alias))
                break
    w, h = _coerce_dims(width, height, **kwargs)
    off = _coerce_offset(offset, **kwargs)
    key = _cache_key(key_segment, w, h, off)
    now = _now()
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is not None and not refresh and _is_fresh(entry, now, ttl_f):
            entry["hits"] = int(entry.get("hits", 0)) + 1
            meta = _entry_to_info(key, entry, ttl_f)
            meta["cache"] = "HIT"
            return bytes(entry["data"]), meta
        data, exists = _capture_segment_frame(key_segment, width=w, height=h, offset=off)
        try:
            mtime = Path(key_segment).stat().st_mtime if not key_segment.startswith("recording:") else now
        except OSError:
            mtime = now
        new_entry: Dict[str, Any] = {
            "data": bytes(data),
            "content_type": CONTENT_TYPE_JPEG,
            "updated_at": now,
            "hits": 0,
            "width": w,
            "height": h,
            "segment": key_segment,
            "offset": off,
            "exists": bool(exists),
            "mtime": float(mtime) if isinstance(mtime, (int, float)) else now,
        }
        _CACHE[key] = new_entry
        _evict_if_needed()
        meta = _entry_to_info(key, new_entry, ttl_f)
        meta["cache"] = "MISS"
        return bytes(data), meta


def get_segment_thumbnail(segment: Any = None, *args: Any, **kwargs: Any) -> bytes:
    data, _meta = get_segment_thumbnail_with_meta(segment, *args, **kwargs)
    return data


def peek(segment: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    seg = _coerce_segment(segment, **kwargs) or _resolve_segment_string(segment, **kwargs)
    if not seg:
        rid = _coerce_recording_id(None, **kwargs)
        seg = f"recording:{int(rid)}" if rid is not None else ""
    if not seg:
        return None
    w, h = _coerce_dims(kwargs.get("width"), kwargs.get("height"), **kwargs)
    off = _coerce_offset(kwargs.get("offset"), **kwargs)
    key = _cache_key(resolve_segment(seg, **kwargs) or seg, w, h, off)
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        return dict(entry)


def get_info(segment: Any = None, ttl: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    seg = _coerce_segment(segment, **kwargs) or _resolve_segment_string(segment, **kwargs)
    if not seg:
        rid = _coerce_recording_id(None, **kwargs)
        seg = f"recording:{int(rid)}" if rid is not None else ""
    if not seg:
        return None
    ttl_f = _coerce_ttl(ttl, **kwargs)
    w, h = _coerce_dims(kwargs.get("width"), kwargs.get("height"), **kwargs)
    off = _coerce_offset(kwargs.get("offset"), **kwargs)
    key = _cache_key(resolve_segment(seg, **kwargs) or seg, w, h, off)
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        return _entry_to_info(key, entry, ttl_f)


def is_fresh(segment: Any = None, ttl: Any = None, **kwargs: Any) -> bool:
    seg = _coerce_segment(segment, **kwargs) or _resolve_segment_string(segment, **kwargs)
    if not seg:
        return False
    ttl_f = _coerce_ttl(ttl, **kwargs)
    w, h = _coerce_dims(kwargs.get("width"), kwargs.get("height"), **kwargs)
    off = _coerce_offset(kwargs.get("offset"), **kwargs)
    key = _cache_key(resolve_segment(seg, **kwargs) or seg, w, h, off)
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return False
        return _is_fresh(entry, _now(), ttl_f)


def set_segment_thumbnail(segment: Any = None, data: Any = None, *args: Any,
                          content_type: str = CONTENT_TYPE_JPEG,
                          **kwargs: Any) -> Dict[str, Any]:
    if segment is None:
        segment = _resolve_segment_string(*args, **kwargs)
    if isinstance(segment, dict):
        segment = _coerce_segment(None, segment=segment)
    segment = str(segment).strip() if segment is not None else ""
    if not segment:
        rid = _coerce_recording_id(None, **kwargs)
        if rid is not None:
            segment = f"recording:{int(rid)}"
    if not segment:
        raise ValueError("segment is required")
    if data is None:
        for alias in ("image", "frame", "bytes", "payload", "content", "jpeg", "thumbnail"):
            if kwargs.get(alias) is not None:
                data = kwargs.get(alias)
                break
    if data is None and args:
        for a in args:
            if isinstance(a, (bytes, bytearray)):
                data = bytes(a)
                break
    if data is None:
        raise ValueError("thumbnail data is required")
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, (bytes,)):
        raise ValueError("thumbnail data must be bytes")
    key_segment = resolve_segment(segment, **kwargs) or str(segment)
    w, h = _coerce_dims(kwargs.get("width"), kwargs.get("height"), **kwargs)
    off = _coerce_offset(kwargs.get("offset"), **kwargs)
    key = _cache_key(key_segment, w, h, off)
    with _LOCK:
        entry: Dict[str, Any] = {
            "data": bytes(data),
            "content_type": content_type or CONTENT_TYPE_JPEG,
            "updated_at": _now(),
            "hits": 0,
            "width": w,
            "height": h,
            "segment": key_segment,
            "offset": off,
            "exists": True,
            "mtime": _now(),
        }
        _CACHE[key] = entry
        _evict_if_needed()
        return _entry_to_info(key, entry)


def invalidate(segment: Any = None, **kwargs: Any) -> bool:
    seg = _coerce_segment(segment, **kwargs) or _resolve_segment_string(segment, **kwargs)
    if not seg:
        rid = _coerce_recording_id(None, **kwargs)
        seg = f"recording:{int(rid)}" if rid is not None else ""
    if not seg:
        return False
    w, h = _coerce_dims(kwargs.get("width"), kwargs.get("height"), **kwargs)
    off = _coerce_offset(kwargs.get("offset"), **kwargs)
    # If dims/offset explicitly given, invalidate exact key; else invalidate all keys for segment.
    exact = any(kwargs.get(k) is not None for k in (
        "width", "height", "w", "h", "offset", "offset_seconds", "seek", "position"))
    resolved = resolve_segment(seg, **kwargs) or seg
    with _LOCK:
        if exact:
            key = _cache_key(resolved, w, h, off)
            return _CACHE.pop(key, None) is not None
        doomed = [k for k, v in _CACHE.items() if str(v.get("segment", "")) == resolved or k.startswith(resolved + "|")]
        for k in doomed:
            _CACHE.pop(k, None)
        return len(doomed) > 0


def clear(segment: Any = None, **kwargs: Any) -> None:
    """Clear one segment's entries when given, else clear all."""
    seg = _coerce_segment(segment, **kwargs) if segment is not None else None
    if seg is None:
        seg = _resolve_segment_string(**kwargs) if kwargs else ""
    if seg or kwargs.get("recording_id") is not None:
        invalidate(seg or "", **kwargs)
        return
    if segment is not None:
        invalidate(segment, **kwargs)
        return
    with _LOCK:
        _CACHE.clear()


def list_thumbnails(ttl: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    ttl_f = _coerce_ttl(ttl if ttl is not None else kwargs.get("ttl", DEFAULT_TTL_SECONDS))
    with _LOCK:
        return [_entry_to_info(k, v, ttl_f) for k, v in sorted(_CACHE.items())]


def get_stats() -> Dict[str, Any]:
    with _LOCK:
        total_hits = sum(int(v.get("hits", 0)) for v in _CACHE.values())
        total_bytes = sum(len(v.get("data", b"")) for v in _CACHE.values())
        return {
            "entries": len(_CACHE),
            "count": len(_CACHE),
            "total_hits": total_hits,
            "hits": total_hits,
            "total_bytes": total_bytes,
            "size_bytes": total_bytes,
            "ttl_seconds": float(DEFAULT_TTL_SECONDS),
            "default_ttl_seconds": float(DEFAULT_TTL_SECONDS),
        }


async def get_thumbnail_for_recording_async(recording_id: Any, *args: Any,
                                            db: Any = None, session: Any = None,
                                            **kwargs: Any) -> Tuple[bytes, Dict[str, Any]]:
    sess = db if db is not None else session
    rid = _coerce_recording_id(recording_id, **kwargs)
    if rid is None:
        raise ValueError("recording_id is required")
    seg_path: Optional[str] = None
    if sess is not None:
        try:
            from app.models.entities import Recording as _Recording  # lazy import

            row = await sess.get(_Recording, int(rid))
            if row is not None and getattr(row, "path", None):
                seg_path = str(getattr(row, "path"))
        except Exception:  # noqa: BLE001
            seg_path = None
    # Filesystem fallback: recording path may be a directory of segments.
    if seg_path is not None:
        p = Path(seg_path)
        try:
            if p.is_dir():
                cands = sorted(p.glob("*.mp4"))
                if cands:
                    seg_path = str(cands[0])
        except OSError:
            pass
    target = seg_path or f"recording:{int(rid)}"
    data, meta = get_segment_thumbnail_with_meta(target, *args, recording_id=int(rid), **kwargs)
    meta = dict(meta)
    meta.setdefault("recording_id", int(rid))
    if seg_path is not None:
        meta["recording_path"] = seg_path
    return data, meta


async def get_info_for_recording_async(recording_id: Any, *args: Any,
                                       db: Any = None, session: Any = None,
                                       **kwargs: Any) -> Optional[Dict[str, Any]]:
    try:
        _data, meta = await get_thumbnail_for_recording_async(
            recording_id, *args, db=db, session=session, **kwargs)
        return meta
    except ValueError:
        return None


# Compatibility aliases (repo convention: many names, same behaviour).
get_thumbnail = get_segment_thumbnail
fetch_thumbnail = get_segment_thumbnail
fetch = get_segment_thumbnail
get = get_segment_thumbnail
thumbnail_for = get_segment_thumbnail
cached_thumbnail = get_segment_thumbnail
get_frame_thumbnail = get_segment_thumbnail
capture_thumbnail = get_segment_thumbnail
thumbnail = get_segment_thumbnail
get_segment_thumb = get_segment_thumbnail
segment_thumbnail = get_segment_thumbnail

get_with_meta = get_segment_thumbnail_with_meta
fetch_with_meta = get_segment_thumbnail_with_meta
get_thumb_with_meta = get_segment_thumbnail_with_meta

store_thumbnail = set_segment_thumbnail
put_thumbnail = set_segment_thumbnail
put = set_segment_thumbnail
store = set_segment_thumbnail
save_thumbnail = set_segment_thumbnail
update_thumbnail = set_segment_thumbnail
set_cached_thumbnail = set_segment_thumbnail
set_thumbnail = set_segment_thumbnail

invalidate_thumbnail = invalidate
evict = invalidate
remove = invalidate
delete = invalidate
drop = invalidate

clear_all = clear
reset = clear
flush = clear

list_cached = list_thumbnails
list_cache = list_thumbnails
cached_list = list_thumbnails
all_thumbnails = list_thumbnails
list_segment_thumbs = list_thumbnails
list_segment_thumbnails = list_thumbnails

cache_info = get_stats
stats = get_stats
info = get_info

find = list_segments
find_segments = list_segments
scan = list_segments
scan_segments = list_segments


@dataclass
class SegmentThumb:
    segment: str
    data: bytes
    content_type: str = CONTENT_TYPE_JPEG
    updated_at: float = 0.0
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    offset: float = DEFAULT_OFFSET_SECONDS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segment": self.segment,
            "segment_path": self.segment,
            "size": len(self.data),
            "content_type": self.content_type,
            "updated_at": self.updated_at,
            "width": self.width,
            "height": self.height,
            "offset": self.offset,
        }


class SegmentThumbCache:
    """Object-oriented facade over the module-level segment-thumb cache."""

    def get(self, segment: Any = None, **kwargs: Any) -> bytes:
        return get_segment_thumbnail(segment, **kwargs)

    def get_with_meta(self, segment: Any = None, **kwargs: Any) -> Tuple[bytes, Dict[str, Any]]:
        return get_segment_thumbnail_with_meta(segment, **kwargs)

    def set(self, segment: Any = None, data: Any = None, **kwargs: Any) -> Dict[str, Any]:
        return set_segment_thumbnail(segment, data, **kwargs)

    def peek(self, segment: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return peek(segment, **kwargs)

    def info(self, segment: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_info(segment, **kwargs)

    def invalidate(self, segment: Any = None, **kwargs: Any) -> bool:
        return invalidate(segment, **kwargs)

    def clear(self, segment: Any = None, **kwargs: Any) -> None:
        return clear(segment, **kwargs)

    def list(self) -> List[Dict[str, Any]]:
        return list_thumbnails()

    def stats(self) -> Dict[str, Any]:
        return get_stats()

    def segments(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_segments(*args, **kwargs)


class SegmentThumbsService(SegmentThumbCache):
    pass


class SegmentThumbStore(SegmentThumbCache):
    pass


class SegmentThumbnailService(SegmentThumbCache):
    pass


segment_thumb_cache = SegmentThumbCache()
segment_thumbs_service = SegmentThumbCache()
segment_thumbs = SegmentThumbCache()
cache = SegmentThumbCache()
service = SegmentThumbCache()
store = SegmentThumbCache()


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "MAX_CACHE_ENTRIES",
    "CONTENT_TYPE_JPEG",
    "VIDEO_EXTENSIONS",
    "get_segment_thumbnail",
    "get_segment_thumbnail_with_meta",
    "set_segment_thumbnail",
    "get_info",
    "peek",
    "is_fresh",
    "invalidate",
    "clear",
    "list_thumbnails",
    "list_segments",
    "resolve_segment",
    "get_stats",
    "get_thumbnail_for_recording_async",
    "SegmentThumb",
    "SegmentThumbCache",
    "SegmentThumbsService",
    "SegmentThumbStore",
    "segment_thumb_cache",
]
