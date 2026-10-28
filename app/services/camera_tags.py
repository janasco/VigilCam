"""Camera tagging service (in-memory, per-camera tag lists).

Study notes (read-only, ``/opt/others/ispy-agent-api`` + VigilCam models):

* iSpy Agent DVR concepts: devices are targeted by object id + object-type id,
  **or** by ``group`` (all devices in that General-tab group), **or** every
  device when neither is given. Bulk on/off/schedule commands filter with a
  csv, case-insensitive ``group`` param plus a ``location`` param. Separately,
  recordings/alerts carry ``tag``/``tags`` (and AI JSON) and rules match on
  ``tags``/``tagrule`` plus ``group:<name>`` / ``location:<name>`` selectors.
  So iSpy distinguishes *device grouping* (one group string per device, used
  for bulk targeting) from *tagging* (free-form multi-labels on media/rules).
* VigilCam verification: ``Camera`` (``app/models/entities.py``) has only
  ``group`` (``String(120)``) + ``group_id`` (FK to ``Group``) -- there is no
  ``tags``/``tag`` column on cameras. ``tags`` exists only on ``Event``
  (``JSON`` + ``tag`` synonym, normalized/deduped). ``CameraBase`` schema
  likewise exposes ``group``/``group_id`` and no tag list.

This module fills the camera-tagging gap without a DB migration: a
thread-safe in-memory store mapping camera id -> ordered, de-duplicated tag
list. An optional ``db``/``session`` argument is accepted by the async
entrypoints for forward-compatibility but is ignored (best-effort no-op),
mirroring ``app.services.camera_notes``.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
_TAGS: Dict[str, List[str]] = {}
_ID_DISPLAY: Dict[str, Any] = {}

MAX_TAG_LENGTH = 64
MAX_TAGS_PER_CAMERA = 50

TAG_KEYS = (
    "tags", "tag", "tag_list", "tagList", "labels", "label",
    "values", "value", "items", "item", "tagNames", "tagnames",
    "tag_names", "tagName", "tagname",
)
CAMERA_ID_KEYS = (
    "camera_id", "cameraId", "camera", "cameraid",
    "cam_id", "camId", "cam", "source_id", "sourceId",
    "id_camera", "for_camera", "cameraID",
)


def _coerce_tag_items(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, dict):
        for key in ("tags", "tag", "values", "items", "labels", "tag_list"):
            if key in value and value.get(key) is not None:
                return _coerce_tag_items(value.get(key))
        return []
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except Exception:
            parsed = None
        if parsed is not None:
            if isinstance(parsed, str):
                return [parsed]
            if isinstance(parsed, (list, tuple, set)):
                out: List[Any] = []
                for item in parsed:
                    out.extend(_coerce_tag_items(item))
                return out
            if isinstance(parsed, dict):
                return _coerce_tag_items(parsed)
            return [parsed]
        return [part for part in s.split(",")]
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(_coerce_tag_items(item))
        return out
    return [value]


def normalize_tag(tag: Any) -> Optional[str]:
    """Normalize one tag; return ``None`` when it carries no value."""
    if tag is None or isinstance(tag, bool):
        return None
    s = str(tag).strip()
    if not s:
        return None
    if s.lower() in {"none", "null"}:
        return None
    if len(s) > MAX_TAG_LENGTH:
        s = s[:MAX_TAG_LENGTH].strip()
        if not s:
            return None
    return s


def normalize_tags(value: Any) -> List[str]:
    """Flatten + strip + case-insensitive-dedup, preserving first order."""
    out: List[str] = []
    seen = set()
    for raw in _coerce_tag_items(value):
        s = normalize_tag(raw)
        if s is None:
            continue
        lowered = s.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(s)
    return out


def _normalize_camera_key(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return str(int(s))
    except (TypeError, ValueError):
        pass
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return s


def _display_camera_id(key: str) -> Any:
    if key in _ID_DISPLAY:
        return _ID_DISPLAY[key]
    try:
        return int(key)
    except (TypeError, ValueError):
        return key


def _first_present(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _extract_camera_key(*args: Any, **kwargs: Any) -> Optional[str]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (int, float)) and not any(k in data for k in CAMERA_ID_KEYS):
            data["camera_id"] = a
    data.update(kwargs or {})
    raw = _first_present(data, CAMERA_ID_KEYS)
    if raw is None and len(args) == 1 and isinstance(args[0], (int, float)):
        raw = args[0]
    if raw is None:
        return None
    return _normalize_camera_key(raw)


def _extract_tags_value(*args: Any, **kwargs: Any) -> Any:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            for k, v in a.items():
                data.setdefault(k, v)
        elif isinstance(a, (list, tuple, set)):
            data.setdefault("tags", list(a))
        elif isinstance(a, str):
            data.setdefault("tags", a)
    data.update(kwargs or {})
    raw = _first_present(data, TAG_KEYS)
    if raw is None:
        # bare list/str positional
        for a in args:
            if isinstance(a, (list, tuple, set, str)):
                return a
        return []
    return raw


def get_tags(camera_id: Any = None, *args: Any, **kwargs: Any) -> List[str]:
    """Return the ordered tag list for a camera (``[]`` when untagged)."""
    key: Optional[str] = None
    if kwargs or args:
        key = _extract_camera_key(*(([camera_id] if camera_id is not None else []) + list(args)), **kwargs)
    else:
        key = _normalize_camera_key(camera_id)
    if key is None:
        return []
    with _lock:
        return list(_TAGS.get(key, []))


def set_tags(camera_id: Any = None, tags: Any = None, *args: Any, **kwargs: Any) -> List[str]:
    """Replace the tag list for a camera. ``tags`` may be any tag-like value."""
    if tags is None:
        tags = _extract_tags_value(*args, **kwargs)
        key = _extract_camera_key(camera_id, *args, **kwargs)
    else:
        extra = _extract_tags_value(*args, **kwargs)
        extra_norm = normalize_tags(extra)
        key = _extract_camera_key(camera_id, *args, **kwargs)
        base = normalize_tags(tags)
        if extra_norm:
            seen = {t.lower() for t in base}
            for t in extra_norm:
                if t.lower() not in seen:
                    seen.add(t.lower())
                    base.append(t)
        tags = base
        if key is None:
            key = _normalize_camera_key(camera_id)
        with _lock:
            cleaned = normalize_tags(tags)[:MAX_TAGS_PER_CAMERA]
            if cleaned:
                _TAGS[key] = cleaned
                try:
                    _ID_DISPLAY[key] = int(key)
                except (TypeError, ValueError):
                    _ID_DISPLAY[key] = camera_id
            else:
                _TAGS.pop(key, None)
            return list(_TAGS.get(key, []))
    if key is None:
        key = _normalize_camera_key(camera_id)
    if key is None:
        raise ValueError("camera_id is required")
    with _lock:
        cleaned = normalize_tags(tags)[:MAX_TAGS_PER_CAMERA]
        if cleaned:
            _TAGS[key] = cleaned
            if key not in _ID_DISPLAY:
                try:
                    _ID_DISPLAY[key] = int(key)
                except (TypeError, ValueError):
                    _ID_DISPLAY[key] = key
        else:
            _TAGS.pop(key, None)
        return list(_TAGS.get(key, []))


def add_tags(camera_id: Any = None, tags: Any = None, *args: Any, **kwargs: Any) -> List[str]:
    """Union ``tags`` into the camera's list; returns the updated list."""
    if tags is None:
        tags = _extract_tags_value(*args, **kwargs)
    key = _extract_camera_key(camera_id, *args, **kwargs)
    if key is None:
        key = _normalize_camera_key(camera_id)
    if key is None:
        raise ValueError("camera_id is required")
    new_tags = normalize_tags(tags)
    with _lock:
        current = list(_TAGS.get(key, []))
        seen = {t.lower() for t in current}
        for t in new_tags:
            if t.lower() not in seen:
                seen.add(t.lower())
                current.append(t)
        current = current[:MAX_TAGS_PER_CAMERA]
        if current:
            _TAGS[key] = current
            if key not in _ID_DISPLAY:
                try:
                    _ID_DISPLAY[key] = int(key)
                except (TypeError, ValueError):
                    _ID_DISPLAY[key] = key
        else:
            _TAGS.pop(key, None)
        return list(_TAGS.get(key, []))


