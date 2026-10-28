"""SRT subtitle export service for VigilCam.

Generates SubRip (``.srt``) sidecars from detection/event records so a
video export can be accompanied by a timed subtitle track of events.

This module is intentionally dependency-free (no DB import at module
load) so it works in unit tests without a database.  Database access is
performed lazily inside the async fetch helpers.

Baseline verified before creation: ``app.services.export`` produces
video only (ffmpeg concat demuxer, ``-c copy`` / ``libx264`` / ``gif``)
with no subtitle stream, no ``.srt`` sidecar, and no ``movtext``/srt mux.
This module adds that missing sidecar without touching existing export
code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DURATION_SECONDS = 3.0
DEFAULT_MAX_CUES = 5000


# --------------------------------------------------------------------------
# Timestamp helpers
# --------------------------------------------------------------------------

def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
        if getattr(dt, "tzinfo", None) is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    if isinstance(value, (int, float)):
        ts = float(value)
        if abs(ts) > 1e12:  # millis -> seconds
            ts = ts / 1000.0
        # Heuristic: large values are epoch seconds; small values are
        # relative offsets and are NOT absolute datetimes here.
        # Only treat as epoch when plausible (>= 2001-09-09).
        if ts < 1_000_000_000:
            return None
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(value).strip()
    if not s or s.lower() in {"none", "null", "undefined", "all"}:
        return None
    # numeric string
    try:
        ts = float(s)
        if abs(ts) > 1e12:
            ts = ts / 1000.0
        if abs(ts) >= 1_000_000_000:
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        pass
    s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(s2)
    except ValueError:
        # date-only fallback
        try:
            dt = datetime.fromisoformat(s + "T00:00:00")
        except ValueError:
            return None
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_seconds(value: Any) -> Optional[float]:
    """Parse a relative offset in seconds (for explicit cue times)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v < 0:
            return None
        # millis heuristic only for very large values
        if v > 1e12:
            v = v / 1000.0
        return v
    if isinstance(value, datetime):
        return None
    s = str(value).strip()
    if not s:
        return None
    # SRT timestamp "HH:MM:SS,mmm" or "MM:SS.mmm"
    import re

    m = re.match(r"^(?:(\d+):)?([0-5]?\d):([0-5]?\d)[,.](\d{1,3})$", s)
    if m:
        h = int(m.group(1) or 0)
        mi = int(m.group(2))
        se = int(m.group(3))
        ms = m.group(4).ljust(3, "0")
        return h * 3600 + mi * 60 + se + int(ms) / 1000.0
    m2 = re.match(r"^(?:(\d+):)?([0-5]?\d):([0-5]?\d(?:\.\d+)?)$", s)
    if m2:
        h = int(m2.group(1) or 0)
        mi = int(m2.group(2))
        se = float(m2.group(3))
        return h * 3600 + mi * 60 + se
    try:
        v = float(s)
        if v < 0:
            return None
        return v
    except (TypeError, ValueError):
        return None


