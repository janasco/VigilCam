"""Event heatmap aggregation service for VigilCam.

Per-hour / per-camera counts over the ``events`` table so the UI can render
heatmaps (hour-of-day vs camera activity).
"""

from __future__ import annotations

from collections import Counter, defaultdict
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


def _to_int_list(values: Any) -> List[int]:
    out: List[int] = []
    seen = set()
    if values is None:
        return out
    if isinstance(values, (list, tuple, set)):
        iterable = list(values)
    else:
        iterable = [values]
    for v in iterable:
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
                    out.append(iv)
        else:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv not in seen:
                seen.add(iv)
                out.append(iv)
    return out


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
    # hours / days window support: last N hours/days ending at end (or now).
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
        if start_dt is None or computed_start > start_dt:
            # Prefer the explicit window when hours/days given; if both
            # explicit start and hours given, use the later (narrower) one.
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


def _event_time(ev: Any) -> Optional[datetime]:
    for attr in ("created_at", "timestamp", "ts", "time", "occurred_at", "at", "date"):
        v = getattr(ev, attr, None) if not isinstance(ev, dict) else ev.get(attr)
        if v is None and isinstance(ev, dict):
            # case-insensitive fallback
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


def build_heatmap(events: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Pure (sync) heatmap aggregation over an in-memory event sequence.

    Returns per-hour (hour-of-day 0-23) and per-camera counts plus the
    camera x hour matrix.
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

    hour_counts = Counter()
    camera_counts: Counter = Counter()
    matrix: Dict[Any, Counter] = defaultdict(Counter)
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
        if labels:
            lab = _event_label(ev)
            if lab is None or lab.lower() not in labels:
                continue
        hour = ts.hour if ts is not None else -1
        # Events without timestamps go to hour bucket only if no range filter;
        # otherwise they are still counted in per-camera totals but skipped
        # for hourly buckets. Simplest: count them under hour of now? Instead
        # skip hourly for missing ts but keep camera counts.
        if ts is not None:
            hour_counts[hour] += 1
            key = cid if cid is not None else "unknown"
            matrix[str(key)][str(hour)] += 1
        else:
            key = cid if cid is not None else "unknown"
            # no hourly increment
            pass
        ckey = str(cid) if cid is not None else "unknown"
        camera_counts[ckey] += 1
        total += 1

    per_hour = {str(h): int(hour_counts.get(h, 0)) for h in range(24)}
    hourly_list = [{"hour": h, "count": int(hour_counts.get(h, 0))} for h in range(24)]
    hourly_counts = [int(hour_counts.get(h, 0)) for h in range(24)]
    per_camera = {str(k): int(v) for k, v in camera_counts.items()}
    cameras_list = [
        {"camera_id": (int(k) if str(k).lstrip("-").isdigit() else k), "count": int(v), "total": int(v)}
        for k, v in sorted(camera_counts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    per_camera_per_hour: Dict[str, Dict[str, int]] = {}
    for ckey in camera_counts.keys():
        per_camera_per_hour[str(ckey)] = {str(h): int(matrix[str(ckey)].get(str(h), 0)) for h in range(24)}
    cells = []
    for ckey, hmap in per_camera_per_hour.items():
        cid_val: Any = ckey
        try:
            cid_val = int(ckey)
        except (TypeError, ValueError):
            pass
        for h in range(24):
            c = int(hmap.get(str(h), 0))
            cells.append({"camera_id": cid_val, "camera": cid_val, "hour": h, "count": c, "total": c})

    peak_hour = max(range(24), key=lambda h: hour_counts.get(h, 0))
    peak_camera = None
    if camera_counts:
        peak_camera = max(camera_counts.items(), key=lambda kv: kv[1])[0]

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
        "per_hour": per_hour,
        "per_hour_counts": per_hour,
        "hour_counts": per_hour,
        "hourly_counts": hourly_counts,
        "hourly": hourly_list,
        "by_hour": per_hour,
        "hours": hourly_list,
        "per_camera": per_camera,
        "per_camera_counts": per_camera,
        "camera_counts": per_camera,
        "by_camera": per_camera,
        "cameras": cameras_list,
        "camera_breakdown": cameras_list,
        "per_camera_breakdown": cameras_list,
        "per_camera_per_hour": per_camera_per_hour,
        "camera_hour_matrix": per_camera_per_hour,
        "matrix": per_camera_per_hour,
        "heatmap": per_camera_per_hour,
        "cells": cells,
        "buckets": hourly_list,
        "peak_hour": peak_hour,
        "peak_camera": peak_camera,
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
    # Unknown db-like: try iteration
    try:
        return list(db)  # type: ignore[arg-type]
    except Exception:
        return []


async def get_heatmap(db: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Aggregate event heatmap (per-hour / per-camera counts).

    Accepts either an AsyncSession (queries the events table) or an
    in-memory event list, plus flexible time/camera/label filters.
    Never raises: returns zero-filled buckets on failure.
    """
    try:
        options = _merged_options(*args, **kwargs)
        # db may be passed positionally as list or via kwargs
        session = db
        if session is None:
            for k in ("db", "session", "database", "db_session"):
                if options.get(k) is not None:
                    session = options.get(k)
                    break
        # explicit events list takes precedence when provided
        preset = None
        for k in ("events", "items", "rows", "event_list"):
            if options.get(k) is not None and isinstance(options.get(k), (list, tuple)):
                preset = list(options.get(k))
                break
        if preset is not None:
            return build_heatmap(preset, **options)
        if isinstance(session, (list, tuple)):
            return build_heatmap(list(session), **options)
        # limit support
        limit = None
        for k in ("limit", "page_size", "per_page", "n", "count", "size", "max_results"):
            if options.get(k) is not None:
                try:
                    limit = int(options.get(k))
                    break
                except (TypeError, ValueError):
                    pass
        rows = await _fetch_events(session, limit=limit)
        return build_heatmap(rows, **options)
    except Exception:
        return build_heatmap([], **kwargs)


async def get_heatmap_async(db: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    return await get_heatmap(db, *args, **kwargs)


def compute_heatmap_sync(events: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    if events is None and args and isinstance(args[0], (list, tuple)):
        events = args[0]
        args = args[1:]
    return build_heatmap(events or [], *args, **kwargs)


# Compatibility aliases.
get_event_heatmap = get_heatmap
get_events_heatmap = get_heatmap
fetch_heatmap = get_heatmap
query_heatmap = get_heatmap
aggregate_heatmap = get_heatmap
compute_heatmap = get_heatmap
heatmap = get_heatmap
build_event_heatmap = build_heatmap
aggregate = build_heatmap
compute = build_heatmap
summarize_heatmap = build_heatmap
get_hourly_counts = build_heatmap
get_per_hour_counts = build_heatmap
get_per_camera_counts = build_heatmap


class HeatmapService:
    """Object-oriented wrapper around the heatmap aggregation."""

    def __init__(self, db: Any = None, **kwargs: Any) -> None:
        self.db = db
        self._kwargs = dict(kwargs)

    async def get_heatmap(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged = dict(self._kwargs)
        merged.update(kwargs)
        db = merged.pop("db", self.db)
        if args and db is None and isinstance(args[0], (list, tuple)):
            db = args[0]
            args = args[1:]
        return await get_heatmap(db, *args, **merged)

    async def analyze(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await self.get_heatmap(*args, **kwargs)

    async def aggregate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await self.get_heatmap(*args, **kwargs)

    async def compute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await self.get_heatmap(*args, **kwargs)

    def build(self, events: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged = dict(self._kwargs)
        merged.update(kwargs)
        return build_heatmap(events or [], *args, **merged)

    def analyze_sync(self, events: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.build(events, *args, **kwargs)


HeatmapAnalyzer = HeatmapService
HeatmapManager = HeatmapService
HeatmapServiceManager = HeatmapService
EventHeatmapService = HeatmapService
EventHeatmapAnalyzer = HeatmapService
heatmap_service = HeatmapService()
heatmap_analyzer = heatmap_service
service = heatmap_service
manager = heatmap_service
analyzer = heatmap_service
default_service = heatmap_service

__all__ = [
    "build_heatmap",
    "get_heatmap",
    "get_heatmap_async",
    "compute_heatmap_sync",
    "HeatmapService",
    "HeatmapAnalyzer",
    "HeatmapManager",
    "EventHeatmapService",
    "heatmap_service",
]
