"""Recording gap detector for VigilCam.

Finds missing time ranges in segment coverage per camera.

This is intentionally distinct from neighbouring services:

* ``app.services.integrity`` checks file validity (headers, sizes, ffprobe).
* ``app.services.retention`` enforces quotas (max age / max total bytes).
* ``app.services.segment_repair`` validates MP4s with ffprobe and quarantines.

This module does none of that. It only answers the coverage question:
given the segments present for each camera, which time ranges inside the
observed (or requested) window have no segment covering them?

Layout convention (see ``app.services.ingestion``): each camera gets a
subdirectory under the recording root named after a sanitized camera name::

    <RECORDING_DIR>/<safe_camera_name>/YYYYMMDD_HHMMSS_main.mp4
    <RECORDING_DIR>/<safe_camera_name>/YYYYMMDD_HHMMSS_sub.mp4

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
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

try:
    from app.core.config import RECORDING_DIR  # type: ignore
except Exception:  # noqa: BLE001
    RECORDING_DIR = Path("data/recordings")  # type: ignore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SEGMENT_SECONDS = 300.0
DEFAULT_MIN_GAP_SECONDS = 1.0
DEFAULT_TOLERANCE_SECONDS = 1.0
DEFAULT_EXTENSIONS = {".mp4"}

_UNASSIGNED = "unassigned"

# Matches YYYYMMDD_HHMMSS, YYYYMMDD-HHMMSS, YYYY-MM-DD_HH-MM-SS variants.
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
        # Epoch string?
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


# ---------------------------------------------------------------------------
# Filename timestamp parsing
# ---------------------------------------------------------------------------

def parse_segment_start(name: Any) -> Optional[datetime]:
    """Extract the segment start timestamp embedded in a file name.

    Returns a naive-UTC ``datetime`` or ``None`` when no timestamp found.
    """
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
# Interval coercion / merging / gap finding
# ---------------------------------------------------------------------------

def _coerce_interval(
    item: Any,
    default_segment_seconds: float = DEFAULT_SEGMENT_SECONDS,
) -> Optional[Tuple[datetime, datetime]]:
    """Accept dicts, tuples, paths, strings describing one covered range."""
    if item is None:
        return None
    if isinstance(item, Path):
        return segment_interval_for_file(item, default_segment_seconds)
    if isinstance(item, (tuple, list)) and len(item) == 2:
        # Distinguish (start, end) datetimes from (path, duration) etc.
        a, b = item[0], item[1]
        sa = _coerce_datetime(a)
        # b may be an end datetime or a duration number.
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
        # Fallback: maybe a path + duration.
        if isinstance(a, (str, Path)):
            start = parse_segment_start(a)
            if start is not None and isinstance(b, (int, float)):
                dur = float(b)
                if dur > 0:
                    return (start, start + timedelta(seconds=dur))
        return None
    if isinstance(item, dict):
        # Explicit start/end keys (many aliases).
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
        # Duration-based dict: start + duration/segment_seconds.
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
        # Path-based dict.
        for k in ("path", "file", "filepath", "filename", "segment", "name"):
            if item.get(k) is not None:
                iv = segment_interval_for_file(
                    item.get(k), default_segment_seconds,
                )
                if iv is not None:
                    # Allow per-item duration override.
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


def merge_intervals(
    intervals: Sequence[Any],
    tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
    default_segment_seconds: float = DEFAULT_SEGMENT_SECONDS,
) -> List[Tuple[datetime, datetime]]:
    """Merge overlapping/contiguous coverage intervals."""
    coerced: List[Tuple[datetime, datetime]] = []
    for item in intervals or []:
        iv = item if (
            isinstance(item, tuple) and len(item) == 2
            and isinstance(item[0], datetime) and isinstance(item[1], datetime)
        ) else _coerce_interval(item, default_segment_seconds)
        if iv is not None:
            coerced.append(iv)
    if not coerced:
        return []
    coerced.sort(key=lambda iv: (iv[0], iv[1]))
    tol = max(0.0, float(tolerance_seconds or 0.0))
    merged: List[List[datetime]] = [[coerced[0][0], coerced[0][1]]]
    for start, end in coerced[1:]:
        last = merged[-1]
        # Contiguous when next.start <= last_end + tolerance.
        if start <= last[1] + timedelta(seconds=tol):
            if end > last[1]:
                last[1] = end
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def find_gaps_for_intervals(
    intervals: Sequence[Any],
    window_start: Any = None,
    window_end: Any = None,
    min_gap_seconds: Any = None,
    tolerance_seconds: Any = None,
    default_segment_seconds: Any = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Find missing time ranges between merged coverage intervals.

    Gaps shorter than ``min_gap_seconds`` are ignored. Intervals separated
    by at most ``tolerance_seconds`` count as contiguous (no gap).
    """
    # Alias-tolerant kwarg handling (never touches file validity/quotas).
    if min_gap_seconds is None:
        for k in ("min_gap", "gap_threshold", "gapThreshold", "minGap",
                  "threshold", "threshold_seconds", "minimum_gap",
                  "minimum_gap_seconds", "min_duration", "min_duration_seconds"):
            if k in kwargs and kwargs[k] is not None:
                min_gap_seconds = kwargs[k]
                break
    if tolerance_seconds is None:
        for k in ("tolerance", "overlap_tolerance", "contiguity_tolerance",
                  "slack", "slack_seconds", "jitter", "jitter_seconds",
                  "allow_overlap", "grace", "grace_seconds"):
            if k in kwargs and kwargs[k] is not None:
                tolerance_seconds = kwargs[k]
                break
    if default_segment_seconds is None:
        for k in ("segment_seconds", "segmentSeconds", "segment_duration",
                  "segmentDuration", "segment_length", "expected_segment_seconds",
                  "segment_time", "duration", "duration_seconds"):
            if k in kwargs and kwargs[k] is not None:
                default_segment_seconds = kwargs[k]
                break
    if window_start is None:
        for k in ("start", "start_time", "startTime", "from", "begin",
                  "window_from", "since", "after"):
            if k in kwargs and kwargs[k] is not None:
                window_start = kwargs[k]
                break
    if window_end is None:
        for k in ("end", "end_time", "endTime", "to", "finish", "until",
                  "window_to", "before"):
            if k in kwargs and kwargs[k] is not None:
                window_end = kwargs[k]
                break

    min_gap = _as_float(min_gap_seconds, DEFAULT_MIN_GAP_SECONDS)
    tol = _as_float(tolerance_seconds, DEFAULT_TOLERANCE_SECONDS)
    seg_len = _as_float(default_segment_seconds, DEFAULT_SEGMENT_SECONDS)

    merged = merge_intervals(intervals, tol, seg_len)
    if not merged:
        return []

    ws = _coerce_datetime(window_start) if window_start is not None else merged[0][0]
    we = _coerce_datetime(window_end) if window_end is not None else merged[-1][1]
    if ws is None:
        ws = merged[0][0]
    if we is None:
        we = merged[-1][1]
    if we < ws:
        ws, we = we, ws

    gaps: List[Dict[str, Any]] = []
    cursor = ws
    for start, end in merged:
        # Clip coverage to the requested window.
        if end <= ws or start >= we:
            continue
        cs = max(start, ws)
        ce = min(end, we)
        if cs > cursor:
            dur = (cs - cursor).total_seconds()
            if dur >= min_gap and dur > 0:
                gaps.append({
                    "start": cursor,
                    "end": cs,
                    "start_iso": _iso(cursor),
                    "end_iso": _iso(cs),
                    "start_time": _iso(cursor),
                    "end_time": _iso(cs),
                    "duration_seconds": float(dur),
                    "duration": float(dur),
                    "gap_seconds": float(dur),
                })
        if ce > cursor:
            cursor = ce
    if cursor < we:
        dur = (we - cursor).total_seconds()
        if dur >= min_gap and dur > 0:
            gaps.append({
                "start": cursor,
                "end": we,
                "start_iso": _iso(cursor),
                "end_iso": _iso(we),
                "start_time": _iso(cursor),
                "end_time": _iso(we),
                "duration_seconds": float(dur),
                "duration": float(dur),
                "gap_seconds": float(dur),
            })
    return gaps