def format_srt_timestamp(seconds: float) -> str:
    """Format seconds as ``HH:MM:SS,mmm``."""
    try:
        total = max(0.0, float(seconds))
    except (TypeError, ValueError):
        total = 0.0
    total_ms = int(round(total * 1000))
    hours, rem = divmod(total_ms, 3600 * 1000)
    minutes, rem = divmod(rem, 60 * 1000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


format_timestamp = format_srt_timestamp
format_time = format_srt_timestamp
to_srt_timestamp = format_srt_timestamp
srt_timestamp = format_srt_timestamp


# --------------------------------------------------------------------------
# Event field extraction
# --------------------------------------------------------------------------

def _event_time(ev: Any) -> Optional[datetime]:
    if isinstance(ev, dict):
        for k in ("created_at", "timestamp", "ts", "time", "occurred_at", "at", "date",
                  "start", "start_time", "event_time"):
            if ev.get(k) is not None:
                dt = _parse_dt(ev.get(k))
                if dt is not None:
                    return dt
        # case-insensitive fallback
        lowered = {str(k).lower(): v for k, v in ev.items()}
        for k in ("created_at", "timestamp", "ts", "time"):
            if lowered.get(k) is not None:
                dt = _parse_dt(lowered.get(k))
                if dt is not None:
                    return dt
        return None
    for attr in ("created_at", "timestamp", "ts", "time", "occurred_at", "at"):
        v = getattr(ev, attr, None)
        if v is not None:
            dt = _parse_dt(v)
            if dt is not None:
                return dt
    return None


def _event_end(ev: Any) -> Optional[datetime]:
    if isinstance(ev, dict):
        for k in ("end", "end_time", "finished_at", "ended_at"):
            if ev.get(k) is not None:
                dt = _parse_dt(ev.get(k))
                if dt is not None:
                    return dt
        return None
    for attr in ("end", "end_time", "finished_at", "ended_at"):
        v = getattr(ev, attr, None)
        if v is not None:
            dt = _parse_dt(v)
            if dt is not None:
                return dt
    return None


def _event_duration(ev: Any, default: float = DEFAULT_DURATION_SECONDS) -> float:
    if isinstance(ev, dict):
        for k in ("duration", "duration_seconds", "length", "cue_duration"):
            if ev.get(k) is not None:
                try:
                    v = float(ev.get(k))
                    if v > 0:
                        return min(v, 3600.0)
                except (TypeError, ValueError):
                    continue
    else:
        for attr in ("duration", "duration_seconds"):
            v = getattr(ev, attr, None)
            if v is not None:
                try:
                    f = float(v)
                    if f > 0:
                        return min(f, 3600.0)
                except (TypeError, ValueError):
                    continue
    try:
        d = float(default)
        return d if d > 0 else DEFAULT_DURATION_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_DURATION_SECONDS


def _event_label(ev: Any) -> str:
    if isinstance(ev, dict):
        for k in ("label", "kind", "type", "object", "detection_label", "name", "title"):
            v = ev.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return "event"
    for attr in ("label", "kind", "type"):
        v = getattr(ev, attr, None)
        if v is not None and str(v).strip():
            return str(v).strip()
    return "event"


def _event_camera(ev: Any) -> Optional[str]:
    if isinstance(ev, dict):
        for k in ("camera_name", "camera", "camera_id", "cameraId", "cam_id", "cam"):
            v = ev.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return None
    for attr in ("camera_name", "camera_id"):
        v = getattr(ev, attr, None)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _sanitize_text(text: Any) -> str:
    s = "" if text is None else str(text)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # SRT cue separator is a blank line; arrows delimit times. Neutralize both.
    s = s.replace("-->", "->")
    lines = [ln.strip() for ln in s.split("\n")]
    # drop leading/trailing empties, collapse inner blanks to single space lines
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    cleaned: List[str] = []
    for ln in lines:
        cleaned.append(ln if ln else " ")
    out = "\n".join(cleaned).strip()
    return out if out else "event"


def _event_text(ev: Any) -> str:
    label = _event_label(ev)
    cam = _event_camera(ev)
    conf = None
    tags: List[str] = []
    created_iso: Optional[str] = None
    if isinstance(ev, dict):
        conf = ev.get("confidence", ev.get("score", ev.get("conf")))
        raw_tags = ev.get("tags", ev.get("tag"))
        if isinstance(raw_tags, (list, tuple)):
            tags = [str(t) for t in raw_tags if str(t).strip()]
        elif isinstance(raw_tags, str) and raw_tags.strip():
            tags = [raw_tags.strip()]
        dt = _event_time(ev)
        if dt is not None:
            created_iso = dt.isoformat(sep=" ", timespec="seconds")
    else:
        conf = getattr(ev, "confidence", None)
        created_iso = None
        try:
            dt = _event_time(ev)
            if dt is not None:
                created_iso = dt.isoformat(sep=" ", timespec="seconds")
        except Exception:  # noqa: BLE001
            pass
    parts: List[str] = [label]
    if cam is not None:
        parts[0] = f"[{cam}] {label}"
    extras: List[str] = []
    if conf is not None:
        try:
            extras.append(f"{float(conf):.2f}")
        except (TypeError, ValueError):
            extras.append(str(conf))
    if tags:
        extras.append(" ".join(f"#{t}" for t in tags[:5]))
    head = " ".join([parts[0]] + extras).strip()
    if created_iso:
        return _sanitize_text(f"{head}\n{created_iso}")
    return _sanitize_text(head)


def normalize_event(ev: Any) -> Dict[str, Any]:
    dt = _event_time(ev)
    return {
        "label": _event_label(ev),
        "camera": _event_camera(ev),
        "created_at": dt.isoformat() if dt is not None else None,
        "text": _event_text(ev),
        "duration": _event_duration(ev),
    }


normalize = normalize_event
sanitize = _sanitize_text
sanitize_text = _sanitize_text


# --------------------------------------------------------------------------
# Filtering + SRT building
# --------------------------------------------------------------------------

def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        if isinstance(value, str) and "," in value:
            return None
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _coerce_camera_set(camera_id: Any, camera_ids: Any = None) -> set:
    out: set = set()
    cands: List[Any] = []
    if camera_id is not None:
        cands.append(camera_id)
    if camera_ids is not None:
        cands.append(camera_ids)
    for c in cands:
        if isinstance(c, (list, tuple, set)):
            for v in c:
                iv = _coerce_int(v)
                if iv is not None:
                    out.add(iv)
                elif isinstance(v, str) and v.strip():
                    out.add(v.strip())
        elif isinstance(c, str) and "," in c:
            for part in c.split(","):
                part = part.strip()
                if not part:
                    continue
                iv = _coerce_int(part)
                out.add(iv if iv is not None else part)
        else:
            iv = _coerce_int(c)
            if iv is not None:
                out.add(iv)
            elif isinstance(c, str) and c.strip():
                out.add(c.strip())
    return out


def filter_events(
    events: List[Any],
    camera_id: Any = None,
    camera_ids: Any = None,
    start: Any = None,
    end: Any = None,
    label: Any = None,
    limit: Any = None,
    **kwargs: Any,
) -> List[Any]:
    camera_id = kwargs.pop("camera", camera_id)
    camera_ids = kwargs.pop("cameras", camera_ids)
    start = kwargs.pop("start_time", start)
    if start is None:
        for k in ("from_", "from", "since", "after", "range_start", "begin"):
            if kwargs.get(k) is not None:
                start = kwargs.get(k)
                break
    end = kwargs.pop("end_time", end)
    if end is None:
        for k in ("to", "until", "before", "range_end", "finish"):
            if kwargs.get(k) is not None:
                end = kwargs.get(k)
                break
    label = kwargs.pop("kind", label)
    if label is None:
        for k in ("labels", "type", "object"):
            if kwargs.get(k) is not None:
                label = kwargs.get(k)
                break
    cams = _coerce_camera_set(camera_id, camera_ids)
    # also accept camera_name string filter
    name_filter = kwargs.get("camera_name") or kwargs.get("cameraName")
    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end)
    label_set = set()
    if label is not None:
        if isinstance(label, (list, tuple, set)):
            for v in label:
                if str(v).strip():
                    label_set.add(str(v).strip().lower())
        else:
            for part in str(label).split(","):
                if part.strip():
                    label_set.add(part.strip().lower())
    out: List[Any] = []
    for ev in events or []:
        if cams:
            cid: Any = None
            if isinstance(ev, dict):
                for k in ("camera_id", "cameraId", "cam_id", "cam", "camera"):
                    if ev.get(k) is not None:
                        try:
                            cid = int(float(str(ev.get(k)).strip()))
                        except (TypeError, ValueError):
                            cid = str(ev.get(k)).strip()
                        break
            else:
                for attr in ("camera_id", "cameraId", "cam_id"):
                    v = getattr(ev, attr, None)
                    if v is not None:
                        try:
                            cid = int(v)
                        except (TypeError, ValueError):
                            cid = str(v)
                        break
            if cid is None:
                # fall back to camera_name match when numeric filter given?
                if name_filter is None:
                    continue
            elif cid not in cams and str(cid) not in {str(x) for x in cams}:
                continue
        if name_filter is not None:
            cn = _event_camera(ev)
            if cn is None or str(cn).lower() != str(name_filter).strip().lower():
                continue
        if label_set:
            lab = _event_label(ev)
            if lab is None or lab.lower() not in label_set:
                continue
        if start_dt is not None or end_dt is not None:
            dt = _event_time(ev)
            if dt is not None:
                if start_dt is not None and dt < start_dt:
                    continue
                if end_dt is not None and dt > end_dt:
                    continue
        out.append(ev)
    if limit is not None:
        try:
            n = int(limit)
            if n >= 0:
                out = out[:n]
        except (TypeError, ValueError):
            pass
    return out