def remove_tags(camera_id: Any = None, tags: Any = None, *args: Any, **kwargs: Any) -> List[str]:
    """Remove ``tags`` from the camera's list; returns the updated list."""
    if tags is None:
        tags = _extract_tags_value(*args, **kwargs)
    key = _extract_camera_key(camera_id, *args, **kwargs)
    if key is None:
        key = _normalize_camera_key(camera_id)
    if key is None:
        raise ValueError("camera_id is required")
    doomed = {t.lower() for t in normalize_tags(tags)}
    with _lock:
        current = [t for t in _TAGS.get(key, []) if t.lower() not in doomed]
        if current:
            _TAGS[key] = current
        else:
            _TAGS.pop(key, None)
        return list(current)


def clear_tags(camera_id: Any = None, *args: Any, **kwargs: Any) -> int:
    """Remove all tags for one camera; returns the number removed."""
    key = _extract_camera_key(*(([camera_id] if camera_id is not None else []) + list(args)), **kwargs)
    if key is None:
        key = _normalize_camera_key(camera_id)
    if key is None:
        return 0
    with _lock:
        return len(_TAGS.pop(key, []))


def has_tag(camera_id: Any = None, tag: Any = None, *args: Any, **kwargs: Any) -> bool:
    """Case-insensitive membership check for a single tag."""
    if tag is None:
        tag = _first_present(dict(kwargs), ("tag", "label", "value", "item", "name"))
        if tag is None and args:
            tag = args[-1] if len(args) > 1 else args[0]
    norm = normalize_tag(tag)
    if norm is None:
        return False
    return norm.lower() in {t.lower() for t in get_tags(camera_id, **kwargs)}


