"""Time-of-day event search for VigilCam.

Filter events by wall-clock time-of-day window (e.g. events between
02:00-04:00 on any date).

Verified non-overlap (do NOT merge):

* ``app.services.heatmap`` only *aggregates* events into per-hour (0-23)
  buckets for display; it never filters events down to a time-of-day window.
* ``app.services.events`` / ``app.api.events`` filter by absolute datetime
  ranges (``start``/``end`` datetimes); they have no recurring daily
  wall-clock window notion.
* ``app.services.quiet_hours`` gates alerts inside a recurring window; it
  does not search or return events.

Window semantics (closed interval on both ends):

* ``start``/``end`` accept times-of-day: ``"02:00"``, ``"02:00:30"``,
  ``"2am"``, ``"0200"``, bare hours (``2`` / ``"2"``), or full ISO
  datetimes (only the time component is used).
* An event matches when ``start <= tod(event) <= end``, where ``tod`` is
  seconds since midnight derived from the event timestamp.
* Overnight windows wrap: ``start="22:00", end="02:00"`` matches
  ``tod >= 22:00 OR tod <= 02:00``.
* ``start == end`` matches the whole day.
* ``hours`` / ``hour`` (int, ``"2,3,4"``, ``"2-4"``, list) filters by whole
  hours instead of a range.
* Pass ``include_end=False`` (or ``end_exclusive=True``) for half-open
  ``[start, end)`` behaviour.

All public entry points accept flexible ``*args``/``**kwargs`` spellings so
slightly different caller conventions keep working.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

START_KEYS = (
    "start", "start_time", "startTime", "start_tod", "tod_start",
    "from_time", "from", "from_", "window_start", "range_start",
    "begin", "after_time", "start_hour", "hour_start", "startHour",
)

END_KEYS = (
    "end", "end_time", "endTime", "end_tod", "tod_end",
    "to_time", "to", "window_end", "range_end", "finish",
    "before_time", "end_hour", "hour_end", "endHour",
)

HOURS_KEYS = (
    "hours", "hour", "hour_list", "hour_filter", "hours_of_day",
    "tod_hours", "hour_set",
)

RANGE_KEYS = (
    "time_range", "timerange", "range", "window", "between",
    "tod_range", "time_window", "tod_window",
)

CAMERA_KEYS = (
    "camera_id", "camera_ids", "cameras", "camera", "cam",
    "cam_id", "cameraId", "cameraIds",
)

LABEL_KEYS = (
    "label", "labels", "kind", "kinds", "type", "types",
    "object", "objects", "detection_label",
)

LIMIT_KEYS = (
    "limit", "top_k", "k", "n", "count", "size",
    "max_results", "per_page", "page_size", "take",
)

OFFSET_KEYS = (
    "offset", "skip", "start_index", "start_offset",
)

ORDER_KEYS = (
    "order", "sort", "sort_order", "direction", "ordering",
)

EVENT_LIST_KEYS = (
    "events", "event_list", "eventList", "items", "rows",
    "results", "event_rows", "data_events",
)

EVENT_TIME_ATTRS = (
    "created_at", "timestamp", "ts", "time", "occurred_at", "at", "date",
)

_TOD_RE = re.compile(
    r"^(\d{1,2})\s*:\s*(\d{1,2})"
    r"(?:\s*:\s*(\d{1,2})(?:\.(\d{1,6}))?)?"
    r"\s*([ap]\.?m?\.?)?$",
    re.IGNORECASE,
)
_BARE_AMPM_RE = re.compile(r"^(\d{1,2})\s*([ap])\s*\.?\s*m\s*\.?\s*$", re.IGNORECASE)
_HHMM_RE = re.compile(r"^\d{3,4}$")


def _parse_time_string(s: str) -> Optional[float]:
    """Parse ``HH:MM[:SS]`` (optional am/pm) to seconds since midnight."""
    m = _TOD_RE.match(s.strip())
    if not m:
        return None
    try:
        hour = int(m.group(1))
        minute = int(m.group(2))
        second = int(m.group(3)) if m.group(3) is not None else 0
        frac = ("0." + m.group(4)) if m.group(4) else 0.0
        ap = (m.group(5) or "").replace(".", "").lower()
    except (TypeError, ValueError):
        return None
    if hour > 23 or minute > 59 or second > 59:
        # Allow 24:00 as end-of-day alias.
        if hour == 24 and minute == 0 and second == 0 and not frac:
            return 86400.0
        return None
    if ap:
        if ap.startswith("p") and hour < 12:
            hour += 12
        elif ap.startswith("a") and hour == 12:
            hour = 0
    return float(hour * 3600 + minute * 60 + second) + float(frac)


def parse_tod(value: Any) -> Optional[float]:
    """Parse a time-of-day value to seconds since midnight (0..86400)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return float(
            value.hour * 3600 + value.minute * 60 + value.second
        ) + value.microsecond / 1e6
    if isinstance(value, (int, float)):
        v = float(value)
        if 0 <= v <= 23 and v == int(v):
            return float(int(v) * 3600)
        if 0 <= v < 24:
            return v * 3600.0
        if 0 <= v < 1440 and v == int(v):
            return float(int(v) * 60)
        return v % 86400.0
    s = str(value).strip()
    if not s:
        return None
    parsed = _parse_time_string(s)
    if parsed is not None:
        return parsed
    m = _BARE_AMPM_RE.match(s)
    if m:
        try:
            hour = int(m.group(1))
            ap = m.group(2).lower()
        except (TypeError, ValueError):
            return None
        if hour < 1 or hour > 12:
            return None
        if ap == "p" and hour < 12:
            hour += 12
        elif ap == "a" and hour == 12:
            hour = 0
        return float(hour * 3600)
    if _HHMM_RE.match(s):
        iv = int(s)
        if len(s) <= 2:
            return float((iv % 24) * 3600)
        hour, minute = iv // 100, iv % 100
        if minute > 59:
            return None
        return float((hour % 24) * 3600 + minute * 60)
    try:
        return parse_tod(float(s))
    except (TypeError, ValueError):
        pass
    try:
        s2 = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        dt = datetime.fromisoformat(s2)
        return parse_tod(dt)
    except (ValueError, TypeError):
        return None


