"""Event notes/comments service (in-memory, per-event CRUD).

Notes are operator-authored annotations attached to an event id. The store
is intentionally in-memory (thread-safe) so the feature works without DB
migrations and without requiring ``app/main.py`` edits. An optional
``db``/``session`` argument is accepted by the async entrypoints for
forward-compatibility but is currently ignored (best-effort no-op).
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
_NOTES: Dict[int, Dict[str, Any]] = {}
_EVENT_INDEX: Dict[int, List[int]] = {}
_ID_COUNTER = 0

TEXT_KEYS = (
    "text", "body", "content", "message", "note", "comment",
    "description", "summary", "value", "data",
)
EVENT_ID_KEYS = (
    "event_id", "eventId", "event", "eventid",
    "id_event", "for_event", "camera_event_id",
)
AUTHOR_KEYS = (
    "author", "username", "user", "actor", "created_by",
    "createdBy", "author_name", "authorName", "by",
)
ID_KEYS = ("id", "note_id", "noteId", "comment_id", "commentId", "noteID")


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
        for nested_key in ("note", "comment", "payload", "options", "config",
                           "params", "data", "body", "request", "fields", "entry"):
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    if isinstance(data, (str, bytes, bytearray)):
        s = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else str(data)
        return {"text": s}
    try:
        return dict(data)
    except Exception:
        return {"text": str(data)}


def _extract_event_id(*args: Any, **kwargs: Any) -> Optional[int]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (int, float)) and "event_id" not in data:
            data["event_id"] = a
    data.update(kwargs or {})
    # nested dicts
    for nested_key in ("note", "comment", "payload", "params", "data", "body"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    raw = _first_present(data, EVENT_ID_KEYS)
    if raw is None and len(args) == 1 and isinstance(args[0], (int, float)):
        raw = args[0]
    return _coerce_int(raw, None)


def _extract_text(data: Dict[str, Any]) -> Optional[str]:
    raw = _first_present(data, TEXT_KEYS)
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        return s if s else None
    s = str(raw).strip()
    return s if s else None


def _extract_author(data: Dict[str, Any]) -> Optional[str]:
    raw = _first_present(data, AUTHOR_KEYS)
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s else None


def _to_public(note: Dict[str, Any]) -> Dict[str, Any]:
    return dict(note)


def create_note(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create a note attached to an event.

    Accepts flexible inputs, e.g.::

        create_note(event_id=1, text="hello")
        create_note(1, "hello")
        create_note({"event_id": 1, "body": "hello"})
    """
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
        elif isinstance(a, (str, bytes, bytearray)):
            s = a.decode("utf-8", errors="replace") if isinstance(a, (bytes, bytearray)) else str(a)
            flat.setdefault("text", s)
        elif isinstance(a, (int, float)) and "event_id" not in flat:
            flat["event_id"] = a
        else:
            try:
                flat.update(dict(a))
            except Exception:
                pass
    flat.update(kwargs or {})
    for nested_key in ("note", "comment", "payload", "params", "data", "body", "request"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)

    event_id = _coerce_int(_first_present(flat, EVENT_ID_KEYS), None)
    if event_id is None:
        raise ValueError("event_id is required")
    text = _extract_text(flat)
    if text is None:
        raise ValueError("text/body/content is required")
    author = _extract_author(flat)
    now = _now_iso()
    with _lock:
        nid = _next_id()
        note: Dict[str, Any] = {
            "id": nid,
            "note_id": nid,
            "comment_id": nid,
            "event_id": event_id,
            "eventId": event_id,
            "text": text,
            "body": text,
            "content": text,
            "message": text,
            "note": text,
            "comment": text,
            "author": author,
            "username": author,
            "created_by": author,
            "created_at": now,
            "updated_at": now,
        }
        for k, v in flat.items():
            if k not in note:
                note[k] = v
        _NOTES[nid] = note
        _EVENT_INDEX.setdefault(event_id, []).append(nid)
        return _to_public(note)


