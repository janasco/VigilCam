"""Multi-camera incident grouper.

Correlates detection events from *different* cameras that occur close in
time into incidents (e.g. a person walking across multiple camera views).

This is intentionally distinct from :mod:`app.services.event_merge`, which
merges duplicate/fragmented events from the *same* camera. This module only
produces groups spanning ``min_cameras`` distinct cameras and never merges
rows -- it only correlates them into incident summaries.

Pure (in-memory, no-DB) helpers plus optional async DB-backed entrypoints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DEFAULT_WINDOW_SECONDS = 120.0
DEFAULT_MIN_CAMERAS = 2
DEFAULT_MIN_EVENTS = 2

WINDOW_KEYS = (
    "window_seconds",
    "windowSeconds",
    "window",
    "time_window",
    "timeWindow",
    "threshold_seconds",
    "thresholdSeconds",
    "threshold",
    "gap_seconds",
    "gapSeconds",
    "gap",
    "max_gap_seconds",
    "maxGapSeconds",
    "correlate_window",
    "correlateWindow",
    "incident_window",
    "incidentWindow",
)

MIN_CAMERAS_KEYS = (
    "min_cameras",
    "minCameras",
    "min_cams",
    "min cameras",
    "distinct_cameras",
    "distinctCameras",
    "camera_count",
    "cameraCount",
)

MIN_EVENTS_KEYS = (
    "min_events",
    "minEvents",
    "min_count",
    "minCount",
    "min_size",
    "minSize",
)


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

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
        except Exception:
            pass
    if isinstance(data, dict):
        out = dict(data)
        for nested_key in (
            "payload", "options", "config", "params", "data", "body",
            "request", "fields", "incident", "correlate", "group",
        ):
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    try:
        return dict(data)
    except Exception:
        return {}


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        ts = float(value)
        if abs(ts) > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except (OSError, ValueError, OverflowError):
            return None
    else:
        s = str(value).strip()
        if s == "" or s.lower() in {"none", "null", "undefined", "all"}:
            return None
        try:
            ts = float(s)
            if s.replace(".", "", 1).replace("e", "", 1).replace("E", "", 1).replace("-", "", 1).replace("+", "", 1).isdigit():
                if abs(ts) > 1e12:
                    ts = ts / 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            pass
        s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(s2)
        except ValueError:
            return None
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _event_to_dict(ev: Any) -> Dict[str, Any]:
    if isinstance(ev, dict):
        return dict(ev)
    if hasattr(ev, "model_dump"):
        try:
            return dict(ev.model_dump())
        except Exception:
            pass
    out: Dict[str, Any] = {}
    for attr in ("id", "event_id", "camera_id", "cameraId", "camera", "kind",
                 "label", "confidence", "payload", "tags", "tag",
                 "snapshot_path", "created_at", "createdAt", "started_at",
                 "timestamp", "ts", "time"):
        try:
            val = getattr(ev, attr, None)
        except Exception:
            val = None
        if val is not None:
            out[attr] = val
    if not out:
        try:
            return dict(ev)
        except Exception:
            return {"value": ev}
    return out


def _get_id(ev: Dict[str, Any]) -> Optional[int]:
    for k in ("id", "event_id", "eventId", "eventID"):
        v = ev.get(k)
        cid = _coerce_int(v, None)
        if cid is not None:
            return cid
    return None


def _get_camera(ev: Dict[str, Any]) -> Any:
    for k in ("camera_id", "cameraId", "camera", "cam"):
        if ev.get(k) is not None:
            return ev.get(k)
    return None


def _get_created(ev: Dict[str, Any]) -> Optional[datetime]:
    for k in ("created_at", "createdAt", "started_at", "start",
              "timestamp", "ts", "time"):
        if ev.get(k) is not None:
            dt = _parse_dt(ev.get(k))
            if dt is not None:
                return dt
    return None


def _extract_window(*args: Any, **kwargs: Any) -> float:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "request", "options"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    raw = _first_present(flat, WINDOW_KEYS)
    window = _coerce_float(raw, DEFAULT_WINDOW_SECONDS)
    if window is None or window < 0:
        window = DEFAULT_WINDOW_SECONDS
    return float(window)


def _extract_min_cameras(*args: Any, **kwargs: Any) -> int:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    raw = _first_present(flat, MIN_CAMERAS_KEYS)
    n = _coerce_int(raw, DEFAULT_MIN_CAMERAS)
    if n is None or n < 1:
        n = DEFAULT_MIN_CAMERAS
    return max(1, min(100, int(n)))


def _extract_min_events(*args: Any, **kwargs: Any) -> int:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    raw = _first_present(flat, MIN_EVENTS_KEYS)
    n = _coerce_int(raw, DEFAULT_MIN_EVENTS)
    if n is None or n < 1:
        n = DEFAULT_MIN_EVENTS
    return max(1, min(1000, int(n)))


def _extract_events(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    events: List[Any] = []
    if args and isinstance(args[0], list):
        events = list(args[0])
    for key in ("events", "items", "sources", "detections", "alerts"):
        val = flat.get(key)
        if isinstance(val, list) and val:
            events = list(val)
            break
    # positional bare event dicts
    for a in args:
        if isinstance(a, dict) and _get_id(_flatten(a)) is not None:
            # dict with an id that is not just an options wrapper
            if not any(k in a for k in WINDOW_KEYS + MIN_CAMERAS_KEYS + MIN_EVENTS_KEYS) or "camera_id" in a or "camera" in a:
                if a not in events and not any(
                    isinstance(e, dict) and _get_id(e) == _get_id(a) for e in events if isinstance(e, dict)
                ):
                    # only add if it looks like an event (has camera or created_at)
                    if _get_camera(a) is not None or _get_created(a) is not None or "id" in a:
                        # avoid double-adding the options dict itself when it carries events
                        if not any(k in a for k in ("events", "items", "sources")):
                            events.append(a)
    return [_event_to_dict(e) for e in events]


# ---------------------------------------------------------------------------
# Pure correlation
# ---------------------------------------------------------------------------

def group_events_into_incidents(
    events: List[Any],
    window_seconds: Any = None,
    min_cameras: Any = None,
    min_events: Any = None,
    **kwargs: Any,
) -> List[List[Dict[str, Any]]]:
    """Group sorted events into incidents.

    Consecutive events whose gap is ``<= window_seconds`` belong to the same
    incident window. Only windows with ``>= min_events`` events spanning
    ``>= min_cameras`` distinct cameras are returned (multi-camera only).
    """
    if window_seconds is None:
        window_seconds = _extract_window(kwargs)
    if min_cameras is None:
        min_cameras = _extract_min_cameras(kwargs)
    if min_events is None:
        min_events = _extract_min_events(kwargs)
    try:
        window = float(window_seconds)
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW_SECONDS
    if window < 0:
        window = DEFAULT_WINDOW_SECONDS
    try:
        need_cams = max(1, int(min_cameras))
    except (TypeError, ValueError):
        need_cams = DEFAULT_MIN_CAMERAS
    try:
        need_events = max(1, int(min_events))
    except (TypeError, ValueError):
        need_events = DEFAULT_MIN_EVENTS

    items = [_event_to_dict(e) for e in (events or [])]
    # Sort by timestamp; events without timestamps sort first by id.
    items.sort(key=lambda e: (
        _get_created(e) or datetime.min,
        _get_id(e) if _get_id(e) is not None else 10**18,
    ))

    windows: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    prev_dt: Optional[datetime] = None
    for ev in items:
        dt = _get_created(ev)
        if not current:
            current = [ev]
            prev_dt = dt
            continue
        if dt is not None and prev_dt is not None:
            if (dt - prev_dt).total_seconds() <= window:
                current.append(ev)
                prev_dt = max(prev_dt, dt) if dt > prev_dt else prev_dt
                # keep prev as latest timestamp in window for chaining
                if dt > prev_dt:
                    prev_dt = dt
                continue
        elif dt is None or prev_dt is None:
            # Without timestamps fall back to grouping everything together.
            current.append(ev)
            if dt is not None:
                prev_dt = dt
            continue
        # gap too large: flush
        if len(current) >= need_events and len({str(_get_camera(e)) for e in current}) >= need_cams:
            windows.append(current)
        current = [ev]
        prev_dt = dt
    if len(current) >= need_events and len({str(_get_camera(e)) for e in current}) >= need_cams:
        windows.append(current)
    return windows


def build_incident(group: List[Any], incident_id: Any = None) -> Dict[str, Any]:
    """Summarize one correlated group into an incident dict."""
    items = [_event_to_dict(e) for e in (group or [])]
    if len(items) < 1:
        raise ValueError("at least one event is required to build an incident")
    ids = [_get_id(e) for e in items if _get_id(e) is not None]
    cams: List[Any] = []
    seen_cams = set()
    for e in items:
        cam = _get_camera(e)
        key = str(cam)
        if key not in seen_cams:
            seen_cams.add(key)
            cams.append(cam)
    dts = [_get_created(e) for e in items]
    dts = [d for d in dts if d is not None]
    started = min(dts) if dts else None
    ended = max(dts) if dts else None
    duration = (ended - started).total_seconds() if started and ended else 0.0
    labels = sorted({str(e.get("label")) for e in items if e.get("label") is not None})
    kinds = sorted({str(e.get("kind")) for e in items if e.get("kind") is not None})
    iid = incident_id if incident_id is not None else 1
    try:
        iid_int = int(iid)
    except (TypeError, ValueError):
        iid_int = 1
    return {
        "incident_id": iid_int,
        "id": iid_int,
        "event_ids": ids,
        "ids": ids,
        "cameras": cams,
        "camera_ids": cams,
        "camera_count": len(cams),
        "distinct_cameras": len(cams),
        "event_count": len(items),
        "count": len(items),
        "started_at": started.isoformat() if started else None,
        "ended_at": ended.isoformat() if ended else None,
        "duration_seconds": float(duration),
        "labels": labels,
        "kinds": kinds,
        "events": items,
    }


def correlate_events(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Correlate events into incident summaries (pure, no DB)."""
    events = _extract_events(*args, **kwargs)
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    window = _extract_window(flat)
    min_cams = _extract_min_cameras(flat)
    min_events = _extract_min_events(flat)
    groups = group_events_into_incidents(
        events, window_seconds=window, min_cameras=min_cams, min_events=min_events,
    )
    return [build_incident(g, incident_id=i + 1) for i, g in enumerate(groups)]