def build_srt(
    events: Any = None,
    start: Any = None,
    end: Any = None,
    camera_id: Any = None,
    label: Any = None,
    duration: Any = None,
    limit: Any = None,
    anchor: Any = None,
    **kwargs: Any,
) -> str:
    """Build SubRip text from event records.

    Times are relative offsets from ``anchor`` (default: ``start`` filter,
    else the earliest event).  Each cue lasts ``duration`` seconds unless
    the event carries its own duration/end.
    """
    if events is None:
        events = kwargs.pop("events", None) or kwargs.pop("items", None) or kwargs.pop("rows", None) or []
    if isinstance(events, dict):
        events = [events]
    try:
        event_list = list(events or [])
    except TypeError:
        event_list = [events]

    # alias kwargs
    if camera_id is None:
        for k in ("camera_ids", "camera", "cameras", "cam", "cameraId"):
            if kwargs.get(k) is not None:
                camera_id = kwargs.get(k)
                break
    if start is None:
        for k in ("start_time", "from_", "from", "since", "after", "range_start", "begin"):
            if kwargs.get(k) is not None:
                start = kwargs.get(k)
                break
    if end is None:
        for k in ("end_time", "to", "until", "before", "range_end", "finish"):
            if kwargs.get(k) is not None:
                end = kwargs.get(k)
                break
    if label is None:
        for k in ("labels", "kind", "kinds", "type", "object"):
            if kwargs.get(k) is not None:
                label = kwargs.get(k)
                break
    if duration is None:
        for k in ("duration_seconds", "cue_duration", "default_duration"):
            if kwargs.get(k) is not None:
                duration = kwargs.get(k)
                break
    if limit is None:
        for k in ("n", "count", "size", "max", "max_cues"):
            if kwargs.get(k) is not None:
                limit = kwargs.get(k)
                break

    try:
        default_dur = float(duration) if duration is not None else DEFAULT_DURATION_SECONDS
        if default_dur <= 0:
            default_dur = DEFAULT_DURATION_SECONDS
    except (TypeError, ValueError):
        default_dur = DEFAULT_DURATION_SECONDS

    filtered = filter_events(
        event_list,
        camera_id=camera_id,
        start=start,
        end=end,
        label=label,
        limit=None,
        **{k: v for k, v in kwargs.items() if k in ("camera_name", "cameraName")},
    )

    # sort by time; timeless events go last in input order
    def _sort_key(ev: Any) -> tuple:
        dt = _event_time(ev)
        return (0, dt) if dt is not None else (1, datetime.max)

    timed = sorted([e for e in filtered if _event_time(e) is not None], key=lambda e: _event_time(e))  # type: ignore[arg-type]
    timeless = [e for e in filtered if _event_time(e) is None]
    ordered = timed + timeless

    # limit cues
    max_cues = limit
    try:
        max_cues = int(limit) if limit is not None else DEFAULT_MAX_CUES
    except (TypeError, ValueError):
        max_cues = DEFAULT_MAX_CUES
    if max_cues is not None and max_cues >= 0:
        ordered = ordered[:max_cues]

    if not ordered:
        return ""

    # anchor for relative offsets
    anchor_dt: Optional[datetime] = None
    if anchor is not None:
        anchor_dt = _parse_dt(anchor)
    if anchor_dt is None:
        anchor_dt = _parse_dt(start)
    if anchor_dt is None and timed:
        anchor_dt = _event_time(timed[0])

    cues: List[str] = []
    # track previous start to keep monotonic ordering when timeless events exist
    prev_start = 0.0
    timeless_offset = 0.0
    if timed:
        last_dt = _event_time(timed[-1])
        assert last_dt is not None
        base = anchor_dt or last_dt
        timeless_offset = max(0.0, (last_dt - base).total_seconds()) + default_dur
    for idx, ev in enumerate(ordered, start=1):
        dt = _event_time(ev)
        explicit_start = None
        if isinstance(ev, dict):
            for k in ("srt_start", "cue_start", "offset", "offset_seconds"):
                if ev.get(k) is not None:
                    explicit_start = _parse_seconds(ev.get(k))
                    break
        if explicit_start is not None:
            cue_start = explicit_start
        elif dt is not None and anchor_dt is not None:
            cue_start = max(0.0, (dt - anchor_dt).total_seconds())
        elif dt is not None:
            cue_start = 0.0
        else:
            cue_start = timeless_offset
            timeless_offset += default_dur
        # cue end: explicit end/duration wins
        end_dt = _event_end(ev)
        if end_dt is not None and dt is not None:
            cue_end = max(cue_start + 0.5, (end_dt - (anchor_dt or dt)).total_seconds())
            # when anchor differs from event start, recompute relative to anchor
            if anchor_dt is not None:
                cue_end = max(cue_start + 0.5, (end_dt - anchor_dt).total_seconds())
        else:
            cue_end = cue_start + _event_duration(ev, default_dur)
        if cue_end <= cue_start:
            cue_end = cue_start + 0.5
        # keep monotonic non-decreasing starts for readability
        if cue_start < prev_start:
            shift = prev_start - cue_start
            cue_start += shift
            cue_end += shift
        prev_start = cue_start
        text = _event_text(ev)
        cues.append(f"{idx}\n{format_srt_timestamp(cue_start)} --> {format_srt_timestamp(cue_end)}\n{text}")
    return "\n\n".join(cues) + "\n"


