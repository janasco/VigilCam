"""Saved event filter service (in-memory CRUD for named event queries).

A saved event filter is a named, reusable set of event-list criteria
(camera, label/kind, tags, confidence bounds, time range, ordering, ...).
The store is intentionally in-memory (thread-safe) so the feature works
without DB migrations and without requiring ``app/main.py`` edits. An
optional ``db``/``session`` argument is accepted by the async entrypoints
for forward-compatibility but is currently ignored (best-effort no-op).
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
_FILTERS: Dict[int, Dict[str, Any]] = {}
_ID_COUNTER = 0

NAME_KEYS = (
    "name",
    "filter_name",
    "filterName",
    "saved_filter_name",
    "savedFilterName",
    "title",
    "display_name",
    "displayName",
    "label",
)
ID_KEYS = (
    "id",
    "filter_id",
    "filterId",
    "saved_filter_id",
    "savedFilterId",
    "saved_filterId",
    "key",
    "uuid",
)
DESCRIPTION_KEYS = (
    "description",
    "desc",
    "detail",
    "details",
    "notes",
    "note",
    "comment",
    "summary",
)
CRITERIA_KEYS = (
    "criteria",
    "criterion",
    "filter",
    "filters",
    "query",
    "params",
    "conditions",
    "spec",
    "definition",
    "config",
    "options",
)

# Event-query keys that, when supplied top-level, are mirrored into criteria.
EVENT_FILTER_KEYS = (
    "camera_id",
    "camera_ids",
    "cameras",
    "camera",
    "cam",
    "label",
    "labels",
    "kind",
    "kinds",
    "type",
    "types",
    "tag",
    "tags",
    "event_tag",
    "event_tags",
    "min_confidence",
    "confidence_min",
    "min_score",
    "max_confidence",
    "confidence_max",
    "max_score",
    "start",
    "start_time",
    "start_date",
    "from",
    "from_time",
    "after",
    "since",
    "end",
    "end_time",
    "end_date",
    "to",
    "to_time",
    "until",
    "before",
    "order",
    "sort",
    "limit",
    "offset",
    "skip",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_id() -> int:
    global _ID_COUNTER
    _ID_COUNTER += 1
    return _ID_COUNTER


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
        for nested_key in CRITERIA_KEYS + ("payload", "data", "body", "request", "fields", "entry", "saved_filter", "savedFilter"):
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    try:
        return dict(data)
    except Exception:
        return {}


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    for a in args:
        if a is None:
            continue
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if "name" not in flat:
                flat["name"] = s
            else:
                flat.setdefault("description", s)
        else:
            try:
                flat.update(dict(a))
            except Exception:
                pass
    flat.update(kwargs or {})
    for nested_key in ("saved_filter", "savedFilter", "saved-filter", "payload", "params", "data", "body", "request", "fields", "entry"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    return flat


def _extract_criteria(flat: Dict[str, Any]) -> Dict[str, Any]:
    criteria: Dict[str, Any] = {}
    for key in CRITERIA_KEYS:
        val = flat.get(key)
        if isinstance(val, dict):
            for k, v in val.items():
                criteria.setdefault(k, v)
    # Also fold nested criteria one level deep (e.g. {"criteria": {"filters": {...}}}).
    changed = True
    while changed:
        changed = False
        for key in CRITERIA_KEYS:
            nested = criteria.get(key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    if k not in criteria:
                        criteria[k] = v
                        changed = True
    # Mirror known event-filter keys found top-level.
    for key in EVENT_FILTER_KEYS:
        if key in flat and flat.get(key) is not None and key not in criteria:
            criteria[key] = flat.get(key)
        # case-insensitive fallback
        if key not in criteria:
            for fk, fv in flat.items():
                if str(fk).lower() == key.lower() and fv is not None and key not in criteria:
                    criteria[key] = fv
                    break
    return criteria


def _to_public(record: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(record)


def _build_record(flat: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = dict(existing) if existing else {}
    name = _first_present(flat, NAME_KEYS)
    description = _first_present(flat, DESCRIPTION_KEYS)
    criteria = _extract_criteria(flat)
    # Explicit criteria dicts in flat win over mirrored keys.
    for key in CRITERIA_KEYS:
        val = flat.get(key)
        if isinstance(val, dict):
            criteria.update({k: v for k, v in val.items() if not isinstance(v, dict) or k not in CRITERIA_KEYS})
    now = _now_iso()
    if existing is None:
        fid = _next_id()
        record: Dict[str, Any] = {
            "id": fid,
            "filter_id": fid,
            "saved_filter_id": fid,
            "filterId": fid,
            "savedFilterId": fid,
            "name": str(name).strip() if name is not None else "",
            "filter_name": str(name).strip() if name is not None else "",
            "filterName": str(name).strip() if name is not None else "",
            "title": str(flat.get("title", name)).strip() if name is not None else str(flat.get("title", "")),
            "label": str(flat.get("label", name)).strip() if name is not None and "label" in flat else (str(name).strip() if name is not None else ""),
            "description": str(description).strip() if description is not None else str(flat.get("description", "")),
            "criteria": copy.deepcopy(criteria),
            "filters": copy.deepcopy(criteria),
            "query": copy.deepcopy(criteria),
            "params": copy.deepcopy(criteria),
            "created_at": now,
            "updated_at": now,
        }
        # Mirror criteria keys top-level for easy filtering.
        for k, v in criteria.items():
            record.setdefault(k, copy.deepcopy(v))
        # Preserve extra caller keys.
        for k, v in flat.items():
            if k not in record:
                record[k] = copy.deepcopy(v)
        # Keep criteria aliases in sync after extras.
        record["criteria"] = copy.deepcopy(criteria)
        record["filters"] = copy.deepcopy(criteria)
        record["query"] = copy.deepcopy(criteria)
        record["params"] = copy.deepcopy(criteria)
        return record
    # Update path: mutate a copy.
    record = copy.deepcopy(base)
    if name is not None:
        s = str(name).strip()
        if s:
            record["name"] = s
            record["filter_name"] = s
            record["filterName"] = s
            if "title" in flat or record.get("title") in (None, ""):
                record["title"] = str(flat.get("title", s))
    if description is not None:
        record["description"] = str(description)
    if criteria:
        merged = dict(record.get("criteria") or {})
        merged.update(criteria)
        record["criteria"] = copy.deepcopy(merged)
        record["filters"] = copy.deepcopy(merged)
        record["query"] = copy.deepcopy(merged)
        record["params"] = copy.deepcopy(merged)
        for k, v in merged.items():
            record[k] = copy.deepcopy(v)
    for k, v in flat.items():
        if k.lower() in {x.lower() for x in ID_KEYS}:
            continue
        if k in NAME_KEYS + DESCRIPTION_KEYS + CRITERIA_KEYS:
            continue
        if k in ("payload", "params", "data", "body", "request", "fields", "entry", "saved_filter", "savedFilter", "criteria", "filters", "query"):
            continue
        record[k] = copy.deepcopy(v)
    record["updated_at"] = _now_iso()
    # Re-sync criteria aliases in case criteria keys were patched directly.
    merged = dict(record.get("criteria") or {})
    for k in EVENT_FILTER_KEYS:
        if k in flat and flat.get(k) is not None:
            merged[k] = flat.get(k)
    if merged != (record.get("criteria") or {}):
        record["criteria"] = copy.deepcopy(merged)
        record["filters"] = copy.deepcopy(merged)
        record["query"] = copy.deepcopy(merged)
        record["params"] = copy.deepcopy(merged)
    return record


def create_filter(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create a saved event filter. ``name`` is required.

    Accepts flexible inputs, e.g.::

        create_filter(name="motion only", criteria={"label": "motion"})
        create_filter({"name": "all", "camera_id": 1})
    """
    flat = _normalize_inputs(*args, **kwargs)
    name = _first_present(flat, NAME_KEYS)
    if name is None or not str(name).strip():
        raise ValueError("name is required")
    with _lock:
        record = _build_record(flat, None)
        _FILTERS[int(record["id"])] = record
        return _to_public(record)