parse_time_of_day = parse_tod
parse_time = parse_tod
tod_to_seconds = parse_tod


def parse_hour(value: Any) -> Optional[int]:
    """Parse an hour-of-day value to int 0..23, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) % 24
    s = str(value).strip()
    if not s:
        return None
    if ":" in s or s.lower().rstrip().endswith(("am", "pm")):
        tod = parse_tod(s)
        return int(tod // 3600) % 24 if tod is not None else None
    if _HHMM_RE.match(s) and len(s) > 2:
        return (int(s) // 100) % 24
    try:
        return int(float(s)) % 24
    except (TypeError, ValueError):
        return None


def _parse_hour_set(value: Any) -> Optional[List[int]]:
    """Parse ``hours`` filter to a sorted hour list, or None if unusable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        h = parse_hour(value)
        return [h] if h is not None else None
    tokens: List[str] = []
    if isinstance(value, str):
        tokens = re.split(r"[,\s;|/]+", value.strip())
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            if item is None or isinstance(item, bool):
                continue
            tokens.extend(re.split(r"[,\s;|/]+", str(item).strip()))
    else:
        return None
    out: List[int] = []
    for tok in tokens:
        if not tok:
            continue
        if "-" in tok and not tok.startswith("-"):
            lo_s, _, hi_s = tok.partition("-")
            try:
                lo = int(float(lo_s.strip())) % 24
                hi = int(float(hi_s.strip())) % 24
            except (TypeError, ValueError):
                continue
            if lo <= hi:
                rng = range(lo, hi + 1)
            else:  # wrap, e.g. "22-2"
                rng = list(range(lo, 24)) + list(range(0, hi + 1))  # type: ignore[assignment]
            for h in rng:
                if h not in out:
                    out.append(h)
            continue
        h = parse_hour(tok)
        if h is not None and h not in out:
            out.append(h)
    if not out:
        return None
    return sorted(out)