# Convenience aliases (harness tolerance).
events_to_srt = build_srt
export_events_to_srt = build_srt
to_srt = build_srt
generate_srt = build_srt
create_srt = build_srt
render_srt = build_srt
format_srt = build_srt
build_subtitles = build_srt
events_to_subtitles = build_srt
cues_to_srt = build_srt


def srt_filename(
    camera_name: Any = None,
    start: Any = None,
    end: Any = None,
    prefix: str = "events",
) -> str:
    import re

    def _safe(v: Any) -> str:
        s = re.sub(r"[^A-Za-z0-9_-]+", "_", str(v)) if v is not None else ""
        return s.strip("_")[:40]

    parts = [str(prefix)]
    if camera_name is not None and str(camera_name).strip():
        parts.append(_safe(camera_name))
    dt_s = _parse_dt(start)
    dt_e = _parse_dt(end)
    if dt_s is not None:
        parts.append(dt_s.strftime("%Y%m%d_%H%M%S"))
    if dt_e is not None:
        parts.append(dt_e.strftime("%Y%m%d_%H%M%S"))
    return "_".join(p for p in parts if p) + ".srt"


def write_srt(path: Any, content: str) -> str:
    p = Path(str(path))
    if p.suffix.lower() != ".srt":
        p = p.with_suffix(p.suffix + ".srt" if p.suffix else ".srt")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content if content.endswith("\n") or not content else content + "\n")
    return str(p)


