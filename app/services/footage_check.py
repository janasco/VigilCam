"""Point-in-time footage availability check for VigilCam.

Answers the single question neither ``gap_detector`` nor ``export`` answers:

    "Is there footage for camera X at time T?"

* ``gap_detector`` finds *missing* time ranges in segment coverage per camera.
* ``export`` builds clips from a time range.
* This module answers point availability: given a camera name and a
  timestamp, report whether any recorded segment covers that instant, and
  which file provides it.

Layout convention (see ``app.services.ingestion`` and ``gap_detector``):
each camera gets a subdirectory under the recording root named after a
sanitized camera name::

    <RECORDING_DIR>/<safe_camera_name>/YYYYMMDD_HHMMSS_main.mp4

Files directly under the storage root (no camera subdirectory) are grouped
under the ``"unassigned"`` bucket, matching ``storage_analyzer``.

Segment timing model: the ingestor writes fixed-length segments
(``segment_seconds``, default 300). Each file whose name embeds a start
timestamp covers ``[start, start + segment_seconds)``. Callers that know
exact per-segment durations can instead pass explicit intervals; those
take precedence over filename-derived durations.

Standard library only.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from app.core.config import RECORDING_DIR  # type: ignore
except Exception:  # noqa: BLE001
    RECORDING_DIR = Path("data/recordings")  # type: ignore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SEGMENT_SECONDS = 300.0
DEFAULT_TOLERANCE_SECONDS = 1.0
DEFAULT_EXTENSIONS = {".mp4"}

_UNASSIGNED = "unassigned"

_STAMP_PATTERNS = [
    re.compile(
        r"(?P<y>\d{4})-?(?P<m>\d{2})-?(?P<d>\d{2})"
        r"[_\-T ]"
        r"(?P<H>\d{2})-?(?P<M>\d{2})-?(?P<S>\d{2})"
    ),
]


# ---------------------------------------------------------------------------
# Small coercion helpers
# ---------------------------------------------------------------------------

def _coerce_path(value: Any) -> Optional[Path]:
    if value is None:
        return None
    if isinstance(value, Path):
        return value.expanduser()
    if isinstance(value, (bytes, bytearray)):
        try:
            return Path(value.decode())
        except Exception:  # noqa: BLE001
            return Path(str(value))
    if isinstance(value, dict):
        for k in (
            "path", "storage_path", "root_path", "storage_root", "root",
            "directory", "dir", "folder", "location", "recordings_dir",
            "recording_dir", "storage_dir", "base_dir", "scan_path",
            "recordings_root",
        ):
            if value.get(k) is not None:
                p = _coerce_path(value.get(k))
                if p is not None:
                    return p
        return None
    for attr in ("path", "storage_path", "root", "directory", "storage_root"):
        if hasattr(value, attr):
            try:
                v = getattr(value, attr)
                if v is not None and v is not value:
                    p = _coerce_path(v)
                    if p is not None:
                        return p
            except Exception:  # noqa: BLE001
                pass
    try:
        s = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    if not s:
        return None
    return Path(s).expanduser()


def _default_root() -> Path:
    try:
        return Path(str(RECORDING_DIR)).expanduser()
    except Exception:  # noqa: BLE001
        return Path("data/recordings")


def _as_float(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(value, str):
        s = value.strip().lower()
        if not s or s in ("none", "null", "auto"):
            return default
        try:
            return float(s)
        except (TypeError, ValueError):
            return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _coerce_datetime(value: Any) -> Optional[datetime]:
    """Coerce datetime | timestamp | ISO string -> naive UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            try:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:  # noqa: BLE001
                dt = dt.replace(tzinfo=None)
        return dt
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("none", "null", "auto"):
            return None
        try:
            if re.fullmatch(r"-?\d+(\.\d+)?", s):
                return datetime.fromtimestamp(float(s), tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            pass
        text = s
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            pass
        for fmt in (
            "%Y%m%d_%H%M%S", "%Y%m%d-%H%M%S", "%Y-%m-%d_%H-%M-%S",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%d%H%M%S",
            "%Y-%m-%d", "%Y%m%d",
        ):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None
    return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:  # noqa: BLE001
        return None


def _ts(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    try:
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except Exception:  # noqa: BLE001
        return None


def _safe_name(name: Any) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))


