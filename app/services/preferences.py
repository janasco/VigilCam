"""User preferences service (in-memory, per-user key/value store).

Preferences are operator-specific UI/API settings keyed by user id. The store
is intentionally in-memory (thread-safe) so the feature works without DB
migrations and without requiring ``app/main.py`` edits. An optional
``db``/``session`` argument is accepted by the async entrypoints for
forward-compatibility but is currently ignored (best-effort no-op).
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
_PREFS: Dict[str, Dict[str, Any]] = {}

DEFAULT_USER_ID = "default"

DEFAULT_PREFERENCES: Dict[str, Any] = {
    "theme": "light",
    "language": "en",
    "locale": "en",
    "notifications_enabled": True,
    "email_notifications": False,
    "items_per_page": 25,
    "layout": "grid",
}

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

PREFS_NESTED_KEYS = (
    "preferences",
    "prefs",
    "settings",
    "options",
    "config",
    "payload",
    "params",
    "data",
    "body",
)


def _coerce_user_id(value: Any) -> Optional[str]:
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
        for nested_key in PREFS_NESTED_KEYS + ("request", "fields", "entry", "note"):
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
            # A bare string positional is treated as user_id only when no
            # dict already supplied one; otherwise ignore here (handled by caller).
            if len(args) == 1:
                flat["user_id"] = a
    flat.update(kwargs or {})
    for nested_key in PREFS_NESTED_KEYS:
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    raw = _first_present(flat, USER_ID_KEYS)
    if raw is None and len(args) == 1 and isinstance(args[0], str):
        raw = args[0]
    return _coerce_user_id(raw)


def _extract_prefs_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return preference fields from a flat dict, excluding user-id keys."""
    skip = {k.lower() for k in USER_ID_KEYS}
    out: Dict[str, Any] = {}
    for k, v in data.items():
        if k.lower() in skip:
            continue
        if k in PREFS_NESTED_KEYS and isinstance(v, dict):
            continue
        out[k] = v
    # Merge nested preferences dict if present (explicit nested wins on conflict).
    for nested_key in PREFS_NESTED_KEYS:
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                if k.lower() in skip:
                    continue
                out[k] = v
    return out


