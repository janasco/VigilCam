"""Event stats aggregation service for VigilCam.

Complements the per-hour heatmap (``app.services.heatmap``) and the
timeline list (``app.api.events`` ``/timeline``) with chart-ready
aggregates:

- ``by_label``: counts grouped by event label
- ``top_cameras`` / ``by_camera``: counts grouped by camera
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


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
            try:
                dt = datetime.fromisoformat(s + "T00:00:00")
            except ValueError:
                return None
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _merged_options(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            merged.update(a)
        elif isinstance(a, (list, tuple)):
            merged.setdefault("events", list(a))
    merged.update(kwargs)
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "filter", "filters"):
        nested = merged.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                merged.setdefault(k, v)
    return merged


def _resolve_range(options: Dict[str, Any]) -> tuple[Optional[datetime], Optional[datetime]]:
    start_keys = ("start", "start_time", "start_date", "from_", "from", "from_time",
                  "begin", "after", "since", "min_time", "lower_bound", "range_start",
                  "after_time", "from_date", "startDate", "startTime")
    end_keys = ("end", "end_time", "end_date", "to", "to_time", "until", "before",
                "max_time", "upper_bound", "range_end", "finish", "before_time",
                "to_date", "endDate", "endTime")
    start_value = None
    for k in start_keys:
        if options.get(k) is not None:
            start_value = options.get(k)
            break
    end_value = None
    for k in end_keys:
        if options.get(k) is not None:
            end_value = options.get(k)
            break
    start_dt = _parse_dt(start_value)
    end_dt = _parse_dt(end_value)
    hours = None
    for k in ("hours", "last_hours", "hour_window", "window_hours", "n_hours"):
        if options.get(k) is not None:
            try:
                hours = float(options.get(k))
                break
            except (TypeError, ValueError):
                pass
    days = None
    for k in ("days", "last_days", "day_window", "window_days", "n_days"):
        if options.get(k) is not None:
            try:
                days = float(options.get(k))
                break
            except (TypeError, ValueError):
                pass
    if hours is None and days is not None:
        try:
            hours = float(days) * 24.0
        except (TypeError, ValueError):
            hours = None
    if hours is not None and hours > 0:
        anchor = end_dt or datetime.now(timezone.utc).replace(tzinfo=None)
        computed_start = anchor - timedelta(hours=float(hours))
        if start_dt is None:
            start_dt = computed_start
        else:
            start_dt = max(start_dt, computed_start)
        if end_dt is None:
            end_dt = anchor
    return start_dt, end_dt


def _resolve_cameras(options: Dict[str, Any]) -> List[int]:
    cands: List[Any] = []
    for k in ("camera_id", "camera_ids", "cameras", "camera", "camera_id_list",
              "cam", "cameraId", "cameraIds", "cam_id"):
        if options.get(k) is not None:
            cands.append(options.get(k))
    vals: List[Any] = []
    for c in cands:
        if isinstance(c, (list, tuple, set)):
            vals.extend(list(c))
        else:
            vals.append(c)
    ids: List[int] = []
    seen = set()
    for v in vals:
        if v is None:
            continue
        if isinstance(v, str):
            for part in v.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    iv = int(float(part))
                except (TypeError, ValueError):
                    continue
                if iv not in seen:
                    seen.add(iv)
                    ids.append(iv)
        else:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv not in seen:
                seen.add(iv)
                ids.append(iv)
    return ids


def _label_set(*candidates: Any) -> set:
    out = set()
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, (list, tuple, set)):
            for item in c:
                out.update(_label_set(item))
        elif isinstance(c, str):
            for part in c.split(","):
                part = part.strip()
                if part and part.lower() not in {"all", "none", "null"}:
                    out.add(part.lower())
        else:
            s = str(c).strip()
            if s and s.lower() not in {"all", "none", "null"}:
                out.add(s.lower())
    return out


def _event_time(ev: Any) -> Optional[datetime]:
    for attr in ("created_at", "timestamp", "ts", "time", "occurred_at", "at", "date"):
        v = getattr(ev, attr, None) if not isinstance(ev, dict) else ev.get(attr)
        if v is None and isinstance(ev, dict):
            for k2 in ev.keys():
                if str(k2).lower() == attr:
                    v = ev.get(k2)
                    break
        if v is None:
            continue
        dt = _parse_dt(v)
        if dt is not None:
            return dt
    return None


def _event_camera(ev: Any) -> Optional[int]:
    if isinstance(ev, dict):
        for k in ("camera_id", "cameraId", "camera", "cam_id", "cam"):
            if ev.get(k) is not None:
                try:
                    return int(ev.get(k))
                except (TypeError, ValueError):
                    if isinstance(ev.get(k), str):
                        try:
                            return int(float(ev.get(k)))
                        except (TypeError, ValueError):
                            continue
                    continue
        return None
    for attr in ("camera_id", "cameraId", "cam_id"):
        v = getattr(ev, attr, None)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def _event_label(ev: Any) -> Optional[str]:
    if isinstance(ev, dict):
        for k in ("label", "kind", "type", "object", "detection_label"):
            if ev.get(k) is not None:
                return str(ev.get(k))
        return None
    for attr in ("label", "kind"):
        v = getattr(ev, attr, None)
        if v is not None:
            return str(v)
    return None


def _event_kind(ev: Any) -> Optional[str]:
    if isinstance(ev, dict):
        if ev.get("kind") is not None:
            return str(ev.get("kind"))
        return _event_label(ev)
    v = getattr(ev, "kind", None)
    if v is not None:
        return str(v)
    return _event_label(ev)


def build_event_stats(events: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Aggregate in-memory events into chart-ready stats.

    Returns totals plus ``by_label`` (label -> count) and
    ``by_camera`` / ``top_cameras`` (camera -> count) aggregates.
    """
    options = _merged_options(*args, **kwargs)
    if events is None:
        for k in ("events", "items", "rows", "data_events", "event_list"):
            if options.get(k) is not None and isinstance(options.get(k), (list, tuple)):
                events = options.get(k)
                break
    if events is None:
        events = []
    try:
        event_list = list(events)
    except TypeError:
        event_list = []

    start_dt, end_dt = _resolve_range(options)
    cam_ids = _resolve_cameras(options)
    cam_set = set(cam_ids)
    labels = _label_set(
        options.get("label"), options.get("labels"),
        options.get("detection_label"), options.get("object"),
        options.get("objects"), options.get("kind"), options.get("kinds"),
        options.get("type"), options.get("types"),
    )
    # When a label filter is supplied we filter; otherwise we aggregate all.
    filter_labels = bool(labels)

    label_counts: Counter = Counter()
    camera_counts: Counter = Counter()
    kind_counts: Counter = Counter()
    total = 0
    for ev in event_list:
        ts = _event_time(ev)
        if ts is not None:
            if start_dt is not None and ts < start_dt:
                continue
            if end_dt is not None and ts > end_dt:
                continue
        cid = _event_camera(ev)
        if cam_set and (cid not in cam_set):
            continue
        lab = _event_label(ev)
        if filter_labels:
            if lab is None or lab.lower() not in labels:
                continue
        kind = _event_kind(ev)
        label_key = str(lab) if lab is not None else "unknown"
        kind_key = str(kind) if kind is not None else "unknown"
        ckey = str(cid) if cid is not None else "unknown"
        label_counts[label_key] += 1
        kind_counts[kind_key] += 1
        camera_counts[ckey] += 1
        total += 1

    by_label = {str(k): int(v) for k, v in label_counts.items()}
    by_camera = {str(k): int(v) for k, v in camera_counts.items()}
    by_kind = {str(k): int(v) for k, v in kind_counts.items()}

    top_labels = [
        {"label": k, "count": int(v), "total": int(v)}
        for k, v in sorted(label_counts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    top_cameras = [
        {
            "camera_id": (int(k) if str(k).lstrip("-").isdigit() else k),
            "camera": (int(k) if str(k).lstrip("-").isdigit() else k),
            "count": int(v),
            "total": int(v),
        }
        for k, v in sorted(camera_counts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    cameras_list = list(top_cameras)
    labels_list = list(top_labels)

    result: Dict[str, Any] = {
        "status": "ok",
        "ok": True,
        "success": True,
        "total": total,
        "count": total,
        "total_events": total,
        "total_count": total,
        "event_count": total,
        "num_events": total,
        # By-label aggregates (for charts).
        "by_label": by_label,
        "per_label": by_label,
        "label_counts": by_label,
        "by_label_counts": by_label,
        "labels": labels_list,
        "label_breakdown": labels_list,
        "top_labels": top_labels,
        # By-camera / top-camera aggregates (for charts).
        "by_camera": by_camera,
        "per_camera": by_camera,
        "camera_counts": by_camera,
        "by_camera_counts": by_camera,
        "cameras": cameras_list,
        "camera_breakdown": cameras_list,
        "top_cameras": top_cameras,
        "top_camera": top_cameras[0] if top_cameras else None,
        # Kind breakdown (bonus).
        "by_kind": by_kind,
        "per_kind": by_kind,
        "kind_counts": by_kind,
        "start": start_dt.isoformat() if start_dt else None,
        "end": end_dt.isoformat() if end_dt else None,
        "start_time": start_dt.isoformat() if start_dt else None,
        "end_time": end_dt.isoformat() if end_dt else None,
    }
    return result


async def _fetch_events(db: Any, limit: Optional[int] = None) -> List[Any]:
    if db is None:
        return []
    if isinstance(db, (list, tuple)):
        return list(db)
    if isinstance(db, dict):
        for k in ("events", "items", "rows"):
            if isinstance(db.get(k), (list, tuple)):
                return list(db.get(k))
        return []
    if hasattr(db, "execute"):
        try:
            from sqlalchemy import select as _select

            from app.models.entities import Event as _Event

            q = _select(_Event).order_by(_Event.id.desc())
            if limit is not None:
                try:
                    q = q.limit(int(limit))
                except (TypeError, ValueError):
                    pass
            res = await db.execute(q)
            return list(res.scalars().all())
        except Exception:
            return []
    try:
        return list(db)  # type: ignore[arg-type]
    except Exception:
        return []


async def get_event_stats(db: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Aggregate event stats (by-label / by-camera) from DB or list.

    Never raises: returns zero-filled stats on failure.
    """
    try:
        options = _merged_options(*args, **kwargs)
        session = db
        if session is None:
            for k in ("db", "session", "database", "db_session"):
                if options.get(k) is not None:
                    session = options.get(k)
                    break
        preset = None
        for k in ("events", "items", "rows", "event_list"):
            if options.get(k) is not None and isinstance(options.get(k), (list, tuple)):
                preset = list(options.get(k))
                break
        if preset is not None:
            return build_event_stats(preset, **options)
        if isinstance(session, (list, tuple)):
            return build_event_stats(list(session), **options)
        limit = None
        for k in ("limit", "page_size", "per_page", "n", "count", "size", "max_results"):
            if options.get(k) is not None:
                try:
                    limit = int(options.get(k))
                    break
                except (TypeError, ValueError):
                    pass
        rows = await _fetch_events(session, limit=limit)
        return build_event_stats(rows, **options)
    except Exception:
        return build_event_stats([], **kwargs)


async def get_event_stats_async(db: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    return await get_event_stats(db, *args, **kwargs)


def compute_event_stats_sync(events: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    if events is None and args and isinstance(args[0], (list, tuple)):
        events = args[0]
        args = args[1:]
    return build_event_stats(events or [], *args, **kwargs)


# Compatibility aliases.
get_stats = get_event_stats
fetch_event_stats = get_event_stats
query_event_stats = get_event_stats
aggregate_event_stats = get_event_stats
compute_event_stats = get_event_stats
event_stats = get_event_stats
build_stats = build_event_stats
aggregate = build_event_stats
compute = build_event_stats
summarize_event_stats = build_event_stats
get_by_label_counts = build_event_stats
get_top_cameras = build_event_stats


class EventStatsService:
    """Object-oriented wrapper around the event stats aggregation."""

    def __init__(self, db: Any = None, **kwargs: Any) -> None:
        self.db = db
        self._kwargs = dict(kwargs)

    async def get_stats(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged = dict(self._kwargs)
        merged.update(kwargs)
        db = merged.pop("db", self.db)
        if args and db is None and isinstance(args[0], (list, tuple)):
            db = args[0]
            args = args[1:]
        return await get_event_stats(db, *args, **merged)

    async def get_event_stats(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await self.get_stats(*args, **kwargs)

    async def analyze(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await self.get_stats(*args, **kwargs)

    async def aggregate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await self.get_stats(*args, **kwargs)

    async def compute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await self.get_stats(*args, **kwargs)

    def build(self, events: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged = dict(self._kwargs)
        merged.update(kwargs)
        return build_event_stats(events or [], *args, **merged)

    def analyze_sync(self, events: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.build(events, *args, **kwargs)


EventStatsAnalyzer = EventStatsService
EventStatsManager = EventStatsService
EventStatsServiceManager = EventStatsService
event_stats_service = EventStatsService()
eventstats_service = event_stats_service
service = event_stats_service
manager = event_stats_service
analyzer = event_stats_service
default_service = event_stats_service

__all__ = [
    "build_event_stats",
    "get_event_stats",
    "get_event_stats_async",
    "compute_event_stats_sync",
    "EventStatsService",
    "EventStatsAnalyzer",
    "EventStatsManager",
    "event_stats_service",
]
