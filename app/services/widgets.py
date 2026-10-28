"""Dashboard widgets config service (in-memory, per-user widget layout).

Stores per-user dashboard widget configurations as ordered lists of widget
dicts. The store is intentionally in-memory (thread-safe) so the feature
works without DB migrations and without requiring ``app/main.py`` edits.
An optional ``db``/``session`` argument is accepted by the async entrypoints
for forward-compatibility but is currently ignored (best-effort no-op).
"""

from __future__ import annotations

import copy
import threading
import uuid
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
_WIDGETS: Dict[str, List[Dict[str, Any]]] = {}

DEFAULT_USER_ID = "default"

SUPPORTED_WIDGET_TYPES = (
    "cameras",
    "events",
    "recordings",
    "system",
    "storage",
    "heatmap",
    "alerts",
    "map",
    "timeline",
    "stats",
    "live",
    "snapshots",
    "health",
    "activity",
    "custom",
)

DEFAULT_WIDGETS: List[Dict[str, Any]] = [
    {
        "id": "cameras",
        "type": "cameras",
        "title": "Cameras",
        "enabled": True,
        "x": 0,
        "y": 0,
        "w": 6,
        "h": 4,
        "config": {},
    },
    {
        "id": "events",
        "type": "events",
        "title": "Recent Events",
        "enabled": True,
        "x": 6,
        "y": 0,
        "w": 6,
        "h": 4,
        "config": {},
    },
    {
        "id": "system",
        "type": "system",
        "title": "System Status",
        "enabled": True,
        "x": 0,
        "y": 4,
        "w": 4,
        "h": 3,
        "config": {},
    },
    {
        "id": "storage",
        "type": "storage",
        "title": "Storage",
        "enabled": True,
        "x": 4,
        "y": 4,
        "w": 4,
        "h": 3,
        "config": {},
    },
    {
        "id": "heatmap",
        "type": "heatmap",
        "title": "Activity Heatmap",
        "enabled": False,
        "x": 8,
        "y": 4,
        "w": 4,
        "h": 3,
        "config": {},
    },
]

WIDGET_ID_KEYS = ("id", "widget_id", "widgetId", "key", "name", "slug")

WIDGET_TYPE_KEYS = ("type", "widget_type", "widgetType", "kind", "widget")

WIDGET_NESTED_KEYS = (
    "widgets",
    "items",
    "layout",
    "dashboard",
    "dashboard_widgets",
    "dashboardWidgets",
    "config",
    "payload",
    "params",
    "data",
    "body",
)

USER_ID_KEYS = (
    "user_id",
    "userId",
    "user",
    "username",
    "user_name",
    "uid",
    "owner",
    "account_id",
    "accountId",
    "id",
)

# Keys that identify the envelope rather than widget content.
_ENVELOPE_SKIP = {k.lower() for k in USER_ID_KEYS} | {
    "widgets",
    "items",
    "layout",
    "dashboard",
    "dashboard_widgets",
    "dashboardwidgets",
    "widget",
    "widget_id",
    "widgetid",
}


def _coerce_user_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    s = str(value).strip()
    return s if s else None


def _coerce_widget_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    s = str(value).strip()
    return s if s else None


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
        for nested_key in WIDGET_NESTED_KEYS + ("request", "fields", "entry", "note"):
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    try:
        return dict(data)
    except Exception:
        return {}


def _extract_user_id(*args: Any, **kwargs: Any) -> Optional[str]:
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif isinstance(a, str) and "user_id" not in {k.lower() for k in flat}:
            if len(args) == 1:
                flat["user_id"] = a
    flat.update(kwargs or {})
    for nested_key in WIDGET_NESTED_KEYS:
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    raw = _first_present(flat, USER_ID_KEYS)
    if raw is None and len(args) == 1 and isinstance(args[0], str):
        raw = args[0]
    return _coerce_user_id(raw)


