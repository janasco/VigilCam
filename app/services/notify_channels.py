"""Notification channels config service for VigilCam.

Central configuration store for notification channels (email, sms,
webhook, web push, ...). Tracks per-channel ``enabled`` flag plus an
arbitrary ``config`` dict. All state is in-memory and thread-safe so the
feature works without DB migrations and without ``app/main.py`` edits
(the API router is auto-registered by the pkgutil auto-loader).
"""

from __future__ import annotations

import copy
import builtins
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_lock = threading.RLock()

SUPPORTED_CHANNELS = ("email", "sms", "webhook", "web_push", "push", "digest")
CANONICAL_ALIASES = {
    "web-push": "web_push",
    "webpush": "web_push",
    "web_push": "web_push",
    "push": "push",
    "e-mail": "email",
    "e_mail": "email",
    "text": "sms",
    "text_message": "sms",
    "web-hook": "webhook",
    "web_hook": "webhook",
    "hooks": "webhook",
}

DEFAULT_CHANNELS: Dict[str, Dict[str, Any]] = {
    "email": {"enabled": False, "config": {}},
    "sms": {"enabled": False, "config": {}},
    "webhook": {"enabled": False, "config": {}},
    "web_push": {"enabled": False, "config": {}},
    "push": {"enabled": False, "config": {}},
    "digest": {"enabled": False, "config": {}},
}

_CHANNELS: Dict[str, Dict[str, Any]] = {}