def _to_public(user_id: str, stored: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(copy.deepcopy(DEFAULT_PREFERENCES))
    merged.update(copy.deepcopy(stored))
    merged["user_id"] = user_id
    # Common aliases so varied clients can read the id back.
    merged.setdefault("userId", user_id)
    merged.setdefault("username", user_id)
    merged.setdefault("uid", user_id)
    return merged


def get_preferences(user_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Return merged (defaults + stored overrides) preferences for a user."""
    uid = _extract_user_id(*(([user_id] if user_id is not None else []) + list(args)), **kwargs)
    if uid is None:
        uid = DEFAULT_USER_ID
    with _lock:
        stored = copy.deepcopy(_PREFS.get(uid, {}))
    return _to_public(uid, stored)


def set_preferences(user_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Replace stored overrides for a user with the given prefs dict."""
    flat: Dict[str, Any] = {}
    for a in list(args) + ([user_id] if isinstance(user_id, dict) else []):
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
    if isinstance(user_id, dict):
        pass
    elif isinstance(user_id, str) and user_id.strip():
        flat.setdefault("user_id", user_id)
    flat.update(kwargs or {})
    for nested_key in PREFS_NESTED_KEYS:
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    uid = _coerce_user_id(_first_present(flat, USER_ID_KEYS))
    if uid is None and isinstance(user_id, str):
        uid = _coerce_user_id(user_id)
    if uid is None:
        uid = DEFAULT_USER_ID
    prefs = _extract_prefs_dict(flat)
    with _lock:
        _PREFS[uid] = copy.deepcopy(prefs)
        stored = copy.deepcopy(_PREFS[uid])
    return _to_public(uid, stored)


def update_preferences(user_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Merge the given prefs into the stored overrides for a user."""
    flat: Dict[str, Any] = {}
    for a in list(args) + ([user_id] if isinstance(user_id, dict) else []):
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
    if isinstance(user_id, str) and user_id.strip():
        flat.setdefault("user_id", user_id)
    flat.update(kwargs or {})
    for nested_key in PREFS_NESTED_KEYS:
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    uid = _coerce_user_id(_first_present(flat, USER_ID_KEYS))
    if uid is None and isinstance(user_id, str):
        uid = _coerce_user_id(user_id)
    if uid is None:
        uid = DEFAULT_USER_ID
    patch = _extract_prefs_dict(flat)
    with _lock:
        current = _PREFS.get(uid, {})
        current.update(copy.deepcopy(patch))
        _PREFS[uid] = current
        stored = copy.deepcopy(current)
    return _to_public(uid, stored)


def reset_preferences(user_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Clear stored overrides for a user, returning defaults."""
    uid = _extract_user_id(*(([user_id] if user_id is not None else []) + list(args)), **kwargs)
    if uid is None:
        uid = DEFAULT_USER_ID
    with _lock:
        _PREFS.pop(uid, None)
    return _to_public(uid, {})


def delete_preferences(user_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    uid = _extract_user_id(*(([user_id] if user_id is not None else []) + list(args)), **kwargs)
    if uid is None:
        uid = DEFAULT_USER_ID
    with _lock:
        return _PREFS.pop(uid, None) is not None


def list_users(*args: Any, **kwargs: Any) -> List[str]:
    with _lock:
        return sorted(_PREFS.keys())


def list_all_preferences(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    with _lock:
        users = sorted(_PREFS.keys())
        snapshot = {u: copy.deepcopy(_PREFS[u]) for u in users}
    return [_to_public(u, snapshot[u]) for u in users]


def count_users(*args: Any, **kwargs: Any) -> int:
    with _lock:
        return len(_PREFS)


def clear(*args: Any, **kwargs: Any) -> int:
    with _lock:
        n = len(_PREFS)
        _PREFS.clear()
        return n


# --- Async entrypoints (db-optional, ignored) --------------------------------

async def get_preferences_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return get_preferences(*args, **kwargs)


async def set_preferences_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return set_preferences(*args, **kwargs)


async def update_preferences_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return update_preferences(*args, **kwargs)


async def reset_preferences_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return reset_preferences(*args, **kwargs)


class PreferencesService:
    """Object-oriented facade over the module-level store."""

    def get(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_preferences(*args, **kwargs)

    def set(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_preferences(*args, **kwargs)

    def update(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return update_preferences(*args, **kwargs)

    def reset(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return reset_preferences(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return delete_preferences(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_all_preferences(*args, **kwargs)

    # aliases
    get_preferences = get
    fetch_preferences = get
    get_user_preferences = get
    set_user_preferences = set
    update_user_preferences = update
    reset_user_preferences = reset
    delete_user_preferences = delete
    clear_preferences = clear


preferences_service = PreferencesService()
preference_service = preferences_service
user_preferences_service = preferences_service
service = preferences_service
manager = preferences_service
store = preferences_service


# Compatibility aliases (function level).
get_user_preferences = get_preferences
fetch_preferences = get_preferences
fetch_user_preferences = get_preferences
load_preferences = get_preferences
read_preferences = get_preferences
retrieve_preferences = get_preferences

set_user_preferences = set_preferences
save_preferences = set_preferences
save_user_preferences = set_preferences
put_preferences = set_preferences
store_preferences = set_preferences
create_preferences = set_preferences

update_user_preferences = update_preferences
patch_preferences = update_preferences
patch_user_preferences = update_preferences
edit_preferences = update_preferences
merge_preferences = update_preferences

reset_user_preferences = reset_preferences
clear_preferences = reset_preferences
restore_defaults = reset_preferences
restore_default_preferences = reset_preferences
default_preferences = reset_preferences

remove_preferences = delete_preferences
delete_user_preferences = delete_preferences
remove_user_preferences = delete_preferences

get_all_preferences = list_all_preferences
list_preferences = list_all_preferences
fetch_all_preferences = list_all_preferences

__all__ = [
    "DEFAULT_PREFERENCES",
    "DEFAULT_USER_ID",
    "get_preferences",
    "set_preferences",
    "update_preferences",
    "reset_preferences",
    "delete_preferences",
    "list_users",
    "list_all_preferences",
    "count_users",
    "clear",
    "get_preferences_async",
    "set_preferences_async",
    "update_preferences_async",
    "reset_preferences_async",
    "PreferencesService",
    "preferences_service",
]
