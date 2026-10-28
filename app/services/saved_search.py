"""Saved-search execution service (saved filter -> events).

Combines :mod:`app.services.saved_filters` (named, reusable event queries)
with the event store to actually *run* a saved filter and return matching
events. This is intentionally distinct from
``GET /api/saved-filters/{id}/query`` (which only echoes stored criteria):
this module applies the criteria to events.

Two execution modes:

* in-memory: pass ``events=[...]`` (list of dicts or ORM ``Event``
  objects). Pure-python matching, no DB required. Used by unit tests and
  by the API when the caller supplies an explicit event list.
* database: pass ``db`` (an ``AsyncSession``). Delegates to
  :func:`app.services.events._filter_events` so DB semantics match
  ``GET /api/events`` exactly.

A small "scheduled runner" helper (:func:`run_all`) executes several (or
all) saved filters in one call and records per-filter last-run metadata,
so interval-based runners can poll ``get_last_run`` without extra state.
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.services import saved_filters as _saved_filters

# ---------------------------------------------------------------------------
# alias sets (mirror app/api/events.py + app/services/saved_filters.py)
# ---------------------------------------------------------------------------

CAMERA_KEYS = (
    "camera_id", "camera_ids", "cameras", "camera", "cam",
    "camera_id_list", "camera_name", "camera_names",
)
LABEL_KEYS = (
    "label", "labels", "detection_label", "detection_labels",
    "object", "objects", "object_label", "class", "classes",
    "kind", "kinds", "type", "types",
)
TAG_KEYS = ("tag", "tags", "event_tag", "event_tags", "tag_name", "tag_names")
MIN_CONF_KEYS = (
    "min_confidence", "confidence_min", "min_score", "score_min",
    "min_conf", "confidence_minimum", "min_confidence_score",
    "confidence_threshold", "min_probability", "probability_min",
)
MAX_CONF_KEYS = (
    "max_confidence", "confidence_max", "max_score", "score_max",
    "max_conf", "confidence_maximum", "max_probability", "probability_max",
)
START_KEYS = (
    "start", "start_time", "start_date", "from", "from_", "from_time",
    "begin", "after", "since", "min_time", "lower_bound", "range_start",
    "after_time", "from_date",
)
END_KEYS = (
    "end", "end_time", "end_date", "to", "to_time", "until", "before",
    "max", "max_time", "upper", "upper_bound", "finish", "range_end",
    "before_time", "to_date",
)
ORDER_KEYS = ("order", "sort", "sort_order", "direction", "ordering", "sort_by")
LIMIT_KEYS = ("limit", "page_size", "per_page", "n", "count", "size", "max_results")
OFFSET_KEYS = ("offset", "skip", "start_index", "start_offset")

ID_KEYS = ("filter_id", "filterId", "saved_filter_id", "savedFilterId",
           "id", "uuid", "key", "filter", "saved_filter", "savedFilter",
           "filter_name", "filterName", "name")

_lock = threading.Lock()
_LAST_RUNS: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# small coercion helpers
# ---------------------------------------------------------------------------

def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).strip()
    if not s or s.lower() in {"none", "null", "undefined", "all"}:
        return default
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s.lower() in {"none", "null", "undefined", "all"}:
        return default
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


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
        if not s or s.lower() in {"none", "null", "undefined", "all"}:
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


def _normalize_tags(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        for key in ("tags", "tag", "values", "items", "labels", "names"):
            if key in value and value.get(key) is not None:
                return _normalize_tags(value.get(key))
        return []
    if isinstance(value, str):
        try:
            import json as _json
            parsed = _json.loads(value)
            if isinstance(parsed, list):
                value = parsed
            elif parsed is not None and not isinstance(parsed, dict):
                return [str(parsed)]
            else:
                value = [p.strip() for p in value.split(",") if p.strip()]
        except Exception:
            value = [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, (tuple, set)):
        value = list(value)
    if not isinstance(value, list):
        return [str(value)] if value is not None else []
    out: List[str] = []
    seen = set()
    for item in value:
        s = str(item).strip() if item is not None else ""
        if not s or s.lower() in {"none", "null"}:
            continue
        if s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
    return out


def _field(event: Any, *names: str, default: Any = None) -> Any:
    """Read a field from a dict or an ORM object (case-insensitive for dicts)."""
    if isinstance(event, dict):
        for n in names:
            if n in event and event.get(n) is not None:
                return event.get(n)
        lower = {str(k).lower(): v for k, v in event.items()}
        for n in names:
            if n.lower() in lower and lower[n.lower()] is not None:
                return lower[n.lower()]
        return default
    for n in names:
        if hasattr(event, n):
            try:
                v = getattr(event, n)
            except Exception:
                continue
            if v is not None:
                return v
    return default


def _event_created_at(event: Any) -> Optional[datetime]:
    raw = _field(event, "created_at", "createdAt", "timestamp", "time", "date", default=None)
    dt = _parse_dt(raw)
    if dt is not None:
        return dt
    # ORM Event always has datetime; dicts may already hold one.
    if isinstance(raw, datetime):
        d = raw
        if getattr(d, "tzinfo", None) is not None:
            d = d.astimezone(timezone.utc).replace(tzinfo=None)
        return d
    return None


def _event_to_dict(event: Any) -> Dict[str, Any]:
    if isinstance(event, dict):
        out = dict(event)
        tags = _normalize_tags(out.get("tags", out.get("tag")))
        out["tags"] = tags
        out["tag"] = tags
        ca = out.get("created_at")
        if isinstance(ca, datetime):
            out["created_at"] = ca.isoformat()
        return out
    tags = _normalize_tags(getattr(event, "tags", None))
    created = getattr(event, "created_at", None)
    return {
        "id": getattr(event, "id", None),
        "camera_id": getattr(event, "camera_id", None),
        "kind": getattr(event, "kind", None),
        "label": getattr(event, "label", None),
        "confidence": getattr(event, "confidence", None),
        "payload": getattr(event, "payload", None),
        "tags": tags,
        "tag": tags,
        "snapshot_path": getattr(event, "snapshot_path", None),
        "created_at": created.isoformat() if isinstance(created, datetime) else created,
    }


# ---------------------------------------------------------------------------
# criteria resolution
# ---------------------------------------------------------------------------

def _extract_filter_ref(*args: Any, **kwargs: Any) -> Any:
    if args:
        first = args[0]
        if isinstance(first, (int, str)) and not isinstance(first, bool):
            return first
        if isinstance(first, dict):
            for k in ID_KEYS:
                if k in first and first.get(k) is not None:
                    return first.get(k)
            lower = {str(k).lower(): v for k, v in first.items()}
            for k in ID_KEYS:
                if k.lower() in lower and lower[k.lower()] is not None:
                    return lower[k.lower()]
    for k in ID_KEYS:
        if k in kwargs and kwargs.get(k) is not None:
            return kwargs.get(k)
    # case-insensitive fallback
    lower_kw = {str(k).lower(): v for k, v in kwargs.items()}
    for k in ID_KEYS:
        if k.lower() in lower_kw and lower_kw[k.lower()] is not None:
            return lower_kw[k.lower()]
    return None


def resolve_criteria(filter_ref: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Return the stored event-query criteria for a saved filter.

    ``filter_ref`` may be an id, a name, or a dict carrying one. Extra
    ``kwargs`` are treated as execution-time overrides merged over the
    stored criteria (``None`` values do not override).
    """
    ref = filter_ref
    if ref is None:
        ref = _extract_filter_ref(*args, **kwargs)
    if isinstance(ref, dict):
        # Caller passed the filter record itself (or a criteria dict).
        crit = ref.get("criteria") or ref.get("filters") or ref.get("query") or {}
        if isinstance(crit, dict) and crit:
            base = copy.deepcopy(crit)
        else:
            base = {k: copy.deepcopy(v) for k, v in ref.items()
                    if k not in {"id", "name", "description", "created_at", "updated_at"}}
    else:
        if ref is None:
            raise ValueError("filter_id (or name) is required")
        record = _saved_filters.get_filter(ref)
        if record is None:
            raise LookupError(f"Saved filter not found: {ref!r}")
        base = copy.deepcopy(record.get("criteria") or {})
    overrides = dict(kwargs)
    for a in args:
        if isinstance(a, dict):
            # A trailing dict arg is overrides (unless it was the filter ref).
            if a is not filter_ref:
                for k, v in a.items():
                    if k not in ID_KEYS and k.lower() not in {x.lower() for x in ID_KEYS}:
                        overrides.setdefault(k, v)
    for k, v in overrides.items():
        if v is None:
            continue
        if k in ID_KEYS or k.lower() in {x.lower() for x in ID_KEYS}:
            continue
        if isinstance(v, str) and v.strip().lower() in {"", "all", "none", "null"} and k not in LIMIT_KEYS + OFFSET_KEYS + ORDER_KEYS:
            continue
        base[k] = v
    return base


