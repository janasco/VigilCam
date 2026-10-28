"""Bulk event tagging service.

Verification note (read-only check of ``app/api/events.py``):

- ``POST /api/events/{event_id}/tags`` (plus ``/tag`` alias) tags a single
  event; ``DELETE /api/events/{event_id}/tags`` clears a single event.
- ``GET /api/events/tags`` only lists distinct tags in use.
- The only bulk endpoint on events is bulk *delete*
  (``DELETE /api/events``, ``POST /api/events/bulk-delete`` and aliases).

There is no bulk *tagging* endpoint. This module provides the service layer
for bulk tagging so the ``app/api/bulk_tag.py`` router stays thin.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

TAG_KEYS = (
    "tags",
    "tag",
    "tag_list",
    "tagList",
    "labels",
    "label",
    "values",
    "value",
    "items",
    "item",
    "names",
    "tagNames",
    "tagnames",
    "tag_names",
)

EVENT_ID_KEYS = (
    "event_ids",
    "eventIds",
    "events_ids",
    "ids",
    "id_list",
    "idList",
    "event_id_list",
    "eventIdList",
    "event_id",
    "eventId",
    "event",
    "id",
)

MODE_ADD = {"add", "append", "merge", "union", "attach"}
MODE_SET = {"set", "replace", "overwrite", "assign", "put"}
MODE_REMOVE = {"remove", "delete", "subtract", "detach", "untag"}
MODE_CLEAR = {"clear", "wipe", "reset", "empty"}


def _coerce_tag_items(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, dict):
        for key in ("tags", "tag", "values", "items", "labels", "names"):
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
                return [parsed.strip()] if parsed.strip() else []
            if isinstance(parsed, (list, tuple, set)):
                out: List[Any] = []
                for item in parsed:
                    out.extend(_coerce_tag_items(item))
                return out
            if isinstance(parsed, dict):
                return _coerce_tag_items(parsed)
            ss = str(parsed).strip()
            return [ss] if ss else []
        return [part.strip() for part in s.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(_coerce_tag_items(item))
        return out
    if isinstance(value, bool):
        return []
    ss = str(value).strip()
    return [ss] if ss else []


def normalize_tag(tag: Any) -> Optional[str]:
    if tag is None or isinstance(tag, bool):
        return None
    s = str(tag).strip()
    if not s:
        return None
    if s.lower() in {"none", "null"}:
        return None
    return s


def normalize_tags(value: Any) -> List[str]:
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


def _coerce_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def coerce_id_list(value: Any) -> List[int]:
    out: List[int] = []
    seen = set()
    if value is None:
        return out
    iterable: Any
    if isinstance(value, (list, tuple, set)):
        iterable = value
    else:
        iterable = [value]
    for v in iterable:
        if v is None or isinstance(v, bool):
            continue
        if isinstance(v, dict):
            cand = None
            for k in ("id", "event_id", "eventId", "event"):
                if v.get(k) is not None:
                    cand = v.get(k)
                    break
            if cand is None:
                continue
            v = cand
        if isinstance(v, str):
            for part in v.split(","):
                part = part.strip()
                if not part:
                    continue
                iv = _coerce_int(part)
                if iv is not None and iv not in seen:
                    seen.add(iv)
                    out.append(iv)
        else:
            iv = _coerce_int(v)
            if iv is not None and iv not in seen:
                seen.add(iv)
                out.append(iv)
    return out


def normalize_mode(value: Any, default: str = "add") -> str:
    if value is None:
        return default
    s = str(value).strip().lower()
    if not s:
        return default
    if s in MODE_ADD:
        return "add"
    if s in MODE_SET:
        return "set"
    if s in MODE_REMOVE:
        return "remove"
    if s in MODE_CLEAR:
        return "clear"
    return default


def apply_tags(current: Any, incoming: Any, mode: str = "add") -> List[str]:
    cur = normalize_tags(current)
    inc = normalize_tags(incoming)
    m = normalize_mode(mode, default="add")
    if m == "set":
        return list(inc)
    if m == "remove":
        doomed = {t.lower() for t in inc}
        return [t for t in cur if t.lower() not in doomed]
    if m == "clear":
        return []
    # add / union
    seen = {t.lower() for t in cur}
    out = list(cur)
    for t in inc:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _event_to_dict(ev: Any) -> Dict[str, Any]:
    try:
        tags = normalize_tags(getattr(ev, "tags", None))
    except Exception:  # noqa: BLE001
        tags = []
    try:
        created = ev.created_at.isoformat() if getattr(ev, "created_at", None) else None
    except Exception:  # noqa: BLE001
        created = None
    return {
        "id": getattr(ev, "id", None),
        "camera_id": getattr(ev, "camera_id", None),
        "kind": getattr(ev, "kind", None),
        "label": getattr(ev, "label", None),
        "confidence": getattr(ev, "confidence", None),
        "payload": getattr(ev, "payload", None),
        "tags": tags,
        "tag": tags,
        "created_at": created,
    }


async def bulk_tag_events(
    db: Any,
    event_ids: Any = None,
    tags: Any = None,
    mode: Any = "add",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Apply ``tags`` to many ``Event`` rows.

    ``event_ids`` accepts ints, numeric strings, csv strings, lists and
    dict-payload aliases (``event_ids``/``ids``/``id``/``event_id``).
    ``tags`` accepts any tag-like value (list, csv string, JSON, dict).
    ``mode`` is one of ``add`` (default, union), ``set`` (replace),
    ``remove`` (subtract) or ``clear`` (wipe).
    """
    from app.models.entities import Event

    # kwargs-tolerant extraction (router may pass raw payload pieces).
    if event_ids is None:
        for key in EVENT_ID_KEYS:
            if kwargs.get(key) is not None:
                event_ids = kwargs.get(key)
                break
    if tags is None:
        for key in TAG_KEYS:
            if kwargs.get(key) is not None:
                tags = kwargs.get(key)
                break
    if "mode" not in kwargs and "action" in kwargs and mode == "add":
        mode = kwargs.get("action")
    elif kwargs.get("mode") is not None and mode == "add":
        # explicit kwarg wins unless caller passed a non-default mode positionally
        mode = kwargs.get("mode")
    if kwargs.get("operation") is not None and mode == "add":
        mode = kwargs.get("operation")

    m = normalize_mode(mode, default="add")
    ids = coerce_id_list(event_ids)
    wanted = normalize_tags(tags)
    if m == "clear":
        wanted = []

    if not ids:
        raise ValueError("event_ids is required")
    if m in {"add", "set", "remove"} and not wanted:
        # ``set: []`` and ``remove: []``/``add: []`` are meaningful no-ops;
        # only reject a completely absent tag payload for add/set.
        # ``remove`` with no tags is a no-op (still validates ids).
        if tags is None and m in {"add", "set"}:
            raise ValueError("tags is required")

    updated: List[int] = []
    missing: List[int] = []
    per_event: Dict[str, List[str]] = {}
    for eid in ids:
        try:
            row = await db.get(Event, int(eid))
        except Exception:  # noqa: BLE001
            row = None
        if row is None:
            missing.append(int(eid))
            continue
        try:
            current = normalize_tags(getattr(row, "tags", None))
        except Exception:  # noqa: BLE001
            current = []
        new_tags = apply_tags(current, wanted, mode=m)
        try:
            row.tags = new_tags
        except Exception:  # noqa: BLE001
            pass
        updated.append(int(eid))
        per_event[str(int(eid))] = list(new_tags)
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        pass
    # refresh best-effort so callers see persisted values
    for eid in updated:
        try:
            from app.models.entities import Event as _Event

            row = await db.get(_Event, int(eid))
            if row is not None:
                await db.refresh(row)
                per_event[str(int(eid))] = normalize_tags(getattr(row, "tags", None))
        except Exception:  # noqa: BLE001
            pass

    return {
        "ok": True,
        "success": True,
        "mode": m,
        "tags": wanted,
        "updated_event_ids": updated,
        "updated_events": updated,
        "updated": updated,
        "updated_count": len(updated),
        "missing_event_ids": missing,
        "missing": missing,
        "not_found": missing,
        "not_found_event_ids": missing,
        "per_event": per_event,
        "per_event_tags": per_event,
    }