def _extract_widgets_list(data: Any) -> Optional[List[Any]]:
    """Find a list of widgets inside flexible payload shapes."""
    if isinstance(data, list):
        return data
    if hasattr(data, "model_dump"):
        try:
            data = dict(data.model_dump())
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    for key in WIDGET_NESTED_KEYS:
        val = data.get(key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            # nested dict containing a list, e.g. {"dashboard": {"widgets": [...]}, ...}
            for inner in val.values():
                if isinstance(inner, list):
                    return inner
    # single widget dict payload counts as one-element list
    if isinstance(_first_present(data, WIDGET_TYPE_KEYS), str) or isinstance(
        _first_present(data, WIDGET_ID_KEYS), str
    ):
        # avoid treating envelope-only dicts as widgets
        rest = {k: v for k, v in data.items() if k.lower() not in _ENVELOPE_SKIP}
        if rest or _first_present(data, WIDGET_TYPE_KEYS) is not None:
            return [data]
    return None


def normalize_widget_type(value: Any) -> Optional[str]:
    """Normalize a widget type string to its canonical supported value."""
    if value is None:
        return None
    if isinstance(value, dict):
        for k in WIDGET_TYPE_KEYS:
            if value.get(k) is not None:
                return normalize_widget_type(value.get(k))
        return None
    s = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if not s:
        return None
    alias_map = {
        "camera": "cameras",
        "cams": "cameras",
        "live": "live",
        "event": "events",
        "alert": "alerts",
        "stat": "stats",
        "statistic": "stats",
        "statistics": "stats",
        "disk": "storage",
        "disks": "storage",
        "system_status": "system",
        "systemstatus": "system",
        "heat_map": "heatmap",
    }
    s = alias_map.get(s, s)
    if s in SUPPORTED_WIDGET_TYPES:
        return s
    return None


def is_valid_widget_type(value: Any) -> bool:
    return normalize_widget_type(value) is not None


def normalize_widget(data: Any, default_id: Optional[str] = None) -> Dict[str, Any]:
    """Normalize a single widget dict with id/type/title/layout defaults."""
    flat = _flatten(data)
    wid = _first_present(flat, WIDGET_ID_KEYS)
    wtype = _first_present(flat, WIDGET_TYPE_KEYS)
    canonical_type = normalize_widget_type(wtype)
    # Fall back: derive type from id when id matches a known type.
    if canonical_type is None and wid is not None:
        maybe = normalize_widget_type(wid)
        if maybe is not None:
            canonical_type = maybe
    if canonical_type is None:
        # custom widgets are allowed but must still carry a type label
        raw_type = str(wtype).strip() if wtype is not None else None
        canonical_type = raw_type or "custom"
    widget_id = _coerce_widget_id(wid) or _coerce_widget_id(default_id)
    if widget_id is None:
        widget_id = canonical_type or f"widget-{uuid.uuid4().hex[:8]}"
    title = flat.get("title")
    if title is None:
        title = flat.get("label") or flat.get("name") or str(widget_id)
    enabled = flat.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "no", "off", "disabled")
    else:
        enabled = bool(enabled)

    def _num(key: str, default: Any) -> Any:
        v = flat.get(key)
        if v is None:
            return default
        try:
            if isinstance(default, float):
                return float(v)
            if isinstance(default, int) and not isinstance(default, bool):
                return int(v)
            return v
        except (TypeError, ValueError):
            return default

    normalized: Dict[str, Any] = {
        "id": str(widget_id),
        "type": str(canonical_type),
        "title": str(title),
        "enabled": enabled,
        "x": _num("x", 0),
        "y": _num("y", 0),
        "w": _num("w", 4),
        "h": _num("h", 3),
        "config": dict(flat.get("config", {}) or {}),
    }
    # Preserve common aliases so varied clients can read values back.
    normalized.setdefault("widget_id", normalized["id"])
    normalized.setdefault("widgetId", normalized["id"])
    normalized.setdefault("widget_type", normalized["type"])
    normalized.setdefault("widgetType", normalized["type"])
    # Carry through extra known display keys when present.
    for extra in ("order", "position", "size", "visible", "collapsed", "refresh_interval", "refreshInterval"):
        if flat.get(extra) is not None and extra not in normalized:
            normalized[extra] = flat.get(extra)
    return normalized


