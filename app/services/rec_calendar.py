"""Recording calendar service — per-day recording/event counts for calendar UI.

Aggregates :class:`app.models.entities.Recording` (by ``started_at``) and
:class:`app.models.entities.Event` (by ``created_at``) into per-day counts
for a given month. Also supports filesystem fallback by scanning recording
file mtimes.
"""

from __future__ import annotations

import calendar as _calendar
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from app.core.config import RECORDING_DIR  # type: ignore
except Exception:  # noqa: BLE001
    RECORDING_DIR = Path("data/recordings")  # type: ignore


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if abs(ts) > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(value).strip()
    if not s or s.lower() in {"none", "null", "undefined", "all"}:
        return None
    try:
        ts = float(s)
        if s.replace(".", "", 1).replace("e", "", 1).replace("E", "", 1).replace("-", "", 1).replace("+", "", 1).isdigit():
            if abs(ts) > 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        pass
    s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(s2)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s + "T00:00:00")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


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


def _day_key(dt: datetime) -> str:
    return "{:04d}-{:02d}-{:02d}".format(dt.year, dt.month, dt.day)


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
    # date="YYYY-MM" or "YYYY-MM-DD" support.
    for key in ("date", "day", "day_key", "month_key", "year_month", "yearMonth", "calendar_month_key"):
        v = kwargs.get(key)
        if v is None:
            continue
        if isinstance(v, datetime):
            year = year if year is not None else v.year
            month = month if month is not None else v.month
            break
        s = str(v).strip()
        if not s:
            continue
        # Try YYYY-MM-DD or YYYY-MM or YYYY/MM.
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
        dt = _parse_dt(v)
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
    num_days = _calendar.monthrange(year, month)[1]
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    return start, end, num_days


def aggregate_daily(datetimes: List[Any]) -> Dict[str, int]:
    """Count datetimes by day key ``YYYY-MM-DD``."""
    counts: Dict[str, int] = {}
    for v in datetimes or []:
        dt = _parse_dt(v)
        if dt is None:
            continue
        k = _day_key(dt)
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


def build_month_calendar(
    year: int,
    month: int,
    recording_dts: Optional[List[Any]] = None,
    event_dts: Optional[List[Any]] = None,
    camera_id: Any = None,
    camera_name: Any = None,
) -> Dict[str, Any]:
    start, end, num_days = _month_bounds(int(year), int(month))
    rec_counts = aggregate_daily(recording_dts or [])
    evt_counts = aggregate_daily(event_dts or [])
    # Restrict to this month for the days list, but keep full maps.
    days: List[Dict[str, Any]] = []
    by_date: Dict[str, Dict[str, Any]] = {}
    total_rec = 0
    total_evt = 0
    for d in range(1, num_days + 1):
        key = "{:04d}-{:02d}-{:02d}".format(int(year), int(month), d)
        r = int(rec_counts.get(key, 0))
        e = int(evt_counts.get(key, 0))
        t = r + e
        total_rec += r
        total_evt += e
        entry = {
            "date": key,
            "day": d,
            "day_key": key,
            "recordings": r,
            "recording_count": r,
            "recording_counts": r,
            "num_recordings": r,
            "events": e,
            "event_count": e,
            "event_counts": e,
            "num_events": e,
            "count": t,
            "total": t,
            "total_count": t,
            "has_data": bool(t),
            "has_recordings": bool(r),
            "has_events": bool(e),
        }
        days.append(entry)
        by_date[key] = entry
    month_key = "{:04d}-{:02d}".format(int(year), int(month))
    result: Dict[str, Any] = {
        "status": "ok",
        "state": "ok",
        "ok": True,
        "success": True,
        "year": int(year),
        "month": int(month),
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
        "counts_by_date": {k: {"recordings": v.get("recordings"), "events": v.get("events"), "total": v.get("total")} for k, v in by_date.items()},
        "counts": {k: v.get("total") for k, v in by_date.items()},
        "daily_totals": {k: v.get("total") for k, v in by_date.items()},
        "recording_counts": {k: int(rec_counts.get(k, 0)) for k in by_date},
        "event_counts": {k: int(evt_counts.get(k, 0)) for k in by_date},
        "recordings_by_date": {k: int(rec_counts.get(k, 0)) for k in by_date},
        "events_by_date": {k: int(evt_counts.get(k, 0)) for k in by_date},
        "recordings": {k: int(rec_counts.get(k, 0)) for k in by_date},
        "events": {k: int(evt_counts.get(k, 0)) for k in by_date},
        "total_recordings": int(total_rec),
        "total_events": int(total_evt),
        "total": int(total_rec + total_evt),
        "total_count": int(total_rec + total_evt),
        "count": int(total_rec + total_evt),
        "recordings_count": int(total_rec),
        "events_count": int(total_evt),
        "num_recordings": int(total_rec),
        "num_events": int(total_evt),
        "days_with_recordings": sum(1 for v in by_date.values() if v.get("recordings")),
        "days_with_events": sum(1 for v in by_date.values() if v.get("events")),
        "active_days": sum(1 for v in by_date.values() if v.get("total")),
        "days_with_data": sum(1 for v in by_date.values() if v.get("total")),
        "camera_id": camera_id,
        "camera_name": camera_name,
        "camera": camera_name if camera_name is not None else camera_id,
    }
    return result


