"""Event-to-segment linker service.

Links detection/motion events to the recording segments that overlap them
in time. Two complementary mechanisms are provided:

* **Auto-discovery** — given an event (camera + timestamp), scan the
  recording root (``<RECORDING_DIR>/<safe_camera_name>/...mp4``) and/or the
  ``Recording`` DB table for segments overlapping
  ``[event_ts - window_before, event_ts + window_after]``.
* **Manual links** — explicit, persisted (in-memory) ``event_id <-> segment``
  associations created via :func:`link_event_to_segment`.

The store is intentionally in-memory (thread-safe) so the feature works
without DB migrations and without edits to ``app/main.py``. Optional
``db``/``session`` arguments are accepted by the async entrypoints for
forward-compatibility.

Existing behaviour (verified): ``app.services.export`` builds clips from
segments (``find_segments``/``concat_segments``/``export_range``) and
``app.services.snapshots`` attaches images to events
(``save_event_snapshot``); no event-to-segment linking existed before this
module.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from app.core.config import RECORDING_DIR as _DEFAULT_RECORDING_DIR  # type: ignore
except Exception:  # noqa: BLE001
    _DEFAULT_RECORDING_DIR = Path("data/recordings")  # type: ignore

DEFAULT_WINDOW_BEFORE = 30.0
DEFAULT_WINDOW_AFTER = 30.0
DEFAULT_SEGMENT_DURATION = 300.0
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".ts", ".mov", ".m4v")

_TS_RE = re.compile(r"(\d{8})_(\d{6})")

_lock = threading.Lock()
_LINKS: Dict[int, Dict[str, Any]] = {}
_EVENT_INDEX: Dict[int, List[int]] = {}
_SEGMENT_INDEX: Dict[str, List[int]] = {}
_ID_COUNTER = 0

EVENT_ID_KEYS = (
    "event_id", "eventId", "event", "eventid", "id_event", "for_event",
    "camera_event_id", "id",
)
SEGMENT_KEYS = (
    "segment", "segment_path", "segmentPath", "path", "file_path", "filePath",
    "file", "recording_path", "recordingPath", "recording", "clip", "video",
    "src", "source", "source_path", "recording_id", "recordingId", "segment_id",
    "segmentId",
)
WINDOW_BEFORE_KEYS = (
    "window_before", "windowBefore", "before", "pre_roll", "preRoll",
    "preroll", "pre", "padding_before", "lookback", "look_back",
)
WINDOW_AFTER_KEYS = (
    "window_after", "windowAfter", "after", "post_roll", "postRoll",
    "postroll", "post", "padding_after", "lookahead", "look_ahead",
)
WINDOW_KEYS = ("window", "window_seconds", "windowSeconds", "padding", "tolerance", "delta")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_id() -> int:
    global _ID_COUNTER
    _ID_COUNTER += 1
    return _ID_COUNTER


def _safe_name(name: Any) -> str:
    s = str(name if name is not None else "default")
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default


def _coerce_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return default


def _first_present(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _flatten(data: Any) -> Dict[str, Any]:
    if data is None:
        return {}
    if hasattr(data, "model_dump"):
        try:
            return dict(data.model_dump())
        except Exception:  # noqa: BLE001
            pass
    if isinstance(data, dict):
        out = dict(data)
        for nested_key in (
            "payload", "options", "config", "params", "data", "body",
            "request", "fields", "entry", "link", "segment", "event",
        ):
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    try:
        return dict(data)
    except Exception:  # noqa: BLE001
        return {}


def _parse_time(value: Any) -> Optional[float]:
    """Parse a time value into epoch seconds (best effort)."""
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
        try:
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:  # noqa: BLE001
            return None
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
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%d_%H%M%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _parse_filename_ts(name: str) -> Optional[float]:
    m = _TS_RE.search(str(name))
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


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
            s = str(a).strip()
            # Heuristic: looks like a directory path (exists or has separators).
            p = Path(s).expanduser()
            if p.exists() and p.is_dir():
                return p
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    for key in (
        "recording_dir", "recordingDir", "recordings_dir", "recordingsDir",
        "storage_root", "storageRoot", "root", "path_root", "base_dir",
        "directory", "dir", "folder", "location", "scan_path", "recording_root",
    ):
        if flat.get(key) is not None:
            try:
                return Path(str(flat.get(key))).expanduser()
            except Exception:  # noqa: BLE001
                continue
    return _default_recording_dir(flat.get("recording_dir"))


def _resolve_windows(*args: Any, **kwargs: Any) -> Tuple[float, float]:
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "options", "request"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    before = _first_present(flat, WINDOW_BEFORE_KEYS)
    after = _first_present(flat, WINDOW_AFTER_KEYS)
    both = _first_present(flat, WINDOW_KEYS)
    if before is None and both is not None:
        before = both
    if after is None and both is not None:
        after = both
    b = _coerce_float(before, DEFAULT_WINDOW_BEFORE)
    a = _coerce_float(after, DEFAULT_WINDOW_AFTER)
    if b is None or b < 0:
        b = DEFAULT_WINDOW_BEFORE
    if a is None or a < 0:
        a = DEFAULT_WINDOW_AFTER
    return float(b), float(a)


def _event_to_dict(ev: Any) -> Dict[str, Any]:
    if ev is None:
        return {}
    if isinstance(ev, dict):
        return dict(ev)
    if hasattr(ev, "model_dump"):
        try:
            return dict(ev.model_dump())
        except Exception:  # noqa: BLE001
            pass
    out: Dict[str, Any] = {}
    for attr in (
        "id", "event_id", "eventId", "camera_id", "cameraId", "camera",
        "camera_name", "cameraName", "kind", "label", "confidence", "payload",
        "tags", "snapshot_path", "created_at", "createdAt", "started_at",
        "timestamp", "time", "event_time",
    ):
        try:
            val = getattr(ev, attr, None)
        except Exception:  # noqa: BLE001
            val = None
        if val is not None:
            out[attr] = val
    if not out:
        try:
            return dict(ev)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return {"value": ev}
    return out


def _event_time(event: Dict[str, Any]) -> Optional[float]:
    for key in (
        "created_at", "createdAt", "timestamp", "time", "event_time",
        "eventTime", "started_at", "start", "occurred_at",
    ):
        if event.get(key) is not None:
            ts = _parse_time(event.get(key))
            if ts is not None:
                return ts
    return None


def _event_camera(event: Dict[str, Any]) -> Tuple[Any, Any]:
    cam_id = None
    for key in ("camera_id", "cameraId", "cameraID", "cam_id"):
        if event.get(key) is not None:
            try:
                cam_id = int(event.get(key))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                cam_id = event.get(key)
            break
    cam_name = None
    for key in ("camera_name", "cameraName", "camera", "cam", "name"):
        val = event.get(key)
        if val is not None and not isinstance(val, dict):
            # Avoid treating numeric camera id stored under "camera" as name
            # when an explicit camera_id is absent; keep raw but prefer id.
            cam_name = val
            break
    return cam_id, cam_name


def _list_segment_files(
    camera_name: Any = None,
    recording_dir: Any = None,
) -> List[Path]:
    root = _default_recording_dir(recording_dir)
    files: List[Path] = []
    try:
        if not root.exists():
            return []
        if camera_name is None:
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in filenames:
                    if fn.lower().endswith(VIDEO_EXTENSIONS):
                        files.append(Path(dirpath) / fn)
            return sorted(set(files))
        safe = _safe_name(camera_name)
        candidates = [root / safe, root / str(camera_name)]
        found = False
        for cam_dir in candidates:
            if cam_dir.exists() and cam_dir.is_dir():
                for ext in ("*.mp4", "*.mkv", "*.ts", "*.mov", "*.m4v"):
                    files.extend(cam_dir.glob(ext))
                # also recursive one level for nested date dirs
                for sub in cam_dir.iterdir():
                    try:
                        if sub.is_dir():
                            for ext in ("*.mp4", "*.mkv", "*.ts", "*.mov", "*.m4v"):
                                files.extend(sub.glob(ext))
                    except OSError:
                        continue
                found = True
        if not found:
            # Fall back to full-tree scan filtered by safe-name prefix.
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in filenames:
                    if fn.lower().endswith(VIDEO_EXTENSIONS):
                        files.append(Path(dirpath) / fn)
            # Prefer files whose path contains the safe name.
            preferred = [f for f in files if safe.lower() in str(f).lower()]
            if preferred:
                return sorted(set(preferred))
        return sorted(set(files))
    except Exception:  # noqa: BLE001
        return []


def _file_info(path: Any) -> Dict[str, Any]:
    p = Path(str(path))
    name = p.name
    ts = _parse_filename_ts(name)
    mtime: Optional[float] = None
    size: Optional[int] = None
    try:
        st = p.stat()
        mtime = float(st.st_mtime)
        size = int(st.st_size)
    except OSError:
        pass
    if ts is None:
        ts = mtime
    end = (ts + DEFAULT_SEGMENT_DURATION) if ts is not None else None
    info: Dict[str, Any] = {
        "path": str(p),
        "segment_path": str(p),
        "file_path": str(p),
        "file": str(p),
        "recording_path": str(p),
        "filename": name,
        "name": name,
        "start": ts,
        "start_time": ts,
        "timestamp": ts,
        "end": end,
        "end_time": end,
        "duration": DEFAULT_SEGMENT_DURATION,
        "size_bytes": size,
        "size": size,
        "mtime": mtime,
    }
    # camera bucket = parent dir name
    try:
        info["camera_name"] = p.parent.name
        info["camera"] = p.parent.name
    except Exception:  # noqa: BLE001
        pass
    return info


def find_segments(
    *args: Any,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Find recording segment files overlapping ``[start, end]``.

    Accepts flexible inputs, e.g.::

        find_segments("cam1", start=..., end=...)
        find_segments(camera_name="cam1", start_time=..., end_time=...)
        find_segments({"camera": "cam1", "start": ..., "end": ...})
    """
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif isinstance(a, (str, Path)) and "camera_name" not in flat and "camera" not in flat:
            flat["camera_name"] = a
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "options", "request", "filter"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    cam = flat.get("camera_name", flat.get("cameraName", flat.get("camera", flat.get("cam"))))
    if cam is None:
        cam = flat.get("camera_id") if isinstance(flat.get("camera_id"), str) else None
    start = None
    end = None
    for key in ("start", "start_time", "startTime", "from", "from_time", "begin", "since", "after", "min_time"):
        if flat.get(key) is not None:
            start = flat.get(key)
            break
    for key in ("end", "end_time", "endTime", "to", "to_time", "until", "before", "max_time"):
        if flat.get(key) is not None:
            end = flat.get(key)
            break
    start_ts = _parse_time(start)
    end_ts = _parse_time(end)
    recording_dir = _coerce_recording_dir(*args, **kwargs)
    files = _list_segment_files(cam, recording_dir)
    out: List[Dict[str, Any]] = []
    for f in files:
        info = _file_info(f)
        fts = info.get("start")
        fend = info.get("end")
        if start_ts is None and end_ts is None:
            out.append(info)
            continue
        s = start_ts if start_ts is not None else float("-inf")
        e = end_ts if end_ts is not None else float("inf")
        if fts is None:
            out.append(info)
            continue
        seg_end = fend if fend is not None else (fts + DEFAULT_SEGMENT_DURATION)
        if seg_end >= s and fts <= e:
            out.append(info)
    return out