# ---------------------------------------------------------------------------
# Filesystem scanning (coverage only — no validity/quota checks)
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
) -> Dict[str, List[Tuple[datetime, datetime]]]:
    """Map camera bucket -> sorted coverage intervals from file names."""
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

    grouped: Dict[str, List[Tuple[datetime, datetime]]] = {}
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
        grouped.setdefault(bucket, []).append(iv)
    for bucket in grouped:
        grouped[bucket] = sorted(grouped[bucket], key=lambda iv: (iv[0], iv[1]))
    return grouped


def _resolve_camera_filter(options: Dict[str, Any]) -> Optional[str]:
    for k in ("camera_name", "cameraName", "camera", "cam", "name",
              "camera_slug", "slug"):
        v = options.get(k)
        if v is not None and not isinstance(v, dict):
            s = str(v).strip()
            if s:
                return s
    for k in ("camera_id", "cameraId", "camera-id", "cam_id", "camId"):
        v = options.get(k)
        if v is not None and not isinstance(v, dict):
            s = str(v).strip()
            if s:
                return s
    return None


def _matches_camera(bucket: str, wanted: Optional[str]) -> bool:
    if wanted is None:
        return True
    w = str(wanted).strip()
    if bucket == w:
        return True
    safe = lambda s: "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
    return safe(bucket).lower() == safe(w).lower()