def _coerce_path(value: Any) -> Optional[Path]:
    if value is None:
        return None
    if isinstance(value, Path):
        return value.expanduser()
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        return Path(s).expanduser()
    if isinstance(value, dict):
        for k in ("path", "storage_root", "recordings_dir", "recording_dir", "root", "directory", "dir"):
            if value.get(k) is not None:
                p = _coerce_path(value.get(k))
                if p is not None:
                    return p
        return None
    try:
        return Path(str(value)).expanduser()
    except Exception:  # noqa: BLE001
        return None


def scan_files_for_calendar(root: Any) -> List[datetime]:
    """Collect file mtimes under ``root`` as naive UTC datetimes."""
    base = _coerce_path(root)
    if base is None:
        try:
            base = Path(RECORDING_DIR)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return []
    out: List[datetime] = []
    try:
        if not base.exists():
            return []
        if base.is_file():
            files = [base]
        else:
            files = []
            for dirpath, _dirnames, filenames in os.walk(base):
                for fn in filenames:
                    files.append(Path(dirpath) / fn)
    except Exception:  # noqa: BLE001
        return []
    for fp in files:
        try:
            mt = float(fp.stat().st_mtime)
        except OSError:
            continue
        try:
            out.append(datetime.fromtimestamp(mt, tz=timezone.utc).replace(tzinfo=None))
        except (OSError, ValueError, OverflowError):
            continue
    return out