def list_notes(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """List notes, optionally filtered by event_id."""
    flat = _flatten(args[0]) if len(args) == 1 and isinstance(args[0], dict) else {}
    for a in args:
        if isinstance(a, (int, float)) and "event_id" not in flat:
            flat["event_id"] = a
    flat.update(kwargs or {})
    event_id = _coerce_int(_first_present(flat, EVENT_ID_KEYS), None)
    if event_id is None and len(args) == 1 and isinstance(args[0], (int, float)):
        event_id = int(args[0])
    limit = _coerce_int(_first_present(flat, ("limit", "n", "count", "size", "page_size")), None)
    offset = _coerce_int(_first_present(flat, ("offset", "skip", "start_index")), 0) or 0
    query = _first_present(flat, ("q", "query", "search", "filter", "text_search"))
    with _lock:
        if event_id is None:
            items = [_to_public(_NOTES[k]) for k in sorted(_NOTES.keys())]
        else:
            ids = list(_EVENT_INDEX.get(int(event_id), []))
            items = [_to_public(_NOTES[k]) for k in sorted(ids) if k in _NOTES]
    if query is not None:
        qs = str(query).strip().lower()
        if qs:
            items = [n for n in items if qs in str(n.get("text", "")).lower()]
    if offset:
        items = items[offset:]
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items


def get_note(note_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if note_id is None:
        note_id = _first_present(flat, ID_KEYS)
    if note_id is None and args:
        for a in args:
            if isinstance(a, (int, float, str)) and not isinstance(a, bool):
                try:
                    int(a)
                    note_id = a
                    break
                except (TypeError, ValueError):
                    continue
    nid = _coerce_int(note_id, None)
    if nid is None:
        return None
    with _lock:
        note = _NOTES.get(int(nid))
        return _to_public(note) if note is not None else None


def update_note(note_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
        elif isinstance(a, (str, bytes, bytearray)):
            s = a.decode("utf-8", errors="replace") if isinstance(a, (bytes, bytearray)) else str(a)
            flat.setdefault("text", s)
    flat.update(kwargs or {})
    if note_id is None:
        note_id = _first_present(flat, ID_KEYS)
    nid = _coerce_int(note_id, None)
    if nid is None:
        return None
    with _lock:
        note = _NOTES.get(int(nid))
        if note is None:
            return None
        text = _extract_text(flat)
        if text is not None:
            for k in ("text", "body", "content", "message", "note", "comment"):
                note[k] = text
        author = _extract_author(flat)
        if author is not None:
            note["author"] = author
            note["username"] = author
            note["created_by"] = author
        for k, v in flat.items():
            if k.lower() in {x.lower() for x in ID_KEYS + EVENT_ID_KEYS}:
                continue
            if k not in ("text", "body", "content", "message", "note", "comment",
                         "author", "username", "user", "actor", "created_by"):
                note[k] = v
        note["updated_at"] = _now_iso()
        return _to_public(note)


def delete_note(note_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    if note_id is None:
        note_id = _first_present(flat, ID_KEYS)
    if note_id is None and args:
        for a in args:
            if isinstance(a, (int, float)):
                note_id = a
                break
    nid = _coerce_int(note_id, None)
    if nid is None:
        return False
    with _lock:
        note = _NOTES.pop(int(nid), None)
        if note is None:
            return False
        eid = note.get("event_id")
        try:
            lst = _EVENT_INDEX.get(int(eid), [])
            if int(nid) in lst:
                lst.remove(int(nid))
        except (TypeError, ValueError):
            pass
        return True


def delete_notes_for_event(event_id: Any = None, *args: Any, **kwargs: Any) -> int:
    eid = _extract_event_id(*(([event_id] if event_id is not None else []) + list(args)), **kwargs)
    if eid is None:
        return 0
    with _lock:
        ids = list(_EVENT_INDEX.get(int(eid), []))
    n = 0
    for nid in ids:
        if delete_note(nid):
            n += 1
    return n


def count_notes(event_id: Any = None, *args: Any, **kwargs: Any) -> int:
    return len(list_notes(*(([event_id] if event_id is not None else []) + list(args)), **kwargs))


def clear(*args: Any, **kwargs: Any) -> int:
    with _lock:
        n = len(_NOTES)
        _NOTES.clear()
        _EVENT_INDEX.clear()
        return n


# --- Async entrypoints (db-optional, ignored) --------------------------------

async def _maybe_await(value: Any) -> Any:
    return value


async def create_note_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    kwargs.pop("db", None)
    kwargs.pop("session", None)
    return create_note(*args, **kwargs)


async def list_notes_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_notes(*args, **kwargs)


async def get_note_async(note_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_note(note_id, *args, **kwargs)


async def update_note_async(note_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return update_note(note_id, *args, **kwargs)


async def delete_note_async(note_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> bool:
    return delete_note(note_id, *args, **kwargs)


class EventNotesService:
    """Object-oriented facade over the module-level store."""

    def create(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_note(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_notes(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_note(*args, **kwargs)

    def update(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return update_note(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return delete_note(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    def count(self, *args: Any, **kwargs: Any) -> int:
        return count_notes(*args, **kwargs)

    # aliases
    create_note = create
    add_note = create
    list_notes = list
    get_notes = list
    get_note = get
    update_note = update
    delete_note = delete
    clear_notes = clear


event_notes_service = EventNotesService()
event_note_service = event_notes_service
notes_service = event_notes_service
service = event_notes_service
manager = event_notes_service
store = event_notes_service


# Compatibility aliases (function level).
add_note = create_note
create_event_note = create_note
add_event_note = create_note
new_note = create_note
post_note = create_note
create_comment = create_note
add_comment = create_note
add_event_comment = create_note
create_event_comment = create_note

get_notes = list_notes
get_event_notes = list_notes
list_event_notes = list_notes
fetch_notes = list_notes
fetch_event_notes = list_notes
get_event_comments = list_notes
list_event_comments = list_notes
query_notes = list_notes
search_notes = list_notes

get_event_note = get_note
fetch_note = get_note
find_note = get_note

edit_note = update_note
update_event_note = update_note
edit_event_note = update_note
patch_note = update_note
update_comment = update_note

remove_note = delete_note
delete_event_note = delete_note
remove_event_note = delete_note
delete_comment = delete_note
remove_comment = delete_note
delete_event_comment = delete_note

clear_notes = clear
clear_event_notes = clear
reset = clear
purge = clear

__all__ = [
    "create_note", "list_notes", "get_note", "update_note", "delete_note",
    "delete_notes_for_event", "count_notes", "clear",
    "create_note_async", "list_notes_async", "get_note_async",
    "update_note_async", "delete_note_async",
    "EventNotesService", "event_notes_service",
]
