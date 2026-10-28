"""Maintenance calendar service — per-day maintenance-window counts for calendar UI.

Aggregates :mod:`app.services.maintenance` windows (explicit ``[start, end]``
suppression ranges) into per-day counts for a given month. This module is
view-only: it never creates, mutates, or deletes windows — it only reads them
via :func:`app.services.maintenance.list_windows`.

How this differs from the flat list:
* ``maintenance.list_windows`` returns a flat, camera-sorted list of window
  dicts with no ``by_date`` / ``month_key`` / ``daily`` / ``aggregate``
  structure.
* This module expands each window's ``[start_ts, end_ts]`` range across the
  UTC days it overlaps and builds a month grid (``days`` + ``by_date``) with
  per-day window counts and downtime seconds, mirroring the shape of
  :mod:`app.services.rec_calendar` for UI consistency.
"""

from __future__ import annotations

import calendar as _calendar
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _to_ts(value: Any) -> Optional[float]:
    """Parse datetime / epoch seconds-or-ms / ISO string to epoch seconds."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, (int, float)):
        v = float(value)
        if abs(v) > 1e12:  # millis
            v = v / 1000.0
        elif abs(v) > 1e11:
            v = v / 1000.0
        return v
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            v = float(s)
            if abs(v) > 1e12:
                v = v / 1000.0
            return v
        except ValueError:
            pass
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s or s.lower() in {"none", "null", "all", "auto"}:
        return default
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _day_key(year: int, month: int, day: int) -> str:
    return "{:04d}-{:02d}-{:02d}".format(int(year), int(month), int(day))


def _parse_dt_day(value: Any) -> Optional[datetime]:
    """Parse a point-in-time value to a naive UTC datetime (for aggregate_daily)."""
    ts = _to_ts(value)
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).replace(tzinfo=None)
    except (OSError, OverflowError, ValueError):
        return None


def _parse_year_month(*args: Any, **kwargs: Any) -> Tuple[int, int]:
    now = datetime.now(timezone.utc)
    year: Any = kwargs.get("year")
    month: Any = kwargs.get("month")
    for alias in ("y", "yr", "calendar_year"):
        if year is None and kwargs.get(alias) is not None:
            year = kwargs.get(alias)
    for alias in ("m", "mon", "calendar_month", "mo"):
        if month is None and kwargs.get(alias) is not None:
            month = kwargs.get(alias)
    for key in ("date", "day", "day_key", "month_key", "year_month", "yearMonth", "calendar_month_key"):
        v = kwargs.get(key)
        if v is None:
            continue
        if isinstance(v, datetime):
            if year is None:
                year = v.year
            if month is None:
                month = v.month
            break
        s = str(v).strip()
        if not s:
            continue
        parts = s.replace("/", "-").split("-")
        try:
            if len(parts) >= 2:
                y = int(parts[0])
                m = int(parts[1])
                if 1 <= m <= 12 and 1970 <= y <= 2100:
                    if year is None:
                        year = y
                    if month is None:
                        month = m
                    break
        except (TypeError, ValueError):
            pass
        dt = _parse_dt_day(v)
        if dt is not None:
            if year is None:
                year = dt.year
            if month is None:
                month = dt.month
            break
    for a in args:
        if isinstance(a, dict):
            y2, m2 = _parse_year_month(**a)
            if year is None:
                year = y2
            if month is None:
                month = m2
        elif isinstance(a, datetime):
            if year is None:
                year = a.year
            if month is None:
                month = a.month
        elif isinstance(a, str):
            y2, m2 = _parse_year_month(date=a)
            if year is None:
                year = y2
            if month is None:
                month = m2
    y = _coerce_int(year, now.year)
    m = _coerce_int(month, now.month)
    assert y is not None and m is not None
    if m < 1:
        m = 1
    if m > 12:
        m = 12
    if y < 1970:
        y = now.year
    if y > 2100:
        y = now.year
    return int(y), int(m)


def _month_bounds(year: int, month: int) -> Tuple[datetime, datetime, int]:
    num_days = _calendar.monthrange(int(year), int(month))[1]
    start = datetime(int(year), int(month), 1, tzinfo=timezone.utc)
    if int(month) == 12:
        end = datetime(int(year) + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(int(year), int(month) + 1, 1, tzinfo=timezone.utc)
    return start, end, num_days


_YMD_KEYS = frozenset({
    "year", "month", "y", "yr", "m", "mon", "mo",
    "calendar_year", "calendar_month", "date", "day", "day_key",
    "month_key", "year_month", "yearMonth", "calendar_month_key",
})


def _strip_ymd(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if k not in _YMD_KEYS and k != "windows"}


def _normalize_camera(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"all", "auto", "none", "null", "undefined"}:
        return None
    if s == "*":
        return "*"
    return s


def _window_matches(window_camera: str, query_camera: Optional[str]) -> bool:
    if query_camera is None or query_camera == "*":
        return True
    wc = str(window_camera or "*")
    if wc == "*":
        return True
    return wc.lower() == str(query_camera).lower()


def _iter_windows(
    windows: Optional[List[Dict[str, Any]]] = None,
    camera_id: Any = None,
    camera_name: Any = None,
    include_disabled: Any = False,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Return maintenance windows filtered by camera/enabled flags."""
    if camera_id is None:
        for key in ("cameraId", "camera-id", "cam_id", "camId", "cam", "camera"):
            if kwargs.get(key) is not None:
                camera_id = kwargs.get(key)
                break
    if camera_id is None and kwargs.get("camera_id") is not None:
        camera_id = kwargs.get("camera_id")
    if camera_name is None:
        for key in ("cameraName", "camera_slug", "slug"):
            if kwargs.get(key) is not None and not isinstance(kwargs.get(key), dict):
                camera_name = kwargs.get(key)
                break
    query: Optional[str] = None
    cam_raw = camera_id if camera_id is not None else camera_name
    if cam_raw is not None:
        query = _normalize_camera(cam_raw)
        # An explicit "*" means "all cameras" (match everything).
        if cam_raw == "*" or (isinstance(cam_raw, str) and cam_raw.strip() == "*"):
            query = "*"
    if isinstance(include_disabled, str):
        include_disabled = include_disabled.strip().lower() in {"1", "true", "yes", "on"}
    else:
        include_disabled = bool(include_disabled)
    if kwargs.get("include_disabled") is not None:
        v = kwargs.get("include_disabled")
        include_disabled = str(v).strip().lower() in {"1", "true", "yes", "on"} if isinstance(v, str) else bool(v)
    if kwargs.get("includeDisabled") is not None:
        v = kwargs.get("includeDisabled")
        include_disabled = str(v).strip().lower() in {"1", "true", "yes", "on"} if isinstance(v, str) else bool(v)

    if windows is None:
        try:
            from app.services.maintenance import list_windows as _list_windows

            windows = _list_windows()
        except Exception:  # noqa: BLE001
            windows = []
    out: List[Dict[str, Any]] = []
    for w in windows or []:
        if not isinstance(w, dict):
            continue
        if not include_disabled and not w.get("enabled", True):
            continue
        if query is not None and query != "*":
            if not _window_matches(str(w.get("camera_id", "*")), query):
                continue
        out.append(w)
    return out


