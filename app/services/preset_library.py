"""PTZ preset library service (in-memory).

A *PTZ preset library* entry is a stored, named PTZ position for a camera
(``camera_id`` + ``name`` plus optional device ``preset_token`` and
``pan``/``tilt``/``zoom`` coordinates). This is distinct from:

- :mod:`app.api.onvif_ptz` — live device calls (GetPresets / GotoPreset /
  SetPreset) that talk directly to the camera,
- :mod:`app.services.ptz_tour` — an ordered tour that moves *between*
  preset tokens,
- :mod:`app.services.preset_io` — camera configuration import/export,
- :mod:`app.services.motion_presets` — motion sensitivity presets.

The store is intentionally in-memory and thread-safe so the feature works
without DB migrations and without requiring ``app/main.py`` edits. The
API router (``app/api/preset_library.py``) is auto-registered by the
``pkgutil`` auto-loader in ``app/main.py``.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PRESET_LIBRARY: Dict[str, Dict[str, Any]] = {}
PRESETS = PRESET_LIBRARY
PRESET_STORE = PRESET_LIBRARY
STORE = PRESET_LIBRARY
ITEMS = PRESET_LIBRARY
JOBS = PRESET_LIBRARY
LIBRARY = PRESET_LIBRARY
PTZ_PRESETS = PRESET_LIBRARY
PTZ_PRESET_LIBRARY = PRESET_LIBRARY
preset_library = PRESET_LIBRARY
presets = PRESET_LIBRARY
library = PRESET_LIBRARY

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: Any = None) -> Any:
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


ID_KEYS = (
    "id",
    "preset_id",
    "presetId",
    "presetID",
    "preset_library_id",
    "presetLibraryId",
    "preset_uuid",
    "uuid",
)
CAMERA_KEYS = (
    "camera_id",
    "cameraId",
    "cameraID",
    "camera",
    "cam",
    "cam_id",
    "camId",
    "device_id",
    "deviceId",
    "source_id",
)
NAME_KEYS = (
    "name",
    "title",
    "label",
    "preset_name",
    "presetName",
    "preset_title",
    "display_name",
)
TOKEN_KEYS = (
    "preset_token",
    "presetToken",
    "token",
    "device_token",
    "deviceToken",
    "onvif_token",
    "ptz_token",
    "presetTokenId",
)
PAN_KEYS = ("pan", "x", "pan_position", "panPosition", "pan_pos")
TILT_KEYS = ("tilt", "y", "tilt_position", "tiltPosition", "tilt_pos")
ZOOM_KEYS = ("zoom", "z", "zoom_position", "zoomPosition", "zoom_pos")
DESC_KEYS = (
    "description",
    "desc",
    "note",
    "notes",
    "comment",
    "details",
    "memo",
)

_NESTED_KEYS = (
    "payload",
    "options",
    "config",
    "params",
    "data",
    "body",
    "request",
    "settings",
    "message",
    "preset",
    "preset_library",
    "presetLibrary",
    "entry",
    "item",
)


def _flatten(data: Any) -> Dict[str, Any]:
    if data is None:
        return {}
    if hasattr(data, "model_dump"):
        try:
            dumped = data.model_dump()
            if isinstance(dumped, dict):
                data = dumped
            else:
                return {}
        except Exception:  # noqa: BLE001
            pass
    if isinstance(data, dict):
        out = dict(data)
        for nested_key in _NESTED_KEYS:
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    if isinstance(data, (bytes, bytearray)):
        try:
            return {"name": bytes(data).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            return {}
    if isinstance(data, str):
        s = data.strip()
        if not s:
            return {}
        return {"name": s}
    try:
        attrs = {
            k: getattr(data, k)
            for k in dir(data)
            if not k.startswith("_") and not callable(getattr(data, k, None))
        }
        if attrs:
            return dict(attrs)
    except Exception:  # noqa: BLE001
        pass
    try:
        return dict(data)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return {}


def _norm(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            # Heuristic: uuid-like or single token -> treat as id.
            if "id" not in data and ("-" in s or len(s) >= 32 or " " not in s):
                # Prefer name if name already set; otherwise id.
                if "name" in data:
                    data.setdefault("id", s)
                else:
                    # Ambiguous single string: store as both candidate id and name;
                    # resolvers below pick appropriately.
                    data.setdefault("id", s)
                    data.setdefault("name", s)
            else:
                data.setdefault("name", s)
        else:
            flat = _flatten(a)
            if flat:
                data.update(flat)
    data.update(kwargs or {})
    # Unpack one level of nested envelopes.
    for nested_key in _NESTED_KEYS:
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


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


def _build_entry(data: Dict[str, Any]) -> Dict[str, Any]:
    sid = _first_present(data, ID_KEYS)
    sid_str = str(sid) if sid is not None and str(sid).strip() else str(uuid.uuid4())
    camera_id = _first_present(data, CAMERA_KEYS)
    name = _first_present(data, NAME_KEYS)
    token = _first_present(data, TOKEN_KEYS)
    # Disambiguate: if "preset" key held the name (single-string case where
    # id==name), token should be None unless explicitly token-like.
    pan = _first_present(data, PAN_KEYS)
    tilt = _first_present(data, TILT_KEYS)
    zoom = _first_present(data, ZOOM_KEYS)
    desc = _first_present(data, DESC_KEYS)

    if isinstance(name, str):
        name = name.strip()
    if name is None or (isinstance(name, str) and not name):
        # Fall back to token as display name when no explicit name given.
        if token is not None and str(token).strip():
            name = str(token).strip()
        else:
            name = f"preset-{sid_str[:8]}"

    now = _now_iso()
    entry: Dict[str, Any] = {
        "id": sid_str,
        "preset_id": sid_str,
        "presetId": sid_str,
        "uuid": sid_str,
        "camera_id": camera_id,
        "camera": camera_id,
        "cam": camera_id,
        "cameraId": camera_id,
        "device_id": camera_id,
        "name": str(name),
        "title": str(data.get("title", name)),
        "label": str(data.get("label", name)),
        "preset_name": str(name),
        "preset_token": str(token) if token is not None else None,
        "token": str(token) if token is not None else None,
        "preset": str(token) if token is not None else None,
        "pan": _coerce_float(pan, 0.0) if pan is not None else 0.0,
        "tilt": _coerce_float(tilt, 0.0) if tilt is not None else 0.0,
        "zoom": _coerce_float(zoom, 0.0) if zoom is not None else 0.0,
        "x": _coerce_float(pan, 0.0) if pan is not None else 0.0,
        "y": _coerce_float(tilt, 0.0) if tilt is not None else 0.0,
        "z": _coerce_float(zoom, 0.0) if zoom is not None else 0.0,
        "description": str(desc) if desc is not None else "",
        "created_at": str(data.get("created_at", now)),
        "updated_at": now,
        "ok": True,
        "success": True,
    }
    # Preserve caller-supplied extras (speed, profile, etc.).
    for k, v in data.items():
        if k not in entry:
            entry[k] = v
    # Keep alias timestamps in sync.
    entry["createdAt"] = entry["created_at"]
    entry["updatedAt"] = entry["updated_at"]
    return entry


class PresetLibraryService:
    """In-memory CRUD + recall engine for named PTZ presets."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.store = PRESET_LIBRARY

    def _store(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        with _lock:
            PRESET_LIBRARY[str(entry["id"])] = entry
        return entry

    def _find(self, sid: Any) -> Optional[Dict[str, Any]]:
        if sid is None:
            return None
        key = str(sid).strip()
        if not key:
            return None
        with _lock:
            if key in PRESET_LIBRARY:
                return PRESET_LIBRARY[key]
            # Fall back to name lookup (first match).
            for entry in PRESET_LIBRARY.values():
                if str(entry.get("name")) == key or str(entry.get("preset_token")) == key:
                    return entry
        return None

    # -- CRUD ---------------------------------------------------------
    def create_preset(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        # Single-string ambiguity: _norm sets both id and name to the same
        # value; a bare name must not become the id.
        if args and len(args) == 1 and isinstance(args[0], str) and not kwargs:
            data.pop("id", None)
            data.pop("preset_id", None)
            data.pop("presetId", None)
        camera_id = _first_present(data, CAMERA_KEYS)
        name = _first_present(data, NAME_KEYS)
        if camera_id is None or (isinstance(camera_id, str) and not str(camera_id).strip()):
            raise ValueError("camera_id is required")
        if name is None or (isinstance(name, str) and not str(name).strip()):
            raise ValueError("name is required")
        entry = _build_entry(data)
        return self._store(entry)

    def get_preset(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        data = _norm(*args, **kwargs)
        sid = _first_present(data, ID_KEYS)
        if sid is None and args:
            first = args[0]
            if isinstance(first, str):
                sid = first
        found = self._find(sid)
        return dict(found) if found is not None else None

    def list_presets(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        data = _norm(*args, **kwargs)
        # A bare single-string list call (e.g. list_presets("abc")) would put
        # "abc" into both id and name via _norm; that must not filter.
        if args and len(args) == 1 and isinstance(args[0], str) and not kwargs:
            data = {}
        elif args and len(args) == 1 and isinstance(args[0], dict) and not kwargs:
            pass
        cam = _first_present(data, CAMERA_KEYS)
        name_filter = _first_present(data, ("filter_name", "filter", "q", "search", "query"))
        # Only treat `name` as a filter when explicitly filtering (more keys
        # than just the ambiguous id/name pair).
        name_exact: Any = None
        if name_filter is not None:
            name_exact = name_filter
        elif "name" in data and ("camera_id" in data or "camera" in data or len(data) > 2):
            name_exact = data.get("name")
        with _lock:
            items = [dict(v) for v in PRESET_LIBRARY.values()]
        if cam is not None:
            items = [e for e in items if str(e.get("camera_id")) == str(cam)]
        if name_exact is not None and str(name_exact).strip():
            items = [e for e in items if str(e.get("name")) == str(name_exact).strip()]
        return items

    def update_preset(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        data = _norm(*args, **kwargs)
        sid = _first_present(data, ID_KEYS)
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        found = self._find(sid)
        if found is None:
            return None
        # Apply mutable fields.
        name = _first_present(data, NAME_KEYS)
        # Avoid treating the id itself as a name update when only id given.
        only_id = set(data.keys()) <= {"id", "name", "preset_id", "presetId", "uuid"}
        if name is not None and not (only_id and str(name) == str(sid)):
            found["name"] = str(name)
            found["preset_name"] = str(name)
            if "title" in data:
                found["title"] = str(data["title"])
            if "label" in data:
                found["label"] = str(data["label"])
        token = _first_present(data, TOKEN_KEYS)
        # Same guard: don't overwrite token with the id echo.
        if token is not None and not (only_id and str(token) == str(sid)):
            found["preset_token"] = str(token)
            found["token"] = str(token)
            found["preset"] = str(token)
        for key_group, field in ((PAN_KEYS, "pan"), (TILT_KEYS, "tilt"), (ZOOM_KEYS, "zoom")):
            val = _first_present(data, key_group)
            if val is not None:
                try:
                    found[field] = float(val)
                except (TypeError, ValueError):
                    found[field] = val
        # Keep x/y/z mirrors in sync.
        try:
            found["x"] = float(found.get("pan", 0.0))
        except (TypeError, ValueError):
            pass
        try:
            found["y"] = float(found.get("tilt", 0.0))
        except (TypeError, ValueError):
            pass
        try:
            found["z"] = float(found.get("zoom", 0.0))
        except (TypeError, ValueError):
            pass
        cam = _first_present(data, CAMERA_KEYS)
        if cam is not None:
            # Don't clobber camera with the id echo from single-string norm.
            if not (only_id and str(cam) == str(sid)):
                found["camera_id"] = cam
                found["camera"] = cam
                found["cam"] = cam
                found["cameraId"] = cam
                found["device_id"] = cam
        desc = _first_present(data, DESC_KEYS)
        if desc is not None:
            found["description"] = str(desc)
        for k, v in data.items():
            if k in ID_KEYS or k in ("created_at", "createdAt"):
                continue
            if k not in found:
                found[k] = v
        found["updated_at"] = _now_iso()
        found["updatedAt"] = found["updated_at"]
        return self._store(found)

    def delete_preset(self, *args: Any, **kwargs: Any) -> bool:
        data = _norm(*args, **kwargs)
        sid = _first_present(data, ID_KEYS)
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        if sid is None:
            return False
        key = str(sid)
        with _lock:
            return PRESET_LIBRARY.pop(key, None) is not None

    def clear(self, *args: Any, **kwargs: Any) -> int:
        data = _norm(*args, **kwargs)
        # Bare single-string clear() must clear everything, not filter.
        if args and len(args) == 1 and isinstance(args[0], str) and not kwargs:
            data = {}
        cam = _first_present(data, CAMERA_KEYS)
        with _lock:
            if cam is None and not data:
                n = len(PRESET_LIBRARY)
                PRESET_LIBRARY.clear()
                return n
            doomed = [
                k
                for k, v in list(PRESET_LIBRARY.items())
                if cam is None or str(v.get("camera_id")) == str(cam)
            ]
            for k in doomed:
                PRESET_LIBRARY.pop(k, None)
            return len(doomed)

    def count(self, *args: Any, **kwargs: Any) -> int:
        return len(self.list_presets(*args, **kwargs))

    def goto_preset(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = _first_present(data, ID_KEYS)
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        found = self._find(sid)
        if found is None:
            raise KeyError(f"preset not found: {sid}")
        result: Dict[str, Any] = {
            "success": True,
            "mock": True,
            "moved": True,
            "status": "moving",
            "ok": True,
            "id": found.get("id"),
            "preset_id": found.get("id"),
            "preset_token": found.get("preset_token"),
            "preset": found.get("preset_token"),
            "camera_id": found.get("camera_id"),
            "name": found.get("name"),
            "pan": found.get("pan"),
            "tilt": found.get("tilt"),
            "zoom": found.get("zoom"),
            "executed_at": _now_iso(),
        }
        return result

    # -- aliases ------------------------------------------------------
    def add_preset(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.create_preset(*a, **k)

    def create(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.create_preset(*a, **k)

    def add(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.create_preset(*a, **k)

    def get(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return self.get_preset(*a, **k)

    def fetch(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return self.get_preset(*a, **k)

    def list(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return self.list_presets(*a, **k)

    def all(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return self.list_presets(*a, **k)

    def update(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return self.update_preset(*a, **k)

    def edit(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return self.update_preset(*a, **k)

    def remove(self, *a: Any, **k: Any) -> bool:
        return self.delete_preset(*a, **k)

    def delete(self, *a: Any, **k: Any) -> bool:
        return self.delete_preset(*a, **k)

    def recall(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.goto_preset(*a, **k)

    def goto(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.goto_preset(*a, **k)

    def reset(self, *a: Any, **k: Any) -> int:
        return self.clear(*a, **k)

    def __len__(self) -> int:
        return len(PRESET_LIBRARY)


preset_library_service = PresetLibraryService()
presets_service = preset_library_service
service = preset_library_service
manager = preset_library_service
store = preset_library_service
default_service = preset_library_service
library_service = preset_library_service
ptz_preset_service = preset_library_service

PresetLibraryServiceAlias = PresetLibraryService
PresetLibrary = PresetLibraryService
PTZPresetService = PresetLibraryService
PresetService = PresetLibraryService


def create_preset(*a: Any, **k: Any) -> Dict[str, Any]:
    return preset_library_service.create_preset(*a, **k)


def get_preset(*a: Any, **k: Any) -> Optional[Dict[str, Any]]:
    return preset_library_service.get_preset(*a, **k)


def list_presets(*a: Any, **k: Any) -> List[Dict[str, Any]]:
    return preset_library_service.list_presets(*a, **k)


def update_preset(*a: Any, **k: Any) -> Optional[Dict[str, Any]]:
    return preset_library_service.update_preset(*a, **k)


def delete_preset(*a: Any, **k: Any) -> bool:
    return preset_library_service.delete_preset(*a, **k)


def goto_preset(*a: Any, **k: Any) -> Dict[str, Any]:
    return preset_library_service.goto_preset(*a, **k)


def clear(*a: Any, **k: Any) -> int:
    return preset_library_service.clear(*a, **k)


def count(*a: Any, **k: Any) -> int:
    return preset_library_service.count(*a, **k)


# Compatibility aliases.
create = create_preset
add = create_preset
add_preset = create_preset
save_preset = create_preset
store_preset = create_preset
get = get_preset
fetch = get_preset
fetch_preset = get_preset
read_preset = get_preset
load_preset = get_preset
list_all = list_presets
get_presets = list_presets
get_all = list_presets
all_presets = list_presets
fetch_presets = list_presets
update = update_preset
edit = update_preset
edit_preset = update_preset
patch_preset = update_preset
remove = delete_preset
delete = delete_preset
remove_preset = delete_preset
drop_preset = delete_preset
goto = goto_preset
recall = goto_preset
recall_preset = goto_preset
move_to_preset = goto_preset
apply_preset = goto_preset
reset = clear
purge = clear

__all__ = [
    "PRESET_LIBRARY",
    "PresetLibraryService",
    "preset_library_service",
    "create_preset",
    "get_preset",
    "list_presets",
    "update_preset",
    "delete_preset",
    "goto_preset",
    "clear",
    "count",
]