def _matches_camera(bucket: str, wanted: Optional[str]) -> bool:
    if wanted is None:
        return True
    w = str(wanted).strip()
    if bucket == w:
        return True
    return _safe_name(bucket).lower() == _safe_name(w).lower()


# ---------------------------------------------------------------------------
# Filename timestamp parsing
# ---------------------------------------------------------------------------

def parse_segment_start(name: Any) -> Optional[datetime]:
    """Extract the segment start timestamp embedded in a file name."""
    if name is None:
        return None
    if isinstance(name, Path):
        text = name.name
    elif isinstance(name, (bytes, bytearray)):
        try:
            text = Path(name.decode()).name
        except Exception:  # noqa: BLE001
            text = str(name)
    else:
        try:
            text = Path(str(name)).name
        except Exception:  # noqa: BLE001
            return None
    for pat in _STAMP_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            return datetime(
                int(m.group("y")), int(m.group("m")), int(m.group("d")),
                int(m.group("H")), int(m.group("M")), int(m.group("S")),
            )
        except ValueError:
            continue
    return None


def segment_interval_for_file(
    path: Any,
    segment_seconds: Any = None,
    duration_seconds: Any = None,
) -> Optional[Tuple[datetime, datetime]]:
    """Return ``(start, end)`` coverage for a segment file, or ``None``."""
    seg_len = _as_float(
        duration_seconds if duration_seconds is not None else segment_seconds,
        DEFAULT_SEGMENT_SECONDS,
    )
    if seg_len <= 0:
        seg_len = DEFAULT_SEGMENT_SECONDS
    start = parse_segment_start(path)
    if start is None:
        return None
    return (start, start + timedelta(seconds=float(seg_len)))


# ---------------------------------------------------------------------------
# Interval coercion
# ---------------------------------------------------------------------------

def _coerce_interval(
    item: Any,
    default_segment_seconds: float = DEFAULT_SEGMENT_SECONDS,
) -> Optional[Tuple[datetime, datetime]]:
    if item is None:
        return None
    if isinstance(item, Path):
        return segment_interval_for_file(item, default_segment_seconds)
    if isinstance(item, (tuple, list)) and len(item) == 2:
        a, b = item[0], item[1]
        sa = _coerce_datetime(a)
        sb = _coerce_datetime(b)
        if sa is not None and sb is not None:
            if sb < sa:
                sa, sb = sb, sa
            if sb == sa:
                return None
            return (sa, sb)
        if sa is not None and isinstance(b, (int, float)) and not isinstance(b, bool):
            dur = float(b)
            if dur <= 0:
                return None
            return (sa, sa + timedelta(seconds=dur))
        if isinstance(a, (str, Path)):
            start = parse_segment_start(a)
            if start is not None and isinstance(b, (int, float)):
                dur = float(b)
                if dur > 0:
                    return (start, start + timedelta(seconds=dur))
        return None
    if isinstance(item, dict):
        start = None
        end = None
        for k in ("start", "start_time", "startTime", "begin", "from",
                  "gap_start", "interval_start", "coverage_start", "timestamp",
                  "time", "datetime", "at"):
            if item.get(k) is not None:
                start = _coerce_datetime(item.get(k))
                if start is not None:
                    break
        for k in ("end", "end_time", "endTime", "finish", "to",
                  "gap_end", "interval_end", "coverage_end", "stop"):
            if item.get(k) is not None:
                end = _coerce_datetime(item.get(k))
                if end is not None:
                    break
        if start is not None and end is not None:
            if end < start:
                start, end = end, start
            if end == start:
                return None
            return (start, end)
        if start is not None and end is None:
            dur = None
            for k in ("duration", "duration_seconds", "durationSeconds",
                      "segment_seconds", "segmentSeconds", "length",
                      "length_seconds", "seconds", "segment_duration"):
                if item.get(k) is not None:
                    try:
                        dur = float(item.get(k))
                        break
                    except (TypeError, ValueError):
                        continue
            if dur is None:
                dur = float(default_segment_seconds)
            if dur <= 0:
                return None
            return (start, start + timedelta(seconds=dur))
        for k in ("path", "file", "filepath", "filename", "segment", "name"):
            if item.get(k) is not None:
                iv = segment_interval_for_file(item.get(k), default_segment_seconds)
                if iv is not None:
                    dur = None
                    for dk in ("duration", "duration_seconds", "segment_seconds"):
                        if item.get(dk) is not None:
                            try:
                                dur = float(item.get(dk))
                                break
                            except (TypeError, ValueError):
                                continue
                    if dur is not None and dur > 0:
                        return (iv[0], iv[0] + timedelta(seconds=dur))
                    return iv
        return None
    if isinstance(item, str):
        return segment_interval_for_file(item, default_segment_seconds)
    if isinstance(item, datetime):
        return (item, item + timedelta(seconds=float(default_segment_seconds)))
    if isinstance(item, (int, float)) and not isinstance(item, bool):
        start = _coerce_datetime(item)
        if start is None:
            return None
        return (start, start + timedelta(seconds=float(default_segment_seconds)))
    return None


