"""Event-to-recording finder service.

Given an event (camera + timestamp), find the ``Recording`` DB rows whose
``[started_at, ended_at]`` interval overlaps
``[event_ts - window_before, event_ts + window_after]``.

This complements ``app.services.event_segments`` (verified): that module
discovers *segment files* on demand (filesystem scan + manual links +
``_recordings_for_window`` helper), but there is no direct
"which recordings cover event X time" finder. This module provides exactly
that: a pure overlap filter plus DB-backed async entrypoints.

The sync entrypoint works without a DB when an explicit ``recordings``
candidate list is supplied (list of dicts / ORM objects). The async
entrypoints accept an optional ``db``/``session`` (SQLAlchemy async
session) for forward-compatibility and query the ``Recording`` table.
No edits to ``app/main.py`` are required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_WINDOW_BEFORE = 30.0
DEFAULT_WINDOW_AFTER = 30.0

EVENT_ID_KEYS = (
    "event_id", "eventId", "event", "eventid", "id_event", "for_event",
    "camera_event_id", "id",
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
RECORDINGS_KEYS = (
    "recordings", "recordings_list", "candidates", "rows", "items",
    "recording_list", "recordingRows",
)


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
            "request", "fields", "entry", "event",
        ):
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    try:
        return dict(data)  # type: ignore[arg-type]
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
        "camera_name", "cameraName", "kind", "label", "confidence",
        "created_at", "createdAt", "started_at", "timestamp", "time",
        "event_time",
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
            cam_name = val
            break
    return cam_id, cam_name


def _recording_to_dict(rec: Any) -> Dict[str, Any]:
    if isinstance(rec, dict):
        d = dict(rec)
    elif hasattr(rec, "model_dump"):
        try:
            d = dict(rec.model_dump())
        except Exception:  # noqa: BLE001
            d = {}
    else:
        d = {}
        for attr in ("id", "camera_id", "cameraId", "path", "started_at",
                     "startedAt", "start", "ended_at", "endedAt", "end",
                     "size_bytes", "size", "created_at"):
            try:
                val = getattr(rec, attr, None)
            except Exception:  # noqa: BLE001
                val = None
            if val is not None:
                d[attr] = val
        if not d:
            return {}
    out: Dict[str, Any] = dict(d)
    rid = out.get("id", out.get("recording_id", out.get("recordingId")))
    if rid is not None:
        out.setdefault("id", rid)
        out.setdefault("recording_id", rid)
        out.setdefault("recordingId", rid)
    if out.get("camera_id") is None and out.get("cameraId") is not None:
        out["camera_id"] = out.get("cameraId")
    started = out.get("started_at", out.get("startedAt", out.get("start", out.get("start_time"))))
    ended = out.get("ended_at", out.get("endedAt", out.get("end", out.get("end_time"))))
    sts = _parse_time(started)
    ets = _parse_time(ended)
    out["start"] = sts if sts is not None else out.get("start")
    out["start_time"] = sts if sts is not None else out.get("start_time")
    out["end"] = ets if ets is not None else out.get("end")
    out["end_time"] = ets if ets is not None else out.get("end_time")
    # ISO strings for API output when datetimes were supplied.
    for key in ("started_at", "ended_at"):
        val = d.get(key)
        if isinstance(val, datetime):
            try:
                out[key] = val.isoformat()
            except Exception:  # noqa: BLE001
                pass
    return out


def _recording_window(rec: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    start = _parse_time(rec.get("start", rec.get("start_time", rec.get("started_at"))))
    end = _parse_time(rec.get("end", rec.get("end_time", rec.get("ended_at"))))
    return start, end


def _overlaps(rec_start: Optional[float], rec_end: Optional[float],
              win_start: float, win_end: float) -> bool:
    if rec_start is None:
        return False
    # Open-ended recording (still recording): covers everything after start.
    rec_end_eff = rec_end if rec_end is not None else float("inf")
    return rec_end_eff >= win_start and rec_start <= win_end


def _extract_event_id(*args: Any, **kwargs: Any) -> Optional[int]:
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif isinstance(a, (int, float)) and not isinstance(a, bool) and "event_id" not in flat:
            flat["event_id"] = a
    flat.update(kwargs or {})
    for nested_key in ("event", "payload", "params", "data", "body", "request"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    raw = _first_present(flat, EVENT_ID_KEYS)
    if raw is not None and isinstance(raw, dict):
        raw = raw.get("id", raw.get("event_id"))
    return _coerce_int(raw, None)


def _extract_recordings(*args: Any, **kwargs: Any) -> List[Any]:
    for key in RECORDINGS_KEYS:
        if kwargs.get(key) is not None:
            val = kwargs.get(key)
            if isinstance(val, list):
                return list(val)
            return [val]
    for a in args:
        if isinstance(a, (list, tuple)):
            return list(a)
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    for key in RECORDINGS_KEYS:
        val = flat.get(key)
        if isinstance(val, list):
            return list(val)
        if val is not None:
            return [val]
    return []


def find_recordings_for_event(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Find recordings overlapping an event's timestamp (sync, no DB).

    Accepts flexible inputs, e.g.::

        find_recordings_for_event({"id": 1, "camera_id": 2, "created_at": ...},
                                  recordings=[...])
        find_recordings_for_event(event_id=1, created_at=..., camera_id=2,
                                  recordings=[...])

    ``recordings`` may be a list of dicts or ORM objects with
    ``started_at``/``ended_at`` (or ``start``/``end``) attributes. When no
    candidate list is supplied the sync path returns ``[]``; use the async
    entrypoint with a ``db`` session for DB-backed lookup.
    """
    flat: Dict[str, Any] = {}
    event_dict: Optional[Dict[str, Any]] = None
    for a in args:
        if isinstance(a, dict):
            if any(k in a for k in ("camera_id", "camera_name", "created_at", "timestamp", "camera")) and event_dict is None:
                event_dict = dict(a)
            else:
                flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:  # noqa: BLE001
                pass
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "options", "request", "event"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            if event_dict is None and any(k in nested for k in ("camera_id", "camera_name", "created_at", "timestamp")):
                event_dict = dict(nested)
            for k, v in nested.items():
                flat.setdefault(k, v)
    if event_dict is None:
        cand = {k: flat.get(k) for k in (
            "camera_id", "cameraId", "camera", "camera_name", "cameraName",
            "created_at", "createdAt", "timestamp", "time", "event_time",
            "kind", "label", "id", "event_id",
        ) if flat.get(k) is not None}
        if cand:
            event_dict = dict(cand)
    if event_dict is None:
        # Direct time-window query: camera_id + start/end without an event.
        cam_direct = flat.get("camera_id", flat.get("cameraId", flat.get("camera")))
        start_direct = _parse_time(_first_present(flat, ("start", "start_time", "startTime", "from", "from_time", "since")))
        end_direct = _parse_time(_first_present(flat, ("end", "end_time", "endTime", "to", "to_time", "until")))
        if cam_direct is not None or (start_direct is not None and end_direct is not None):
            event_dict = {"camera_id": cam_direct, "created_at": start_direct}
            if start_direct is not None and end_direct is not None:
                # Treat as explicit window: filter candidates directly.
                out: List[Dict[str, Any]] = []
                for rec in _extract_recordings(*args, **kwargs):
                    info = _recording_to_dict(rec)
                    if not info:
                        continue
                    rs, re = _recording_window(info)
                    if rs is None:
                        continue
                    if _overlaps(rs, re, min(start_direct, end_direct), max(start_direct, end_direct)):
                        if cam_direct is not None:
                            try:
                                if info.get("camera_id") is not None and int(info.get("camera_id")) != int(cam_direct):  # type: ignore[arg-type]
                                    continue
                            except (TypeError, ValueError):
                                pass
                        out.append(info)
                out.sort(key=lambda d: (d.get("start") or 0))
                limit = _coerce_int(_first_present(flat, ("limit", "n", "count", "size", "page_size")), None)
                if limit is not None and limit >= 0:
                    out = out[:limit]
                return out
        return []
    event_dict = _event_to_dict(event_dict)
    # Explicit caller hints override the event dict.
    for k in ("camera_id", "cameraId", "camera", "camera_name", "cameraName",
              "created_at", "createdAt", "timestamp", "time", "event_time"):
        if flat.get(k) is not None:
            event_dict[k] = flat.get(k)
    ts = _event_time(event_dict)
    if ts is None:
        return []
    before, after = _resolve_windows(flat, event_dict)
    window_start = ts - before
    window_end = ts + after
    cam_id, _cam_name = _event_camera(event_dict)
    for key in ("camera_name", "cameraName", "camera", "cam"):
        if flat.get(key) is not None and not isinstance(flat.get(key), dict):
            break
    candidates = _extract_recordings(*args, **kwargs)
    out = []
    for rec in candidates:
        info = _recording_to_dict(rec)
        if not info:
            continue
        rs, re = _recording_window(info)
        if rs is None:
            continue
        if not _overlaps(rs, re, window_start, window_end):
            continue
        if cam_id is not None and info.get("camera_id") is not None:
            try:
                if int(info.get("camera_id")) != int(cam_id):  # type: ignore[arg-type]
                    continue
            except (TypeError, ValueError):
                pass
        info = dict(info)
        info.setdefault("event_id", event_dict.get("id", event_dict.get("event_id")))
        info["overlap_start"] = max(rs, window_start)
        info["overlap_end"] = min(re if re is not None else window_end, window_end)
        info["window_start"] = window_start
        info["window_end"] = window_end
        out.append(info)
    out.sort(key=lambda d: (d.get("start") or 0))
    limit = _coerce_int(_first_present(flat, ("limit", "n", "count", "size", "page_size")), None)
    if limit is not None and limit >= 0:
        out = out[:limit]
    return out