def list_filters(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """List saved filters, optionally filtered by name/query."""
    flat = _flatten(args[0]) if len(args) == 1 and isinstance(args[0], dict) else {}
    for a in args:
        if isinstance(a, str) and "q" not in flat and "name" not in flat:
            flat["q"] = a
    flat.update(kwargs or {})
    q = _first_present(flat, ("q", "query", "search", "filter", "text_search", "name_search"))
    name = _first_present(flat, ("name", "filter_name", "filterName", "title"))
    camera_id = _first_present(flat, ("camera_id", "cameraId", "camera", "cam"))
    limit = _coerce_int(_first_present(flat, ("limit", "n", "count", "size", "page_size")), None)
    offset = _coerce_int(_first_present(flat, ("offset", "skip", "start_index")), 0) or 0
    with _lock:
        items = [_to_public(_FILTERS[k]) for k in sorted(_FILTERS.keys())]
    if name is not None and str(name).strip():
        ns = str(name).strip().lower()
        items = [f for f in items if ns == str(f.get("name", "")).strip().lower()]
    if q is not None and str(q).strip():
        qs = str(q).strip().lower()
        items = [f for f in items if qs in str(f.get("name", "")).lower() or qs in str(f.get("description", "")).lower()]
    if camera_id is not None and str(camera_id).strip() != "":
        cs = str(camera_id).strip()
        def _matches(f: Dict[str, Any]) -> bool:
            crit = f.get("criteria") or {}
            for key in ("camera_id", "camera_ids", "cameras", "camera", "cam"):
                val = f.get(key, crit.get(key))
                if val is None:
                    continue
                if isinstance(val, (list, tuple, set)):
                    if any(str(v) == cs for v in val):
                        return True
                elif str(val) == cs:
                    return True
            return False
        items = [f for f in items if _matches(f)]
    if offset:
        items = items[offset:]
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items


def get_filter(filter_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if filter_id is None:
        filter_id = _first_present(flat, ID_KEYS)
    if filter_id is None and args:
        for a in args:
            if isinstance(a, (int, float, str)) and not isinstance(a, bool):
                filter_id = a
                break
    if filter_id is None:
        # Fall back to name lookup.
        name = _first_present(flat, NAME_KEYS)
        if name is not None:
            return get_filter_by_name(name)
        return None
    # Try integer id first.
    nid = _coerce_int(filter_id, None)
    with _lock:
        if nid is not None and int(nid) in _FILTERS:
            return _to_public(_FILTERS[int(nid)])
        # String id (uuid-style) or name fallback.
        s = str(filter_id).strip()
        for record in _FILTERS.values():
            if str(record.get("id")) == s or str(record.get("filter_id")) == s:
                return _to_public(record)
        lowered = s.lower()
        for record in _FILTERS.values():
            if str(record.get("name", "")).strip().lower() == lowered:
                return _to_public(record)
    return None


def get_filter_by_name(name: Any) -> Optional[Dict[str, Any]]:
    if name is None:
        return None
    s = str(name).strip().lower()
    if not s:
        return None
    with _lock:
        for record in _FILTERS.values():
            if str(record.get("name", "")).strip().lower() == s:
                return _to_public(record)
    return None


def update_filter(filter_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
        elif isinstance(a, str):
            s = a.strip()
            if s and "name" not in flat and filter_id is None:
                # Bare string with no id yet: treat as id only if it matches existing.
                flat.setdefault("_positional", s)
    flat.update(kwargs or {})
    if filter_id is None:
        filter_id = _first_present(flat, ID_KEYS)
    positional = flat.pop("_positional", None)
    if filter_id is None and positional is not None:
        # Could be an id or a new name; prefer id lookup.
        existing = get_filter(positional)
        if existing is not None:
            filter_id = existing["id"]
        else:
            return None
    if filter_id is None and args:
        for a in args:
            if isinstance(a, (int, float)) and not isinstance(a, bool):
                filter_id = a
                break
    nid = _coerce_int(filter_id, None)
    with _lock:
        target_id: Optional[int] = None
        if nid is not None and int(nid) in _FILTERS:
            target_id = int(nid)
        elif filter_id is not None:
            s = str(filter_id).strip()
            for k, record in _FILTERS.items():
                if str(record.get("id")) == s or str(record.get("name", "")).strip().lower() == s.lower():
                    target_id = k
                    break
        if target_id is None:
            return None
        updated = _build_record(flat, _FILTERS[target_id])
        # Keep canonical id fields stable.
        updated["id"] = target_id
        updated["filter_id"] = target_id
        updated["saved_filter_id"] = target_id
        _FILTERS[target_id] = updated
        return _to_public(updated)


def delete_filter(filter_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    if filter_id is None:
        filter_id = _first_present(flat, ID_KEYS)
    if filter_id is None and args:
        for a in args:
            if isinstance(a, (int, float, str)) and not isinstance(a, bool):
                filter_id = a
                break
    nid = _coerce_int(filter_id, None)
    with _lock:
        if nid is not None and int(nid) in _FILTERS:
            _FILTERS.pop(int(nid), None)
            return True
        if filter_id is not None:
            s = str(filter_id).strip()
            for k, record in list(_FILTERS.items()):
                if str(record.get("id")) == s or str(record.get("name", "")).strip().lower() == s.lower():
                    _FILTERS.pop(k, None)
                    return True
        return False


def count_filters(*args: Any, **kwargs: Any) -> int:
    return len(list_filters(*args, **kwargs))


def clear(*args: Any, **kwargs: Any) -> int:
    with _lock:
        n = len(_FILTERS)
        _FILTERS.clear()
        return n


def get_filter_query(filter_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return the stored event-query criteria for a saved filter."""
    record = get_filter(filter_id, *args, **kwargs)
    if record is None:
        return None
    criteria = record.get("criteria")
    if isinstance(criteria, dict):
        return copy.deepcopy(criteria)
    return {}


# --- Async entrypoints (db-optional, ignored) --------------------------------

async def _maybe_await(value: Any) -> Any:
    return value


async def create_filter_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    kwargs.pop("db", None)
    kwargs.pop("session", None)
    return create_filter(*args, **kwargs)


async def list_filters_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_filters(*args, **kwargs)


async def get_filter_async(filter_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_filter(filter_id, *args, **kwargs)


async def update_filter_async(filter_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return update_filter(filter_id, *args, **kwargs)


async def delete_filter_async(filter_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> bool:
    return delete_filter(filter_id, *args, **kwargs)


class SavedFiltersService:
    """Object-oriented facade over the module-level store."""

    def create(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_filter(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_filters(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_filter(*args, **kwargs)

    def update(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return update_filter(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return delete_filter(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    def count(self, *args: Any, **kwargs: Any) -> int:
        return count_filters(*args, **kwargs)

    def query(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_filter_query(*args, **kwargs)

    # aliases
    create_filter = create
    add_filter = create
    save_filter = create
    list_filters = list
    get_filters = list
    get_filter = get
    update_filter = update
    delete_filter = delete
    clear_filters = clear


SavedFilterService = SavedFiltersService
EventFilterService = SavedFiltersService
EventFiltersService = SavedFiltersService

saved_filters_service = SavedFiltersService()
saved_filter_service = saved_filters_service
event_filter_service = saved_filters_service
event_filters_service = saved_filters_service
service = saved_filters_service
manager = saved_filters_service
store = saved_filters_service


# Compatibility aliases (function level).
create_saved_filter = create_filter
add_saved_filter = create_filter
save_saved_filter = create_filter
new_filter = create_filter
post_filter = create_filter
create_event_filter = create_filter
add_event_filter = create_filter
save_event_filter = create_filter
create_event_saved_filter = create_filter

list_saved_filters = list_filters
get_saved_filters = list_filters
fetch_filters = list_filters
fetch_saved_filters = list_filters
list_event_filters = list_filters
get_event_filters = list_filters
query_filters = list_filters
search_filters = list_filters
all_filters = list_filters

get_saved_filter = get_filter
fetch_filter = get_filter
fetch_saved_filter = get_filter
find_filter = get_filter
get_event_filter = get_filter
get_by_name = get_filter_by_name
get_saved_filter_by_name = get_filter_by_name

edit_filter = update_filter
update_saved_filter = update_filter
edit_saved_filter = update_filter
patch_filter = update_filter
update_event_filter = update_filter

remove_filter = delete_filter
delete_saved_filter = delete_filter
remove_saved_filter = delete_filter
delete_event_filter = delete_filter

clear_filters = clear
clear_saved_filters = clear
clear_event_filters = clear
reset = clear
purge = clear

to_query = get_filter_query
apply_filter = get_filter_query
apply_saved_filter = get_filter_query
filter_query = get_filter_query

__all__ = [
    "create_filter",
    "list_filters",
    "get_filter",
    "get_filter_by_name",
    "update_filter",
    "delete_filter",
    "count_filters",
    "clear",
    "get_filter_query",
    "create_filter_async",
    "list_filters_async",
    "get_filter_async",
    "update_filter_async",
    "delete_filter_async",
    "SavedFiltersService",
    "saved_filters_service",
]