def list_all_tags(*args: Any, **kwargs: Any) -> List[str]:
    """All distinct tags in use, sorted case-insensitively."""
    with _lock:
        seen: Dict[str, str] = {}
        for tags in _TAGS.values():
            for t in tags:
                if t.lower() not in seen:
                    seen[t.lower()] = t
    return [seen[k] for k in sorted(seen.keys())]


def tag_counts(*args: Any, **kwargs: Any) -> Dict[str, int]:
    """Map of display-tag -> number of cameras carrying it."""
    with _lock:
        counts: Dict[str, int] = {}
        canon: Dict[str, str] = {}
        for tags in _TAGS.values():
            for t in tags:
                lowered = t.lower()
                canon.setdefault(lowered, t)
                counts[lowered] = counts.get(lowered, 0) + 1
    return {canon[k]: v for k, v in counts.items()}


def cameras_with_tag(tag: Any, *args: Any, **kwargs: Any) -> List[Any]:
    """Camera ids carrying ``tag`` (case-insensitive)."""
    norm = normalize_tag(tag)
    if norm is None:
        return []
    lowered = norm.lower()
    with _lock:
        out = []
        for key, tags in _TAGS.items():
            if any(t.lower() == lowered for t in tags):
                out.append(_display_camera_id(key))
    try:
        return sorted(out, key=lambda v: (str(type(v)), str(v)))
    except Exception:
        return out


def find_cameras_by_tags(tags: Any = None, *args: Any, match: str = "any", **kwargs: Any) -> List[Any]:
    """Find camera ids matching ``tags`` with ``any`` (default) or ``all``."""
    if tags is None:
        tags = _extract_tags_value(*args, **kwargs)
    if isinstance(kwargs.get("match"), str):
        match = kwargs["match"]
    wanted = [t.lower() for t in normalize_tags(tags)]
    if not wanted:
        return []
    strict = str(match).strip().lower() in {"all", "every", "and"}
    with _lock:
        out = []
        for key, current in _TAGS.items():
            have = {t.lower() for t in current}
            if strict:
                if all(w in have for w in wanted):
                    out.append(_display_camera_id(key))
            elif any(w in have for w in wanted):
                out.append(_display_camera_id(key))
    try:
        return sorted(out, key=lambda v: (str(type(v)), str(v)))
    except Exception:
        return out