def find_recordings_for_time(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Find recordings overlapping an explicit ``[start, end]`` window."""
    return find_recordings_for_event(*args, **kwargs)


def count_recordings_for_event(*args: Any, **kwargs: Any) -> int:
    return len(find_recordings_for_event(*args, **kwargs))


def clear(*args: Any, **kwargs: Any) -> int:
    """No-op for API symmetry (recordings live in the DB, not memory)."""
    return 0


# --- Async entrypoints (db-backed) -------------------------------------------

async def _fetch_event(db: Any, event_id: int) -> Optional[Any]:
    try:
        from app.models.entities import Event as _Event  # lazy import

        return await db.get(_Event, int(event_id))
    except Exception:  # noqa: BLE001
        return None


async def _fetch_recordings(db: Any, camera_id: Any) -> List[Any]:
    try:
        from sqlalchemy import select as _select  # lazy import

        from app.models.entities import Recording as _Recording  # lazy import

        stmt = _select(_Recording)
        if camera_id is not None:
            try:
                stmt = stmt.where(_Recording.camera_id == int(camera_id))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
        result = await db.execute(stmt)
        return list(result.scalars().all())
    except Exception:  # noqa: BLE001
        return []


async def find_recordings_for_event_async(
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
            for k in (
                "camera_id", "cameraId", "camera", "camera_name", "cameraName",
                "created_at", "createdAt", "timestamp", "time", "event_time",
            ):
                if flat.get(k) is not None:
                    inline[k] = flat.get(k)
    if inline is None:
        # Direct time-window query against the DB.
        if sess is not None and any(flat.get(k) is not None for k in (
            "camera_id", "cameraId", "camera", "start", "start_time", "end", "end_time",
        )):
            cam = flat.get("camera_id", flat.get("cameraId", flat.get("camera")))
            rows = await _fetch_recordings(sess, cam)
            merged_kwargs = dict(flat)
            merged_kwargs.setdefault("recordings", rows)
            return find_recordings_for_event(*args, **merged_kwargs)
        # Sync fallback over an explicit candidate list.
        cands = _extract_recordings(*args, **kwargs)
        if cands:
            return find_recordings_for_event(*args, **kwargs)
        return []
    inline = dict(inline)
    ts = _event_time(_event_to_dict(inline))
    if ts is None:
        return []
    before, after = _resolve_windows(flat, inline)
    cam_id, _cn = _event_camera(_event_to_dict(inline))
    # Explicit camera override wins.
    for key in ("camera_id", "cameraId", "camera"):
        if flat.get(key) is not None:
            try:
                cam_id = int(flat.get(key))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                cam_id = flat.get(key)
            break
    rows: List[Any] = []
    if sess is not None:
        rows = await _fetch_recordings(sess, cam_id)
    # Merge explicit candidates (useful for tests / offline mode).
    rows = list(rows) + _extract_recordings(*args, **kwargs)
    merged = dict(flat)
    merged["recordings"] = rows
    # Ensure the resolved inline event drives the window.
    for k, v in inline.items():
        merged.setdefault(k, v)
    merged_event = dict(inline)
    for k in ("camera_id", "cameraId", "camera", "camera_name", "cameraName",
              "created_at", "createdAt", "timestamp", "time", "event_time"):
        if flat.get(k) is not None:
            merged_event[k] = flat.get(k)
    return find_recordings_for_event(merged_event, **merged)


async def get_recordings_for_event_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return await find_recordings_for_event_async(*args, db=db, session=session, **kwargs)


class EventRecordingsService:
    """Object-oriented facade over the event-to-recording finder."""

    def find(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return find_recordings_for_event(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return find_recordings_for_event(*args, **kwargs)

    def count(self, *args: Any, **kwargs: Any) -> int:
        return count_recordings_for_event(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    # aliases
    find_recordings = find
    get_recordings = get
    recordings_for_event = get
    recordings = get
    for_event = get


event_recordings_service = EventRecordingsService()
event_recording_service = event_recordings_service
recordings_service = event_recordings_service
service = event_recordings_service
manager = event_recordings_service
store = event_recordings_service
finder = event_recordings_service
event_finder = event_recordings_service


# Compatibility aliases (function level).
get_recordings_for_event = find_recordings_for_event
recordings_for_event = find_recordings_for_event
recordings_for_events = find_recordings_for_event
find_recordings = find_recordings_for_event
get_event_recordings = find_recordings_for_event
list_event_recordings = find_recordings_for_event
event_recordings = find_recordings_for_event
find = find_recordings_for_event
get = find_recordings_for_event

__all__ = [
    "find_recordings_for_event",
    "find_recordings_for_time",
    "get_recordings_for_event",
    "count_recordings_for_event",
    "clear",
    "find_recordings_for_event_async",
    "get_recordings_for_event_async",
    "EventRecordingsService",
    "event_recordings_service",
]