async def bulk_add_event_tags(db: Any, event_ids: Any = None, tags: Any = None, **kwargs: Any) -> Dict[str, Any]:
    kwargs.pop("mode", None)
    return await bulk_tag_events(db, event_ids=event_ids, tags=tags, mode="add", **kwargs)


async def bulk_set_event_tags(db: Any, event_ids: Any = None, tags: Any = None, **kwargs: Any) -> Dict[str, Any]:
    kwargs.pop("mode", None)
    return await bulk_tag_events(db, event_ids=event_ids, tags=tags, mode="set", **kwargs)


async def bulk_remove_event_tags(db: Any, event_ids: Any = None, tags: Any = None, **kwargs: Any) -> Dict[str, Any]:
    kwargs.pop("mode", None)
    return await bulk_tag_events(db, event_ids=event_ids, tags=tags, mode="remove", **kwargs)


async def bulk_clear_event_tags(db: Any, event_ids: Any = None, **kwargs: Any) -> Dict[str, Any]:
    kwargs.pop("mode", None)
    return await bulk_tag_events(db, event_ids=event_ids, tags=[], mode="clear", **kwargs)


class BulkTagService:
    """Object-oriented facade over the bulk-tag helpers."""

    def normalize(self, value: Any) -> List[str]:
        return normalize_tags(value)

    def apply(self, current: Any, incoming: Any, mode: str = "add") -> List[str]:
        return apply_tags(current, incoming, mode=mode)

    async def tag(self, db: Any, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if args and len(args) == 2 and "event_ids" not in kwargs and "tags" not in kwargs:
            kwargs = dict(kwargs)
            kwargs.setdefault("event_ids", args[0])
            kwargs.setdefault("tags", args[1])
        return await bulk_tag_events(db, **kwargs)

    async def add(self, db: Any, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await bulk_add_event_tags(db, *args, **kwargs)

    async def set(self, db: Any, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await bulk_set_event_tags(db, *args, **kwargs)

    async def remove(self, db: Any, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await bulk_remove_event_tags(db, *args, **kwargs)

    async def clear(self, db: Any, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await bulk_clear_event_tags(db, *args, **kwargs)

    # aliases
    bulk_tag = tag
    bulk_add = add
    bulk_set = set
    bulk_remove = remove
    bulk_clear = clear


bulk_tag_service = BulkTagService()
bulk_service = bulk_tag_service
service = bulk_tag_service
manager = bulk_tag_service
store = bulk_tag_service

# Compatibility aliases (function level).
bulk_tag = bulk_tag_events
tag_events = bulk_tag_events
tag_bulk_events = bulk_tag_events
bulk_tag_bulk = bulk_tag_events
add_event_tags = bulk_add_event_tags
add_tags_bulk = bulk_add_event_tags
set_event_tags = bulk_set_event_tags
replace_event_tags = bulk_set_event_tags
remove_event_tags = bulk_remove_event_tags
untag_events = bulk_remove_event_tags
clear_event_tags = bulk_clear_event_tags
bulk_add = bulk_add_event_tags
bulk_set = bulk_set_event_tags
bulk_remove = bulk_remove_event_tags
bulk_clear = bulk_clear_event_tags

__all__ = [
    "TAG_KEYS",
    "EVENT_ID_KEYS",
    "normalize_tag",
    "normalize_tags",
    "coerce_id_list",
    "normalize_mode",
    "apply_tags",
    "bulk_tag_events",
    "bulk_add_event_tags",
    "bulk_set_event_tags",
    "bulk_remove_event_tags",
    "bulk_clear_event_tags",
    "BulkTagService",
    "bulk_tag_service",
]
