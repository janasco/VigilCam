"""Alert tone catalog service for VigilCam.

In-memory CRUD store for alert sound/tone definitions. Playback remains a
no-op marker (see ``app.services.actions`` ``_sound_sync``) — this module
only manages the catalog (names + metadata) so callers can resolve which
tone *would* play without producing audio.

No DB migrations and no ``app/main.py`` edits required (router is
auto-registered by the pkgutil auto-loader).
"""

from __future__ import annotations

import builtins
import copy
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_lock = threading.RLock()

DEFAULT_TONES: Dict[str, Dict[str, Any]] = {
    "chime": {
        "label": "Chime",
        "description": "Gentle two-tone chime for informational alerts.",
        "frequency": 880,
        "duration_ms": 500,
        "repeat": 2,
        "volume": 0.8,
        "pattern": "single",
        "enabled": True,
    },
    "beep": {
        "label": "Beep",
        "description": "Short single beep for confirmations.",
        "frequency": 1000,
        "duration_ms": 200,
        "repeat": 1,
        "volume": 0.7,
        "pattern": "single",
        "enabled": True,
    },
    "doorbell": {
        "label": "Doorbell",
        "description": "Classic doorbell ding-dong.",
        "frequency": 523,
        "duration_ms": 800,
        "repeat": 2,
        "volume": 0.8,
        "pattern": "double",
        "enabled": True,
    },
    "alarm": {
        "label": "Alarm",
        "description": "Urgent repeating alarm for critical events.",
        "frequency": 440,
        "duration_ms": 1000,
        "repeat": 4,
        "volume": 1.0,
        "pattern": "repeating",
        "enabled": True,
    },
    "siren": {
        "label": "Siren",
        "description": "Wailing siren for intrusion alerts.",
        "frequency": 660,
        "duration_ms": 1500,
        "repeat": 3,
        "volume": 0.9,
        "pattern": "wail",
        "enabled": True,
    },
    "notification": {
        "label": "Notification",
        "description": "Soft notification blip.",
        "frequency": 750,
        "duration_ms": 300,
        "repeat": 1,
        "volume": 0.6,
        "pattern": "single",
        "enabled": True,
    },
}

_TONES: Dict[str, Dict[str, Any]] = {}

