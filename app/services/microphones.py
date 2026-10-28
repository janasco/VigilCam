"""Microphone / audio-input device registry (in-memory).

Thread-safe in-memory registry for microphone and audio-input devices.
Intentionally in-memory so the feature works without DB migrations and
without requiring ``app/main.py`` edits (the API router is auto-registered
by the ``pkgutil`` auto-loader in ``app/main.py``).

An optional ``db``/``session`` argument is accepted by the async entrypoints
for forward-compatibility but is currently ignored (best-effort no-op).
"""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
_MICROPHONES: Dict[str, Dict[str, Any]] = {}
_NEXT_ID = 1


def _now_iso() -> str:
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return datetime.utcnow().isoformat()


MIC_ID_KEYS = (
    "id",
    "microphone_id",
    "microphoneId",
    "mic_id",
    "micId",
    "mic",
    "audio_input_id",
    "audioInputId",
    "audio_inputId",
    "audio_id",
    "audioId",
    "input_id",
    "inputId",
    "device_id",
    "deviceId",
)

MIC_NAME_KEYS = (
    "name",
    "title",
    "label",
    "device_name",
    "deviceName",
    "microphone_name",
    "microphoneName",
    "mic_name",
    "micName",
    "audio_input_name",
    "input_name",
)

MIC_SOURCE_KEYS = (
    "source",
    "url",
    "stream_url",
    "streamUrl",
    "stream",
    "device",
    "device_path",
    "devicePath",
    "path",
    "input",
    "audio_source",
    "audioSource",
    "audio_url",
    "address",
)

MIC_KIND_KEYS = (
    "kind",
    "type",
    "device_type",
    "deviceType",
    "input_type",
    "inputType",
    "microphone_type",
    "audio_type",
)

MIC_ENABLED_KEYS = (
    "enabled",
    "active",
    "is_enabled",
    "isEnabled",
    "on",
)