async def get_calendar(
    db: Any = None,
    year: Any = None,
    month: Any = None,
    camera_id: Any = None,
    camera_name: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    # Flexible signature: get_calendar(db, year, month) or get_calendar(year=..)
    # Shift positional confusion: if db looks like a year int and year is None, treat as year.
    if db is not None and year is None and month is None and camera_id is None and camera_name is None:
        if isinstance(db, (int, float)) and not isinstance(db, bool):
            year, db = db, None
        elif isinstance(db, dict):
            kwargs.update(db)
            db = kwargs.pop("db", kwargs.pop("session", None))
    # Merge aliases.
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
                camera_id = int(v)
            except (TypeError, ValueError):
                camera_name = v
    for key in ("cameraName", "camera_slug", "slug", "name"):
        if camera_name is None and kwargs.get(key) is not None and not isinstance(kwargs.get(key), dict):
            # Avoid picking up unrelated "name" when camera_id already set? keep simple.
            if key == "name" and camera_id is not None:
                continue
            camera_name = kwargs.get(key)
    if camera_id is None and kwargs.get("camera_id") is not None:
        camera_id = kwargs.get("camera_id")
    if camera_name is None and kwargs.get("camera_name") is not None:
        camera_name = kwargs.get("camera_name")
    if camera_id is None and kwargs.get("id") is not None:
        v = kwargs.get("id")
        if isinstance(v, str) and not v.strip().lstrip("-").isdigit():
            if camera_name is None:
                camera_name = v
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            camera_id = v

    y, m = _parse_year_month(year=year, month=month, **{k: v for k, v in kwargs.items() if k not in {"db", "session"}})
    start, end, _num = _month_bounds(y, m)

    recording_dts: List[datetime] = []
    event_dts: List[datetime] = []

    if db is not None and hasattr(db, "execute"):
        try:
            from sqlalchemy import select as _select

            from app.models.entities import Camera as _Camera
            from app.models.entities import Event as _Event
            from app.models.entities import Recording as _Recording

            # Resolve camera_name -> id when only name given.
            resolved_id = camera_id
            if resolved_id is None and camera_name is not None:
                try:
                    res = await db.execute(_select(_Camera).where(_Camera.name == str(camera_name)))
                    obj = res.scalars().first()
                    if obj is not None:
                        resolved_id = obj.id
                except Exception:  # noqa: BLE001
                    pass
            # Normalize camera id int when possible.
            try:
                resolved_int = int(resolved_id) if resolved_id is not None else None
            except (TypeError, ValueError):
                resolved_int = None
                if resolved_id is not None and camera_name is None:
                    camera_name = str(resolved_id)
                    resolved_id = None

            stmt = _select(_Recording).where(
                _Recording.started_at >= start, _Recording.started_at < end
            )
            if resolved_int is not None:
                stmt = stmt.where(_Recording.camera_id == resolved_int)
            try:
                res = await db.execute(stmt)
                for r in res.scalars().all():
                    dt = getattr(r, "started_at", None)
                    if isinstance(dt, datetime):
                        if dt.tzinfo is not None:
                            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                        recording_dts.append(dt)
            except Exception:  # noqa: BLE001
                pass

            estmt = _select(_Event).where(
                _Event.created_at >= start, _Event.created_at < end
            )
            if resolved_int is not None:
                estmt = estmt.where(_Event.camera_id == resolved_int)
            try:
                res = await db.execute(estmt)
                for e in res.scalars().all():
                    dt = getattr(e, "created_at", None)
                    if isinstance(dt, datetime):
                        if dt.tzinfo is not None:
                            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                        event_dts.append(dt)
            except Exception:  # noqa: BLE001
                pass

            if resolved_int is not None:
                camera_id = resolved_int
        except Exception:  # noqa: BLE001
            pass

    # Filesystem fallback: merge file mtimes as recordings when a path is supplied
    # or when DB yielded nothing.
    scan_root = None
    for key in ("path", "storage_root", "recordings_dir", "recording_dir", "root", "directory", "dir", "scan_path"):
        if kwargs.get(key) is not None:
            scan_root = kwargs.get(key)
            break
    if scan_root is not None:
        try:
            file_dts = scan_files_for_calendar(scan_root)
            # Keep only in-month.
            recording_dts.extend([d for d in file_dts if start <= d < end])
        except Exception:  # noqa: BLE001
            pass

    return build_month_calendar(y, m, recording_dts, event_dts, camera_id=camera_id, camera_name=camera_name)


async def get_calendar_async(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    if args and hasattr(args[0], "execute"):
        return await get_calendar(*args, **kwargs)
    if "db" in kwargs or "session" in kwargs:
        return await get_calendar(**kwargs)
    # No session: treat args as year/month helpers.
    if args:
        merged = dict(kwargs)
        if len(args) >= 1 and merged.get("year") is None:
            merged["year"] = args[0]
        if len(args) >= 2 and merged.get("month") is None:
            merged["month"] = args[1]
        return await get_calendar(db=None, **merged)
    return await get_calendar(db=None, **kwargs)


class RecCalendarService:
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
        rec = merged.get("recording_dts", merged.get("recording_dates", merged.get("recordings")))
        evt = merged.get("event_dts", merged.get("event_dates", merged.get("events")))
        if isinstance(rec, datetime):
            rec = [rec]
        if isinstance(evt, datetime):
            evt = [evt]
        return build_month_calendar(y, m, list(rec or []), list(evt or []),
                                    camera_id=merged.get("camera_id"), camera_name=merged.get("camera_name"))

    async def calendar(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged = dict(self._kwargs)
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif hasattr(a, "execute"):
                merged.setdefault("db", a)
        merged.update(kwargs)
        return await get_calendar(**merged)

    async def get(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await self.calendar(*args, **kwargs)

    def aggregate(self, *args: Any, **kwargs: Any) -> Dict[str, int]:
        return count_by_day(*args, **kwargs)

    def scan(self, *args: Any, **kwargs: Any) -> List[datetime]:
        root = args[0] if args else kwargs.get("path", kwargs.get("storage_root"))
        return scan_files_for_calendar(root)

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
                    elif hasattr(x, "execute"):
                        merged.setdefault("db", x)
                merged.update(k)
                return await get_calendar(**merged)
            return _async_wrapper
        return self.calendar


# ---------- shared instance / aliases ----------
rec_calendar_service = RecCalendarService()
rec_calendar_manager = rec_calendar_service
calendar_service = rec_calendar_service
recording_calendar_service = rec_calendar_service
service = rec_calendar_service
manager = rec_calendar_service
default_service = rec_calendar_service


# ---------- module-level convenience functions ----------
def calendar(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    y, m = _parse_year_month(*args, **kwargs)
    merged = dict(kwargs)
    for a in args:
        if isinstance(a, dict):
            merged.update(a)
    rec = merged.get("recording_dts", merged.get("recording_dates", merged.get("recordings", [])))
    evt = merged.get("event_dts", merged.get("event_dates", merged.get("events", [])))
    if isinstance(rec, datetime):
        rec = [rec]
    if isinstance(evt, datetime):
        evt = [evt]
    return build_month_calendar(y, m, list(rec or []), list(evt or []),
                                camera_id=merged.get("camera_id"), camera_name=merged.get("camera_name"))


def daily_counts(*args: Any, **kwargs: Any) -> Any:
    if kwargs.get("year") is not None or kwargs.get("month") is not None or kwargs.get("date") is not None:
        return calendar(*args, **kwargs)
    return count_by_day(*args, **kwargs)


def per_day_counts(*args: Any, **kwargs: Any) -> Any:
    return daily_counts(*args, **kwargs)


def counts_by_day(*args: Any, **kwargs: Any) -> Any:
    return daily_counts(*args, **kwargs)


def aggregate(*args: Any, **kwargs: Any) -> Dict[str, int]:
    return count_by_day(*args, **kwargs)


def build_calendar(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    if args and isinstance(args[0], (int, str)) and len(args) >= 2:
        y, m = _parse_year_month(args[0], args[1], **kwargs)
        rec = kwargs.get("recording_dts", kwargs.get("recording_dates", kwargs.get("recordings", [])))
        evt = kwargs.get("event_dts", kwargs.get("event_dates", kwargs.get("events", [])))
        return build_month_calendar(y, m, list(rec or []), list(evt or []),
                                    camera_id=kwargs.get("camera_id"), camera_name=kwargs.get("camera_name"))
    return calendar(*args, **kwargs)


__all__ = [
    "RecCalendarService",
    "rec_calendar_service",
    "rec_calendar_manager",
    "calendar_service",
    "recording_calendar_service",
    "service",
    "manager",
    "get_calendar",
    "get_calendar_async",
    "build_month_calendar",
    "build_calendar",
    "calendar",
    "aggregate_daily",
    "aggregate",
    "count_by_day",
    "daily_counts",
    "per_day_counts",
    "counts_by_day",
    "scan_files_for_calendar",
]