def _split_range(value: Any) -> Optional[Tuple[Any, Any]]:
    """Split a ``"02:00-04:00"`` style range into (start, end) raw parts."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[0], value[1]
    s = str(value).strip()
    if not s:
        return None
    lowered = s.lower()
    for sep in (" to ", "..", "\u2013", "\u2014", ",", ";", "|"):
        if sep in lowered:
            idx = lowered.find(sep)
            left, right = s[:idx].strip(), s[idx + len(sep):].strip()
            if left and right:
                return left, right
    if " - " in s:
        left, right = s.split(" - ", 1)
        return left.strip(), right.strip()
    if s.count("-") == 1 and ":" in s:
        left, right = s.split("-", 1)
        left, right = left.strip(), right.strip()
        if left and right and parse_tod(left) is not None and parse_tod(right) is not None:
            return left, right
    return None


def _event_time(ev: Any) -> Optional[datetime]:
    if isinstance(ev, dict):
        lowered = {str(k).lower(): v for k, v in ev.items()}
        for attr in EVENT_TIME_ATTRS:
            if ev.get(attr) is not None:
                raw = ev.get(attr)
            elif lowered.get(attr) is not None:
                raw = lowered.get(attr)
            else:
                continue
            dt = _coerce_dt(raw)
            if dt is not None:
                return dt
        return None
    for attr in EVENT_TIME_ATTRS:
        raw = getattr(ev, attr, None)
        if raw is not None:
            dt = _coerce_dt(raw)
            if dt is not None:
                return dt
    return None


def _coerce_dt(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, bool):
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
        if not s or s.lower() in {"none", "null", "undefined", "all"}:
            return None
        try:
            num = float(s)
            if s.replace(".", "", 1).replace("e", "", 1).replace("E", "", 1).replace(
                "-", "", 1
            ).replace("+", "", 1).isdigit():
                if abs(num) > 1e12:
                    num = num / 1000.0
                return datetime.fromtimestamp(num, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            pass
        s2 = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        try:
            dt = datetime.fromisoformat(s2)
        except ValueError:
            return None
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def event_tod(ev: Any) -> Optional[float]:
    """Seconds since midnight for an event's timestamp, or None."""
    dt = _event_time(ev)
    if dt is None:
        return None
    return float(dt.hour * 3600 + dt.minute * 60 + dt.second) + dt.microsecond / 1e6


def event_hour(ev: Any) -> Optional[int]:
    """Hour-of-day (0..23) for an event's timestamp, or None."""
    dt = _event_time(ev)
    if dt is None:
        return None
    return int(dt.hour)


event_tod_seconds = event_tod
event_time_of_day = event_tod
tod_of_event = event_tod
hour_of_event = event_hour


def in_window(
    tod: Any,
    start: Any,
    end: Any = None,
    include_start: bool = True,
    include_end: bool = True,
) -> bool:
    """True when time-of-day ``tod`` falls inside ``[start, end]``.

    ``start == end`` matches the whole day. Overnight windows (start > end)
    wrap past midnight.
    """
    t = parse_tod(tod)
    s = parse_tod(start)
    e = parse_tod(end)
    if t is None:
        return False
    if s is None and e is None:
        return True
    if s is None:
        assert e is not None
        return t < e or (bool(include_end) and t == e)
    if e is None:
        return t > s or (bool(include_start) and t == s)
    if s == e:
        return True
    lo_ok = t > s or (bool(include_start) and t == s)
    hi_ok = t < e or (bool(include_end) and t == e)
    if s < e:
        return bool(lo_ok and hi_ok)
    return bool(lo_ok or hi_ok)


matches_window = in_window
in_time_window = in_window
matches_tod = in_window
is_in_window = in_window