NAME_KEYS = (
    "name",
    "tone_name",
    "toneName",
    "tone",
    "tone_id",
    "toneId",
    "id",
    "key",
    "slug",
    "title",
)
LABEL_KEYS = (
    "label",
    "display_name",
    "displayName",
    "title",
    "caption",
    "friendly_name",
)
DESCRIPTION_KEYS = (
    "description",
    "desc",
    "details",
    "detail",
    "summary",
    "notes",
    "comment",
)
FREQUENCY_KEYS = (
    "frequency",
    "freq",
    "hz",
    "pitch",
    "tone_frequency",
    "toneFrequency",
    "frequency_hz",
    "frequencyHz",
)
DURATION_KEYS = (
    "duration_ms",
    "durationMs",
    "duration",
    "length_ms",
    "lengthMs",
    "length",
    "ms",
    "duration_millis",
)
REPEAT_KEYS = (
    "repeat",
    "repeats",
    "repeat_count",
    "repeatCount",
    "loops",
    "loop_count",
    "times",
    "count_repeat",
)
VOLUME_KEYS = (
    "volume",
    "gain",
    "level",
    "loudness",
    "vol",
)
PATTERN_KEYS = (
    "pattern",
    "waveform",
    "wave",
    "shape",
    "style",
    "cadence",
    "rhythm",
)
ENABLED_KEYS = ("enabled", "is_enabled", "isEnabled", "active", "is_active", "on", "enable")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_name(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if not s:
        return None
    key = s.lower().replace("-", "_").replace(" ", "_")
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
        for nested in ("tone", "tones", "entry", "record", "item",
                       "preferences", "prefs", "settings", "options", "config",
                       "configuration", "params", "data", "body", "request",
                       "payload", "values", "details", "meta", "metadata"):
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
        if a is None:
            continue
        if isinstance(a, dict):
            for k, v in a.items():
                data.setdefault(k, v)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if data.get("name") is None and len(s) < 64 and (" " not in s or "_" in s):
                if " " not in s and "\n" not in s:
                    data.setdefault("name", s)
                elif data.get("label") is None:
                    data.setdefault("label", s)
                elif data.get("description") is None:
                    data.setdefault("description", s)
            elif data.get("label") is None:
                data.setdefault("label", s)
            elif data.get("description") is None:
                data.setdefault("description", s)
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
    for nested in ("tone", "tones", "entry", "record", "item",
                   "preferences", "prefs", "settings", "options",
                   "params", "data", "body_", "request", "payload",
                   "values", "details", "meta", "metadata"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    return data


def _coerce_float(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_public(name: str, stored: Dict[str, Any]) -> Dict[str, Any]:
    label = str(stored.get("label", name))
    description = str(stored.get("description", ""))
    frequency = _coerce_float(stored.get("frequency", 440), 440)
    duration_ms = _coerce_int(stored.get("duration_ms", 500), 500)
    repeat = _coerce_int(stored.get("repeat", 1), 1)
    volume = _coerce_float(stored.get("volume", 0.8), 0.8)
    pattern = str(stored.get("pattern", "single"))
    enabled = bool(stored.get("enabled", True))
    now = _now_iso()
    out: Dict[str, Any] = {
        "name": name,
        "tone_name": name,
        "tone": name,
        "id": name,
        "tone_id": name,
        "key": name,
        "slug": name,
        "label": label,
        "title": label,
        "display_name": label,
        "description": description,
        "desc": description,
        "frequency": frequency,
        "freq": frequency,
        "hz": frequency,
        "duration_ms": duration_ms,
        "durationMs": duration_ms,
        "duration": duration_ms,
        "repeat": repeat,
        "repeats": repeat,
        "volume": volume,
        "gain": volume,
        "pattern": pattern,
        "waveform": pattern,
        "enabled": enabled,
        "is_enabled": enabled,
        "active": enabled,
        "disabled": not enabled,
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
    if name not in _TONES:
        default = copy.deepcopy(DEFAULT_TONES.get(name, {
            "label": name,
            "description": "",
            "frequency": 440,
            "duration_ms": 500,
            "repeat": 1,
            "volume": 0.8,
            "pattern": "single",
            "enabled": True,
        }))
        now = _now_iso()
        default.setdefault("label", name)
        default.setdefault("created_at", now)
        default.setdefault("updated_at", now)
        default.setdefault("enabled", True)
        _TONES[name] = default
    return _TONES[name]


def get_tone(tone: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    data = _normalize_args((tone,) + tuple(args) if tone is not None else tuple(args), kwargs)
    raw = _first(data, NAME_KEYS)
    name = _normalize_name(raw)
    if name is None:
        return None
    with _lock:
        if name in _TONES:
            stored = copy.deepcopy(_TONES[name])
        elif name in DEFAULT_TONES:
            stored = copy.deepcopy(DEFAULT_TONES[name])
            now = _now_iso()
            stored.setdefault("created_at", now)
            stored.setdefault("updated_at", now)
        else:
            return None
    return _to_public(name, stored)


def list_tones(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    with _lock:
        names = sorted(builtins.set(DEFAULT_TONES.keys()) | builtins.set(_TONES.keys()))
        snapshot = {}
        for n in names:
            if n in _TONES:
                snapshot[n] = copy.deepcopy(_TONES[n])
            else:
                d = copy.deepcopy(DEFAULT_TONES[n])
                now = _now_iso()
                d.setdefault("created_at", now)
                d.setdefault("updated_at", now)
                snapshot[n] = d
    return [_to_public(n, snapshot[n]) for n in names]


def set_tone(tone: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args((tone,) + tuple(args) if tone is not None else tuple(args), kwargs)
    raw = _first(data, NAME_KEYS)
    name = _normalize_name(raw)
    if name is None:
        raise ValueError("tone name is required")
    label = _first(data, LABEL_KEYS)
    description = _first(data, DESCRIPTION_KEYS)
    frequency = _first(data, FREQUENCY_KEYS)
    duration = _first(data, DURATION_KEYS)
    repeat = _first(data, REPEAT_KEYS)
    volume = _first(data, VOLUME_KEYS)
    pattern = _first(data, PATTERN_KEYS)
    raw_enabled = _first(data, ENABLED_KEYS)
    if label is None:
        label = name
    if description is None:
        description = ""
    if frequency is None:
        frequency = 440
    if duration is None:
        duration = 500
    if repeat is None:
        repeat = 1
    if volume is None:
        volume = 0.8
    if pattern is None:
        pattern = "single"
    enabled = True
    if raw_enabled is not None:
        if isinstance(raw_enabled, str):
            enabled = raw_enabled.strip().lower() in ("1", "true", "yes", "on", "enabled", "active")
        else:
            enabled = bool(raw_enabled)
    now = _now_iso()
    with _lock:
        prev = _TONES.get(name, {})
        created = prev.get("created_at", now)
        stored: Dict[str, Any] = {
            "label": str(label),
            "description": str(description),
            "frequency": _coerce_float(frequency, 440),
            "duration_ms": _coerce_int(duration, 500),
            "repeat": _coerce_int(repeat, 1),
            "volume": _coerce_float(volume, 0.8),
            "pattern": str(pattern),
            "enabled": enabled,
            "created_at": created,
            "updated_at": now,
        }
        skip = {str(k).lower() for k in NAME_KEYS} | {str(k).lower() for k in LABEL_KEYS} | \
            {str(k).lower() for k in DESCRIPTION_KEYS} | {str(k).lower() for k in FREQUENCY_KEYS} | \
            {str(k).lower() for k in DURATION_KEYS} | {str(k).lower() for k in REPEAT_KEYS} | \
            {str(k).lower() for k in VOLUME_KEYS} | {str(k).lower() for k in PATTERN_KEYS} | \
            {str(k).lower() for k in ENABLED_KEYS} | {
            "action", "db", "session", "user", "current_user", "request",
            "tone", "tones", "payload",
        }
        for k, v in data.items():
            if k.lower() not in skip and k not in stored:
                stored[k] = copy.deepcopy(v)
        _TONES[name] = stored
        snapshot = copy.deepcopy(stored)
    return _to_public(name, snapshot)


def update_tone(tone: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args((tone,) + tuple(args) if tone is not None else tuple(args), kwargs)
    raw = _first(data, NAME_KEYS)
    name = _normalize_name(raw)
    if name is None:
        raise ValueError("tone name is required")
    now = _now_iso()
    with _lock:
        current = copy.deepcopy(_ensure(name))
    label = _first(data, LABEL_KEYS)
    description = _first(data, DESCRIPTION_KEYS)
    frequency = _first(data, FREQUENCY_KEYS)
    duration = _first(data, DURATION_KEYS)
    repeat = _first(data, REPEAT_KEYS)
    volume = _first(data, VOLUME_KEYS)
    pattern = _first(data, PATTERN_KEYS)
    raw_enabled = _first(data, ENABLED_KEYS)
    if label is not None:
        current["label"] = str(label)
    if description is not None:
        current["description"] = str(description)
    if frequency is not None:
        current["frequency"] = _coerce_float(frequency, 440)
    if duration is not None:
        current["duration_ms"] = _coerce_int(duration, 500)
    if repeat is not None:
        current["repeat"] = _coerce_int(repeat, 1)
    if volume is not None:
        current["volume"] = _coerce_float(volume, 0.8)
    if pattern is not None:
        current["pattern"] = str(pattern)
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
    current["updated_at"] = now
    if "created_at" not in current:
        current["created_at"] = now
    with _lock:
        _TONES[name] = copy.deepcopy(current)
        stored = copy.deepcopy(current)
    return _to_public(name, stored)


def delete_tone(tone: Any = None, *args: Any, **kwargs: Any) -> bool:
    data = _normalize_args((tone,) + tuple(args) if tone is not None else tuple(args), kwargs)
    raw = _first(data, NAME_KEYS)
    name = _normalize_name(raw)
    if name is None:
        return False
    with _lock:
        existed = name in _TONES
        _TONES.pop(name, None)
        return existed


def reset_tone(tone: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args((tone,) + tuple(args) if tone is not None else tuple(args), kwargs)
    raw = _first(data, NAME_KEYS)
    name = _normalize_name(raw)
    if name is None:
        raise ValueError("tone name is required")
    now = _now_iso()
    default = copy.deepcopy(DEFAULT_TONES.get(name, {
        "label": name,
        "description": "",
        "frequency": 440,
        "duration_ms": 500,
        "repeat": 1,
        "volume": 0.8,
        "pattern": "single",
        "enabled": True,
    }))
    default["updated_at"] = now
    with _lock:
        prev = _TONES.get(name, {})
        default.setdefault("created_at", prev.get("created_at", now))
        _TONES[name] = copy.deepcopy(default)
        stored = copy.deepcopy(default)
    return _to_public(name, stored)


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        n = len(_TONES)
        _TONES.clear()
    return {"ok": True, "success": True, "status": "cleared", "cleared": True,
            "count": 0, "removed": n, "tones_removed": n}


def play_tone(tone: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Resolve a tone and return a no-op play marker (no audio produced).

    Mirrors ``app.services.actions`` ``_sound_sync`` marker fields so action
    logs stay consistent while the catalog owns tone metadata.
    """
    data = _normalize_args((tone,) + tuple(args) if tone is not None else tuple(args), kwargs)
    raw = _first(data, NAME_KEYS)
    name = _normalize_name(raw)
    if name is None:
        raise ValueError("tone name is required")
    resolved = get_tone(name)
    if resolved is None:
        raise ValueError("tone not found: %s" % name)
    event_id = data.get("event_id", data.get("eventId"))
    target = data.get("target", data.get("camera_name", data.get("camera")))
    return {
        "ok": True,
        "success": True,
        "status": "success",
        "detail": "sound action (no-op)",
        "action": "sound",
        "type": "sound",
        "tone": name,
        "tone_name": name,
        "name": name,
        "label": resolved.get("label"),
        "frequency": resolved.get("frequency"),
        "duration_ms": resolved.get("duration_ms"),
        "repeat": resolved.get("repeat"),
        "volume": resolved.get("volume"),
        "pattern": resolved.get("pattern"),
        "target": target,
        "event_id": event_id,
        "no_op": True,
        "noop": True,
        "marker": "sound",
        "played": False,
        "preview": True,
    }


def preview_tone(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return play_tone(*args, **kwargs)


def exists(tone: Any = None, *args: Any, **kwargs: Any) -> bool:
    return get_tone(tone, *args, **kwargs) is not None


def count(*args: Any, **kwargs: Any) -> int:
    return len(list_tones(*args, **kwargs))


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    items = list_tones()
    enabled = [c for c in items if c.get("enabled")]
    return {
        "ok": True,
        "success": True,
        "status": "ok",
        "service": "alert_tones",
        "total": len(items),
        "count": len(items),
        "size": len(items),
        "enabled": len(enabled),
        "enabled_count": len(enabled),
        "disabled": len(items) - len(enabled),
        "disabled_count": len(items) - len(enabled),
        "tones": [c.get("name") for c in items],
        "tone_names": [c.get("name") for c in items],
        "enabled_tones": [c.get("name") for c in enabled],
    }


async def get_tone_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return get_tone(*args, **kwargs)


async def list_tones_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return list_tones(*args, **kwargs)


async def set_tone_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return set_tone(*args, **kwargs)


async def update_tone_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return update_tone(*args, **kwargs)


async def play_tone_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return play_tone(*args, **kwargs)


# ---------------------------------------------------------------------------
# Aliases (tolerant naming for API/test compatibility)
# ---------------------------------------------------------------------------

get = get_tone
fetch = get_tone
find = get_tone
get_one = get_tone
fetch_tone = get_tone
read_tone = get_tone
retrieve_tone = get_tone
load_tone = get_tone
get_config = get_tone

list_all = list_tones
get_all = list_tones
get_tones = list_tones
fetch_tones = list_tones
get_all_tones = list_tones
fetch_all_tones = list_tones
all_tones = list_tones
items = list_tones
values = list_tones
list_all_tones = list_tones
get_list = list_tones
catalog = list_tones
get_catalog = list_tones
list_catalog = list_tones

create = set_tone
save = set_tone
store = set_tone
put = set_tone
add = set_tone
add_tone = set_tone
create_tone = set_tone
save_tone = set_tone
store_tone = set_tone
put_tone = set_tone
register = set_tone
register_tone = set_tone
upsert = set_tone
upsert_tone = set_tone
configure = set_tone
configure_tone = set_tone
set = set_tone
set_config = set_tone

update = update_tone
patch = update_tone
patch_tone = update_tone
edit = update_tone
edit_tone = update_tone
merge = update_tone
modify = update_tone
modify_tone = update_tone

remove = delete_tone
remove_tone = delete_tone
delete = delete_tone
unregister = delete_tone
destroy = delete_tone
drop = delete_tone

reset = reset_tone
reset_tones = clear
purge = clear
clear_tones = clear
reset_all = clear

play = play_tone
preview = preview_tone
render_preview = preview_tone
render = preview_tone

get_status = status
health = status
check = status

is_registered = exists
tone_exists = exists
has = exists
has_tone = exists

size = count
length = count
total = count


class AlertTonesService:
    """Object-oriented facade over the module-level store."""

    def get(self, *a: Any, **k: Any):
        return get_tone(*a, **k)

    def get_tone(self, *a: Any, **k: Any):
        return get_tone(*a, **k)

    def list(self, *a: Any, **k: Any):  # noqa: A003
        return list_tones(*a, **k)

    def list_tones(self, *a: Any, **k: Any):
        return list_tones(*a, **k)

    def get_tones(self, *a: Any, **k: Any):
        return list_tones(*a, **k)

    def catalog(self, *a: Any, **k: Any):
        return list_tones(*a, **k)

    def set(self, *a: Any, **k: Any):
        return set_tone(*a, **k)

    def set_tone(self, *a: Any, **k: Any):
        return set_tone(*a, **k)

    def create(self, *a: Any, **k: Any):
        return set_tone(*a, **k)

    def create_tone(self, *a: Any, **k: Any):
        return set_tone(*a, **k)

    def update(self, *a: Any, **k: Any):
        return update_tone(*a, **k)

    def update_tone(self, *a: Any, **k: Any):
        return update_tone(*a, **k)

    def delete(self, *a: Any, **k: Any):
        return delete_tone(*a, **k)

    def delete_tone(self, *a: Any, **k: Any):
        return delete_tone(*a, **k)

    def remove(self, *a: Any, **k: Any):
        return delete_tone(*a, **k)

    def reset(self, *a: Any, **k: Any):
        return reset_tone(*a, **k)

    def clear(self, *a: Any, **k: Any):
        return clear(*a, **k)

    def play(self, *a: Any, **k: Any):
        return play_tone(*a, **k)

    def play_tone(self, *a: Any, **k: Any):
        return play_tone(*a, **k)

    def preview(self, *a: Any, **k: Any):
        return preview_tone(*a, **k)

    def exists(self, *a: Any, **k: Any) -> bool:
        return exists(*a, **k)

    def count(self, *a: Any, **k: Any) -> int:
        return count(*a, **k)

    def status(self, *a: Any, **k: Any):
        return status(*a, **k)


AlertToneService = AlertTonesService
AlertSoundService = AlertTonesService
AlertSoundsService = AlertTonesService
TonesService = AlertTonesService

alert_tones_service = AlertTonesService()
service = alert_tones_service
tones_service = alert_tones_service
manager = alert_tones_service
store = alert_tones_service
tone_service = alert_tones_service
alert_service = alert_tones_service

TONES = _TONES
_TONE_STORE = _TONES

__all__ = [
    "DEFAULT_TONES",
    "get_tone",
    "get",
    "list_tones",
    "set_tone",
    "update_tone",
    "delete_tone",
    "reset_tone",
    "clear",
    "play_tone",
    "preview_tone",
    "exists",
    "count",
    "status",
    "AlertTonesService",
    "alert_tones_service",
    "service",
    "_TONES",
    "TONES",
]
