"""Bookmark EDL (Edit Decision List) export service (CMX3600).

Reads bookmarks from the in-memory ``app.services.bookmarks`` store and
serialises them to CMX3600-style EDL text for interchange with NLEs
(Premiere / DaVinci / Final Cut via EDL import).

All helpers are sync and dependency-free so they work in tests without a DB.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

DEFAULT_FPS = 30.0
DEFAULT_CLIP_DURATION = 2.0
DEFAULT_TITLE = "VigilCam Bookmarks"
DEFAULT_TIMELINE_START = "01:00:00:00"
TIMELINE_START_SECONDS = 3600.0

# Compatibility aliases.
DEFAULT_EVENT_DURATION = DEFAULT_CLIP_DURATION
DEFAULT_DURATION = DEFAULT_CLIP_DURATION
EDL_TITLE = DEFAULT_TITLE
TITLE = DEFAULT_TITLE
FPS = DEFAULT_FPS
CLIP_DURATION = DEFAULT_CLIP_DURATION


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_fps(value: Any, default: float = DEFAULT_FPS) -> float:
    for candidate in (value,):
        if candidate is None:
            continue
        try:
            v = float(candidate)
        except (TypeError, ValueError):
            continue
        if v >= 1.0 and v <= 120.0:
            return v
    try:
        d = float(default)
        if d >= 1.0 and d <= 120.0:
            return d
    except (TypeError, ValueError):
        pass
    return DEFAULT_FPS


def _coerce_duration(value: Any, default: float = DEFAULT_CLIP_DURATION) -> float:
    v = _coerce_float(value, default)
    if v <= 0:
        return default
    return v


def seconds_to_timecode(seconds: Any, fps: Any = DEFAULT_FPS) -> str:
    """Convert seconds (float) to ``HH:MM:SS:FF`` at ``fps``."""
    fps_val = _coerce_fps(fps, DEFAULT_FPS)
    fps_int = int(round(fps_val))
    if fps_int <= 0:
        fps_int = int(round(DEFAULT_FPS))
    try:
        secs = float(seconds)
    except (TypeError, ValueError):
        secs = 0.0
    if secs < 0:
        secs = 0.0
    total_frames = int(round(secs * float(fps_int)))
    frames_per_hour = fps_int * 3600
    frames_per_min = fps_int * 60
    hours = total_frames // frames_per_hour
    remainder = total_frames % frames_per_hour
    minutes = remainder // frames_per_min
    remainder = remainder % frames_per_min
    secs_part = remainder // fps_int
    frames = remainder % fps_int
    return f"{int(hours):02d}:{int(minutes):02d}:{int(secs_part):02d}:{int(frames):02d}"


# Aliases for timecode helper.
to_timecode = seconds_to_timecode
format_timecode = seconds_to_timecode
timecode = seconds_to_timecode
timecode_for = seconds_to_timecode
format_seconds = seconds_to_timecode
seconds_to_tc = seconds_to_timecode
sec_to_timecode = seconds_to_timecode


def _sanitize_token(value: Any, fallback: str = "AX") -> str:
    s = str(value).strip() if value is not None else ""
    s = re.sub(r"[^A-Za-z0-9_]", "", s.upper())
    if not s:
        return fallback
    return s[:8]


def _reel_for(bm: Dict[str, Any]) -> str:
    cam = bm.get("camera_id", bm.get("camera"))
    rec = bm.get("recording_id", bm.get("recording"))
    if cam is not None and str(cam).strip() != "":
        return _sanitize_token(f"CAM{cam}", "AX")
    if rec is not None and str(rec).strip() != "":
        return _sanitize_token(f"REC{rec}", "AX")
    label = bm.get("label", bm.get("title", ""))
    if label:
        tok = _sanitize_token(str(label)[:8], "")
        if tok:
            return tok
    return "AX"


def _clip_name_for(bm: Dict[str, Any]) -> str:
    for key in ("clip_name", "clipName", "file", "filename", "source", "recording_name"):
        val = bm.get(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()[:64]
    label = str(bm.get("label", bm.get("title", bm.get("name", "")))).strip()
    bid = str(bm.get("id", bm.get("bookmark_id", ""))).strip()
    rec = bm.get("recording_id", bm.get("recording"))
    cam = bm.get("camera_id", bm.get("camera"))
    base = label or f"bookmark-{bid}" if bid else (label or "bookmark")
    suffix_parts = []
    if rec is not None:
        suffix_parts.append(f"rec{rec}")
    if cam is not None:
        suffix_parts.append(f"cam{cam}")
    if suffix_parts:
        base = f"{base}_{'_'.join(suffix_parts)}" if base else "_".join(suffix_parts)
    base = re.sub(r"\s+", "_", base.strip())[:64]
    return base or "bookmark"


def _label_for(bm: Dict[str, Any]) -> str:
    for key in ("label", "title", "name", "note", "notes", "description", "comment"):
        val = bm.get(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return ""


def normalize_bookmark(entry: Any) -> Dict[str, Any]:
    if entry is None:
        return {}
    if hasattr(entry, "model_dump"):
        try:
            return dict(entry.model_dump(mode="json"))  # type: ignore[attr-defined]
        except Exception:
            try:
                return dict(entry.model_dump())  # type: ignore[attr-defined]
            except Exception:
                pass
    if isinstance(entry, dict):
        return dict(entry)
    try:
        return dict(entry)
    except Exception:
        return {"value": entry}


normalize_entry = normalize_bookmark
normalize = normalize_bookmark
sanitize_bookmark = normalize_bookmark
sanitize_entry = normalize_bookmark
sanitize = normalize_bookmark


def _offset_of(bm: Dict[str, Any]) -> float:
    for key in ("offset_seconds", "offset", "timestamp", "time", "position", "position_seconds", "seek_seconds", "seek", "at", "second", "seconds", "ts", "t"):
        if bm.get(key) is not None:
            v = _coerce_float(bm.get(key), 0.0)
            return max(0.0, v)
    return 0.0


def fetch_bookmarks(
    entries: Any = None,
    recording_id: Any = None,
    camera_id: Any = None,
    **filters: Any,
) -> List[Dict[str, Any]]:
    """Return normalized bookmarks, optionally filtered.

    If ``entries`` is supplied it is filtered in-memory.  Otherwise bookmarks
    are read from ``app.services.bookmarks``.
    """
    # Alias handling.
    if recording_id is None:
        for k in ("recording", "rec_id", "recId", "video_id", "media_id"):
            if filters.get(k) is not None:
                recording_id = filters.pop(k)
                break
    if camera_id is None:
        for k in ("camera", "cam", "cam_id", "camId", "source_id"):
            if filters.get(k) is not None:
                camera_id = filters.pop(k)
                break
    # Explicit entries list wins.
    data: List[Dict[str, Any]] = []
    if entries is not None and not isinstance(entries, (str, bytes)):
        try:
            if isinstance(entries, dict):
                data = [normalize_bookmark(entries)]
            else:
                data = [normalize_bookmark(e) for e in list(entries)]
        except Exception:
            data = []
        else:
            return _apply_bookmark_filters(data, recording_id=recording_id, camera_id=camera_id, **filters)
    # Read from store.
    try:
        from app.services import bookmarks as _bm_mod
    except Exception:
        _bm_mod = None  # type: ignore[assignment]
    raw: List[Dict[str, Any]] = []
    if _bm_mod is not None:
        try:
            fetched = _bm_mod.list_bookmarks(
                recording_id=recording_id,
                camera_id=camera_id,
            )
            raw = [normalize_bookmark(b) for b in (fetched or [])]
        except Exception:
            try:
                all_items = list(_bm_mod.BOOKMARKS.values())
                raw = [normalize_bookmark(b) for b in all_items]
            except Exception:
                raw = []
    return _apply_bookmark_filters(raw, recording_id=recording_id, camera_id=camera_id, **filters)


def _apply_bookmark_filters(
    items: List[Dict[str, Any]],
    recording_id: Any = None,
    camera_id: Any = None,
    **filters: Any,
) -> List[Dict[str, Any]]:
    # Drop export-format keys from matching.
    for k in ("format", "fmt", "output_format", "as_format", "fps", "frame_rate", "framerate", "frameRate", "rate", "title", "name", "edl_title", "duration", "clip_duration", "clipDuration", "event_duration", "eventDuration", "length", "reel", "reel_name", "limit", "skip", "offset", "n", "count", "size", "max", "sort", "order"):
        filters.pop(k, None)
    # Sort key (bookmarks carry aliases; ignore unknown filters for tolerance).
    out: List[Dict[str, Any]] = []
    for bm in items:
        if recording_id is not None and str(bm.get("recording_id", bm.get("recording"))) != str(recording_id):
            continue
        if camera_id is not None and str(bm.get("camera_id", bm.get("camera"))) != str(camera_id):
            continue
        ok = True
        for key, expected in filters.items():
            if expected is None:
                continue
            if key in ("id", "bookmark_id", "bookmarkId"):
                if str(bm.get("id", bm.get("bookmark_id"))) != str(expected):
                    ok = False
                    break
                continue
            if bm.get(key) is None or str(bm.get(key)) != str(expected):
                ok = False
                break
        if ok:
            out.append(bm)
    # Deterministic order: by offset then id.
    try:
        out.sort(key=lambda b: (_offset_of(b), str(b.get("id", ""))))
    except Exception:
        pass
    # limit/skip handled by caller; apply here if present in original call path.
    return out


get_bookmarks = fetch_bookmarks
list_bookmarks_for_export = fetch_bookmarks
collect_bookmarks = fetch_bookmarks
query_bookmarks = fetch_bookmarks
bookmarks_for_export = fetch_bookmarks
fetch_entries = fetch_bookmarks
get_entries = fetch_bookmarks


def build_edl(
    bookmarks: Any = None,
    entries: Any = None,
    items: Any = None,
    title: Any = None,
    fps: Any = None,
    duration: Any = None,
    **kwargs: Any,
) -> str:
    """Build a CMX3600 EDL string from bookmarks."""
    raw_list: Any = bookmarks
    if raw_list is None:
        raw_list = entries if entries is not None else items
    if raw_list is None:
        # kwargs may carry filters; fetch from store.
        raw_list = fetch_bookmarks(**kwargs)
    elif isinstance(raw_list, dict):
        raw_list = [raw_list]
    else:
        try:
            raw_list = list(raw_list)
        except Exception:
            raw_list = [raw_list]
    # Resolve title.
    title_val = title
    if title_val is None:
        for k in ("edl_title", "edlTitle", "name", "reel_title", "reelTitle", "export_title", "exportTitle"):
            if kwargs.get(k) is not None:
                title_val = kwargs.get(k)
                break
    if title_val is None:
        title_val = DEFAULT_TITLE
    title_str = str(title_val).strip() or DEFAULT_TITLE
    # Resolve fps.
    fps_val = fps
    if fps_val is None:
        for k in ("frame_rate", "frameRate", "framerate", "rate", "output_fps", "out_fps"):
            if kwargs.get(k) is not None:
                fps_val = kwargs.get(k)
                break
    fps_num = _coerce_fps(fps_val, DEFAULT_FPS)
    fps_int = int(round(fps_num))
    # Resolve clip duration.
    dur_val = duration
    if dur_val is None:
        for k in ("clip_duration", "clipDuration", "event_duration", "eventDuration", "length", "clip_length", "clipLength", "event_length", "eventLength"):
            if kwargs.get(k) is not None:
                dur_val = kwargs.get(k)
                break
    dur_num = _coerce_duration(dur_val, DEFAULT_CLIP_DURATION)
    # Optional recording/camera filter when a plain list was passed with filters.
    rec_filter = kwargs.get("recording_id", kwargs.get("recording"))
    cam_filter = kwargs.get("camera_id", kwargs.get("camera"))
    normed = [normalize_bookmark(b) for b in (raw_list or [])]
    if rec_filter is not None or cam_filter is not None:
        normed = _apply_bookmark_filters(normed, recording_id=rec_filter, camera_id=cam_filter)
    try:
        normed.sort(key=lambda b: (_offset_of(b), str(b.get("id", ""))))
    except Exception:
        pass
    lines: List[str] = [f"TITLE: {title_str}", "FCM: NON-DROP FRAME", ""]
    for idx, bm in enumerate(normed):
        event_no = f"{idx + 1:03d}"
        offset = _offset_of(bm)
        src_in = seconds_to_timecode(offset, fps_int)
        src_out = seconds_to_timecode(offset + dur_num, fps_int)
        rec_in_sec = TIMELINE_START_SECONDS + idx * dur_num
        rec_out_sec = rec_in_sec + dur_num
        rec_in = seconds_to_timecode(rec_in_sec, fps_int)
        rec_out = seconds_to_timecode(rec_out_sec, fps_int)
        reel = _reel_for(bm)
        if len(reel) > 8:
            reel = reel[:8]
        label = _label_for(bm)
        clip_name = _clip_name_for(bm)
        bid = str(bm.get("id", bm.get("bookmark_id", "")))
        lines.append(f"{event_no}  {reel:<8} V     C        {src_in} {src_out} {rec_in} {rec_out}")
        lines.append(f"* FROM CLIP NAME: {clip_name}")
        if label:
            # Avoid newlines in comments.
            safe_label = re.sub(r"\s+", " ", label).strip()[:200]
            lines.append(f"* COMMENT: {safe_label}")
        # Traceability comment (recording/camera/bookmark ids).
        trace_bits = []
        if bid:
            trace_bits.append(f"bookmark={bid}")
        rec = bm.get("recording_id", bm.get("recording"))
        if rec is not None:
            trace_bits.append(f"recording={rec}")
        cam = bm.get("camera_id", bm.get("camera"))
        if cam is not None:
            trace_bits.append(f"camera={cam}")
        if trace_bits:
            lines.append(f"* SOURCE: {' '.join(trace_bits)}")
    lines.append("")
    return "\n".join(lines)


# Export aliases.
to_edl = build_edl
generate_edl = build_edl
create_edl = build_edl
render_edl = build_edl
format_edl = build_edl
export_edl = build_edl
bookmarks_to_edl = build_edl
entries_to_edl = build_edl
export_entries_as_edl = build_edl
export_bookmarks_as_edl = build_edl
build_export = build_edl
generate_export = build_edl
create_export = build_edl


def export_to_edl(bookmarks: Any = None, **kwargs: Any) -> str:
    if bookmarks is None and kwargs:
        if any(k in kwargs for k in ("recording_id", "recording", "camera_id", "camera")):
            return build_edl(fetch_bookmarks(**kwargs), **kwargs)
    return build_edl(bookmarks, **kwargs)


export_as_edl = export_to_edl
export_as_text = export_to_edl


def write_edl(path: Any, bookmarks: Any = None, **kwargs: Any) -> str:
    if bookmarks is None:
        text = build_edl(fetch_bookmarks(**kwargs), **kwargs)
    else:
        text = build_edl(bookmarks, **kwargs)
    with open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return str(path)


save_edl = write_edl
dump_edl = write_edl
write_export = write_edl
save_export = write_edl


class EdlExportService:
    """Convenience wrapper around the module-level helpers."""

    def __init__(self, fps: Any = None, title: Any = None, duration: Any = None) -> None:
        self.fps = _coerce_fps(fps, DEFAULT_FPS) if fps is not None else DEFAULT_FPS
        self.title = str(title) if title is not None else DEFAULT_TITLE
        self.duration = _coerce_duration(duration, DEFAULT_CLIP_DURATION) if duration is not None else DEFAULT_CLIP_DURATION

    def fetch(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        if args and isinstance(args[0], list):
            return fetch_bookmarks(args[0], **kwargs)
        if args and isinstance(args[0], dict):
            return fetch_bookmarks([args[0]], **kwargs)
        return fetch_bookmarks(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.fetch(*args, **kwargs)

    def get_bookmarks(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.fetch(*args, **kwargs)

    def collect(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.fetch(*args, **kwargs)

    def to_edl(self, bookmarks: Any = None, **kwargs: Any) -> str:
        kw = {"fps": self.fps, "title": self.title, "duration": self.duration}
        kw.update(kwargs)
        if bookmarks is None:
            return build_edl(self.fetch(**{k: v for k, v in kwargs.items() if k not in ("fps", "title", "duration")}), **kw)
        return build_edl(bookmarks, **kw)

    def build(self, *args: Any, **kwargs: Any) -> str:
        if args:
            return self.to_edl(args[0], **kwargs)
        return self.to_edl(**kwargs)

    def export(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)

    def generate(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)

    def render(self, *args: Any, **kwargs: Any) -> str:
        return self.build(*args, **kwargs)


# Class aliases.
EDLExportService = EdlExportService
EdlExporter = EdlExportService
EDLExporter = EdlExportService
ExportService = EdlExportService
BookmarkEdlService = EdlExportService
BookmarkEDLService = EdlExportService
EdlService = EdlExportService

edl_export_service = EdlExportService()
export_service = edl_export_service
service = edl_export_service
exporter = edl_export_service
manager = edl_export_service
default_service = edl_export_service


__all__ = [
    "DEFAULT_FPS",
    "DEFAULT_CLIP_DURATION",
    "DEFAULT_TITLE",
    "seconds_to_timecode",
    "normalize_bookmark",
    "fetch_bookmarks",
    "build_edl",
    "export_to_edl",
    "write_edl",
    "EdlExportService",
    "edl_export_service",
]