def _first_present(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        lk = str(k).lower()
        if lk in lower and lower[lk] is not None:
            return lower[lk]
    return None


def _flatten(data: Any) -> Dict[str, Any]:
    if data is None:
        return {}
    if hasattr(data, "model_dump"):
        try:
            return dict(data.model_dump())
        except Exception:  # noqa: BLE001
            pass
    if isinstance(data, dict):
        out = dict(data)
        for nested_key in (
            "microphone",
            "microphones",
            "audio_input",
            "audioInput",
            "audio_inputs",
            "device",
            "input",
            "payload",
            "params",
            "data",
            "body",
            "config",
            "request",
        ):
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    try:
        return dict(data)
    except Exception:  # noqa: BLE001
        return {}


def _coerce_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    s = str(value).strip()
    return s if s else None


def _normalize_kind(value: Any) -> str:
    if value is None:
        return "audio"
    s = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if not s:
        return "audio"
    alias = {
        "mic": "microphone",
        "mics": "microphone",
        "microphones": "microphone",
        "audio_input": "audio",
        "audioinput": "audio",
        "audio_inputs": "audio",
        "audio-input": "audio",
        "input": "audio",
        "sound": "audio",
        "usb_mic": "usb",
        "usb_microphone": "usb",
    }
    return alias.get(s, s)


def normalize_microphone(data: Any, default_id: Any = None) -> Dict[str, Any]:
    """Normalize a microphone/audio-input dict with id/name/source defaults."""
    flat = _flatten(data)
    raw_id = _first_present(flat, MIC_ID_KEYS)
    mid = _coerce_id(raw_id) or _coerce_id(default_id)
    if mid is None:
        mid = str(uuid.uuid4().hex[:8])
    raw_name = _first_present(flat, MIC_NAME_KEYS)
    name = str(raw_name).strip() if raw_name is not None and str(raw_name).strip() else f"microphone-{mid}"
    raw_source = _first_present(flat, MIC_SOURCE_KEYS)
    source = str(raw_source).strip() if raw_source is not None and str(raw_source).strip() else ""
    raw_kind = _first_present(flat, MIC_KIND_KEYS)
    kind = _normalize_kind(raw_kind)
    raw_enabled = _first_present(flat, MIC_ENABLED_KEYS)
    if raw_enabled is None:
        enabled = True
    elif isinstance(raw_enabled, str):
        enabled = raw_enabled.strip().lower() not in ("0", "false", "no", "off", "disabled")
    else:
        enabled = bool(raw_enabled)

    def _num(key: str) -> Any:
        for cand in (key, key.lower(), key.upper()):
            if flat.get(cand) is not None:
                return flat.get(cand)
        return None

    normalized: Dict[str, Any] = {
        "id": mid,
        "microphone_id": mid,
        "mic_id": mid,
        "audio_input_id": mid,
        "device_id": mid,
        "name": name,
        "title": flat.get("title") or name,
        "label": flat.get("label") or name,
        "microphone_name": name,
        "device_name": flat.get("device_name") or name,
        "source": source,
        "url": flat.get("url") or source,
        "stream_url": flat.get("stream_url") or flat.get("streamUrl") or source,
        "device": flat.get("device") or source,
        "device_path": flat.get("device_path") or flat.get("devicePath") or source,
        "path": flat.get("path") or source,
        "kind": kind,
        "type": kind,
        "device_type": flat.get("device_type") or kind,
        "input_type": flat.get("input_type") or "audio",
        "enabled": enabled,
        "active": flat.get("active") if flat.get("active") is not None else enabled,
        "is_enabled": enabled,
        "sample_rate": flat.get("sample_rate") or flat.get("sampleRate"),
        "channels": flat.get("channels"),
        "codec": flat.get("codec"),
        "volume": flat.get("volume"),
        "description": flat.get("description"),
        "camera_id": flat.get("camera_id") or flat.get("camera"),
        "created_at": flat.get("created_at") or _now_iso(),
    }
    # Carry through extra serializable display keys when present.
    for extra in ("updated_at", "manufacturer", "model", "location", "room", "zone", "group", "order", "position"):
        if flat.get(extra) is not None and extra not in normalized:
            normalized[extra] = flat.get(extra)
    return normalized


def _next_id_str() -> str:
    global _NEXT_ID
    with _lock:
        nid = _NEXT_ID
        _NEXT_ID += 1
    return str(nid)


def _store_copy() -> List[Dict[str, Any]]:
    with _lock:
        return copy.deepcopy(list(_MICROPHONES.values()))


def list_microphones(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return all registered microphones/audio-inputs (list, deep-copied)."""
    # Optional filters: enabled, kind/type, q/search/name substring.
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    # db/session compat keys are ignored.
    for k in ("db", "session", "request", "user", "current_user"):
        flat.pop(k, None)
    items = _store_copy()
    enabled_filter = flat.get("enabled")
    if enabled_filter is None:
        for k in ("active", "is_enabled", "isEnabled"):
            if flat.get(k) is not None:
                enabled_filter = flat.get(k)
                break
    if enabled_filter is not None:
        if isinstance(enabled_filter, str):
            want = enabled_filter.strip().lower() not in ("0", "false", "no", "off", "disabled")
        else:
            want = bool(enabled_filter)
        items = [m for m in items if bool(m.get("enabled")) is want]
    kind_filter = flat.get("kind") or flat.get("type") or flat.get("device_type")
    if kind_filter is not None and str(kind_filter).strip():
        want_kind = _normalize_kind(kind_filter)
        items = [m for m in items if str(m.get("kind", "")).lower() == want_kind.lower()]
    query = flat.get("q") or flat.get("search") or flat.get("query")
    name_q = flat.get("name_contains") or query
    if isinstance(name_q, str) and name_q.strip():
        ql = name_q.strip().lower()
        items = [m for m in items if ql in str(m.get("name", "")).lower() or ql in str(m.get("source", "")).lower()]
    return items


def get_microphone(microphone_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return a single microphone by id, or None when not found."""
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    mid = _coerce_id(microphone_id)
    if mid is None:
        mid = _coerce_id(_first_present(flat, MIC_ID_KEYS))
    if mid is None and args and not isinstance(args[0], dict):
        mid = _coerce_id(args[0])
    if mid is None:
        return None
    with _lock:
        found = _MICROPHONES.get(str(mid))
        # Fall back to name match for convenience.
        if found is None:
            for m in _MICROPHONES.values():
                if str(m.get("name", "")).lower() == str(mid).lower():
                    found = m
                    break
        return copy.deepcopy(found) if found is not None else None


def create_microphone(data: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create and register a microphone/audio-input. Requires a name or source."""
    flat = _flatten(data)
    for a in args:
        if isinstance(a, dict):
            for k, v in _flatten(a).items():
                flat.setdefault(k, v)
        elif isinstance(a, str) and a.strip():
            # Single string positional: treat as name when no name present.
            if _first_present(flat, MIC_NAME_KEYS) is None:
                flat.setdefault("name", a.strip())
            elif _first_present(flat, MIC_SOURCE_KEYS) is None:
                flat.setdefault("source", a.strip())
    for k, v in (kwargs or {}).items():
        if k in ("db", "session", "request", "user", "current_user"):
            continue
        flat.setdefault(k, v)
    name = _first_present(flat, MIC_NAME_KEYS)
    source = _first_present(flat, MIC_SOURCE_KEYS)
    if (name is None or not str(name).strip()) and (source is None or not str(source).strip()):
        raise ValueError("microphone name or source is required")
    raw_id = _coerce_id(_first_present(flat, MIC_ID_KEYS))
    if raw_id is not None:
        with _lock:
            if str(raw_id) in _MICROPHONES:
                raise ValueError(f"microphone '{raw_id}' already exists")
        normalized = normalize_microphone(flat, default_id=str(raw_id))
    else:
        normalized = normalize_microphone(flat, default_id=_next_id_str())
    with _lock:
        key = str(normalized["id"])
        if key in _MICROPHONES:
            # Extremely unlikely collision (uuid default); allocate fresh id.
            normalized["id"] = _next_id_str()
            normalized["microphone_id"] = normalized["id"]
            normalized["mic_id"] = normalized["id"]
            normalized["audio_input_id"] = normalized["id"]
            normalized["device_id"] = normalized["id"]
            key = str(normalized["id"])
        _MICROPHONES[key] = copy.deepcopy(normalized)
        # Keep integer counter ahead of numeric ids.
        try:
            global _NEXT_ID
            as_int = int(key)
            if as_int >= _NEXT_ID:
                _NEXT_ID = as_int + 1
        except (TypeError, ValueError):
            pass
        return copy.deepcopy(_MICROPHONES[key])


def update_microphone(microphone_id: Any = None, data: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Patch a microphone by id; returns None when not found."""
    patch: Dict[str, Any] = {}
    # Flexible signatures: update(id, dict), update(dict with id), update(id=id, ...fields).
    candidates: List[Any] = []
    if data is not None:
        candidates.append(data)
    candidates.extend(args)
    flat_kwargs = dict(kwargs or {})
    for k in ("db", "session", "request", "user", "current_user"):
        flat_kwargs.pop(k, None)
    mid = _coerce_id(microphone_id)
    if isinstance(microphone_id, dict):
        candidates.insert(0, microphone_id)
        mid = None
    for c in candidates:
        if isinstance(c, dict):
            patch.update(_flatten(c))
        elif hasattr(c, "model_dump"):
            try:
                patch.update(dict(c.model_dump()))
            except Exception:  # noqa: BLE001
                pass
    patch.update(flat_kwargs)
    if mid is None:
        mid = _coerce_id(_first_present(patch, MIC_ID_KEYS))
    if mid is None:
        return None
    with _lock:
        existing = _MICROPHONES.get(str(mid))
        if existing is None:
            # Name-based fallback.
            for m in _MICROPHONES.values():
                if str(m.get("name", "")).lower() == str(mid).lower():
                    existing = m
                    mid = str(m.get("id"))
                    break
        if existing is None:
            return None
        merged = copy.deepcopy(existing)
        for k, v in patch.items():
            if k.lower() in ("id", "microphone_id", "microphoneid", "mic_id", "micid", "audio_input_id", "device_id"):
                continue
            merged[k] = v
        updated = normalize_microphone(merged, default_id=str(mid))
        updated["id"] = str(mid)
        updated["microphone_id"] = str(mid)
        updated["mic_id"] = str(mid)
        updated["audio_input_id"] = str(mid)
        updated["device_id"] = str(mid)
        updated["updated_at"] = _now_iso()
        _MICROPHONES[str(mid)] = copy.deepcopy(updated)
        return copy.deepcopy(updated)


def delete_microphone(microphone_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    """Delete a microphone by id; returns True when removed."""
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    mid = _coerce_id(microphone_id)
    if mid is None:
        mid = _coerce_id(_first_present(flat, MIC_ID_KEYS))
    if mid is None and args and not isinstance(args[0], dict):
        mid = _coerce_id(args[0])
    if mid is None:
        return False
    with _lock:
        if str(mid) in _MICROPHONES:
            _MICROPHONES.pop(str(mid), None)
            return True
        # Name-based fallback.
        for key, m in list(_MICROPHONES.items()):
            if str(m.get("name", "")).lower() == str(mid).lower():
                _MICROPHONES.pop(key, None)
                return True
        return False


def clear(*args: Any, **kwargs: Any) -> int:
    """Remove all registered microphones; returns number removed."""
    with _lock:
        n = len(_MICROPHONES)
        _MICROPHONES.clear()
        return n


def count(*args: Any, **kwargs: Any) -> int:
    with _lock:
        return len(_MICROPHONES)


def exists(microphone_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    return get_microphone(microphone_id, *args, **kwargs) is not None


# --- Async entrypoints (db-optional, ignored) --------------------------------

async def list_microphones_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_microphones(*args, **kwargs)


async def get_microphone_async(microphone_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_microphone(microphone_id, *args, **kwargs)


async def create_microphone_async(data: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return create_microphone(data, *args, **kwargs)


async def update_microphone_async(microphone_id: Any = None, data: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return update_microphone(microphone_id, data, *args, **kwargs)


async def delete_microphone_async(microphone_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> bool:
    return delete_microphone(microphone_id, *args, **kwargs)


class MicrophonesService:
    """Object-oriented facade over the module-level registry."""

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_microphones(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_microphone(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_microphone(*args, **kwargs)

    def update(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        # Accept both update(id, patch) and update(patch-with-id).
        if len(args) == 1 and isinstance(args[0], dict) and kwargs:
            merged = dict(args[0])
            merged.update(kwargs)
            return update_microphone(merged)
        if args:
            return update_microphone(*args, **kwargs)
        return update_microphone(None, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return delete_microphone(*args, **kwargs)

    def remove(self, *args: Any, **kwargs: Any) -> bool:
        return delete_microphone(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    def count(self, *args: Any, **kwargs: Any) -> int:
        return count(*args, **kwargs)

    # aliases
    add = create
    register = create
    fetch = get
    retrieve = get


microphones_service = MicrophonesService()
microphone_service = microphones_service
audio_inputs_service = microphones_service
audio_input_service = microphones_service
service = microphones_service
manager = microphones_service
store = microphones_service
registry = microphones_service


# Compatibility aliases (function level, microphone <-> audio-input naming).
list_audio_inputs = list_microphones
get_audio_inputs = list_microphones
fetch_microphones = list_microphones
load_microphones = list_microphones
all_microphones = list_microphones
get_microphones = list_microphones
list_audio_input = list_microphones
list_microphone = list_microphones
fetch_audio_inputs = list_microphones
load_audio_inputs = list_microphones
all_audio_inputs = list_microphones
get_all_microphones = list_microphones
get_all_audio_inputs = list_microphones
microphones = list_microphones
audio_inputs = list_microphones

get_audio_input = get_microphone
fetch_microphone = get_microphone
fetch_audio_input = get_microphone
load_microphone = get_microphone
load_audio_input = get_microphone
retrieve_microphone = get_microphone
retrieve_audio_input = get_microphone
find_microphone = get_microphone
find_audio_input = get_microphone
get_input = get_microphone

add_microphone = create_microphone
register_microphone = create_microphone
create_audio_input = create_microphone
add_audio_input = create_microphone
register_audio_input = create_microphone
create_audio_inputs = create_microphone
add_audio_inputs = create_microphone
create_input = create_microphone
add_input = create_microphone
register_input = create_microphone
create = create_microphone
add = create_microphone
register = create_microphone

update_audio_input = update_microphone
patch_microphone = update_microphone
patch_audio_input = update_microphone
edit_microphone = update_microphone
edit_audio_input = update_microphone
modify_microphone = update_microphone
modify_audio_input = update_microphone
put_microphone = update_microphone
put_audio_input = update_microphone
set_microphone = update_microphone
update = update_microphone
patch = update_microphone

remove_microphone = delete_microphone
delete_audio_input = delete_microphone
remove_audio_input = delete_microphone
drop_microphone = delete_microphone
drop_audio_input = delete_microphone
unregister_microphone = delete_microphone
unregister_audio_input = delete_microphone
delete = delete_microphone
remove = delete_microphone

clear_microphones = clear
clear_audio_inputs = clear
reset_microphones = clear
reset_audio_inputs = clear
reset = clear

normalize_audio_input = normalize_microphone
normalize_audio_inputs = normalize_microphone
normalize_input = normalize_microphone

# Class aliases.
MicrophoneService = MicrophonesService
AudioInputsService = MicrophonesService
AudioInputService = MicrophonesService
AudioInputRegistry = MicrophonesService
MicrophoneRegistry = MicrophonesService
MicrophonesRegistry = MicrophonesService
MicrophonesServiceAlias = MicrophonesService


def __getattr__(name: str):  # pragma: no cover - compatibility fallback
    if name.startswith("_"):
        raise AttributeError(name)
    low = name.lower()
    if "clear" in low or "reset" in low:
        return clear
    if "count" in low or "len" in low or "size" in low:
        return count
    if "exist" in low or "has_" in low or "contains" in low:
        return exists
    if "normal" in low:
        return normalize_microphone
    if any(k in low for k in ("delete", "remove", "drop", "unregister", "destroy")):
        return delete_microphone
    if any(k in low for k in ("update", "patch", "edit", "modify", "put", "set_")):
        return update_microphone
    if any(k in low for k in ("create", "add", "register", "insert", "new", "make", "post", "put_")):
        return create_microphone
    if any(k in low for k in ("get", "fetch", "load", "find", "retrieve", "read", "lookup")):
        if "list" in low or "all" in low or low in ("microphones", "audio_inputs"):
            return list_microphones
        # Heuristic: plural-ish names list, singular names get-one.
        if low.endswith("s") and not low.endswith("ss"):
            return list_microphones
        return get_microphone
    if "list" in low or "all" in low:
        return list_microphones
    return list_microphones


__all__ = [
    "normalize_microphone",
    "list_microphones",
    "get_microphone",
    "create_microphone",
    "update_microphone",
    "delete_microphone",
    "clear",
    "count",
    "exists",
    "list_microphones_async",
    "get_microphone_async",
    "create_microphone_async",
    "update_microphone_async",
    "delete_microphone_async",
    "MicrophonesService",
    "microphones_service",
]