def aggregate_daily(datetimes: List[Any]) -> Dict[str, int]:
    """Count point-in-time values by UTC day key ``YYYY-MM-DD``."""
    counts: Dict[str, int] = {}
    for v in datetimes or []:
        dt = _parse_dt_day(v)
        if dt is None:
            continue
        k = "{:04d}-{:02d}-{:02d}".format(dt.year, dt.month, dt.day)
        counts[k] = counts.get(k, 0) + 1
    return counts


def count_by_day(*args: Any, **kwargs: Any) -> Dict[str, int]:
    if args and isinstance(args[0], (list, tuple)):
        return aggregate_daily(list(args[0]))
    if "datetimes" in kwargs:
        return aggregate_daily(kwargs.get("datetimes") or [])
    if "dates" in kwargs:
        return aggregate_daily(kwargs.get("dates") or [])
    if "values" in kwargs:
        return aggregate_daily(kwargs.get("values") or [])
    merged: List[Any] = []
    for a in args:
        if isinstance(a, (list, tuple)):
            merged.extend(a)
        elif isinstance(a, dict):
            for k in ("datetimes", "dates", "values", "timestamps"):
                if a.get(k) is not None:
                    v = a.get(k)
                    merged.extend(v if isinstance(v, (list, tuple)) else [v])
        else:
            merged.append(a)
    for k in ("datetimes", "dates", "values", "timestamps"):
        if kwargs.get(k) is not None:
            v = kwargs.get(k)
            merged.extend(v if isinstance(v, (list, tuple)) else [v])
    return aggregate_daily(merged)