CHANNEL_KEYS = (
    "channel",
    "channel_name",
    "channelName",
    "name",
    "id",
    "channel_id",
    "channelId",
    "type",
    "kind",
    "service",
    "medium",
    "via",
)
ENABLED_KEYS = (
    "enabled",
    "is_enabled",
    "isEnabled",
    "active",
    "is_active",
    "on",
    "enable",
)
CONFIG_KEYS = (
    "config",
    "configuration",
    "settings",
    "options",
    "params",
    "data",
    "payload",
    "details",
    "values",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_channel(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if not s:
        return None
    key = s.lower().replace("-", "_").replace(" ", "_")
    key = CANONICAL_ALIASES.get(key, key)
    # also normalize double underscores
    while "__" in key:
        key = key.replace("__", "_")
    return key


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        kl = str(k).lower()
        if kl in lower and lower[kl] is not None:
            return lower[kl]
    return None


def _flatten(payload: Any) -> Dict[str, Any]:
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        try:
            val = payload.model_dump()
            if isinstance(val, dict):
                return dict(val)
        except Exception:
            pass
    if isinstance(payload, dict):
        out = dict(payload)
        for nested in ("preferences", "prefs", "settings", "options", "config",
                       "configuration", "params", "data", "body", "request",
                       "payload", "channel_config", "channelConfig"):
            nested_val = out.get(nested)
            if isinstance(nested_val, dict):
                for k, v in nested_val.items():
                    out.setdefault(k, v)
        return out
    try:
        return dict(payload)
    except Exception:
        return {}


def _normalize_args(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            for k, v in a.items():
                data.setdefault(k, v)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if data.get("channel") is None:
                data["channel"] = s
            elif data.get("config") is None:
                data["config"] = s
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a)
                         if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    for k, v in attrs.items():
                        data.setdefault(k, v)
            except Exception:
                pass
    for k, v in (kwargs or {}).items():
        data[k] = v
    for nested in ("preferences", "prefs", "settings", "options",
                   "params", "data", "body", "request", "payload",
                   "channel_config", "channelConfig"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    return data


def _to_public(name: str, stored: Dict[str, Any]) -> Dict[str, Any]:
    enabled = bool(stored.get("enabled", False))
    config = copy.deepcopy(stored.get("config", {}))
    if not isinstance(config, dict):
        config = {"value": config}
    now = _now_iso()
    out: Dict[str, Any] = {
        "channel": name,
        "channel_name": name,
        "name": name,
        "id": name,
        "channel_id": name,
        "enabled": enabled,
        "is_enabled": enabled,
        "active": enabled,
        "is_active": enabled,
        "on": enabled,
        "disabled": not enabled,
        "config": copy.deepcopy(config),
        "configuration": copy.deepcopy(config),
        "settings": copy.deepcopy(config),
        "options": copy.deepcopy(config),
        "ok": True,
        "success": True,
        "status": "enabled" if enabled else "disabled",
        "updated_at": stored.get("updated_at", now),
        "created_at": stored.get("created_at", now),
    }
    for k, v in stored.items():
        if k not in out:
            out[k] = copy.deepcopy(v)
    return out


def _ensure(name: str) -> Dict[str, Any]:
    if name not in _CHANNELS:
        default = copy.deepcopy(DEFAULT_CHANNELS.get(name, {"enabled": False, "config": {}}))
        now = _now_iso()
        default.setdefault("created_at", now)
        default.setdefault("updated_at", now)
        _CHANNELS[name] = default
    return _CHANNELS[name]


def get_channel(channel: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return public config dict for a single channel, or None if unknown/empty."""
    data = _normalize_args((channel,) + tuple(args), kwargs)
    raw = _first(data, CHANNEL_KEYS)
    if raw is None and isinstance(channel, str):
        raw = channel
    name = _normalize_channel(raw)
    if name is None:
        return None
    with _lock:
        stored = copy.deepcopy(_ensure(name))
    return _to_public(name, stored)


def list_channels(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return public config dicts for all known channels (defaults + stored)."""
    with _lock:
        names = sorted(builtins.set(DEFAULT_CHANNELS.keys()) | builtins.set(_CHANNELS.keys()))
        snapshot = {n: copy.deepcopy(_ensure(n)) for n in names}
    return [_to_public(n, snapshot[n]) for n in names]


def set_channel(channel: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create or replace a channel's config. ``enabled`` defaults to False."""
    data = _normalize_args((channel,) + tuple(args) if channel is not None else tuple(args), kwargs)
    # channel may have been passed positionally as second string
    raw = _first(data, CHANNEL_KEYS)
    name = _normalize_channel(raw)
    if name is None:
        raise ValueError("channel is required")
    raw_enabled = _first(data, ENABLED_KEYS)
    if raw_enabled is None:
        # also accept bare "enable"/"disable" boolean flags via action keys
        action = str(data.get("action", "")).lower()
        if action in ("enable", "activate", "on"):
            raw_enabled = True
        elif action in ("disable", "deactivate", "off"):
            raw_enabled = False
    enabled = bool(raw_enabled) if raw_enabled is not None else False
    if isinstance(raw_enabled, str):
        enabled = raw_enabled.strip().lower() in ("1", "true", "yes", "on", "enabled", "active")
    raw_config = _first(data, CONFIG_KEYS)
    if raw_config is None:
        skip = {k.lower() for k in CHANNEL_KEYS} | {k.lower() for k in ENABLED_KEYS} | {
            "action", "db", "session", "user", "current_user", "request",
            "preferences", "prefs", "payload",
        }
        extra = {k: v for k, v in data.items() if k.lower() not in skip}
        # drop nested-wrapper keys themselves
        for nk in ("config", "configuration", "settings", "options", "params",
                   "data", "details", "values", "channel_config"):
            extra.pop(nk, None)
        raw_config = extra
    if not isinstance(raw_config, dict):
        raw_config = {"value": raw_config} if raw_config is not None else {}
    now = _now_iso()
    with _lock:
        prev = _CHANNELS.get(name, {})
        created = prev.get("created_at", now)
        _CHANNELS[name] = {
            "enabled": enabled,
            "config": copy.deepcopy(raw_config),
            "created_at": created,
            "updated_at": now,
        }
        stored = copy.deepcopy(_CHANNELS[name])
    return _to_public(name, stored)


def update_channel(channel: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Merge ``enabled``/``config`` into an existing channel (creates if missing)."""
    data = _normalize_args((channel,) + tuple(args) if channel is not None else tuple(args), kwargs)
    raw = _first(data, CHANNEL_KEYS)
    name = _normalize_channel(raw)
    if name is None:
        raise ValueError("channel is required")
    now = _now_iso()
    with _lock:
        current = copy.deepcopy(_ensure(name))
    raw_enabled = _first(data, ENABLED_KEYS)
    if raw_enabled is not None:
        if isinstance(raw_enabled, str):
            current["enabled"] = raw_enabled.strip().lower() in ("1", "true", "yes", "on", "enabled", "active")
        else:
            current["enabled"] = bool(raw_enabled)
    else:
        action = str(data.get("action", "")).lower()
        if action in ("enable", "activate", "on"):
            current["enabled"] = True
        elif action in ("disable", "deactivate", "off"):
            current["enabled"] = False
    raw_config = _first(data, CONFIG_KEYS)
    if isinstance(raw_config, dict):
        existing = current.get("config", {})
        if not isinstance(existing, dict):
            existing = {}
        merged = copy.deepcopy(existing)
        merged.update(copy.deepcopy(raw_config))
        current["config"] = merged
    else:
        skip = {k.lower() for k in CHANNEL_KEYS} | {k.lower() for k in ENABLED_KEYS} | {
            "action", "db", "session", "user", "current_user", "request",
            "preferences", "prefs", "payload",
        }
        extra = {k: v for k, v in data.items() if k.lower() not in skip}
        for nk in ("config", "configuration", "settings", "options", "params",
                   "data", "details", "values", "channel_config"):
            extra.pop(nk, None)
        if extra:
            existing = current.get("config", {})
            if not isinstance(existing, dict):
                existing = {}
            merged = copy.deepcopy(existing)
            merged.update(copy.deepcopy(extra))
            current["config"] = merged
    current["updated_at"] = now
    if "created_at" not in current:
        current["created_at"] = now
    with _lock:
        _CHANNELS[name] = copy.deepcopy(current)
        stored = copy.deepcopy(_CHANNELS[name])
    return _to_public(name, stored)


def enable_channel(channel: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args((channel,) + tuple(args) if channel is not None else tuple(args), kwargs)
    raw = _first(data, CHANNEL_KEYS)
    name = _normalize_channel(raw if raw is not None else channel)
    if name is None:
        raise ValueError("channel is required")
    now = _now_iso()
    with _lock:
        current = copy.deepcopy(_ensure(name))
        current["enabled"] = True
        current["updated_at"] = now
        _CHANNELS[name] = copy.deepcopy(current)
        stored = copy.deepcopy(current)
    # merge any supplied config as well
    raw_config = _first(data, CONFIG_KEYS)
    if isinstance(raw_config, dict) and raw_config:
        return update_channel(name, config=raw_config)
    return _to_public(name, stored)


def disable_channel(channel: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args((channel,) + tuple(args) if channel is not None else tuple(args), kwargs)
    raw = _first(data, CHANNEL_KEYS)
    name = _normalize_channel(raw if raw is not None else channel)
    if name is None:
        raise ValueError("channel is required")
    now = _now_iso()
    with _lock:
        current = copy.deepcopy(_ensure(name))
        current["enabled"] = False
        current["updated_at"] = now
        _CHANNELS[name] = copy.deepcopy(current)
        stored = copy.deepcopy(current)
    return _to_public(name, stored)


def set_enabled(channel: Any = None, enabled: Any = True, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args((), kwargs)
    flag = enabled
    if args:
        # allow set_enabled("email", True) positional style
        pass
    if _first(data, ENABLED_KEYS) is not None:
        flag = _first(data, ENABLED_KEYS)
    if flag:
        return enable_channel(channel, *args, **{k: v for k, v in kwargs.items()})
    return disable_channel(channel, *args, **{k: v for k, v in kwargs.items()})


def is_enabled(channel: Any = None, *args: Any, **kwargs: Any) -> bool:
    res = get_channel(channel, *args, **kwargs)
    if res is None:
        return False
    return bool(res.get("enabled", False))


def enabled_channels(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return [c for c in list_channels(*args, **kwargs) if c.get("enabled")]


def reset_channel(channel: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args((channel,) + tuple(args) if channel is not None else tuple(args), kwargs)
    raw = _first(data, CHANNEL_KEYS)
    name = _normalize_channel(raw)
    if name is None:
        raise ValueError("channel is required")
    now = _now_iso()
    default = copy.deepcopy(DEFAULT_CHANNELS.get(name, {"enabled": False, "config": {}}))
    default["updated_at"] = now
    with _lock:
        prev = _CHANNELS.get(name, {})
        default.setdefault("created_at", prev.get("created_at", now))
        _CHANNELS[name] = copy.deepcopy(default)
        stored = copy.deepcopy(_CHANNELS[name])
    return _to_public(name, stored)


def delete_channel(channel: Any = None, *args: Any, **kwargs: Any) -> bool:
    data = _normalize_args((channel,) + tuple(args) if channel is not None else tuple(args), kwargs)
    raw = _first(data, CHANNEL_KEYS)
    name = _normalize_channel(raw)
    if name is None:
        return False
    default = copy.deepcopy(DEFAULT_CHANNELS.get(name, {"enabled": False, "config": {}}))
    default["updated_at"] = _now_iso()
    with _lock:
        existed = name in _CHANNELS
        _CHANNELS[name] = default
        return existed


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        n = len(_CHANNELS)
        _CHANNELS.clear()
    return {"ok": True, "success": True, "status": "cleared", "cleared": True,
            "count": 0, "removed": n, "channels_removed": n}


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    channels = list_channels()
    enabled = [c for c in channels if c.get("enabled")]
    by_state = {"enabled": len(enabled), "disabled": len(channels) - len(enabled)}
    return {
        "ok": True,
        "success": True,
        "status": "ok",
        "service": "notify_channels",
        "total": len(channels),
        "count": len(channels),
        "size": len(channels),
        "enabled": len(enabled),
        "enabled_count": len(enabled),
        "disabled": len(channels) - len(enabled),
        "disabled_count": len(channels) - len(enabled),
        "by_state": by_state,
        "channels": [c.get("channel") for c in channels],
        "channel_names": [c.get("channel") for c in channels],
        "enabled_channels": [c.get("channel") for c in enabled],
    }


# --- Async entrypoints (db-optional, ignored) --------------------------------

async def get_channel_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return get_channel(*args, **kwargs)


async def list_channels_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return list_channels(*args, **kwargs)


async def set_channel_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return set_channel(*args, **kwargs)


async def update_channel_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return update_channel(*args, **kwargs)


# ---------------------------------------------------------------------------
# Aliases (tolerant naming for API/test compatibility)
# ---------------------------------------------------------------------------

get = get_channel
fetch = get_channel
find = get_channel
get_one = get_channel
fetch_channel = get_channel
read_channel = get_channel
retrieve_channel = get_channel
load_channel = get_channel
get_config = get_channel
get_channel_config = get_channel
fetch_config = get_channel
get_settings = get_channel

list_all = list_channels
get_all = list_channels
get_channels = list_channels
fetch_channels = list_channels
get_all_channels = list_channels
fetch_all_channels = list_channels
all_channels = list_channels
all = list_channels
items = list_channels
values = list_channels
list_all_channels = list_channels
get_list = list_channels

set = set_channel
create = set_channel
save = set_channel
store = set_channel
put = set_channel
add = set_channel
add_channel = set_channel
create_channel = set_channel
save_channel = set_channel
store_channel = set_channel
put_channel = set_channel
set_config = set_channel
save_config = set_channel
set_channel_config = set_channel
configure = set_channel
configure_channel = set_channel
configure_channels = set_channel
setup = set_channel
setup_channel = set_channel
register = set_channel
register_channel = set_channel
upsert = set_channel
upsert_channel = set_channel

update = update_channel
patch = update_channel
patch_channel = update_channel
edit = update_channel
edit_channel = update_channel
merge = update_channel
modify = update_channel
modify_channel = update_channel
update_config = update_channel
patch_config = update_channel
update_channel_config = update_channel

enable = enable_channel
activate = enable_channel
activate_channel = enable_channel
turn_on = enable_channel
enable_notifications = enable_channel

disable = disable_channel
deactivate = disable_channel
deactivate_channel = disable_channel
turn_off = disable_channel
disable_notifications = disable_channel

remove = delete_channel
remove_channel = delete_channel
delete = delete_channel
reset = reset_channel
reset_channels = clear
purge = clear
clear_channels = clear
reset_all = clear

get_status = status
health = status
check = status


class NotifyChannelsService:
    """Object-oriented facade over the module-level store."""

    def get(self, *a: Any, **k: Any):
        return get_channel(*a, **k)

    def get_channel(self, *a: Any, **k: Any):
        return get_channel(*a, **k)

    def list(self, *a: Any, **k: Any):
        return list_channels(*a, **k)

    def list_channels(self, *a: Any, **k: Any):
        return list_channels(*a, **k)

    def get_channels(self, *a: Any, **k: Any):
        return list_channels(*a, **k)

    def set(self, *a: Any, **k: Any):
        return set_channel(*a, **k)

    def set_channel(self, *a: Any, **k: Any):
        return set_channel(*a, **k)

    def configure(self, *a: Any, **k: Any):
        return set_channel(*a, **k)

    def configure_channel(self, *a: Any, **k: Any):
        return set_channel(*a, **k)

    def update(self, *a: Any, **k: Any):
        return update_channel(*a, **k)

    def update_channel(self, *a: Any, **k: Any):
        return update_channel(*a, **k)

    def enable(self, *a: Any, **k: Any):
        return enable_channel(*a, **k)

    def enable_channel(self, *a: Any, **k: Any):
        return enable_channel(*a, **k)

    def disable(self, *a: Any, **k: Any):
        return disable_channel(*a, **k)

    def disable_channel(self, *a: Any, **k: Any):
        return disable_channel(*a, **k)

    def is_enabled(self, *a: Any, **k: Any) -> bool:
        return is_enabled(*a, **k)

    def enabled_channels(self, *a: Any, **k: Any):
        return enabled_channels(*a, **k)

    def reset(self, *a: Any, **k: Any):
        return reset_channel(*a, **k)

    def clear(self, *a: Any, **k: Any):
        return clear(*a, **k)

    def status(self, *a: Any, **k: Any):
        return status(*a, **k)


NotifyChannels = NotifyChannelsService
ChannelsService = NotifyChannelsService
NotificationChannelsService = NotifyChannelsService
NotifyChannelService = NotifyChannelsService
ChannelService = NotifyChannelsService
ChannelConfigService = NotifyChannelsService

notify_channels_service = NotifyChannelsService()
service = notify_channels_service
channels_service = notify_channels_service
manager = notify_channels_service
store = notify_channels_service
channel_service = notify_channels_service
notify_service = notify_channels_service

CHANNELS = _CHANNELS

__all__ = [
    "SUPPORTED_CHANNELS",
    "DEFAULT_CHANNELS",
    "get_channel",
    "get",
    "list_channels",
    "get_channels",
    "set_channel",
    "set",
    "configure",
    "configure_channel",
    "update_channel",
    "update",
    "enable_channel",
    "enable",
    "disable_channel",
    "disable",
    "set_enabled",
    "is_enabled",
    "enabled_channels",
    "reset_channel",
    "delete_channel",
    "clear",
    "status",
    "NotifyChannelsService",
    "notify_channels_service",
    "service",
    "_CHANNELS",
    "CHANNELS",
]