def count_cameras_with_tag(tag: Any) -> int:
    return len(cameras_with_tag(tag))


def clear(*args: Any, **kwargs: Any) -> int:
    """Reset the whole store; returns the number of tag associations removed."""
    with _lock:
        n = sum(len(v) for v in _TAGS.values())
        _TAGS.clear()
        _ID_DISPLAY.clear()
        return n


# --- Async entrypoints (db-optional, ignored) --------------------------------

async def get_tags_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[str]:
    return get_tags(*args, **kwargs)


async def set_tags_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[str]:
    return set_tags(*args, **kwargs)


async def add_tags_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[str]:
    return add_tags(*args, **kwargs)


async def remove_tags_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[str]:
    return remove_tags(*args, **kwargs)


async def list_all_tags_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[str]:
    return list_all_tags(*args, **kwargs)


class CameraTagsService:
    """Object-oriented facade over the module-level store."""

    def get(self, *args: Any, **kwargs: Any) -> List[str]:
        return get_tags(*args, **kwargs)

    def set(self, *args: Any, **kwargs: Any) -> List[str]:
        return set_tags(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> List[str]:
        return add_tags(*args, **kwargs)

    def remove(self, *args: Any, **kwargs: Any) -> List[str]:
        return remove_tags(*args, **kwargs)

    def clear_camera(self, *args: Any, **kwargs: Any) -> int:
        return clear_tags(*args, **kwargs)

    def has(self, *args: Any, **kwargs: Any) -> bool:
        return has_tag(*args, **kwargs)

    def all(self, *args: Any, **kwargs: Any) -> List[str]:
        return list_all_tags(*args, **kwargs)

    def counts(self, *args: Any, **kwargs: Any) -> Dict[str, int]:
        return tag_counts(*args, **kwargs)

    def find(self, *args: Any, **kwargs: Any) -> List[Any]:
        return find_cameras_by_tags(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    # aliases
    get_tags = get
    set_tags = set
    add_tags = add
    remove_tags = remove
    clear_tags = clear_camera
    has_tag = has
    list_tags = all
    list_all_tags = all
    tag_counts = counts
    cameras_with_tag = find
    find_cameras_by_tags = find


camera_tags_service = CameraTagsService()
camera_tag_service = camera_tags_service
tags_service = camera_tags_service
service = camera_tags_service
manager = camera_tags_service
store = camera_tags_service


# Compatibility aliases (function level).
get_camera_tags = get_tags
fetch_camera_tags = get_tags
list_camera_tags = get_tags
query_camera_tags = get_tags
set_camera_tags = set_tags
replace_camera_tags = set_tags
update_camera_tags = set_tags
put_camera_tags = set_tags
assign_tags = set_tags
tag_camera = add_tags
add_camera_tags = add_tags
untag_camera = remove_tags
delete_camera_tags = remove_tags
remove_camera_tags = remove_tags
clear_camera_tags = clear_tags
reset_camera_tags = clear_tags
cameras_for_tag = cameras_with_tag
cameras_by_tag = cameras_with_tag
find_cameras_with_tag = cameras_with_tag
filter_cameras_by_tags = find_cameras_by_tags
search_cameras_by_tags = find_cameras_by_tags
all_tags = list_all_tags
list_tags = list_all_tags
fetch_all_tags = list_all_tags
count_tags = tag_counts
clear_all_tags = clear
reset = clear
purge = clear

__all__ = [
    "normalize_tag", "normalize_tags",
    "get_tags", "set_tags", "add_tags", "remove_tags", "clear_tags",
    "has_tag", "list_all_tags", "tag_counts",
    "cameras_with_tag", "find_cameras_by_tags", "count_cameras_with_tag",
    "clear",
    "get_tags_async", "set_tags_async", "add_tags_async",
    "remove_tags_async", "list_all_tags_async",
    "CameraTagsService", "camera_tags_service",
]