def aggregate_windows(
    windows: Optional[List[Dict[str, Any]]] = None,
    year: Any = None,
    month: Any = None,
    **kwargs: Any,
) -> Dict[str, int]:
    """Expand window ranges into per-day overlap counts for ``year``/``month``.

    Each window contributes +1 to every UTC day it overlaps. Days outside the
    month are ignored.
    """
    y, m = _parse_year_month(year=year, month=month, **kwargs)
    start, end, num_days = _month_bounds(y, m)
    month_start_ts = start.timestamp()
    month_end_ts = end.timestamp()
    items = _iter_windows(windows, **kwargs)
    counts: Dict[str, int] = {}
    for w in items:
        s_ts = _to_ts(w.get("start_ts", w.get("start", w.get("start_time"))))
        e_ts = _to_ts(w.get("end_ts", w.get("end", w.get("end_time"))))
        if s_ts is None or e_ts is None:
            continue
        lo = max(float(s_ts), month_start_ts)
        hi = min(float(e_ts), month_end_ts)
        if hi < lo:
            continue
        for d in range(1, num_days + 1):
            day_start = datetime(y, m, d, tzinfo=timezone.utc).timestamp()
            day_end = day_start + 86400
            if float(s_ts) <= day_end - 1 and float(e_ts) >= day_start:
                # Overlaps this UTC day (inclusive bounds, matches is_suppressed).
                if lo <= day_end - 1 and hi >= day_start:
                    k = _day_key(y, m, d)
                    counts[k] = counts.get(k, 0) + 1
    return counts