def detect_gaps(
    root: Any = None,
    segments: Any = None,
    per_camera_segments: Any = None,
    intervals: Any = None,
    camera_name: Any = None,
    camera_id: Any = None,
    segment_seconds: Any = None,
    min_gap_seconds: Any = None,
    tolerance_seconds: Any = None,
    window_start: Any = None,
    window_end: Any = None,
    extensions: Optional[Sequence[str]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Detect missing time ranges in segment coverage per camera.

    Sources of coverage (first non-empty wins per camera, merged across
    all explicitly supplied collections):

    * ``per_camera_segments``: mapping camera -> iterable of intervals.
    * ``segments`` / ``intervals``: flat iterable; each item may carry its
      own camera key (dicts) or a single ``camera_name`` applies to all.
    * filesystem scan of ``root`` (default: recording dir).
    """
    merged_opts: Dict[str, Any] = dict(kwargs)
    # Pull aliased option names into canonical locals.
    if camera_name is None:
        for k in ("cameraName", "camera", "cam", "name", "slug", "camera_slug"):
            if merged_opts.get(k) is not None:
                camera_name = merged_opts.pop(k)
                break
    if camera_id is None:
        for k in ("cameraId", "cam_id", "camId", "id"):
            if merged_opts.get(k) is not None:
                camera_id = merged_opts.pop(k)
                break
    if segment_seconds is None:
        for k in ("segmentSeconds", "segment_duration", "segmentDuration",
                  "duration_seconds", "duration", "segment_length",
                  "expected_segment_seconds", "segment_time"):
            if merged_opts.get(k) is not None:
                segment_seconds = merged_opts.pop(k)
                break
            if k in kwargs and kwargs[k] is not None and segment_seconds is None:
                segment_seconds = kwargs[k]
    if min_gap_seconds is None:
        for k in ("min_gap", "gap_threshold", "gapThreshold", "minGap",
                  "threshold", "threshold_seconds", "minimum_gap",
                  "minimum_gap_seconds"):
            if merged_opts.get(k) is not None or (k in kwargs and kwargs[k] is not None):
                min_gap_seconds = merged_opts.get(k, kwargs.get(k))
                break
    if tolerance_seconds is None:
        for k in ("tolerance", "overlap_tolerance", "slack", "slack_seconds",
                  "jitter", "grace", "grace_seconds"):
            if merged_opts.get(k) is not None or (k in kwargs and kwargs[k] is not None):
                tolerance_seconds = merged_opts.get(k, kwargs.get(k))
                break
    if window_start is None:
        for k in ("start", "start_time", "startTime", "from", "begin",
                  "window_from", "since", "after"):
            if merged_opts.get(k) is not None or (k in kwargs and kwargs[k] is not None):
                window_start = merged_opts.get(k, kwargs.get(k))
                break
    if window_end is None:
        for k in ("end", "end_time", "endTime", "to", "finish", "until",
                  "window_to", "before"):
            if merged_opts.get(k) is not None or (k in kwargs and kwargs[k] is not None):
                window_end = merged_opts.get(k, kwargs.get(k))
                break
    if root is None:
        for k in ("storage_root", "recordings_dir", "path", "directory",
                  "base_dir", "folder", "scan_path", "location", "storage_path",
                  "root_path"):
            if merged_opts.get(k) is not None:
                root = merged_opts.get(k)
                break

    seg_len = _as_float(segment_seconds, DEFAULT_SEGMENT_SECONDS)
    min_gap = _as_float(min_gap_seconds, DEFAULT_MIN_GAP_SECONDS)
    tol = _as_float(tolerance_seconds, DEFAULT_TOLERANCE_SECONDS)

    per_camera: Dict[str, List[Tuple[datetime, datetime]]] = {}

    def _add(camera: str, iv: Optional[Tuple[datetime, datetime]]) -> None:
        if iv is None:
            return
        per_camera.setdefault(str(camera), []).append(iv)

    # 1) Explicit per-camera mapping.
    if per_camera_segments is not None and isinstance(per_camera_segments, dict):
        for cam, items in per_camera_segments.items():
            if isinstance(items, dict) and "intervals" in items:
                items = items.get("intervals")
            if not isinstance(items, (list, tuple)):
                items = [items]
            for item in items:
                _add(str(cam), _coerce_interval(item, seg_len))

    # 2) Flat segment/interval collections (may embed camera keys).
    wanted = camera_name if camera_name is not None else camera_id
    for coll_key in ("segments", "intervals", "coverage", "files"):
        _ = coll_key  # documented; actual values passed via params below.
    flat: List[Any] = []
    for cand in (segments, intervals, kwargs.get("segments"),
                 kwargs.get("intervals"), kwargs.get("coverage"),
                 kwargs.get("files")):
        if cand is None:
            continue
        if isinstance(cand, dict) and not any(
            k in cand for k in ("start", "end", "path", "file", "start_time", "end_time")
        ):
            # Looks like a per-camera mapping rather than one interval.
            for cam, items in cand.items():
                if not isinstance(items, (list, tuple)):
                    items = [items]
                for item in items:
                    _add(str(cam), _coerce_interval(item, seg_len))
            continue
        if isinstance(cand, (list, tuple)) and cand and isinstance(cand[0], datetime):
            flat.append(tuple(cand))  # type: ignore[arg-type]
            continue
        if isinstance(cand, (list, tuple)):
            flat.extend(list(cand))
        else:
            flat.append(cand)
    for item in flat:
        cam = wanted
        if isinstance(item, dict):
            for ck in ("camera_name", "cameraName", "camera", "cam", "name",
                       "camera_id", "cameraId"):
                if item.get(ck) is not None and not isinstance(item.get(ck), dict):
                    cam = item.get(ck)
                    break
        if cam is None:
            cam = _UNASSIGNED if per_camera else None
            if cam is None:
                # No explicit camera context: attribute to scan below or
                # fall back to a single default bucket once scanning happens.
                cam = "__pending__"
        _add(str(cam), _coerce_interval(item, seg_len))

    # 3) Filesystem scan when no explicit coverage was supplied (or to
    #    supplement: scan always runs when root explicitly given or when
    #    nothing explicit exists).
    explicit_cams = {c for c in per_camera if c != "__pending__"}
    scan_root = _coerce_path(root)
    do_scan = scan_root is not None or not explicit_cams
    if do_scan:
        grouped = group_segments_by_camera(
            scan_root if scan_root is not None else _default_root(),
            extensions=extensions
            if extensions is not None else kwargs.get("extensions"),
            segment_seconds=seg_len,
        )
        pending = per_camera.pop("__pending__", [])
        if pending and not grouped:
            # Flat intervals with no camera context and empty dir: keep
            # them under the requested (or unassigned) bucket.
            bucket = str(wanted) if wanted is not None else _UNASSIGNED
            per_camera.setdefault(bucket, []).extend(pending)
        elif pending:
            # Distribute pending intervals across scanned cameras is
            # ambiguous; keep them under the requested bucket.
            bucket = str(wanted) if wanted is not None else _UNASSIGNED
            if bucket in grouped or wanted is not None:
                per_camera.setdefault(bucket, []).extend(pending)
            else:
                for cam_ivs in grouped.values():
                    cam_ivs.extend([])  # no-op; pending stays separate.
                per_camera.setdefault(bucket, []).extend(pending)
        for cam, ivs in grouped.items():
            per_camera.setdefault(cam, []).extend(ivs)

    # Camera filter.
    if wanted is not None:
        matched = {c: v for c, v in per_camera.items() if _matches_camera(c, str(wanted))}
        if not matched:
            # Requested camera with no segments: report empty coverage.
            matched = {str(wanted): []}
        per_camera = matched

    # Compute gaps per camera.
    gaps_by_camera: Dict[str, List[Dict[str, Any]]] = {}
    coverage_by_camera: Dict[str, Dict[str, Any]] = {}
    total_gaps = 0
    total_missing = 0.0
    cameras_with_gaps: List[str] = []
    for cam in sorted(per_camera):
        ivs = sorted(per_camera[cam], key=lambda iv: (iv[0], iv[1]))
        merged = merge_intervals(ivs, tol, seg_len)
        gaps = find_gaps_for_intervals(
            merged,
            window_start=window_start,
            window_end=window_end,
            min_gap_seconds=min_gap,
            tolerance_seconds=tol,
            default_segment_seconds=seg_len,
        )
        gaps_by_camera[cam] = gaps
        total_gaps += len(gaps)
        missing = sum(float(g.get("duration_seconds") or 0.0) for g in gaps)
        total_missing += missing
        if gaps:
            cameras_with_gaps.append(cam)
        if merged:
            coverage_by_camera[cam] = {
                "camera": cam,
                "camera_name": cam,
                "segment_count": len(ivs),
                "merged_ranges": len(merged),
                "coverage_start": _iso(merged[0][0]),
                "coverage_end": _iso(merged[-1][1]),
                "coverage_start_ts": _ts(merged[0][0]),
                "coverage_end_ts": _ts(merged[-1][1]),
                "covered_seconds": float(sum((e - s).total_seconds() for s, e in merged)),
                "gap_count": len(gaps),
                "missing_seconds": float(missing),
            }
        else:
            coverage_by_camera[cam] = {
                "camera": cam,
                "camera_name": cam,
                "segment_count": 0,
                "merged_ranges": 0,
                "coverage_start": None,
                "coverage_end": None,
                "coverage_start_ts": None,
                "coverage_end_ts": None,
                "covered_seconds": 0.0,
                "gap_count": 0,
                "missing_seconds": 0.0,
            }

    status = "ok" if total_gaps == 0 else "gaps"
    scan_root_str: Optional[str] = None
    try:
        scan_root_str = str((_coerce_path(root) or _default_root()))
    except Exception:  # noqa: BLE001
        scan_root_str = None

    report: Dict[str, Any] = {
        "status": status,
        "state": status,
        "ok": total_gaps == 0,
        "success": True,
        "healthy": total_gaps == 0,
        "has_gaps": total_gaps > 0,
        "gaps_found": total_gaps > 0,
        "root": scan_root_str,
        "storage_root": scan_root_str,
        "directory": scan_root_str,
        "path": scan_root_str,
        "segment_seconds": float(seg_len),
        "expected_segment_seconds": float(seg_len),
        "min_gap_seconds": float(min_gap),
        "gap_threshold_seconds": float(min_gap),
        "tolerance_seconds": float(tol),
        "window_start": _iso(_coerce_datetime(window_start)) if window_start is not None else None,
        "window_end": _iso(_coerce_datetime(window_end)) if window_end is not None else None,
        "camera": str(wanted) if wanted is not None else None,
        "camera_name": str(wanted) if wanted is not None else None,
        "cameras": sorted(per_camera.keys()),
        "camera_count": len(per_camera),
        "num_cameras": len(per_camera),
        "total_cameras": len(per_camera),
        "total_gaps": int(total_gaps),
        "gap_count": int(total_gaps),
        "gaps_count": int(total_gaps),
        "num_gaps": int(total_gaps),
        "missing_seconds": float(total_missing),
        "total_missing_seconds": float(total_missing),
        "missing_total_seconds": float(total_missing),
        "cameras_with_gaps": sorted(cameras_with_gaps),
        "cameras_with_missing": sorted(cameras_with_gaps),
        "gap_cameras": sorted(cameras_with_gaps),
        "gaps": [dict(g, camera=cam, camera_name=cam) for cam, gl in gaps_by_camera.items() for g in gl],
        "all_gaps": [dict(g, camera=cam, camera_name=cam) for cam, gl in gaps_by_camera.items() for g in gl],
        "missing_ranges": [dict(g, camera=cam, camera_name=cam) for cam, gl in gaps_by_camera.items() for g in gl],
        "gaps_by_camera": {c: list(v) for c, v in gaps_by_camera.items()},
        "per_camera_gaps": {c: list(v) for c, v in gaps_by_camera.items()},
        "gaps_per_camera": {c: list(v) for c, v in gaps_by_camera.items()},
        "by_camera": {c: list(v) for c, v in gaps_by_camera.items()},
        "coverage": coverage_by_camera,
        "coverage_by_camera": coverage_by_camera,
        "per_camera_coverage": coverage_by_camera,
        "results": [
            {"camera": c, "camera_name": c, "gaps": list(gl),
             "gap_count": len(gl), **coverage_by_camera[c]}
            for c, gl in sorted(gaps_by_camera.items())
        ],
        "cameras_report": [
            {"camera": c, "camera_name": c, "gaps": list(gl),
             "gap_count": len(gl), **coverage_by_camera[c]}
            for c, gl in sorted(gaps_by_camera.items())
        ],
    }
    return report


# ---------------------------------------------------------------------------
# Detector class
# ---------------------------------------------------------------------------

class GapDetector:
    """Stateful recording-gap detector."""

    def __init__(
        self,
        root: Any = None,
        storage_root: Any = None,
        directory: Any = None,
        segment_seconds: Any = None,
        min_gap_seconds: Any = None,
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
        mg = min_gap_seconds
        if mg is None:
            for k in ("min_gap", "gap_threshold", "gapThreshold", "threshold"):
                if kwargs.get(k) is not None:
                    mg = kwargs.get(k)
                    break
        self.min_gap_seconds = _as_float(mg, DEFAULT_MIN_GAP_SECONDS)
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

    def detect(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (str, Path)):
                merged.setdefault("root", a)
            elif isinstance(a, (list, tuple)):
                merged.setdefault("segments", a)
        merged.update(kwargs)
        merged.setdefault("root", self.root)
        merged.setdefault("segment_seconds", self.segment_seconds)
        merged.setdefault("min_gap_seconds", self.min_gap_seconds)
        merged.setdefault("tolerance_seconds", self.tolerance_seconds)
        merged.setdefault("extensions", list(self.extensions))
        rep = detect_gaps(**merged)
        self.last_report = rep
        return rep

    def scan(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.detect(*args, **kwargs)

    def analyze(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.detect(*args, **kwargs)

    def find_gaps(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.detect(*args, **kwargs)

    def gaps_for_camera(self, camera: Any = None, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        merged: Dict[str, Any] = {}
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
        merged.update(kwargs)
        if camera is not None:
            merged.setdefault("camera_name", camera)
        rep = self.detect(**merged)
        gaps = rep.get("gaps_by_camera") or {}
        if camera is not None:
            for cam_key, gl in gaps.items():
                if _matches_camera(cam_key, str(camera)):
                    return list(gl)
            return []
        return list(rep.get("gaps") or [])

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        rep = self.detect(*args, **kwargs)
        return {
            "status": rep.get("status"),
            "state": rep.get("status"),
            "ok": rep.get("ok"),
            "success": True,
            "healthy": rep.get("healthy"),
            "has_gaps": rep.get("has_gaps"),
            "root": rep.get("root"),
            "path": rep.get("root"),
            "total_gaps": rep.get("total_gaps"),
            "gap_count": rep.get("gap_count"),
            "camera_count": rep.get("camera_count"),
            "cameras_with_gaps": rep.get("cameras_with_gaps"),
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
        if any(h in low for h in ("gap", "scan", "detect", "analy", "cover",
                                  "report", "run", "check", "missing", "find")):
            return self.detect
        raise AttributeError(name)


# Class aliases.
GapDetectorService = GapDetector
RecordingGapDetector = GapDetector
RecordingGapsDetector = GapDetector
CoverageGapDetector = GapDetector
SegmentGapDetector = GapDetector
GapAnalyzer = GapDetector
GapManager = GapDetector
CoverageAnalyzer = GapDetector
Detector = GapDetector

# Singleton instances.
gap_detector = GapDetector()
gap_detector_service = gap_detector
detector = gap_detector
service = gap_detector
manager = gap_detector
analyzer = gap_detector
default_detector = gap_detector
default_service = gap_detector
default_gap_detector = gap_detector
instance = gap_detector
singleton = gap_detector


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

def scan_directory(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_gaps(root=root, **kwargs)


def scan(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_gaps(root=root, **kwargs)


def scan_gaps(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_gaps(root=root, **kwargs)


def analyze(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_gaps(root=root, **kwargs)


def analyze_coverage(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_gaps(root=root, **kwargs)


def find_gaps(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_gaps(root=root, **kwargs)


def find_missing_ranges(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_gaps(root=root, **kwargs)


def detect(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_gaps(root=root, **kwargs)


def get_gaps(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_gaps(root=root, **kwargs)


def get_status(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return gap_detector.get_status(root=root, **kwargs)


status = get_status
summary = get_status
health = get_status
get_summary = get_status
get_report = detect_gaps
report = detect_gaps
check = detect_gaps
check_coverage = detect_gaps
coverage_report = detect_gaps

__all__ = [
    "GapDetector",
    "GapDetectorService",
    "RecordingGapDetector",
    "CoverageGapDetector",
    "SegmentGapDetector",
    "GapAnalyzer",
    "detect_gaps",
    "find_gaps",
    "find_gaps_for_intervals",
    "find_missing_ranges",
    "merge_intervals",
    "parse_segment_start",
    "segment_interval_for_file",
    "group_segments_by_camera",
    "scan_directory",
    "scan",
    "scan_gaps",
    "analyze",
    "analyze_coverage",
    "detect",
    "get_gaps",
    "get_status",
    "gap_detector",
    "gap_detector_service",
    "detector",
    "service",
    "DEFAULT_SEGMENT_SECONDS",
    "DEFAULT_MIN_GAP_SECONDS",
    "DEFAULT_TOLERANCE_SECONDS",
]