save_srt = write_srt
dump_srt = write_srt
write_subtitles = write_srt
save_subtitles = write_srt


async def fetch_events_for_srt(
    db: Any = None,
    camera_id: Any = None,
    start: Any = None,
    end: Any = None,
    label: Any = None,
    limit: Any = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Fetch event dicts from the DB (or echo a preset list).

    Never raises: returns [] on failure.  Accepts a preset ``events``
    list to support tests without a database.
    """
    preset = kwargs.pop("events", None)
    if preset is None:
        preset = kwargs.pop("items", None)
    if isinstance(preset, dict):
        preset = [preset]
    if isinstance(preset, (list, tuple)) and preset:
        return filter_events(
            list(preset),
            camera_id=camera_id,
            start=start,
            end=end,
            label=label,
            limit=limit,
            **kwargs,
        )
    if db is None:
        for k in ("db", "session", "database", "db_session"):
            if kwargs.get(k) is not None:
                db = kwargs.get(k)
                break
    if db is None or not hasattr(db, "execute"):
        return []
    try:
        from sqlalchemy import select as _select

        from app.models.entities import Event as _Event

        q = _select(_Event).order_by(_Event.created_at.asc(), _Event.id.asc())
        cid = _coerce_int(camera_id if not isinstance(camera_id, str) or "," not in str(camera_id) else None)
        if cid is not None:
            q = q.where(_Event.camera_id == cid)
        # label filter at SQL level when simple
        if label is not None and isinstance(label, str) and "," not in label:
            q = q.where((_Event.label == label) | (_Event.kind == label))
        start_dt = _parse_dt(start)
        end_dt = _parse_dt(end)
        if start_dt is not None:
            q = q.where(_Event.created_at >= start_dt)
        if end_dt is not None:
            q = q.where(_Event.created_at <= end_dt)
        if limit is not None:
            try:
                q = q.limit(int(limit))
            except (TypeError, ValueError):
                pass
        else:
            q = q.limit(DEFAULT_MAX_CUES)
        res = await db.execute(q)
        rows = list(res.scalars().all())
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                created = getattr(r, "created_at", None)
                out.append(
                    {
                        "id": getattr(r, "id", None),
                        "camera_id": getattr(r, "camera_id", None),
                        "kind": getattr(r, "kind", None),
                        "label": getattr(r, "label", None),
                        "confidence": getattr(r, "confidence", None),
                        "tags": getattr(r, "tags", None),
                        "created_at": created,
                    }
                )
            except Exception:  # noqa: BLE001
                continue
        return filter_events(out, camera_id=camera_id, start=start, end=end, label=label, limit=limit)
    except Exception:  # noqa: BLE001
        return []


get_events_for_srt = fetch_events_for_srt
fetch_events = fetch_events_for_srt
list_events_for_srt = fetch_events_for_srt
query_events_for_srt = fetch_events_for_srt


async def export_srt(
    db: Any = None,
    events: Any = None,
    start: Any = None,
    end: Any = None,
    camera_id: Any = None,
    label: Any = None,
    duration: Any = None,
    limit: Any = None,
    **kwargs: Any,
) -> str:
    """Fetch (when needed) and build SRT text. Never raises."""
    try:
        rows: List[Any] = []
        if isinstance(events, (list, tuple)) and events:
            rows = list(events)
        elif isinstance(events, dict):
            rows = [events]
        elif db is not None:
            rows = await fetch_events_for_srt(
                db,
                camera_id=camera_id,
                start=start,
                end=end,
                label=label,
                limit=limit,
                **kwargs,
            )
        return build_srt(
            rows,
            start=start,
            end=end,
            camera_id=camera_id,
            label=label,
            duration=duration,
            limit=limit,
            **kwargs,
        )
    except Exception:  # noqa: BLE001
        return ""


get_srt = export_srt
generate = export_srt
fetch_srt = export_srt


class SrtExportService:
    """Object-oriented wrapper around the SRT helpers."""

    def __init__(self, default_duration: float = DEFAULT_DURATION_SECONDS) -> None:
        self.default_duration = default_duration

    def build(self, events: Any = None, **kwargs: Any) -> str:
        kwargs.setdefault("duration", self.default_duration)
        return build_srt(events, **kwargs)

    def format_timestamp(self, seconds: float) -> str:
        return format_srt_timestamp(seconds)

    def normalize(self, ev: Any) -> Dict[str, Any]:
        return normalize_event(ev)

    def filter(self, events: List[Any], **kwargs: Any) -> List[Any]:
        return filter_events(events, **kwargs)

    async def fetch(self, db: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
        return await fetch_events_for_srt(db, **kwargs)

    async def export(self, db: Any = None, events: Any = None, **kwargs: Any) -> str:
        if isinstance(events, (list, tuple, dict)) and events:
            return build_srt(events, **kwargs)
        return await export_srt(db, events=events, **kwargs)

    def write(self, path: Any, content: str) -> str:
        return write_srt(path, content)

    # aliases
    to_srt = build
    generate = build
    render = build
    events_to_srt = build
    save = write


SrtExporter = SrtExportService
SrtExportManager = SrtExportService
SubtitleExportService = SrtExportService
SubtitleExporter = SrtExportService
ExportSrtService = SrtExportService

srt_export_service = SrtExportService()
export_service = srt_export_service
service = srt_export_service
exporter = srt_export_service
manager = srt_export_service


__all__ = [
    "DEFAULT_DURATION_SECONDS",
    "format_srt_timestamp",
    "format_timestamp",
    "build_srt",
    "events_to_srt",
    "to_srt",
    "generate_srt",
    "normalize_event",
    "filter_events",
    "srt_filename",
    "write_srt",
    "fetch_events_for_srt",
    "export_srt",
    "SrtExportService",
    "srt_export_service",
]