def _flatten(value: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if value is None:
        return out
    if isinstance(value, dict):
        for k, v in value.items():
            out[str(k)] = v
            if isinstance(v, dict):
                for kk, vv in v.items():
                    out.setdefault(str(kk), vv)
        return out
    if hasattr(value, "model_dump"):
        try:
            return _flatten(value.model_dump())
        except Exception:
            pass
    return out


def _merge_args(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    time_positionals: List[Any] = []
    for a in args:
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(_flatten(a))
        elif isinstance(a, (list, tuple)):
            items = list(a)
            if items and any(isinstance(x, dict) for x in items):
                data.setdefault("events", items)
            elif "events" in data and data.get("hours") is None and data.get("hour") is None:
                data.setdefault("hours", items)
            else:
                data.setdefault("events", items)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if _split_range(s) is not None and data.get("time_range") is None:
                data.setdefault("time_range", s)
            elif parse_tod(s) is not None:
                time_positionals.append(s)
            elif data.get("label") is None:
                data.setdefault("label", s)
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            time_positionals.append(a)
        elif isinstance(a, datetime):
            time_positionals.append(a)
    data.update(kwargs or {})
    for nested in (
        "payload", "options", "config", "params", "data", "body",
        "request", "filter", "filters", "query_params", "criteria",
    ):
        n = data.get(nested)
        if isinstance(n, dict):
            for k, v in n.items():
                data.setdefault(k, v)
    if time_positionals:
        has_start = any(data.get(k) is not None for k in START_KEYS)
        has_end = any(data.get(k) is not None for k in END_KEYS)
        if not has_start:
            data.setdefault("start", time_positionals[0])
            time_positionals = time_positionals[1:]
        if time_positionals and not has_end:
            data.setdefault("end", time_positionals[0])
    return data


def _first(data: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lowered = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        lk = str(k).lower()
        if lk in lowered and lowered[lk] is not None:
            return lowered[lk]
    return None


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return int(float(str(value).strip())) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "inclusive", "include", "closed"}:
        return True
    if s in {"0", "false", "no", "n", "off", "exclusive", "exclude", "open"}:
        return False
    return default


def _resolve_window(data: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the time-of-day filter to a mode dict."""
    start_raw = _first(data, START_KEYS)
    end_raw = _first(data, END_KEYS)
    if start_raw is None and end_raw is None:
        for rk in RANGE_KEYS:
            if data.get(rk) is not None or _first(data, (rk,)) is not None:
                parts = _split_range(_first(data, (rk,)))
                if parts is not None:
                    start_raw, end_raw = parts
                    break
    hours_raw = _first(data, HOURS_KEYS)
    if start_raw is None and end_raw is None and hours_raw is not None:
        hset = _parse_hour_set(hours_raw)
        if hset is not None:
            return {"mode": "hours", "hours": hset}
    start = parse_tod(start_raw) if start_raw is not None else None
    end = parse_tod(end_raw) if end_raw is not None else None
    if start_raw is not None and start is None:
        raise ValueError(f"Unparseable time-of-day value for start: {start_raw!r}")
    if end_raw is not None and end is None:
        raise ValueError(f"Unparseable time-of-day value for end: {end_raw!r}")
    include_start = _coerce_bool(_first(data, ("include_start", "start_inclusive")), True)
    include_end = _coerce_bool(_first(data, ("include_end", "end_inclusive")), True)
    if _first(data, ("end_exclusive", "exclude_end")) is not None:
        include_end = not _coerce_bool(_first(data, ("end_exclusive", "exclude_end")), False)
    if _first(data, ("start_exclusive", "exclude_start")) is not None:
        include_start = not _coerce_bool(_first(data, ("start_exclusive", "exclude_start")), False)
    if start is None and end is None:
        return {"mode": "none"}
    return {
        "mode": "range",
        "start": start if start is not None else 0.0,
        "end": end if end is not None else 86400.0,
        "start_open": start is None,
        "end_open": end is None,
        "start_raw": start_raw,
        "end_raw": end_raw,
        "include_start": include_start,
        "include_end": include_end,
    }


def _matches_window(tod: float, window: Dict[str, Any]) -> bool:
    mode = window.get("mode", "none")
    if mode == "none":
        return True
    if mode == "hours":
        return int(tod // 3600) % 24 in set(window.get("hours", []))
    s = float(window["start"])
    e = float(window["end"])
    if window.get("start_open") and window.get("end_open"):
        return True
    if window.get("start_open"):
        return tod < e or (window.get("include_end", True) and tod == e)
    if window.get("end_open"):
        return tod > s or (window.get("include_start", True) and tod == s)
    return in_window(tod, s, e, window.get("include_start", True), window.get("include_end", True))


def _event_field(ev: Any, *names: str) -> Any:
    if isinstance(ev, dict):
        for n in names:
            if ev.get(n) is not None:
                return ev.get(n)
        lowered = {str(k).lower(): v for k, v in ev.items()}
        for n in names:
            if lowered.get(n.lower()) is not None:
                return lowered[n.lower()]
        return None
    for n in names:
        if hasattr(ev, n):
            v = getattr(ev, n)
            if v is not None:
                return v
    return None


def _matches_filters(ev: Any, data: Dict[str, Any]) -> bool:
    cam_raw = _first(data, CAMERA_KEYS)
    if cam_raw is not None:
        wanted: List[str] = []
        if isinstance(cam_raw, (list, tuple, set)):
            wanted = [str(x).strip() for x in cam_raw]
        else:
            wanted = [p.strip() for p in str(cam_raw).split(",") if p.strip()]
        cid = _event_field(ev, "camera_id", "cameraId", "camera", "cam", "cam_id")
        if str(cid) not in wanted:
            return False
    lab_raw = _first(data, LABEL_KEYS)
    if lab_raw is not None:
        if isinstance(lab_raw, (list, tuple, set)):
            wanted_labels = [str(x).strip().lower() for x in lab_raw]
        else:
            wanted_labels = [p.strip().lower() for p in str(lab_raw).split(",") if p.strip()]
        lab = _event_field(ev, "label", "kind", "type")
        if lab is None or str(lab).strip().lower() not in wanted_labels:
            return False
    return True


def _event_to_dict(ev: Any) -> Dict[str, Any]:
    if isinstance(ev, dict):
        out = dict(ev)
    else:
        out = {}
        for attr in (
            "id", "camera_id", "kind", "label", "confidence",
            "payload", "tags", "snapshot_path", "created_at",
        ):
            try:
                out[attr] = getattr(ev, attr, None)
            except Exception:
                out[attr] = None
        created = out.get("created_at")
        if created is not None and hasattr(created, "isoformat"):
            try:
                out["created_at"] = created.isoformat()
            except Exception:
                out["created_at"] = str(created)
    tod = event_tod(ev)
    if tod is not None:
        hh = int(tod // 3600) % 24
        mm = int((tod % 3600) // 60)
        ss = int(tod % 60)
        out["hour"] = hh
        out["time_of_day"] = f"{hh:02d}:{mm:02d}:{ss:02d}"
        out["tod"] = f"{hh:02d}:{mm:02d}"
        out["tod_seconds"] = float(tod)
    else:
        out.setdefault("hour", None)
        out.setdefault("time_of_day", None)
        out.setdefault("tod", None)
        out.setdefault("tod_seconds", None)
    return out


def _sort_key(ev: Any) -> Tuple[Any, Any]:
    dt = _event_time(ev)
    eid = _event_field(ev, "id")
    try:
        eid_key = int(eid) if eid is not None else 0
    except (TypeError, ValueError):
        eid_key = 0
    return (dt or datetime.min, eid_key)


def search_events(events: Any = None, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Filter in-memory events to a time-of-day window.

    Examples:
        search_events(events, start="02:00", end="04:00")
        search_events(events, start_hour=2, end_hour=4)
        search_events(events, hours=[2, 3, 4])
        search_events(events, time_range="02:00-04:00")
    """
    data = _merge_args(args, kwargs)
    if events is None:
        for key in EVENT_LIST_KEYS:
            if isinstance(data.get(key), (list, tuple)):
                events = list(data.get(key))
                break
        if events is None:
            for a in args:
                if isinstance(a, (list, tuple)) and any(isinstance(x, dict) for x in a):
                    events = list(a)
                    break
    if events is None:
        events = []
    try:
        event_list = list(events)
    except TypeError:
        event_list = []

    window = _resolve_window(data)
    windowed = window.get("mode", "none") != "none"

    matched: List[Dict[str, Any]] = []
    for ev in event_list:
        tod = event_tod(ev)
        if windowed:
            if tod is None:
                continue
            if not _matches_window(tod, window):
                continue
        if not _matches_filters(ev, data):
            continue
        matched.append(_event_to_dict(ev))

    order_raw = _first(data, ORDER_KEYS)
    order_norm = str(order_raw).strip().lower() if order_raw is not None else ""
    if order_norm in {"asc", "ascending", "chronological", "oldest", "oldest_first"}:
        matched.sort(key=lambda d: (str(d.get("created_at") or ""), d.get("id") or 0))
    elif order_norm in {"desc", "descending", "newest", "latest", "newest_first"}:
        matched.sort(key=lambda d: (str(d.get("created_at") or ""), d.get("id") or 0), reverse=True)

    offset = _coerce_int(_first(data, OFFSET_KEYS), 0) or 0
    if offset < 0:
        offset = 0
    if offset:
        matched = matched[offset:]
    limit = _coerce_int(_first(data, LIMIT_KEYS), None)
    if limit is not None:
        if limit < 0:
            limit = 0
        matched = matched[:limit]
    return matched


filter_events_by_tod = search_events
filter_by_tod = search_events
filter_by_time_of_day = search_events
filter_events_by_time_of_day = search_events
query_by_tod = search_events
find_events_by_tod = search_events
find_by_tod = search_events
events_between = search_events
events_in_window = search_events
events_in_time_window = search_events
search_events_in_window = search_events
search_events_by_tod = search_events
search_events_by_time_of_day = search_events
get_events_by_tod = search_events
query_events_by_tod = search_events
search = search_events
query = search_events
find = search_events


def _without_event_lists(data: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of ``data`` minus in-memory event-list keys (avoids kwarg clash)."""
    return {k: v for k, v in data.items() if k not in EVENT_LIST_KEYS}


async def _fetch_db_events(db: Any, limit: int = 1000) -> List[Any]:
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


async def search_events_async(db: Any = None, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """DB-backed time-of-day search (``db`` may be a session or event list)."""
    data = _merge_args(args, kwargs)
    session = db
    if session is None:
        for k in ("db", "session", "database", "db_session"):
            if data.get(k) is not None:
                session = data.get(k)
                break
    preset = None
    for k in EVENT_LIST_KEYS:
        if isinstance(data.get(k), (list, tuple)):
            preset = list(data.get(k))
            break
    if preset is not None:
        return search_events(preset, **_without_event_lists(data))
    if isinstance(session, (list, tuple)):
        return search_events(list(session), **data)
    fetch_limit = _coerce_int(data.get("fetch_limit"), 1000) or 1000
    rows = await _fetch_db_events(session, limit=fetch_limit)
    return search_events(rows, **data)


search_events_db = search_events_async
search_events_from_db = search_events_async
get_events_by_tod_async = search_events_async
find_events_by_tod_async = search_events_async
query_by_tod_async = search_events_async
filter_events_by_tod_async = search_events_async


class TimeOfDaySearchService:
    """Object-oriented wrapper around the time-of-day search functions."""

    def parse(self, value: Any) -> Optional[float]:
        return parse_tod(value)

    def parse_hour(self, value: Any) -> Optional[int]:
        return parse_hour(value)

    def matches(self, ev: Any, *args: Any, **kwargs: Any) -> bool:
        data = _merge_args(args, kwargs)
        window = _resolve_window(data)
        tod = event_tod(ev)
        if tod is None:
            return window.get("mode", "none") == "none"
        return bool(_matches_window(tod, window))

    def search(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        if args and isinstance(args[0], (list, tuple)):
            return search_events(list(args[0]), *args[1:], **kwargs)
        return search_events(None, *args, **kwargs)

    def filter(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.search(*args, **kwargs)

    async def search_async(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        if args and (hasattr(args[0], "execute") or isinstance(args[0], (list, tuple)) or args[0] is None):
            return await search_events_async(args[0], *args[1:], **kwargs)
        return await search_events_async(None, *args, **kwargs)

    def event_hour(self, ev: Any) -> Optional[int]:
        return event_hour(ev)

    def event_tod(self, ev: Any) -> Optional[float]:
        return event_tod(ev)


ToDSearchService = TimeOfDaySearchService
TodSearchService = TimeOfDaySearchService
TimeOfDaySearch = TimeOfDaySearchService
TODSearch = TimeOfDaySearchService
TimeOfDaySearcher = TimeOfDaySearchService
TimeOfDaySearchEngine = TimeOfDaySearchService
tod_search_service = TimeOfDaySearchService()
time_of_day_search_service = tod_search_service
service = tod_search_service
default_service = tod_search_service


__all__ = [
    "parse_tod",
    "parse_time",
    "parse_time_of_day",
    "parse_hour",
    "event_tod",
    "event_hour",
    "in_window",
    "search_events",
    "search_events_async",
    "TimeOfDaySearchService",
    "tod_search_service",
]