# ---------------------------------------------------------------------------
# Filesystem scanning (coverage only)
# ---------------------------------------------------------------------------

def _iter_segment_files(root: Path, extensions: Sequence[str]) -> List[Path]:
    exts = {str(e).lower() for e in (extensions or DEFAULT_EXTENSIONS)}
    out: List[Path] = []
    try:
        if root.is_file():
            if root.suffix.lower() in exts:
                out.append(root)
            return out
        if not root.exists():
            return out
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in sorted(filenames):
                fp = Path(dirpath) / fn
                try:
                    if fp.suffix.lower() in exts and fp.is_file():
                        out.append(fp)
                except OSError:
                    continue
    except Exception:  # noqa: BLE001
        pass
    return sorted(out)


def _camera_bucket_for(rel: Path) -> str:
    if len(rel.parts) == 0:
        return _UNASSIGNED
    if len(rel.parts) == 1:
        return _UNASSIGNED
    return rel.parts[0]


def group_segments_by_camera(
    root: Any = None,
    extensions: Optional[Sequence[str]] = None,
    segment_seconds: Any = None,
    **kwargs: Any,
) -> Dict[str, List[Tuple[datetime, datetime, Path]]]:
    """Map camera bucket -> sorted ``(start, end, path)`` coverage entries."""
    if root is None:
        for k in ("storage_root", "recordings_dir", "path", "directory",
                  "base_dir", "folder", "scan_path", "location"):
            if kwargs.get(k) is not None:
                root = kwargs.get(k)
                break
    scan_root = _coerce_path(root) or _default_root()
    if extensions is None:
        for k in ("extensions", "exts", "suffixes", "video_extensions"):
            if kwargs.get(k) is not None:
                extensions = kwargs[k]
                break
    if isinstance(extensions, str):
        extensions = [extensions]
    seg_len = _as_float(segment_seconds, DEFAULT_SEGMENT_SECONDS)
    for k in ("segmentSeconds", "segment_duration", "segmentDuration",
              "duration_seconds", "duration"):
        if kwargs.get(k) is not None:
            seg_len = _as_float(kwargs.get(k), seg_len)
            break

    grouped: Dict[str, List[Tuple[datetime, datetime, Path]]] = {}
    try:
        files = _iter_segment_files(scan_root, list(extensions) if extensions else list(DEFAULT_EXTENSIONS))
    except Exception:  # noqa: BLE001
        files = []
    for fp in files:
        try:
            rel = fp.relative_to(scan_root)
            bucket = _camera_bucket_for(rel)
        except (ValueError, AttributeError):
            bucket = _UNASSIGNED
        iv = segment_interval_for_file(fp, seg_len)
        if iv is None:
            continue
        grouped.setdefault(bucket, []).append((iv[0], iv[1], fp))
    for bucket in grouped:
        grouped[bucket] = sorted(grouped[bucket], key=lambda t: (t[0], t[1]))
    return grouped


def _collect_intervals_for_camera(
    camera_entries: Optional[Sequence[Tuple[datetime, datetime, Path]]],
    explicit: Optional[Sequence[Any]],
    seg_len: float,
) -> List[Tuple[datetime, datetime, Optional[Path]]]:
    out: List[Tuple[datetime, datetime, Optional[Path]]] = []
    for cand in (explicit or []):
        if isinstance(cand, (tuple, list)) and len(cand) == 3:
            a, b, c = cand[0], cand[1], cand[2]
            sa, sb = _coerce_datetime(a), _coerce_datetime(b)
            if sa is not None and sb is not None:
                if sb < sa:
                    sa, sb = sb, sa
                if sb != sa:
                    p = c if isinstance(c, Path) else _coerce_path(c)
                    out.append((sa, sb, p))
                continue
        iv = _coerce_interval(cand, seg_len)
        if iv is not None:
            path = None
            if isinstance(cand, dict):
                for k in ("path", "file", "filepath", "filename", "segment", "name"):
                    if cand.get(k) is not None:
                        path = _coerce_path(cand.get(k))
                        break
            elif isinstance(cand, (str, Path)):
                path = _coerce_path(cand)
            out.append((iv[0], iv[1], path))
    for start, end, fp in (camera_entries or []):
        out.append((start, end, fp))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