def discover_segments_for_event(
    *args: Any,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Auto-discover segments overlapping an event's timestamp.

    Examples::

        discover_segments_for_event({"camera_id": 1, "created_at": ...})
        discover_segments_for_event(event_id=1, db=session)
        discover_segments_for_event(1, window_before=60, window_after=60)
    """
    flat: Dict[str, Any] = {}
    event_dict: Optional[Dict[str, Any]] = None
    positional_ids: List[Any] = []
    for a in args:
        if isinstance(a, dict):
            if any(k in a for k in ("camera_id", "camera_name", "created_at", "timestamp", "camera")) and "event_id" not in a and event_dict is None:
                event_dict = dict(a)
            else:
                flat.update(a)
                # an inline event dict passed as the wrapper
                if event_dict is None and any(k in a for k in ("camera_id", "created_at", "timestamp")):
                    event_dict = dict(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            positional_ids.append(a)
        elif isinstance(a, (str, Path)):
            # could be a recording_dir override if it looks like a path
            p = Path(str(a)).expanduser()
            if p.exists():
                flat.setdefault("recording_dir", str(a))
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "options", "request", "event"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            if event_dict is None and any(k in nested for k in ("camera_id", "camera_name", "created_at", "timestamp")):
                event_dict = dict(nested)
            for k, v in nested.items():
                flat.setdefault(k, v)
    if event_dict is None:
        for key in ("event", "source", "source_event", "original", "target"):
            if isinstance(flat.get(key), dict):
                event_dict = dict(flat.get(key))
                break
    if event_dict is None:
        # Build from flat event-ish keys.
        cand = {k: flat.get(k) for k in (
            "camera_id", "cameraId", "camera", "camera_name", "cameraName",
            "created_at", "createdAt", "timestamp", "time", "event_time",
            "kind", "label",
        ) if flat.get(k) is not None}
        if cand:
            event_dict = dict(cand)
    event_id = _first_present(flat, EVENT_ID_KEYS)
    if event_id is None and positional_ids:
        event_id = positional_ids[0]
    if event_dict is None and event_id is not None:
        inline_ts = None
        for key in ("created_at", "createdAt", "timestamp", "time", "event_time", "start", "start_time"):
            if flat.get(key) is not None:
                inline_ts = flat.get(key)
                break
        event_dict = {"id": event_id, "event_id": event_id}
        if inline_ts is not None:
            event_dict["created_at"] = inline_ts
        for key in ("camera_id", "cameraId", "camera", "camera_name", "cameraName"):
            if flat.get(key) is not None:
                event_dict[key] = flat.get(key)
                break
    if event_dict is None:
        return []
    event_dict = _event_to_dict(event_dict)
    # DB enrichment intentionally skipped in sync path; async path handles it.
    ts = _event_time(event_dict)
    if ts is None:
        return []
    before, after = _resolve_windows(flat)
    window_start = ts - before
    window_end = ts + after
    cam_id, cam_name = _event_camera(event_dict)
    anchor: Any = cam_name if cam_name is not None else (str(cam_id) if cam_id is not None else None)
    # If an explicit camera override was supplied, prefer it.
    for key in ("camera_name", "cameraName", "camera", "cam"):
        if flat.get(key) is not None and not isinstance(flat.get(key), dict):
            anchor = flat.get(key)
            break
    recording_dir = _coerce_recording_dir(*args, **kwargs)
    files = _list_segment_files(anchor, recording_dir)
    # When camera is unknown, _list_segment_files(None) already scans all.
    out: List[Dict[str, Any]] = []
    for f in files:
        info = _file_info(f)
        fts = info.get("start")
        fend = info.get("end")
        if fts is None:
            continue
        seg_end = fend if fend is not None else (fts + DEFAULT_SEGMENT_DURATION)
        if seg_end >= window_start and fts <= window_end:
            info = dict(info)
            info["event_id"] = event_dict.get("id", event_dict.get("event_id", event_id))
            info["overlap_start"] = max(fts, window_start)
            info["overlap_end"] = min(seg_end, window_end)
            out.append(info)
    # Merge manual links for this event (dedupe by path).
    eid_int = _coerce_int(event_dict.get("id", event_dict.get("event_id", event_id)), None)
    if eid_int is not None:
        with _lock:
            manual = [_to_public(_LINKS[n]) for n in list(_EVENT_INDEX.get(int(eid_int), [])) if n in _LINKS]
        seen = {str(o.get("path")) for o in out}
        for m in manual:
            mp = str(m.get("segment_path") or m.get("path") or "")
            if mp and mp not in seen:
                info = _file_info(mp) if mp else {}
                merged = dict(info) if info else {}
                merged.update({k: v for k, v in m.items() if v is not None})
                merged.setdefault("path", mp)
                merged.setdefault("segment_path", mp)
                out.append(merged)
                seen.add(mp)
            elif not mp:
                out.append(dict(m))
    out.sort(key=lambda d: (d.get("start") or 0))
    limit = _coerce_int(_first_present(flat, ("limit", "n", "count", "size", "page_size")), None)
    if limit is not None and limit >= 0:
        out = out[:limit]
    return out


def _segment_key(segment: Any) -> str:
    if segment is None:
        return ""
    if isinstance(segment, dict):
        for key in ("segment_path", "path", "file_path", "filePath", "recording_path", "file", "segment"):
            if segment.get(key):
                return str(segment.get(key))
        for key in ("recording_id", "recordingId", "segment_id", "segmentId", "id"):
            if segment.get(key) is not None:
                return str(segment.get(key))
        return ""
    return str(segment)


def _extract_segment_ref(*args: Any, **kwargs: Any) -> Tuple[Any, Any]:
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:  # noqa: BLE001
                pass
    flat.update(kwargs or {})
    for nested_key in ("segment", "link", "payload", "params", "data", "body", "request"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    seg: Any = None
    for key in SEGMENT_KEYS:
        if flat.get(key) is not None:
            seg = flat.get(key)
            break
    if seg is None and args:
        for a in args:
            if isinstance(a, (str, Path)):
                p = Path(str(a)).expanduser()
                if str(a).strip() and (p.exists() or "/" in str(a) or str(a).lower().endswith(VIDEO_EXTENSIONS)):
                    seg = str(a)
                    break
    recording_id = None
    for key in ("recording_id", "recordingId", "recordingID", "segment_id", "segmentId"):
        if flat.get(key) is not None:
            recording_id = flat.get(key)
            break
    if seg is None and recording_id is not None:
        seg = recording_id
    if isinstance(seg, dict):
        # keep dict form; recording id may be inside
        if recording_id is None:
            for key in ("recording_id", "recordingId", "segment_id", "segmentId", "id"):
                if seg.get(key) is not None:
                    recording_id = seg.get(key)
                    break
    return seg, recording_id


def _extract_event_id(*args: Any, **kwargs: Any) -> Optional[int]:
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif isinstance(a, (int, float)) and not isinstance(a, bool) and "event_id" not in flat:
            flat["event_id"] = a
    flat.update(kwargs or {})
    for nested_key in ("event", "link", "payload", "params", "data", "body", "request"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    raw = _first_present(flat, EVENT_ID_KEYS)
    if raw is not None and isinstance(raw, dict):
        raw = raw.get("id", raw.get("event_id"))
    if raw is None and args:
        for a in args:
            if isinstance(a, (int, float)) and not isinstance(a, bool):
                raw = a
                break
    return _coerce_int(raw, None)


def _to_public(link: Dict[str, Any]) -> Dict[str, Any]:
    return dict(link)


def link_event_to_segment(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create (or return existing) manual link between event and segment.

    Accepts flexible inputs::

        link_event_to_segment(event_id=1, segment="/path/to/seg.mp4")
        link_event_to_segment(1, "/path/to/seg.mp4")
        link_event_to_segment({"event_id": 1, "segment_path": "..."})
    """
    flat: Dict[str, Any] = {}
    positional: List[Any] = []
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:  # noqa: BLE001
                pass
        else:
            positional.append(a)
    flat.update(kwargs or {})
    for nested_key in ("link", "payload", "params", "data", "body", "request", "segment"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    event_id = _extract_event_id(*args, **kwargs)
    if event_id is None and positional:
        for p in positional:
            eid = _coerce_int(p, None)
            if eid is not None:
                event_id = eid
                break
    if event_id is None:
        raise ValueError("event_id is required")
    seg, recording_id = _extract_segment_ref(*args, **kwargs)
    if seg is None and positional:
        # second positional is usually the segment
        rest = [p for p in positional if _coerce_int(p, None) != int(event_id)]
        if rest:
            seg = rest[0]
        elif len(positional) >= 2:
            seg = positional[1]
    if seg is None:
        raise ValueError("segment (segment_path/recording_id) is required")
    if isinstance(seg, dict):
        seg_path = _segment_key(seg)
        seg_info = dict(seg)
    else:
        seg_path = str(seg)
        seg_info = {}
    key = seg_path.strip()
    if not key:
        raise ValueError("segment (segment_path/recording_id) is required")
    # Dedupe: same event + same segment key returns existing.
    with _lock:
        for nid in list(_EVENT_INDEX.get(int(event_id), [])):
            existing = _LINKS.get(nid)
            if existing is not None and str(existing.get("segment_key") or "") == key:
                return _to_public(existing)
    now = _now_iso()
    with _lock:
        nid = _next_id()
        link: Dict[str, Any] = {
            "id": nid,
            "link_id": nid,
            "event_id": int(event_id),
            "eventId": int(event_id),
            "segment": seg_path,
            "segment_path": seg_path,
            "segmentPath": seg_path,
            "path": seg_path,
            "file_path": seg_path,
            "recording_path": seg_path,
            "segment_key": key,
            "recording_id": recording_id,
            "recordingId": recording_id,
            "camera_id": flat.get("camera_id", flat.get("cameraId", seg_info.get("camera_id"))),
            "camera_name": flat.get("camera_name", flat.get("camera", seg_info.get("camera_name"))),
            "note": flat.get("note", flat.get("comment")),
            "created_at": now,
            "updated_at": now,
            "manual": True,
            "linked": True,
        }
        for k, v in flat.items():
            if k not in link:
                link[k] = v
        for k, v in seg_info.items():
            link.setdefault(k, v)
        _LINKS[nid] = link
        _EVENT_INDEX.setdefault(int(event_id), []).append(nid)
        _SEGMENT_INDEX.setdefault(key, []).append(nid)
        return _to_public(link)


def get_link(link_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if link_id is None:
        link_id = _first_present(flat, ("id", "link_id", "linkId"))
    if link_id is None and args:
        for a in args:
            if isinstance(a, (int, float, str)) and not isinstance(a, bool):
                try:
                    int(a)
                    link_id = a
                    break
                except (TypeError, ValueError):
                    continue
    nid = _coerce_int(link_id, None)
    if nid is None:
        return None
    with _lock:
        link = _LINKS.get(int(nid))
        return _to_public(link) if link is not None else None


def list_links(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """List manual links, optionally filtered by event_id and/or segment."""
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif isinstance(a, (int, float)) and not isinstance(a, bool) and "event_id" not in flat:
            flat["event_id"] = a
    flat.update(kwargs or {})
    event_id = _coerce_int(_first_present(flat, EVENT_ID_KEYS), None)
    seg_filter = None
    for key in SEGMENT_KEYS:
        if flat.get(key) is not None:
            seg_filter = str(flat.get(key))
            break
    limit = _coerce_int(_first_present(flat, ("limit", "n", "count", "size", "page_size")), None)
    offset = _coerce_int(_first_present(flat, ("offset", "skip", "start_index")), 0) or 0
    with _lock:
        if event_id is None and seg_filter is None:
            items = [_to_public(_LINKS[k]) for k in sorted(_LINKS.keys())]
        elif event_id is not None and seg_filter is None:
            ids = list(_EVENT_INDEX.get(int(event_id), []))
            items = [_to_public(_LINKS[k]) for k in sorted(ids) if k in _LINKS]
        elif seg_filter is not None and event_id is None:
            ids = list(_SEGMENT_INDEX.get(seg_filter.strip(), []))
            items = [_to_public(_LINKS[k]) for k in sorted(ids) if k in _LINKS]
        else:
            ids = list(_EVENT_INDEX.get(int(event_id), []))
            items = [_to_public(_LINKS[k]) for k in sorted(ids)
                     if k in _LINKS and str(_LINKS[k].get("segment_key") or "") == seg_filter.strip()]
    if offset:
        items = items[offset:]
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items


def unlink_event_segment(
    link_id: Any = None,
    *args: Any,
    **kwargs: Any,
) -> bool:
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    if link_id is None:
        link_id = _first_present(flat, ("id", "link_id", "linkId", "link"))
    if link_id is None and args:
        for a in args:
            if isinstance(a, (int, float)) and not isinstance(a, bool):
                link_id = a
                break
    # Support (event_id, segment) form without explicit link id.
    if link_id is None or (isinstance(link_id, dict)):
        event_id = _extract_event_id(*args, **kwargs)
        seg, _rid = _extract_segment_ref(*args, **kwargs)
        if event_id is not None and seg is not None:
            key = _segment_key(seg)
            with _lock:
                for nid in list(_EVENT_INDEX.get(int(event_id), [])):
                    existing = _LINKS.get(nid)
                    if existing is not None and str(existing.get("segment_key") or "") == key:
                        return unlink_event_segment(nid)
            return False
        return False
    nid = _coerce_int(link_id, None)
    if nid is None:
        # maybe "event_id:segment" composite string
        if isinstance(link_id, str) and ":" in link_id:
            left, _, right = link_id.partition(":")
            try:
                return unlink_event_segment(int(left.strip()), right.strip())
            except (TypeError, ValueError):
                return False
        return False
    with _lock:
        link = _LINKS.pop(int(nid), None)
        if link is None:
            return False
        try:
            eid = int(link.get("event_id"))
            lst = _EVENT_INDEX.get(eid, [])
            if int(nid) in lst:
                lst.remove(int(nid))
        except (TypeError, ValueError):
            pass
        try:
            skey = str(link.get("segment_key") or "")
            slst = _SEGMENT_INDEX.get(skey, [])
            if int(nid) in slst:
                slst.remove(int(nid))
        except (TypeError, ValueError):
            pass
        return True


def delete_links_for_event(event_id: Any = None, *args: Any, **kwargs: Any) -> int:
    eid = _extract_event_id(*(([event_id] if event_id is not None else []) + list(args)), **kwargs)
    if eid is None:
        return 0
    with _lock:
        ids = list(_EVENT_INDEX.get(int(eid), []))
    n = 0
    for nid in ids:
        if unlink_event_segment(nid):
            n += 1
    return n


def get_segments_for_event(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Manual links + auto-discovery merged (sync, filesystem only)."""
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    event_id = _extract_event_id(*args, **kwargs)
    # If an inline event dict was supplied, discover directly.
    inline: Optional[Dict[str, Any]] = None
    for a in args:
        if isinstance(a, dict) and any(k in a for k in ("camera_id", "camera_name", "created_at", "timestamp")):
            inline = dict(a)
            break
    if inline is None:
        for key in ("event", "source", "source_event"):
            if isinstance(flat.get(key), dict):
                inline = dict(flat.get(key))
                break
    discovered: List[Dict[str, Any]] = []
    if inline is not None:
        try:
            discovered = discover_segments_for_event(inline, **flat)
        except Exception:  # noqa: BLE001
            discovered = []
    elif event_id is not None:
        # No inline event: return manual links only in sync path.
        discovered = []
        with _lock:
            for nid in sorted(_EVENT_INDEX.get(int(event_id), [])):
                if nid in _LINKS:
                    discovered.append(_to_public(_LINKS[nid]))
        # Also try flat time/camera fallback discovery.
        if not discovered and any(flat.get(k) is not None for k in (
            "camera_id", "camera", "camera_name", "created_at", "timestamp", "time", "event_time",
        )):
            try:
                synth = {"id": event_id, "event_id": event_id}
                for k in ("camera_id", "cameraId", "camera", "camera_name", "created_at", "timestamp", "time"):
                    if flat.get(k) is not None:
                        synth[k] = flat.get(k)
                discovered = discover_segments_for_event(synth, **flat)
            except Exception:  # noqa: BLE001
                pass
    else:
        return list_links(*args, **kwargs)
    return discovered


def get_events_for_segment(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Reverse lookup: manual links matching a segment (sync)."""
    seg, _rid = _extract_segment_ref(*args, **kwargs)
    if seg is None and args:
        for a in args:
            if isinstance(a, (str, Path)):
                seg = str(a)
                break
    if seg is None:
        flat: Dict[str, Any] = {}
        for a in args:
            if isinstance(a, dict):
                flat.update(a)
        flat.update(kwargs or {})
        return list_links(**flat)
    key = _segment_key(seg)
    with _lock:
        ids = list(_SEGMENT_INDEX.get(key.strip(), []))
        return [_to_public(_LINKS[n]) for n in sorted(ids) if n in _LINKS]


def count_links(event_id: Any = None, *args: Any, **kwargs: Any) -> int:
    return len(list_links(*(([event_id] if event_id is not None else []) + list(args)), **kwargs))


def clear(*args: Any, **kwargs: Any) -> int:
    with _lock:
        n = len(_LINKS)
        _LINKS.clear()
        _EVENT_INDEX.clear()
        _SEGMENT_INDEX.clear()
        return n


# --- Async entrypoints (db-optional) ---------------------------------------

async def _fetch_event(db: Any, event_id: int) -> Optional[Any]:
    try:
        from app.models.entities import Event as _Event  # lazy import

        return await db.get(_Event, int(event_id))
    except Exception:  # noqa: BLE001
        return None


async def _resolve_camera_name(db: Any, camera_id: Any) -> Optional[str]:
    if camera_id is None or db is None:
        return None
    try:
        from app.models.entities import Camera as _Camera  # lazy import

        try:
            obj = await db.get(_Camera, int(camera_id))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            obj = None
        if obj is not None:
            return getattr(obj, "name", None)
    except Exception:  # noqa: BLE001
        pass
    return None


async def _recordings_for_window(
    db: Any,
    camera_id: Any,
    window_start: float,
    window_end: float,
) -> List[Dict[str, Any]]:
    if db is None:
        return []
    try:
        from sqlalchemy import select as _select  # lazy import

        from app.models.entities import Recording as _Recording  # lazy import

        try:
            stmt = _select(_Recording)
            if camera_id is not None:
                try:
                    stmt = stmt.where(_Recording.camera_id == int(camera_id))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    pass
            result = await db.execute(stmt)
            rows = list(result.scalars().all())
        except Exception:  # noqa: BLE001
            return []
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                started = getattr(r, "started_at", None)
                ended = getattr(r, "ended_at", None)
                sts = _parse_time(started)
                ets = _parse_time(ended)
            except Exception:  # noqa: BLE001
                sts, ets = None, None
            if sts is not None and ets is not None:
                if ets < window_start or sts > window_end:
                    continue
            elif sts is not None:
                if sts > window_end:
                    continue
            else:
                continue
            out.append({
                "path": getattr(r, "path", None),
                "segment_path": getattr(r, "path", None),
                "file_path": getattr(r, "path", None),
                "recording_id": getattr(r, "id", None),
                "recordingId": getattr(r, "id", None),
                "camera_id": getattr(r, "camera_id", camera_id),
                "start": sts,
                "start_time": sts,
                "end": ets,
                "end_time": ets,
                "size_bytes": getattr(r, "size_bytes", None),
                "db": True,
            })
        return out
    except Exception:  # noqa: BLE001
        return []


async def get_segments_for_event_async(
    *args: Any,
    db: Any = None,
    session: Any = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    sess = db if db is not None else session
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    event_id = _extract_event_id(*args, **kwargs)
    inline: Optional[Dict[str, Any]] = None
    for a in args:
        if isinstance(a, dict) and any(k in a for k in ("camera_id", "camera_name", "created_at", "timestamp")):
            inline = dict(a)
            break
    if inline is None:
        for key in ("event", "source", "source_event"):
            if isinstance(flat.get(key), dict):
                inline = dict(flat.get(key))
                break
    if inline is None and event_id is not None and sess is not None:
        try:
            row = await _fetch_event(sess, int(event_id))
        except Exception:  # noqa: BLE001
            row = None
        if row is not None:
            inline = _event_to_dict(row)
            # Explicit caller hints (camera/timestamp) override the DB row so
            # callers can discover against a tmp recording dir without the DB
            # row's own camera/time shadowing the request.
            for k in (
                "camera_id", "cameraId", "camera", "camera_name", "cameraName",
                "cam", "created_at", "createdAt", "timestamp", "time", "event_time",
            ):
                if flat.get(k) is not None:
                    inline[k] = flat.get(k)
    if inline is None and event_id is not None:
        # fall back to manual-only sync path
        return get_segments_for_event(event_id, **flat)
    if inline is None:
        return []
    inline = dict(inline)
    if inline.get("camera_name") is None and inline.get("camera_id") is not None and sess is not None:
        try:
            resolved = await _resolve_camera_name(sess, inline.get("camera_id"))
        except Exception:  # noqa: BLE001
            resolved = None
        if resolved:
            inline.setdefault("camera_name", resolved)
            inline.setdefault("camera", resolved)
    discovered = discover_segments_for_event(inline, **flat)
    # Merge DB recordings.
    try:
        ts = _event_time(_event_to_dict(inline))
        before, after = _resolve_windows(flat)
        if ts is not None:
            cam_id, _cn = _event_camera(_event_to_dict(inline))
            db_rows = await _recordings_for_window(sess, cam_id, ts - before, ts + after)
            seen = {str(d.get("path")) for d in discovered}
            for r in db_rows:
                if str(r.get("path")) not in seen:
                    r = dict(r)
                    r.setdefault("event_id", inline.get("id", inline.get("event_id", event_id)))
                    discovered.append(r)
                    seen.add(str(r.get("path")))
    except Exception:  # noqa: BLE001
        pass
    discovered.sort(key=lambda d: (d.get("start") or 0))
    return discovered


async def find_segments_for_event_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return await get_segments_for_event_async(*args, db=db, session=session, **kwargs)


async def get_events_for_segment_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    sess = db if db is not None else session
    manual = get_events_for_segment(*args, **kwargs)
    if sess is None:
        return manual
    # Best-effort DB reverse lookup: events overlapping the segment window.
    try:
        seg, _rid = _extract_segment_ref(*args, **kwargs)
        flat: Dict[str, Any] = {}
        for a in args:
            if isinstance(a, dict):
                flat.update(a)
        flat.update(kwargs or {})
        seg_info: Dict[str, Any] = {}
        if isinstance(seg, dict):
            seg_info = dict(seg)
        elif seg is not None:
            seg_info = _file_info(seg)
        seg_start = _parse_time(seg_info.get("start", seg_info.get("start_time")))
        seg_end = _parse_time(seg_info.get("end", seg_info.get("end_time")))
        if seg_start is None:
            return manual
        if seg_end is None:
            seg_end = seg_start + DEFAULT_SEGMENT_DURATION
        before, after = _resolve_windows(flat)
        lo, hi = seg_start - after, seg_end + before
        from sqlalchemy import select as _select  # lazy import

        from app.models.entities import Event as _Event  # lazy import

        result = await sess.execute(_select(_Event).order_by(_Event.id.desc()).limit(2000))
        rows = list(result.scalars().all())
        out = list(manual)
        seen_events = {m.get("event_id") for m in manual}
        for r in rows:
            try:
                ets = _parse_time(getattr(r, "created_at", None))
            except Exception:  # noqa: BLE001
                ets = None
            if ets is None:
                continue
            if lo <= ets <= hi:
                if getattr(r, "id", None) in seen_events:
                    continue
                out.append({
                    "id": getattr(r, "id", None),
                    "event_id": getattr(r, "id", None),
                    "camera_id": getattr(r, "camera_id", None),
                    "kind": getattr(r, "kind", None),
                    "label": getattr(r, "label", None),
                    "created_at": getattr(r, "created_at", None).isoformat() if getattr(r, "created_at", None) else None,
                })
        return out
    except Exception:  # noqa: BLE001
        return manual


async def link_event_to_segment_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    kwargs.pop("db", None)
    kwargs.pop("session", None)
    return link_event_to_segment(*args, **kwargs)


async def unlink_event_segment_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> bool:
    return unlink_event_segment(*args, **kwargs)


async def list_links_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_links(*args, **kwargs)


class EventSegmentsService:
    """Object-oriented facade over the event-segment linker."""

    def link(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return link_event_to_segment(*args, **kwargs)

    def unlink(self, *args: Any, **kwargs: Any) -> bool:
        return unlink_event_segment(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_links(*args, **kwargs)

    def discover(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return discover_segments_for_event(*args, **kwargs)

    def find(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return discover_segments_for_event(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return get_segments_for_event(*args, **kwargs)

    def segments(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return get_segments_for_event(*args, **kwargs)

    def events(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return get_events_for_segment(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    def count(self, *args: Any, **kwargs: Any) -> int:
        return count_links(*args, **kwargs)

    # aliases
    link_event = link
    link_segment = link
    add_link = link
    create_link = link
    attach = link
    attach_segment = link
    list_links = list
    get_links = list
    list_segments = get
    get_segments = get
    segments_for_event = get
    discover_segments = discover
    find_segments = find
    events_for_segment = events
    get_events = events
    delete = unlink
    remove = unlink
    detach = unlink
    remove_link = unlink
    delete_link = unlink


event_segments_service = EventSegmentsService()
event_segment_service = event_segments_service
segments_service = event_segments_service
service = event_segments_service
manager = event_segments_service
store = event_segments_service
linker = event_segments_service
event_linker = event_segments_service


# Compatibility aliases (function level).
link_event = link_event_to_segment
link_segment = link_event_to_segment
add_link = link_event_to_segment
create_link = link_event_to_segment
attach_segment = link_event_to_segment
attach = link_event_to_segment
link = link_event_to_segment
create_event_segment_link = link_event_to_segment
add_event_segment = link_event_to_segment

get_segments = get_segments_for_event
list_segments = get_segments_for_event
segments_for_event = get_segments_for_event
segments_for_events = get_segments_for_event
find_segments_for_event = discover_segments_for_event
get_event_segments = get_segments_for_event
list_event_segments = get_segments_for_event
event_segments = get_segments_for_event
discover = discover_segments_for_event
discover_segments = discover_segments_for_event
find = discover_segments_for_event

get_events = get_events_for_segment
list_events = get_events_for_segment
events_for_segment = get_events_for_segment
events_for_segments = get_events_for_segment
find_events_for_segment = get_events_for_segment
segment_events = get_events_for_segment

get_links = list_links
list_event_segment_links = list_links
event_segment_links = list_links

unlink = unlink_event_segment
detach = unlink_event_segment
remove_link = unlink_event_segment
delete_link = unlink_event_segment
remove_event_segment = unlink_event_segment
delete_event_segment = unlink_event_segment
detach_segment = unlink_event_segment

__all__ = [
    "find_segments",
    "discover_segments_for_event",
    "link_event_to_segment",
    "get_link",
    "list_links",
    "unlink_event_segment",
    "delete_links_for_event",
    "get_segments_for_event",
    "get_events_for_segment",
    "count_links",
    "clear",
    "get_segments_for_event_async",
    "find_segments_for_event_async",
    "get_events_for_segment_async",
    "link_event_to_segment_async",
    "unlink_event_segment_async",
    "list_links_async",
    "EventSegmentsService",
    "event_segments_service",
]