def get_execution_criteria(filter_ref: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    return resolve_criteria(filter_ref, *args, **kwargs)


# ---------------------------------------------------------------------------
# in-memory matching
# ---------------------------------------------------------------------------

def _camera_id_set(criteria: Dict[str, Any]) -> set:
    ids: set = set()
    # case-insensitive lookup
    lower = {str(k).lower(): v for k, v in criteria.items()}
    for key in CAMERA_KEYS:
        vals = []
        if key in criteria and criteria.get(key) is not None:
            vals.append(criteria.get(key))
        elif key.lower() in lower and lower[key.lower()] is not None:
            vals.append(lower[key.lower()])
        for v in vals:
            if isinstance(v, (list, tuple, set)):
                for item in v:
                    iv = _coerce_int(item, None)
                    if iv is not None:
                        ids.add(iv)
                    elif isinstance(item, str):
                        for part in item.split(","):
                            iv2 = _coerce_int(part.strip(), None)
                            if iv2 is not None:
                                ids.add(iv2)
            else:
                if isinstance(v, str) and "," in v:
                    for part in v.split(","):
                        iv = _coerce_int(part.strip(), None)
                        if iv is not None:
                            ids.add(iv)
                else:
                    iv = _coerce_int(v, None)
                    if iv is not None:
                        ids.add(iv)
    return ids


def _label_set(criteria: Dict[str, Any]) -> set:
    out = set()
    lower = {str(k).lower(): v for k, v in criteria.items()}
    for key in LABEL_KEYS:
        vals = []
        if key in criteria and criteria.get(key) is not None:
            vals.append(criteria.get(key))
        elif key.lower() in lower and lower[key.lower()] is not None:
            vals.append(lower[key.lower()])
        for v in vals:
            items = v if isinstance(v, (list, tuple, set)) else [v]
            for item in items:
                if item is None:
                    continue
                for part in str(item).split(","):
                    p = part.strip()
                    if p and p.lower() not in {"all", "none", "null"}:
                        out.add(p.lower())
    return out


def _tag_set(criteria: Dict[str, Any]) -> set:
    out = set()
    lower = {str(k).lower(): v for k, v in criteria.items()}
    for key in TAG_KEYS:
        vals = []
        if key in criteria and criteria.get(key) is not None:
            vals.append(criteria.get(key))
        elif key.lower() in lower and lower[key.lower()] is not None:
            vals.append(lower[key.lower()])
        for v in vals:
            for t in _normalize_tags(v):
                out.add(t.lower())
    return out


def _first_criteria(criteria: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in criteria and criteria.get(k) is not None:
            return criteria.get(k)
    lower = {str(k).lower(): v for k, v in criteria.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def match_event(event: Any, criteria: Dict[str, Any]) -> bool:
    """Return True when a single event satisfies ``criteria``."""
    criteria = criteria or {}
    cam_ids = _camera_id_set(criteria)
    if cam_ids:
        cid = _coerce_int(_field(event, "camera_id", "cameraId", "camera", "cam"), None)
        if cid is None or cid not in cam_ids:
            return False
    labels = _label_set(criteria)
    if labels:
        ev_label = _field(event, "label", "detection_label", "object", default=None)
        ev_kind = _field(event, "kind", "type", "object_label", "class", default=None)
        cand = set()
        if ev_label is not None:
            cand.add(str(ev_label).strip().lower())
        if ev_kind is not None:
            cand.add(str(ev_kind).strip().lower())
        if not (cand & labels):
            return False
    wanted_tags = _tag_set(criteria)
    if wanted_tags:
        ev_tags = {t.lower() for t in _normalize_tags(_field(event, "tags", "tag", "event_tags", default=None))}
        if not (ev_tags & wanted_tags):
            return False
    min_conf = _coerce_float(_first_criteria(criteria, MIN_CONF_KEYS), None)
    if min_conf is not None:
        conf = _coerce_float(_field(event, "confidence", "score", default=None), None)
        if conf is None or conf < min_conf:
            return False
    max_conf = _coerce_float(_first_criteria(criteria, MAX_CONF_KEYS), None)
    if max_conf is not None:
        conf = _coerce_float(_field(event, "confidence", "score", default=None), None)
        if conf is None or conf > max_conf:
            return False
    start_dt = _parse_dt(_first_criteria(criteria, START_KEYS))
    end_dt = _parse_dt(_first_criteria(criteria, END_KEYS))
    if start_dt is not None or end_dt is not None:
        ca = _event_created_at(event)
        if ca is not None:
            if start_dt is not None and ca < start_dt:
                return False
            if end_dt is not None and ca > end_dt:
                return False
    return True


def _sort_key(event: Any):
    ca = _event_created_at(event)
    eid = _coerce_int(_field(event, "id", default=0), 0) or 0
    return (ca or datetime.min, eid)


def filter_event_list(events: List[Any], criteria: Dict[str, Any]) -> List[Any]:
    """Apply criteria matching + ordering + paging to an in-memory list."""
    criteria = dict(criteria or {})
    matched = [e for e in (events or []) if match_event(e, criteria)]
    order_raw = str(_first_criteria(criteria, ORDER_KEYS) or "").strip().lower()
    if order_raw in {"asc", "ascending", "chronological", "oldest", "oldest_first"}:
        matched.sort(key=_sort_key)
    else:
        matched.sort(key=_sort_key, reverse=True)
    offset = _coerce_int(_first_criteria(criteria, OFFSET_KEYS), 0) or 0
    if offset < 0:
        offset = 0
    limit = _coerce_int(_first_criteria(criteria, LIMIT_KEYS), None)
    if offset:
        matched = matched[offset:]
    if limit is not None and limit >= 0:
        matched = matched[:limit]
    return matched


# ---------------------------------------------------------------------------
# execution entrypoints
# ---------------------------------------------------------------------------

def execute(filter_ref: Any = None, *args: Any, events: Optional[List[Any]] = None,
            db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Synchronously execute a saved filter against in-memory events.

    Returns an envelope ``{"filter", "criteria", "count", "events",
    "results"}``. For DB-backed execution use :func:`execute_async`.
    """
    db = db if db is not None else session
    if db is not None:
        raise ValueError("sync execute() does not accept db; use execute_async()")
    ref = filter_ref
    if ref is None and args and isinstance(args[0], (int, str, dict)):
        ref, args = args[0], args[1:]
    overrides: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            overrides.update(a)
    overrides.update(kwargs)
    events_list = overrides.pop("events", events)
    if events_list is None:
        events_list = []
    criteria = resolve_criteria(ref, **{k: v for k, v in overrides.items()})
    matched = filter_event_list(list(events_list), criteria)
    record: Optional[Dict[str, Any]] = None
    try:
        record = _saved_filters.get_filter(ref) if not isinstance(ref, dict) else ref
    except Exception:
        record = None
    payload = {
        "filter": copy.deepcopy(record) if isinstance(record, dict) else record,
        "criteria": copy.deepcopy(criteria),
        "query": copy.deepcopy(criteria),
        "params": copy.deepcopy(criteria),
        "count": len(matched),
        "total": len(matched),
        "events": [_event_to_dict(e) for e in matched],
        "results": [_event_to_dict(e) for e in matched],
    }
    _record_run(ref, len(matched))
    return payload


async def execute_async(filter_ref: Any = None, *args: Any, events: Optional[List[Any]] = None,
                        db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Execute a saved filter, preferring the DB when a session is given."""
    db = db if db is not None else session
    ref = filter_ref
    if ref is None and args and isinstance(args[0], (int, str, dict)):
        ref, args = args[0], args[1:]
    overrides: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            overrides.update(a)
    overrides.update(kwargs)
    events_override = overrides.pop("events", events)
    if events_override is not None and db is None:
        return execute(ref, events=list(events_override), **overrides)
    criteria = resolve_criteria(ref, **{k: v for k, v in overrides.items() if k != "db" and k != "session"})
    record: Optional[Dict[str, Any]] = None
    try:
        record = _saved_filters.get_filter(ref) if not isinstance(ref, dict) else ref
    except Exception:
        record = None
    matched_dicts: List[Dict[str, Any]]
    if db is not None:
        from app.services.events import _filter_events as _db_filter_events
        rows = await _db_filter_events(db, **criteria)
        matched_dicts = [_event_to_dict(r) for r in rows]
    elif events_override is not None:
        matched = filter_event_list(list(events_override), criteria)
        matched_dicts = [_event_to_dict(e) for e in matched]
    else:
        matched_dicts = []
    payload = {
        "filter": copy.deepcopy(record) if isinstance(record, dict) else record,
        "criteria": copy.deepcopy(criteria),
        "query": copy.deepcopy(criteria),
        "params": copy.deepcopy(criteria),
        "count": len(matched_dicts),
        "total": len(matched_dicts),
        "events": matched_dicts,
        "results": matched_dicts,
    }
    _record_run(ref, len(matched_dicts))
    return payload


def _record_run(filter_ref: Any, count: int) -> None:
    try:
        key = str(filter_ref if not isinstance(filter_ref, dict)
                  else filter_ref.get("id", filter_ref.get("name", "?")))
    except Exception:
        key = "?"
    with _lock:
        _LAST_RUNS[key] = {
            "filter": key,
            "count": int(count),
            "ran_at": datetime.now(timezone.utc).isoformat(),
        }


def get_last_run(filter_ref: Any = None) -> Optional[Dict[str, Any]]:
    try:
        key = str(filter_ref)
    except Exception:
        return None
    with _lock:
        item = _LAST_RUNS.get(key)
        return copy.deepcopy(item) if item is not None else None


def clear_runs() -> int:
    with _lock:
        n = len(_LAST_RUNS)
        _LAST_RUNS.clear()
        return n


def run_all(filter_refs: Any = None, *args: Any, events: Optional[List[Any]] = None,
            db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Synchronously run several saved filters (scheduled-runner helper)."""
    db = db if db is not None else session
    if db is not None:
        raise ValueError("sync run_all() does not accept db; use run_all_async()")
    refs: List[Any]
    if filter_refs is None:
        refs = [f.get("id") for f in _saved_filters.list_filters()]
    elif isinstance(filter_refs, (list, tuple, set)):
        refs = list(filter_refs)
    else:
        refs = [filter_refs, *[a for a in args if isinstance(a, (int, str, dict))]]
    out: Dict[str, Any] = {"runs": {}, "counts": {}, "total": 0}
    for ref in refs:
        try:
            res = execute(ref, events=events, **kwargs)
            key = str(ref if not isinstance(ref, dict) else ref.get("id", ref.get("name")))
            out["runs"][key] = res
            out["counts"][key] = res["count"]
            out["total"] += res["count"]
        except (LookupError, ValueError) as exc:
            out["runs"][str(ref)] = {"error": str(exc), "count": 0, "events": []}
            out["counts"][str(ref)] = 0
    return out


async def run_all_async(filter_refs: Any = None, *args: Any, events: Optional[List[Any]] = None,
                        db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Async variant of :func:`run_all` (DB-backed when ``db`` is given)."""
    db = db if db is not None else session
    if filter_refs is None:
        try:
            refs = [f.get("id") for f in _saved_filters.list_filters()]
        except Exception:
            refs = []
    elif isinstance(filter_refs, (list, tuple, set)):
        refs = list(filter_refs)
    else:
        refs = [filter_refs, *[a for a in args if isinstance(a, (int, str, dict))]]
    out: Dict[str, Any] = {"runs": {}, "counts": {}, "total": 0}
    for ref in refs:
        try:
            res = await execute_async(ref, events=events, db=db, **kwargs)
            key = str(ref if not isinstance(ref, dict) else ref.get("id", ref.get("name")))
            out["runs"][key] = res
            out["counts"][key] = res["count"]
            out["total"] += res["count"]
        except (LookupError, ValueError) as exc:
            out["runs"][str(ref)] = {"error": str(exc), "count": 0, "events": []}
            out["counts"][str(ref)] = 0
    return out


class SavedSearchService:
    """Object-oriented facade over the module-level helpers."""

    def criteria(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return resolve_criteria(*args, **kwargs)

    def match(self, event: Any, criteria: Dict[str, Any]) -> bool:
        return match_event(event, criteria)

    def filter(self, events: List[Any], criteria: Dict[str, Any]) -> List[Any]:
        return filter_event_list(events, criteria)

    def execute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return execute(*args, **kwargs)

    async def execute_async(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await execute_async(*args, **kwargs)

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return execute(*args, **kwargs)

    async def run_async(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await execute_async(*args, **kwargs)

    def run_all(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return run_all(*args, **kwargs)

    async def run_all_async(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await run_all_async(*args, **kwargs)

    def last_run(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_last_run(*args, **kwargs)


SavedSearchRunner = SavedSearchService
SavedSearchExecutor = SavedSearchService

saved_search_service = SavedSearchService()
saved_search_runner = saved_search_service
service = saved_search_service
manager = saved_search_service
runner = saved_search_service


# Compatibility aliases (function level).
execute_saved_search = execute
execute_saved_filter = execute
run_saved_search = execute
run_saved_filter = execute
run_filter = execute
run_search = execute
apply_and_execute = execute
execute_filter = execute
search_with_saved_filter = execute
execute_async_saved_search = execute_async
run_saved_search_async = execute_async
execute_saved_filter_async = execute_async
run_all_saved_searches = run_all
run_all_saved_searches_async = run_all_async
execute_all = run_all
execute_all_async = run_all_async

__all__ = [
    "resolve_criteria",
    "get_execution_criteria",
    "match_event",
    "filter_event_list",
    "execute",
    "execute_async",
    "run_all",
    "run_all_async",
    "get_last_run",
    "clear_runs",
    "SavedSearchService",
    "saved_search_service",
]