def _normalize_list(items: Any) -> List[Dict[str, Any]]:
    raw = _extract_widgets_list(items)
    if raw is None:
        if items is None:
            return []
        if isinstance(items, dict):
            raw = _extract_widgets_list(items)
        if raw is None:
            return []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        w = normalize_widget(entry)
        # de-duplicate by id, last wins
        if w["id"] in seen:
            out = [x for x in out if x["id"] != w["id"]]
        seen.add(w["id"])
        out.append(w)
    return out


def _resolve_user_id(user_id: Any, args: tuple, kwargs: dict) -> str:
    positional: List[Any] = []
    if user_id is not None:
        positional.append(user_id)
    positional.extend(args)
    uid = _extract_user_id(*positional, **kwargs)
    if uid is None:
        uid = DEFAULT_USER_ID
    return uid


def _stored_copy(uid: str) -> List[Dict[str, Any]]:
    with _lock:
        stored = _WIDGETS.get(uid)
        if stored is None:
            return copy.deepcopy(DEFAULT_WIDGETS)
        return copy.deepcopy(stored)


def get_widgets(user_id: Any = None, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return the widget list for a user (defaults when never customized)."""
    uid = _resolve_user_id(user_id, args, kwargs)
    return _stored_copy(uid)


def set_widgets(user_id: Any = None, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Replace the widget list for a user with the given widgets."""
    flat: Dict[str, Any] = {}
    widgets_arg: Any = None
    ordered: List[Any] = []
    if isinstance(user_id, (list, tuple)):
        widgets_arg = list(user_id)
    elif isinstance(user_id, dict):
        flat.update(user_id)
    elif isinstance(user_id, str) and user_id.strip():
        flat.setdefault("user_id", user_id)
    for a in args:
        if isinstance(a, (list, tuple)) and widgets_arg is None:
            widgets_arg = list(a)
        elif isinstance(a, dict):
            # dict may be the widgets payload or contain nested widgets
            inner = _extract_widgets_list(a)
            if inner is not None and widgets_arg is None:
                widgets_arg = inner
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                dumped = dict(a.model_dump())
            except Exception:
                dumped = {}
            inner = _extract_widgets_list(dumped)
            if inner is not None and widgets_arg is None:
                widgets_arg = inner
            flat.update(dumped)
    flat.update(kwargs or {})
    for nested_key in WIDGET_NESTED_KEYS:
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    if widgets_arg is None:
        widgets_arg = _extract_widgets_list(flat)
    if widgets_arg is None:
        widgets_arg = []
    uid = _coerce_user_id(_first_present(flat, USER_ID_KEYS))
    if uid is None and isinstance(user_id, str):
        uid = _coerce_user_id(user_id)
    if uid is None:
        uid = DEFAULT_USER_ID
    normalized = _normalize_list(widgets_arg if widgets_arg is not None else [])
    with _lock:
        _WIDGETS[uid] = copy.deepcopy(normalized)
        stored = copy.deepcopy(_WIDGETS[uid])
    return stored


def update_widgets(user_id: Any = None, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Merge the given widgets into the stored list (upsert by id)."""
    flat: Dict[str, Any] = {}
    widgets_arg: Any = None
    if isinstance(user_id, (list, tuple)):
        widgets_arg = list(user_id)
    elif isinstance(user_id, dict):
        flat.update(user_id)
    elif isinstance(user_id, str) and user_id.strip():
        flat.setdefault("user_id", user_id)
    for a in args:
        if isinstance(a, (list, tuple)) and widgets_arg is None:
            widgets_arg = list(a)
        elif isinstance(a, dict):
            inner = _extract_widgets_list(a)
            if inner is not None and widgets_arg is None:
                widgets_arg = inner
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                dumped = dict(a.model_dump())
            except Exception:
                dumped = {}
            inner = _extract_widgets_list(dumped)
            if inner is not None and widgets_arg is None:
                widgets_arg = inner
            flat.update(dumped)
    flat.update(kwargs or {})
    if widgets_arg is None:
        widgets_arg = _extract_widgets_list(flat)
    if widgets_arg is None:
        widgets_arg = []
    uid = _coerce_user_id(_first_present(flat, USER_ID_KEYS))
    if uid is None and isinstance(user_id, str):
        uid = _coerce_user_id(user_id)
    if uid is None:
        uid = DEFAULT_USER_ID
    patch = _normalize_list(widgets_arg)
    with _lock:
        current = _WIDGETS.get(uid)
        if current is None:
            current = copy.deepcopy(DEFAULT_WIDGETS)
        else:
            current = copy.deepcopy(current)
        by_id = {w["id"]: w for w in current}
        for w in patch:
            by_id[w["id"]] = w
        merged = list(by_id.values())
        _WIDGETS[uid] = merged
        stored = copy.deepcopy(merged)
    return stored


def reset_widgets(user_id: Any = None, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Clear custom widgets for a user, returning defaults."""
    uid = _extract_user_id(*(([user_id] if user_id is not None else []) + list(args)), **kwargs)
    if uid is None:
        uid = DEFAULT_USER_ID
    with _lock:
        _WIDGETS.pop(uid, None)
    return copy.deepcopy(DEFAULT_WIDGETS)


def delete_widgets(user_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    uid = _extract_user_id(*(([user_id] if user_id is not None else []) + list(args)), **kwargs)
    if uid is None:
        uid = DEFAULT_USER_ID
    with _lock:
        return _WIDGETS.pop(uid, None) is not None


def add_widget(user_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Add (or replace) a single widget for a user, returning the widget."""
    flat: Dict[str, Any] = {}
    widget_arg: Any = None
    if isinstance(user_id, dict) and _first_present(user_id, WIDGET_TYPE_KEYS + WIDGET_ID_KEYS) is not None:
        widget_arg = dict(user_id)
        flat.update(user_id)
    elif isinstance(user_id, str) and user_id.strip():
        flat.setdefault("user_id", user_id)
    for a in args:
        if isinstance(a, dict):
            if widget_arg is None and (
                _first_present(a, WIDGET_TYPE_KEYS + WIDGET_ID_KEYS) is not None
            ):
                widget_arg = dict(a)
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                dumped = dict(a.model_dump())
            except Exception:
                dumped = {}
            if widget_arg is None:
                widget_arg = dumped
            flat.update(dumped)
    flat.update(kwargs or {})
    if widget_arg is None:
        # Remaining flat keys (minus user id) form the widget.
        candidate = {k: v for k, v in flat.items() if k.lower() not in {k.lower() for k in USER_ID_KEYS}}
        widget_arg = candidate
    uid = _coerce_user_id(_first_present(flat, USER_ID_KEYS))
    if uid is None and isinstance(user_id, str):
        uid = _coerce_user_id(user_id)
    if uid is None:
        uid = DEFAULT_USER_ID
    widget = normalize_widget(widget_arg if isinstance(widget_arg, dict) else {})
    with _lock:
        current = _WIDGETS.get(uid)
        if current is None:
            current = copy.deepcopy(DEFAULT_WIDGETS)
        else:
            current = copy.deepcopy(current)
        current = [w for w in current if w["id"] != widget["id"]]
        current.append(widget)
        _WIDGETS[uid] = current
    return copy.deepcopy(widget)


def update_widget(
    user_id: Any = None, widget_id: Any = None, *args: Any, **kwargs: Any
) -> Optional[Dict[str, Any]]:
    """Patch a single widget by id; returns None when not found."""
    flat: Dict[str, Any] = {}
    patch: Dict[str, Any] = {}
    positional_ids: List[Any] = []
    if isinstance(user_id, dict):
        flat.update(user_id)
    elif isinstance(user_id, str) and user_id.strip():
        # ambiguous: could be user_id; caller may pass widget dict positionally
        flat.setdefault("user_id", user_id)
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
            # treat as patch when it carries widget fields
            for k, v in a.items():
                if k.lower() not in {kk.lower() for kk in USER_ID_KEYS}:
                    patch[k] = v
        elif isinstance(a, str) and widget_id is None:
            widget_id = a
        elif isinstance(a, str):
            positional_ids.append(a)
    flat.update(kwargs or {})
    # explicit patch keys win
    for k, v in flat.items():
        if k.lower() not in {kk.lower() for kk in USER_ID_KEYS}:
            patch.setdefault(k, v)
    uid = _coerce_user_id(_first_present(flat, USER_ID_KEYS))
    if uid is None and isinstance(user_id, str):
        uid = _coerce_user_id(user_id)
    if uid is None:
        uid = DEFAULT_USER_ID
    wid = _coerce_widget_id(widget_id)
    if wid is None:
        wid = _coerce_widget_id(_first_present(flat, WIDGET_ID_KEYS))
    if wid is None and positional_ids:
        wid = _coerce_widget_id(positional_ids[0])
    if wid is None:
        return None
    with _lock:
        current = _WIDGETS.get(uid)
        if current is None:
            current = copy.deepcopy(DEFAULT_WIDGETS)
        else:
            current = copy.deepcopy(current)
        found: Optional[Dict[str, Any]] = None
        for w in current:
            if w.get("id") == wid:
                found = w
                break
        if found is None:
            return None
        merged_raw = dict(found)
        for k, v in patch.items():
            if k.lower() in ("id", "widget_id", "widgetid"):
                continue
            merged_raw[k] = v
        updated = normalize_widget(merged_raw, default_id=wid)
        # normalize_widget derives type from merged; keep id stable
        updated["id"] = wid
        updated["widget_id"] = wid
        updated["widgetId"] = wid
        current = [updated if w.get("id") == wid else w for w in current]
        _WIDGETS[uid] = current
        return copy.deepcopy(updated)


def remove_widget(user_id: Any = None, widget_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    """Remove a single widget by id; returns True when removed."""
    flat: Dict[str, Any] = {}
    if isinstance(user_id, dict):
        flat.update(user_id)
    elif isinstance(user_id, str) and user_id.strip():
        flat.setdefault("user_id", user_id)
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif isinstance(a, str) and widget_id is None:
            widget_id = a
    flat.update(kwargs or {})
    uid = _coerce_user_id(_first_present(flat, USER_ID_KEYS))
    if uid is None and isinstance(user_id, str):
        uid = _coerce_user_id(user_id)
    if uid is None:
        uid = DEFAULT_USER_ID
    wid = _coerce_widget_id(widget_id)
    if wid is None:
        wid = _coerce_widget_id(_first_present(flat, WIDGET_ID_KEYS))
    if wid is None:
        return False
    with _lock:
        current = _WIDGETS.get(uid)
        if current is None:
            current = copy.deepcopy(DEFAULT_WIDGETS)
        else:
            current = copy.deepcopy(current)
        kept = [w for w in current if w.get("id") != wid]
        if len(kept) == len(current):
            return False
        _WIDGETS[uid] = kept
        return True


def get_widget(
    user_id: Any = None, widget_id: Any = None, *args: Any, **kwargs: Any
) -> Optional[Dict[str, Any]]:
    """Return a single widget by id, or None when not found."""
    flat: Dict[str, Any] = {}
    if isinstance(user_id, dict):
        flat.update(user_id)
    elif isinstance(user_id, str) and user_id.strip():
        flat.setdefault("user_id", user_id)
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif isinstance(a, str) and widget_id is None:
            widget_id = a
    flat.update(kwargs or {})
    uid = _coerce_user_id(_first_present(flat, USER_ID_KEYS))
    if uid is None and isinstance(user_id, str):
        uid = _coerce_user_id(user_id)
    if uid is None:
        uid = DEFAULT_USER_ID
    wid = _coerce_widget_id(widget_id)
    if wid is None:
        wid = _coerce_widget_id(_first_present(flat, WIDGET_ID_KEYS))
    if wid is None:
        return None
    for w in _stored_copy(uid):
        if w.get("id") == wid:
            return copy.deepcopy(w)
    return None


def list_users(*args: Any, **kwargs: Any) -> List[str]:
    with _lock:
        return sorted(_WIDGETS.keys())


def count_users(*args: Any, **kwargs: Any) -> int:
    with _lock:
        return len(_WIDGETS)


def clear(*args: Any, **kwargs: Any) -> int:
    with _lock:
        n = len(_WIDGETS)
        _WIDGETS.clear()
        return n


def list_widget_types(*args: Any, **kwargs: Any) -> List[str]:
    return list(SUPPORTED_WIDGET_TYPES)


def get_default_widgets(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return copy.deepcopy(DEFAULT_WIDGETS)


# --- Async entrypoints (db-optional, ignored) --------------------------------

async def get_widgets_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return get_widgets(*args, **kwargs)


async def set_widgets_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return set_widgets(*args, **kwargs)


async def update_widgets_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return update_widgets(*args, **kwargs)


async def reset_widgets_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return reset_widgets(*args, **kwargs)


class WidgetsService:
    """Object-oriented facade over the module-level store."""

    def get(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return get_widgets(*args, **kwargs)

    def set(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return set_widgets(*args, **kwargs)

    def update(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return update_widgets(*args, **kwargs)

    def reset(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return reset_widgets(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return delete_widgets(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return add_widget(*args, **kwargs)

    def remove(self, *args: Any, **kwargs: Any) -> bool:
        return remove_widget(*args, **kwargs)

    def get_one(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_widget(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return get_widgets(*args, **kwargs)

    # aliases
    get_widgets = get
    fetch_widgets = get
    load_widgets = get
    read_widgets = get
    set_user_widgets = set
    save_widgets = set
    reset_user_widgets = reset
    add_user_widget = add
    remove_user_widget = remove


widgets_service = WidgetsService()
widget_service = widgets_service
dashboard_widgets_service = widgets_service
service = widgets_service
manager = widgets_service
store = widgets_service


# Compatibility aliases (function level).
get_user_widgets = get_widgets
fetch_widgets = get_widgets
load_widgets = get_widgets
read_widgets = get_widgets
retrieve_widgets = get_widgets
list_widgets = get_widgets
get_all_widgets = get_widgets
fetch_all_widgets = get_widgets

set_user_widgets = set_widgets
save_widgets = set_widgets
save_user_widgets = set_widgets
put_widgets = set_widgets
store_widgets = set_widgets
create_widgets = set_widgets
replace_widgets = set_widgets

update_user_widgets = update_widgets
patch_widgets = update_widgets
patch_user_widgets = update_widgets
edit_widgets = update_widgets
merge_widgets = update_widgets
upsert_widgets = update_widgets

reset_user_widgets = reset_widgets
clear_widgets = reset_widgets
restore_defaults = reset_widgets
restore_default_widgets = reset_widgets
default_widgets = reset_widgets
get_defaults = get_default_widgets

remove_widgets = delete_widgets
delete_user_widgets = delete_widgets
remove_user_widgets = delete_widgets

add_user_widget = add_widget
create_widget = add_widget
insert_widget = add_widget
upsert_widget = add_widget

delete_widget = remove_widget
remove_user_widget = remove_widget
drop_widget = remove_widget

get_single_widget = get_widget
fetch_widget = get_widget
load_widget = get_widget

__all__ = [
    "DEFAULT_WIDGETS",
    "DEFAULT_USER_ID",
    "SUPPORTED_WIDGET_TYPES",
    "normalize_widget",
    "normalize_widget_type",
    "is_valid_widget_type",
    "get_widgets",
    "set_widgets",
    "update_widgets",
    "reset_widgets",
    "delete_widgets",
    "add_widget",
    "update_widget",
    "remove_widget",
    "get_widget",
    "list_users",
    "count_users",
    "clear",
    "list_widget_types",
    "get_default_widgets",
    "get_widgets_async",
    "set_widgets_async",
    "update_widgets_async",
    "reset_widgets_async",
    "WidgetsService",
    "widgets_service",
]