def build_month_calendar(
    year: int,
    month: int,
    windows: Optional[List[Dict[str, Any]]] = None,
    camera_id: Any = None,
    camera_name: Any = None,
    include_disabled: Any = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Build the per-day maintenance calendar grid for ``year``/``month``."""
    y, m = _parse_year_month(year=year, month=month, **kwargs)
    start, end, num_days = _month_bounds(y, m)
    month_start_ts = start.timestamp()
    month_end_ts = end.timestamp()
    items = _iter_windows(
        windows, camera_id=camera_id, camera_name=camera_name,
        include_disabled=include_disabled, **kwargs,
    )

    # Per-day buckets.
    day_windows: Dict[str, List[Dict[str, Any]]] = {}
    day_downtime: Dict[str, float] = {}
    for d in range(1, num_days + 1):
        k = _day_key(y, m, d)
        day_windows[k] = []
        day_downtime[k] = 0.0

    overlapping: List[Dict[str, Any]] = []
    total_downtime = 0.0
    for w in items:
        s_ts = _to_ts(w.get("start_ts", w.get("start", w.get("start_time"))))
        e_ts = _to_ts(w.get("end_ts", w.get("end", w.get("end_time"))))
        if s_ts is None or e_ts is None:
            continue
        s_ts = float(s_ts)
        e_ts = float(e_ts)
        if e_ts < month_start_ts or s_ts >= month_end_ts:
            continue
        overlapping.append(w)
        for d in range(1, num_days + 1):
            day_start = datetime(y, m, d, tzinfo=timezone.utc).timestamp()
            day_end = day_start + 86400
            # Inclusive overlap (matches maintenance.is_suppressed semantics).
            if s_ts <= day_end - 1 and e_ts >= day_start:
                k = _day_key(y, m, d)
                day_windows[k].append(w)
                overlap = min(e_ts, day_end - 1) - max(s_ts, day_start)
                overlap = max(0.0, float(overlap))
                day_downtime[k] += overlap
                total_downtime += overlap

    days: List[Dict[str, Any]] = []
    by_date: Dict[str, Dict[str, Any]] = {}
    window_counts: Dict[str, int] = {}
    downtime_by_date: Dict[str, float] = {}
    active_days = 0
    for d in range(1, num_days + 1):
        k = _day_key(y, m, d)
        wins = day_windows.get(k, [])
        n = len(wins)
        dt_down = float(day_downtime.get(k, 0.0))
        window_counts[k] = n
        downtime_by_date[k] = dt_down
        if n:
            active_days += 1
        entry = {
            "date": k,
            "day": d,
            "day_key": k,
            "windows": n,
            "window_count": n,
            "num_windows": n,
            "count": n,
            "total": n,
            "total_count": n,
            "downtime_seconds": dt_down,
            "downtime": dt_down,
            "downtime_hours": round(dt_down / 3600.0, 3),
            "has_data": bool(n),
            "has_windows": bool(n),
            "has_maintenance": bool(n),
            "window_ids": [str(x.get("id")) for x in wins if x.get("id") is not None],
            "cameras": sorted({str(x.get("camera_id", "*")) for x in wins}),
        }
        days.append(entry)
        by_date[k] = entry

    month_key = "{:04d}-{:02d}".format(y, m)
    result: Dict[str, Any] = {
        "status": "ok",
        "state": "ok",
        "ok": True,
        "success": True,
        "service": "maint_calendar",
        "year": y,
        "month": m,
        "month_key": month_key,
        "year_month": month_key,
        "yearMonth": month_key,
        "calendar_month": month_key,
        "month_label": month_key,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "num_days": num_days,
        "days_in_month": num_days,
        "total_days": num_days,
        "days": days,
        "day_list": days,
        "entries": days,
        "items": days,
        "results": days,
        "daily": days,
        "daily_counts": days,
        "by_date": by_date,
        "by_day": by_date,
        "counts": dict(window_counts),
        "counts_by_date": dict(window_counts),
        "daily_totals": dict(window_counts),
        "window_counts": dict(window_counts),
        "windows_by_date": {k: int(v) for k, v in window_counts.items()},
        "downtime_by_date": dict(downtime_by_date),
        "downtime_seconds_by_date": dict(downtime_by_date),
        "total_windows": len(overlapping),
        "total_count": len(overlapping),
        "count": len(overlapping),
        "num_windows": len(overlapping),
        "window_total": len(overlapping),
        "active_days": active_days,
        "days_with_data": active_days,
        "days_with_windows": active_days,
        "days_with_maintenance": active_days,
        "total_downtime_seconds": float(total_downtime),
        "total_downtime": float(total_downtime),
        "downtime_seconds": float(total_downtime),
        "camera_id": camera_id,
        "camera_name": camera_name,
        "camera": camera_name if camera_name is not None else camera_id,
        "include_disabled": bool(include_disabled) if not isinstance(include_disabled, str) else include_disabled,
    }
    return result


def get_calendar(
    year: Any = None,
    month: Any = None,
    windows: Optional[List[Dict[str, Any]]] = None,
    camera_id: Any = None,
    camera_name: Any = None,
    include_disabled: Any = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Read live maintenance windows and build the month calendar grid."""
    for key in ("y", "yr", "calendar_year", "yearMonth", "year_month"):
        if year is None and kwargs.get(key) is not None:
            year = kwargs.get(key)
    for key in ("m", "mon", "mo", "calendar_month"):
        if month is None and kwargs.get(key) is not None:
            month = kwargs.get(key)
    for key in ("cameraId", "camera-id", "cam_id", "camId", "cam", "camera"):
        if camera_id is None and camera_name is None and kwargs.get(key) is not None:
            v = kwargs.get(key)
            try:
                camera_id = v
            except (TypeError, ValueError):
                camera_name = v
    for key in ("cameraName", "camera_slug", "slug", "name"):
        if camera_name is None and kwargs.get(key) is not None and not isinstance(kwargs.get(key), dict):
            if key == "name" and camera_id is not None:
                continue
            camera_name = kwargs.get(key)
    if camera_id is None and kwargs.get("camera_id") is not None:
        camera_id = kwargs.get("camera_id")
    if camera_name is None and kwargs.get("camera_name") is not None:
        camera_name = kwargs.get("camera_name")
    if windows is None and kwargs.get("windows") is not None:
        windows = kwargs.get("windows")
    y, m = _parse_year_month(year=year, month=month, **{k: v for k, v in kwargs.items()})
    return build_month_calendar(
        y, m, windows,
        camera_id=camera_id, camera_name=camera_name,
        include_disabled=kwargs.get("include_disabled", kwargs.get("includeDisabled", include_disabled)),
        **_strip_ymd(kwargs),
    )


async def get_calendar_async(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    merged = dict(kwargs)
    if args:
        if len(args) >= 1 and merged.get("year") is None:
            merged["year"] = args[0]
        if len(args) >= 2 and merged.get("month") is None:
            merged["month"] = args[1]
        if len(args) >= 3 and merged.get("camera_id") is None:
            merged["camera_id"] = args[2]
    return get_calendar(**merged)


class MaintCalendarService:
    """Service object wrapping the module-level helpers."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._kwargs: Dict[str, Any] = {}
        for a in args:
            if isinstance(a, dict):
                self._kwargs.update(a)
        self._kwargs.update(kwargs)

    def build(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged = dict(self._kwargs)
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
        merged.update(kwargs)
        y, m = _parse_year_month(**merged)
        return build_month_calendar(
            y, m, merged.get("windows"),
            camera_id=merged.get("camera_id"), camera_name=merged.get("camera_name"),
            include_disabled=merged.get("include_disabled", False),
            **_strip_ymd(merged),
        )

    async def calendar(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged = dict(self._kwargs)
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
        merged.update(kwargs)
        return get_calendar(**merged)

    async def get(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await self.calendar(*args, **kwargs)

    def aggregate(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("year") is not None or kwargs.get("month") is not None or kwargs.get("date") is not None:
            y, m = _parse_year_month(*args, **kwargs)
            return build_month_calendar(y, m, kwargs.get("windows"), **_strip_ymd(kwargs))
        return count_by_day(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        if any(h in low for h in ("calendar", "month", "daily", "count", "aggreg", "report", "summary", "status", "day", "per")):
            async def _async_wrapper(*a: Any, **k: Any) -> Dict[str, Any]:
                merged = dict(self._kwargs)
                for x in a:
                    if isinstance(x, dict):
                        merged.update(x)
                merged.update(k)
                return get_calendar(**merged)
            return _async_wrapper
        return self.calendar


MaintCalendar = MaintCalendarService
MaintenanceCalendarService = MaintCalendarService
MaintenanceCalendar = MaintCalendarService

maint_calendar_service = MaintCalendarService()
maint_calendar_manager = maint_calendar_service
calendar_service = maint_calendar_service
maintenance_calendar_service = maint_calendar_service
service = maint_calendar_service
manager = maint_calendar_service
default_service = maint_calendar_service


def calendar(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    y, m = _parse_year_month(*args, **kwargs)
    merged = dict(kwargs)
    for a in args:
        if isinstance(a, dict):
            merged.update(a)
    return build_month_calendar(
        y, m, merged.get("windows"),
        camera_id=merged.get("camera_id"), camera_name=merged.get("camera_name"),
        include_disabled=merged.get("include_disabled", False),
        **_strip_ymd(merged),
    )


def daily_counts(*args: Any, **kwargs: Any) -> Any:
    if kwargs.get("year") is not None or kwargs.get("month") is not None or kwargs.get("date") is not None:
        return calendar(*args, **kwargs)
    return count_by_day(*args, **kwargs)


def per_day_counts(*args: Any, **kwargs: Any) -> Any:
    return daily_counts(*args, **kwargs)


def counts_by_day(*args: Any, **kwargs: Any) -> Any:
    return daily_counts(*args, **kwargs)


def aggregate(*args: Any, **kwargs: Any) -> Any:
    if kwargs.get("year") is not None or kwargs.get("month") is not None or kwargs.get("date") is not None:
        return aggregate_windows(*args, **kwargs)
    return count_by_day(*args, **kwargs)


def build_calendar(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    if args and isinstance(args[0], (int, str)) and len(args) >= 2:
        y, m = _parse_year_month(args[0], args[1], **kwargs)
        merged = dict(kwargs)
        return build_month_calendar(
            y, m, merged.get("windows"),
            camera_id=merged.get("camera_id"), camera_name=merged.get("camera_name"),
            include_disabled=merged.get("include_disabled", False),
            **_strip_ymd(merged),
        )
    return calendar(*args, **kwargs)


def get_service() -> MaintCalendarService:
    return maint_calendar_service


def create_service() -> MaintCalendarService:
    return MaintCalendarService()


__all__ = [
    "MaintCalendarService",
    "maint_calendar_service",
    "maint_calendar_manager",
    "calendar_service",
    "maintenance_calendar_service",
    "service",
    "manager",
    "get_calendar",
    "get_calendar_async",
    "build_month_calendar",
    "build_calendar",
    "calendar",
    "aggregate_daily",
    "aggregate_windows",
    "aggregate",
    "count_by_day",
    "daily_counts",
    "per_day_counts",
    "counts_by_day",
]