# ---------------------------------------------------------------------------
# Point-in-time availability check
# ---------------------------------------------------------------------------

def check_footage(
    camera: Any = None,
    at: Any = None,
    timestamp: Any = None,
    root: Any = None,
    segments: Any = None,
    intervals: Any = None,
    per_camera_segments: Any = None,
    segment_seconds: Any = None,
    tolerance_seconds: Any = None,
    extensions: Optional[Sequence[str]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Check whether footage exists for a camera at one instant.

    Returns a dict with ``available`` (bool), the covering segment file
    (when found), and the covering interval. Never raises for missing
    cameras/timestamps: those yield ``available=False`` with a ``reason``.
    """
    # Alias-tolerant extraction.
    if camera is None:
        for k in ("camera_name", "cameraName", "camera_id", "cameraId",
                  "cam", "name", "slug", "camera_slug", "cam_id", "camId",
                  "id"):
            if kwargs.get(k) is not None:
                camera = kwargs.get(k)
                break
        if camera is None:
            for k in ("camera_name", "cameraName", "camera", "cam"):
                if isinstance(segments, dict) and segments.get(k) is not None:
                    camera = segments.get(k)
                    break
    if at is None:
        at = timestamp
    if at is None:
        for k in ("time", "datetime", "date", "ts", "epoch", "moment",
                  "when", "point", "at_time", "atTime", "check_time",
                  "timestamp_iso", "start", "start_time"):
            if kwargs.get(k) is not None:
                at = kwargs.get(k)
                break
    if root is None:
        for k in ("storage_root", "recordings_dir", "path", "directory",
                  "base_dir", "folder", "scan_path", "location",
                  "storage_path", "root_path", "recordings_root"):
            if kwargs.get(k) is not None:
                root = kwargs.get(k)
                break
    if segment_seconds is None:
        for k in ("segmentSeconds", "segment_duration", "segmentDuration",
                  "duration_seconds", "duration", "segment_length",
                  "expected_segment_seconds", "segment_time"):
            if kwargs.get(k) is not None:
                segment_seconds = kwargs.get(k)
                break
    if tolerance_seconds is None:
        for k in ("tolerance", "overlap_tolerance", "slack", "slack_seconds",
                  "jitter", "grace", "grace_seconds"):
            if kwargs.get(k) is not None:
                tolerance_seconds = kwargs.get(k)
                break
    if segments is None:
        for k in ("coverage", "files", "items"):
            if kwargs.get(k) is not None:
                segments = kwargs.get(k)
                break
    if intervals is None and kwargs.get("intervals") is not None:
        intervals = kwargs.get("intervals")
    if per_camera_segments is None and kwargs.get("per_camera_segments") is not None:
        per_camera_segments = kwargs.get("per_camera_segments")

    seg_len = _as_float(segment_seconds, DEFAULT_SEGMENT_SECONDS)
    tol = _as_float(tolerance_seconds, DEFAULT_TOLERANCE_SECONDS)

    scan_root = _coerce_path(root) or _default_root()
    try:
        scan_root_str = str(scan_root)
    except Exception:  # noqa: BLE001
        scan_root_str = None

    wanted = str(camera).strip() if camera is not None and str(camera).strip() else None
    target = _coerce_datetime(at)

    base: Dict[str, Any] = {
        "camera": wanted,
        "camera_name": wanted,
        "timestamp": _iso(target),
        "timestamp_iso": _iso(target),
        "timestamp_ts": _ts(target),
        "at": _iso(target),
        "at_iso": _iso(target),
        "at_ts": _ts(target),
        "time": _iso(target),
        "root": scan_root_str,
        "storage_root": scan_root_str,
        "path": scan_root_str,
        "segment_seconds": float(seg_len),
        "tolerance_seconds": float(tol),
    }

    def _miss(reason: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        out = dict(base)
        out.update({
            "available": False,
            "has_footage": False,
            "exists": False,
            "found": False,
            "present": False,
            "covered": False,
            "ok": False,
            "success": True,
            "reason": reason,
            "segment_file": None,
            "segment_path": None,
            "file": None,
            "file_path": None,
            "path_found": None,
            "coverage_start": None,
            "coverage_end": None,
            "coverage_start_iso": None,
            "coverage_end_iso": None,
            "segment_start": None,
            "segment_end": None,
            "segment_start_iso": None,
            "segment_end_iso": None,
        })
        if extra:
            out.update(extra)
        return out

    def _hit(start: datetime, end: datetime, fp: Optional[Path]) -> Dict[str, Any]:
        out = dict(base)
        fstr = str(fp) if fp is not None else None
        out.update({
            "available": True,
            "has_footage": True,
            "exists": True,
            "found": True,
            "present": True,
            "covered": True,
            "ok": True,
            "success": True,
            "reason": "covered",
            "segment_file": fstr,
            "segment_path": fstr,
            "file": fstr,
            "file_path": fstr,
            "path_found": fstr,
            "coverage_start": _iso(start),
            "coverage_end": _iso(end),
            "coverage_start_iso": _iso(start),
            "coverage_end_iso": _iso(end),
            "coverage_start_ts": _ts(start),
            "coverage_end_ts": _ts(end),
            "segment_start": _iso(start),
            "segment_end": _iso(end),
            "segment_start_iso": _iso(start),
            "segment_end_iso": _iso(end),
            "segment_start_ts": _ts(start),
            "segment_end_ts": _ts(end),
        })
        return out

    if wanted is None:
        return _miss("missing_camera")
    if target is None:
        return _miss("missing_timestamp")

    # Gather candidate intervals: explicit collections first, then filesystem.
    explicit_for_camera: List[Any] = []
    if isinstance(per_camera_segments, dict):
        for cam_key, items in per_camera_segments.items():
            if _matches_camera(str(cam_key), wanted):
                if isinstance(items, dict) and "intervals" in items:
                    items = items.get("intervals")
                if not isinstance(items, (list, tuple)):
                    items = [items]
                explicit_for_camera.extend(list(items))
    flat = segments if segments is not None else intervals
    if flat is not None:
        if isinstance(flat, dict) and not any(
            k in flat for k in ("start", "end", "path", "file", "start_time", "end_time")
        ):
            for cam_key, items in flat.items():
                if _matches_camera(str(cam_key), wanted):
                    if not isinstance(items, (list, tuple)):
                        items = [items]
                    explicit_for_camera.extend(list(items))
        else:
            items = list(flat) if isinstance(flat, (list, tuple)) else [flat]
            # Keep items carrying our camera key, plus bare intervals when
            # the caller already scoped the query to one camera.
            for item in items:
                if isinstance(item, dict):
                    cam_val = None
                    for ck in ("camera_name", "cameraName", "camera", "cam",
                               "name", "camera_id", "cameraId"):
                        if item.get(ck) is not None and not isinstance(item.get(ck), dict):
                            cam_val = item.get(ck)
                            break
                    if cam_val is None or _matches_camera(str(cam_val), wanted):
                        explicit_for_camera.append(item)
                else:
                    explicit_for_camera.append(item)

    fs_entries: Optional[List[Tuple[datetime, datetime, Path]]] = None
    try:
        grouped = group_segments_by_camera(
            scan_root, extensions=extensions or kwargs.get("extensions"),
            segment_seconds=seg_len,
        )
    except Exception:  # noqa: BLE001
        grouped = {}
    matched_bucket: Optional[str] = None
    for bucket in grouped:
        if _matches_camera(bucket, wanted):
            matched_bucket = bucket
            fs_entries = grouped.get(bucket) or []
            break

    if not explicit_for_camera and not fs_entries:
        if matched_bucket is None:
            # Check whether the root has any cameras at all (for a better reason).
            if grouped:
                return _miss("unknown_camera", {"cameras": sorted(grouped.keys())})
            return _miss("no_segments")
        return _miss("no_segments")

    candidates = _collect_intervals_for_camera(fs_entries, explicit_for_camera, seg_len)
    if not candidates:
        return _miss("no_segments")

    tol_td = timedelta(seconds=max(0.0, float(tol)))
    for start, end in ((s, e) for s, e, _ in candidates):
        if start - tol_td <= target < end + tol_td:
            # Return the file backing this interval when known.
            for s2, e2, fp in candidates:
                if s2 == start and e2 == end and (s2 - tol_td <= target < e2 + tol_td):
                    return _hit(s2, e2, fp)
            return _hit(start, end, None)

    # Target not covered: classify as before / after / gap.
    starts = [s for s, _, _ in candidates]
    ends = [e for _, e, _ in candidates]
    earliest = min(starts)
    latest = max(ends)
    if target < earliest - tol_td:
        return _miss("before_coverage", {
            "coverage_start": _iso(earliest),
            "coverage_start_iso": _iso(earliest),
            "coverage_start_ts": _ts(earliest),
            "coverage_end": _iso(latest),
            "coverage_end_iso": _iso(latest),
            "coverage_end_ts": _ts(latest),
        })
    if target >= latest + tol_td:
        return _miss("after_coverage", {
            "coverage_start": _iso(earliest),
            "coverage_start_iso": _iso(earliest),
            "coverage_start_ts": _ts(earliest),
            "coverage_end": _iso(latest),
            "coverage_end_iso": _iso(latest),
            "coverage_end_ts": _ts(latest),
        })
    return _miss("gap", {
        "coverage_start": _iso(earliest),
        "coverage_start_iso": _iso(earliest),
        "coverage_start_ts": _ts(earliest),
        "coverage_end": _iso(latest),
        "coverage_end_iso": _iso(latest),
        "coverage_end_ts": _ts(latest),
    })


def has_footage(*args: Any, **kwargs: Any) -> bool:
    """Return ``True`` when footage exists for the camera at the instant."""
    if args and len(args) >= 2 and "camera" not in kwargs and "at" not in kwargs:
        kwargs = dict(kwargs)
        kwargs.setdefault("camera", args[0])
        kwargs.setdefault("at", args[1])
        return bool(check_footage(**kwargs).get("available"))
    if args and len(args) == 1 and isinstance(args[0], dict):
        merged = dict(args[0])
        merged.update(kwargs)
        return bool(check_footage(**merged).get("available"))
    return bool(check_footage(*args, **kwargs).get("available"))


def is_available(*args: Any, **kwargs: Any) -> bool:
    return has_footage(*args, **kwargs)


def footage_exists(*args: Any, **kwargs: Any) -> bool:
    return has_footage(*args, **kwargs)


def check(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    if args and len(args) == 1 and isinstance(args[0], dict):
        merged = dict(args[0])
        merged.update(kwargs)
        return check_footage(**merged)
    if args and len(args) >= 2:
        merged = dict(kwargs)
        merged.setdefault("camera", args[0])
        merged.setdefault("at", args[1])
        if len(args) >= 3:
            merged.setdefault("root", args[2])
        return check_footage(**merged)
    return check_footage(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    rep = check(*args, **kwargs)
    return {
        "status": "available" if rep.get("available") else "missing",
        "state": "available" if rep.get("available") else "missing",
        "available": bool(rep.get("available")),
        "has_footage": bool(rep.get("available")),
        "ok": bool(rep.get("available")),
        "success": True,
        "healthy": bool(rep.get("available")),
        "camera": rep.get("camera"),
        "camera_name": rep.get("camera_name"),
        "timestamp": rep.get("timestamp"),
        "reason": rep.get("reason"),
        "segment_file": rep.get("segment_file"),
        "report": rep,
    }


status = get_status
summary = get_status
health = get_status
get_report = check_footage
report = check_footage
check_coverage = check_footage
coverage_check = check_footage


# ---------------------------------------------------------------------------
# Checker class
# ---------------------------------------------------------------------------

class FootageChecker:
    """Stateful point-in-time footage availability checker."""

    def __init__(
        self,
        root: Any = None,
        storage_root: Any = None,
        directory: Any = None,
        segment_seconds: Any = None,
        tolerance_seconds: Any = None,
        extensions: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        raw = root if root is not None else storage_root
        if raw is None:
            raw = directory
        if raw is None:
            raw = kwargs.pop("recordings_dir", None) or kwargs.pop("base_dir", None) \
                or kwargs.pop("path", None)
        self.root = _coerce_path(raw) or _default_root()
        self.storage_root = self.root
        self.directory = self.root
        self.base_dir = self.root
        self.path = self.root
        seg = segment_seconds
        if seg is None:
            for k in ("segmentSeconds", "segment_duration", "duration_seconds"):
                if kwargs.get(k) is not None:
                    seg = kwargs.get(k)
                    break
        self.segment_seconds = _as_float(seg, DEFAULT_SEGMENT_SECONDS)
        tol = tolerance_seconds
        if tol is None:
            for k in ("tolerance", "slack", "slack_seconds"):
                if kwargs.get(k) is not None:
                    tol = kwargs.get(k)
                    break
        self.tolerance_seconds = _as_float(tol, DEFAULT_TOLERANCE_SECONDS)
        ext = extensions or kwargs.pop("exts", None) or kwargs.pop("video_extensions", None)
        self.extensions = {str(e).lower() for e in (ext or DEFAULT_EXTENSIONS)}
        self.last_report: Optional[Dict[str, Any]] = None

    def check(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (str, Path)):
                # Bare string: camera name unless it parses as a timestamp.
                if _coerce_datetime(a) is not None and "at" not in merged and "timestamp" not in merged:
                    merged.setdefault("at", a)
                else:
                    merged.setdefault("camera", a)
            elif isinstance(a, datetime):
                merged.setdefault("at", a)
            elif isinstance(a, (int, float)) and not isinstance(a, bool):
                merged.setdefault("at", a)
        merged.update(kwargs)
        merged.setdefault("root", self.root)
        merged.setdefault("segment_seconds", self.segment_seconds)
        merged.setdefault("tolerance_seconds", self.tolerance_seconds)
        merged.setdefault("extensions", list(self.extensions))
        rep = check_footage(**merged)
        self.last_report = rep
        return rep

    def check_footage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.check(*args, **kwargs)

    def has_footage(self, *args: Any, **kwargs: Any) -> bool:
        return bool(self.check(*args, **kwargs).get("available"))

    def is_available(self, *args: Any, **kwargs: Any) -> bool:
        return self.has_footage(*args, **kwargs)

    def footage_exists(self, *args: Any, **kwargs: Any) -> bool:
        return self.has_footage(*args, **kwargs)

    def exists(self, *args: Any, **kwargs: Any) -> bool:
        return self.has_footage(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        rep = self.check(*args, **kwargs)
        return {
            "status": "available" if rep.get("available") else "missing",
            "state": "available" if rep.get("available") else "missing",
            "available": bool(rep.get("available")),
            "has_footage": bool(rep.get("available")),
            "ok": bool(rep.get("available")),
            "success": True,
            "healthy": bool(rep.get("available")),
            "camera": rep.get("camera"),
            "camera_name": rep.get("camera_name"),
            "timestamp": rep.get("timestamp"),
            "reason": rep.get("reason"),
            "segment_file": rep.get("segment_file"),
            "report": rep,
        }

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        if any(h in low for h in ("status", "health", "summary", "info", "state")):
            return self.get_status
        if any(h in low for h in ("avail", "exist", "present", "found", "cover",
                                  "has", "check", "lookup", "query", "verify")):
            return self.check
        raise AttributeError(name)


# Class aliases.
FootageCheckService = FootageChecker
FootageAvailabilityChecker = FootageChecker
FootageAvailabilityService = FootageChecker
FootageService = FootageChecker
AvailabilityChecker = FootageChecker
Checker = FootageChecker

# Singleton instances.
footage_checker = FootageChecker()
footage_checker_service = footage_checker
checker = footage_checker
service = footage_checker
manager = footage_checker
instance = footage_checker
singleton = footage_checker
default_checker = footage_checker
default_service = footage_checker


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

def scan_directory(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return check(*args, **kwargs)


def lookup(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return check(*args, **kwargs)


def query(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return check(*args, **kwargs)


def verify(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return check(*args, **kwargs)


__all__ = [
    "FootageChecker",
    "FootageCheckService",
    "FootageAvailabilityChecker",
    "FootageAvailabilityService",
    "FootageService",
    "check_footage",
    "has_footage",
    "is_available",
    "footage_exists",
    "check",
    "lookup",
    "query",
    "verify",
    "get_status",
    "parse_segment_start",
    "segment_interval_for_file",
    "group_segments_by_camera",
    "footage_checker",
    "footage_checker_service",
    "checker",
    "service",
    "DEFAULT_SEGMENT_SECONDS",
    "DEFAULT_TOLERANCE_SECONDS",
]