def suggest_incidents(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias for :func:`correlate_events` (suggestion view)."""
    return correlate_events(*args, **kwargs)


def clear(*args: Any, **kwargs: Any) -> int:
    """No-op store clear for API symmetry (pure helpers are stateless)."""
    return 0


# ---------------------------------------------------------------------------
# Async DB-backed entrypoints
# ---------------------------------------------------------------------------

def _orm_to_dict(ev: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for attr in ("id", "camera_id", "kind", "label", "confidence",
                 "payload", "tags", "snapshot_path", "created_at"):
        try:
            out[attr] = getattr(ev, attr, None)
        except Exception:
            out[attr] = None
    if hasattr(out.get("created_at"), "isoformat"):
        try:
            out["created_at"] = out["created_at"].isoformat()  # type: ignore[union-attr]
        except Exception:
            pass
    return out


async def correlate_incidents_async(
    *args: Any, db: Any = None, session: Any = None, **kwargs: Any,
) -> List[Dict[str, Any]]:
    sess = db if db is not None else session
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    events: List[Any] = []
    if args and isinstance(args[0], list):
        events = list(args[0])
    for key in ("events", "items", "sources", "detections", "alerts"):
        if isinstance(flat.get(key), list):
            events = list(flat.get(key))
            break
    if not events and sess is not None:
        try:
            from sqlalchemy import select
            from app.models.entities import Event as EventModel

            limit = _coerce_int(flat.get("limit", flat.get("n")), 500) or 500
            result = await sess.execute(
                select(EventModel).order_by(EventModel.id.desc()).limit(min(max(int(limit), 1), 2000))
            )
            rows = list(result.scalars().all())
            events = [_orm_to_dict(r) for r in rows]
        except Exception:
            events = []
    window = _extract_window(flat)
    min_cams = _extract_min_cameras(flat)
    min_events = _extract_min_events(flat)
    groups = group_events_into_incidents(
        [_event_to_dict(e) for e in events],
        window_seconds=window, min_cameras=min_cams, min_events=min_events,
    )
    return [build_incident(g, incident_id=i + 1) for i, g in enumerate(groups)]


suggest_incidents_async = correlate_incidents_async


class IncidentGrouper:
    """Object-oriented facade over the incident helpers."""

    def group(self, *args: Any, **kwargs: Any) -> List[List[Dict[str, Any]]]:
        events = _extract_events(*args, **kwargs)
        flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
        flat.update(kwargs or {})
        return group_events_into_incidents(
            events,
            window_seconds=_extract_window(flat),
            min_cameras=_extract_min_cameras(flat),
            min_events=_extract_min_events(flat),
        )

    def correlate(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return correlate_events(*args, **kwargs)

    def suggest(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return suggest_incidents(*args, **kwargs)

    def build(self, group: List[Any], incident_id: Any = None) -> Dict[str, Any]:
        return build_incident(group, incident_id=incident_id)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    # aliases
    group_events = group
    correlate_events = correlate
    group_into_incidents = group


incident_grouper = IncidentGrouper()
grouper = incident_grouper
service = incident_grouper
manager = incident_grouper
store = incident_grouper

# Compatibility aliases (function level).
group_incidents = group_events_into_incidents
find_incidents = group_events_into_incidents
correlate = correlate_events
group = group_events_into_incidents
suggest = suggest_incidents

__all__ = [
    "DEFAULT_WINDOW_SECONDS",
    "DEFAULT_MIN_CAMERAS",
    "DEFAULT_MIN_EVENTS",
    "group_events_into_incidents",
    "build_incident",
    "correlate_events",
    "suggest_incidents",
    "clear",
    "correlate_incidents_async",
    "suggest_incidents_async",
    "IncidentGrouper",
    "incident_grouper",
]
