"""@mention parsing + notification service for notes.

Event notes (``app.services.event_notes``) and camera notes
(``app.services.camera_notes``) are intentionally plain text: they store
``text`` verbatim and perform no ``@mention`` parsing, extraction, or
notification side effects. This module is the *only* place that parses
``@username`` mentions and fans them out into per-user notifications.

It is dependency-free and in-memory (thread-safe) so it works without DB
migrations and without edits to ``app/main.py`` (the ``pkgutil``
auto-loader picks up ``app.api.mentions`` automatically).

Typical flow (without touching the notes services)::

    from app.services import event_notes, mentions

    note = event_notes.create_note(event_id=1, text="hey @alice review this")
    # note["text"] still contains "@alice" verbatim; no notifications yet.
    created = mentions.notify_for_event_note(note)
    # created == [{"user": "alice", ...}]

The same applies to camera notes via :func:`notify_for_camera_note`.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_MENTION_RE = re.compile(r"(?<!\w)@([A-Za-z0-9_][A-Za-z0-9_.\-]*)")

_lock = threading.RLock()
_NOTIFICATIONS: Dict[int, Dict[str, Any]] = {}
_ID_COUNTER = 0


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
        except Exception:
            pass
    if isinstance(data, dict):
        out = dict(data)
        for nested_key in (
            "note", "comment", "payload", "options", "config",
            "params", "data", "body", "request", "fields", "entry",
            "notification", "mention",
        ):
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


def _clean_username(raw: str) -> Optional[str]:
    s = (raw or "").strip()
    # Strip trailing mention punctuation that the regex may have captured
    # (e.g. "@alice." at end of sentence, "@bob," in a list).
    s = s.rstrip(".-_!?,;:") .strip()
    s = s.strip()
    if not s:
        return None
    return s


def parse_mentions(text: Any = None, *args: Any, **kwargs: Any) -> List[str]:
    """Extract ``@username`` mentions from text, preserving order, deduped.

    Accepts a bare string, a dict with a text-like key, or kwargs::

        parse_mentions("hey @alice and @bob, cc @alice")
        # -> ["alice", "bob"]
        parse_mentions({"text": "hi @ops-team"})
        parse_mentions(text="hi @ops-team")

    Matching is case-preserving for display but deduped case-insensitively
    (first occurrence wins). Email-like ``name@domain`` spans are ignored
    via a ``(?<!\\w)`` lookbehind. Trailing sentence punctuation is stripped.
    """
    raw_text: Optional[str] = None
    if text is None and args:
        # Allow parse_mentions({"text": ...}) positionally.
        for a in args:
            if isinstance(a, dict):
                flat = _flatten(a)
                cand = _first_present(
                    flat,
                    ("text", "body", "content", "message", "note", "comment",
                     "description", "summary", "value", "data", "title"),
                )
                if cand is not None:
                    raw_text = str(cand)
                    break
            elif isinstance(a, (bytes, bytearray)):
                raw_text = bytes(a).decode("utf-8", errors="replace")
                break
            elif isinstance(a, str):
                raw_text = a
                break
        if raw_text is None:
            flat = _flatten(kwargs)
            cand = _first_present(
                flat,
                ("text", "body", "content", "message", "note", "comment",
                 "description", "summary", "value", "data", "title"),
            )
            if cand is not None:
                raw_text = str(cand)
    elif isinstance(text, dict):
        flat = _flatten(text)
        for a in args:
            if isinstance(a, dict):
                for k, v in _flatten(a).items():
                    flat.setdefault(k, v)
        merged = dict(flat)
        merged.update({k: v for k, v in kwargs.items()})
        cand = _first_present(
            merged,
            ("text", "body", "content", "message", "note", "comment",
             "description", "summary", "value", "data", "title"),
        )
        raw_text = str(cand) if cand is not None else ""
    elif isinstance(text, (bytes, bytearray)):
        raw_text = bytes(text).decode("utf-8", errors="replace")
    elif text is not None:
        raw_text = str(text)
    if raw_text is None:
        # Fall back to kwargs text-like keys.
        cand = _first_present(
            kwargs,
            ("text", "body", "content", "message", "note", "comment",
             "description", "summary", "value", "data", "title"),
        )
        raw_text = str(cand) if cand is not None else ""
    if not raw_text:
        return []
    seen: set = set()
    out: List[str] = []
    for m in _MENTION_RE.finditer(raw_text):
        cleaned = _clean_username(m.group(1))
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def _normalize_notify_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if a is None:
            continue
        if isinstance(a, dict):
            flat = _flatten(a)
            for k, v in flat.items():
                data.setdefault(k, v)
        elif isinstance(a, (bytes, bytearray)):
            try:
                s = bytes(a).decode("utf-8", errors="replace")
            except Exception:
                continue
            if data.get("text") is None:
                data["text"] = s
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if data.get("text") is None:
                data["text"] = s
            else:
                data.setdefault("note_text", s)
        elif hasattr(a, "model_dump"):
            try:
                for k, v in dict(a.model_dump()).items():
                    data.setdefault(k, v)
            except Exception:
                pass
        else:
            try:
                for k, v in dict(a).items():
                    data.setdefault(k, v)
            except Exception:
                pass
    for k, v in (kwargs or {}).items():
        data[k] = v
    for nested_key in ("note", "comment", "payload", "params", "data", "body", "request"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _extract_text(data: Dict[str, Any]) -> str:
    raw = _first_present(
        data,
        ("text", "body", "content", "message", "note", "comment",
         "description", "summary", "value", "data", "title"),
    )
    if raw is None:
        return ""
    return str(raw)


def _extract_author(data: Dict[str, Any]) -> Optional[str]:
    raw = _first_present(
        data,
        ("author", "username", "user", "actor", "created_by",
         "createdBy", "author_name", "authorName", "by"),
    )
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s else None


def _to_public(note: Dict[str, Any]) -> Dict[str, Any]:
    return dict(note)


def _build_notification(
    username: str,
    text: str,
    author: Optional[str] = None,
    note_id: Any = None,
    note_type: Optional[str] = None,
    event_id: Any = None,
    camera_id: Any = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = _now_iso()
    nid = _next_id()
    notif: Dict[str, Any] = {
        "id": nid,
        "mention_id": nid,
        "notification_id": nid,
        "user": username,
        "username": username,
        "mentioned": username,
        "mentioned_user": username,
        "mentionedUser": username,
        "recipient": username,
        "to": username,
        "author": author,
        "created_by": author,
        "username_author": author,
        "text": text,
        "body": text,
        "content": text,
        "message": text,
        "note_id": note_id,
        "noteId": note_id,
        "note_type": note_type,
        "noteType": note_type,
        "event_id": event_id,
        "eventId": event_id,
        "camera_id": camera_id,
        "cameraId": camera_id,
        "read": False,
        "is_read": False,
        "unread": True,
        "created_at": now,
        "updated_at": now,
    }
    if extra:
        for k, v in extra.items():
            if k not in notif:
                notif[k] = v
    return notif


def notify_for_text(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Parse ``text`` for ``@mentions`` and store one notification per user.

    Returns the list of created notification dicts (empty when no mentions).
    Accepts flexible inputs, e.g.::

        notify_for_text("hey @alice", author="bob", event_id=1)
        notify_for_text({"text": "hey @alice", "author": "bob"})
    """
    data = _normalize_notify_inputs(*args, **kwargs)
    text = _extract_text(data)
    if not text.strip():
        return []
    users = parse_mentions(text)
    if not users:
        return []
    author = _extract_author(data)
    note_id = _first_present(data, ("note_id", "noteId", "id", "comment_id", "commentId"))
    note_type = _first_present(data, ("note_type", "noteType", "kind", "source", "scope"))
    if note_type is not None:
        note_type = str(note_type)
    event_id = _first_present(data, ("event_id", "eventId", "event"))
    camera_id = _first_present(data, ("camera_id", "cameraId", "camera", "cam_id", "cam"))
    skip_self = str(data.get("skip_self", data.get("skipSelf", False))).strip().lower() in (
        "1", "true", "yes", "on",
    )
    created: List[Dict[str, Any]] = []
    with _lock:
        for username in users:
            if skip_self and author is not None and username.lower() == author.lower():
                continue
            notif = _build_notification(
                username, text, author=author, note_id=note_id,
                note_type=note_type, event_id=event_id, camera_id=camera_id,
                extra={k: v for k, v in data.items() if k not in (
                    "text", "body", "content", "message", "note", "comment",
                )},
            )
            _NOTIFICATIONS[notif["id"]] = notif
            created.append(_to_public(notif))
    return created


def _notify_for_note_dict(note: Any, note_type: str, **kwargs: Any) -> List[Dict[str, Any]]:
    if note is None:
        return []
    if isinstance(note, dict):
        data = _flatten(note)
    else:
        data = _flatten(note)
    data = dict(data)
    for k, v in (kwargs or {}).items():
        data.setdefault(k, v)
    data.setdefault("note_type", note_type)
    text = _extract_text(data)
    if not text.strip():
        return []
    users = parse_mentions(text)
    if not users:
        return []
    return notify_for_text(data)


def notify_for_event_note(note: Any = None, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Create ``@mention`` notifications for an event-note dict.

    The note dict is expected to look like the dicts returned by
    ``app.services.event_notes.create_note`` (plain text preserved).
    Returns created notifications (empty when the note has no mentions).
    """
    merged: Dict[str, Any] = {}
    if isinstance(note, dict):
        merged.update(_flatten(note))
    elif note is not None:
        merged.update(_flatten(note))
    for a in args:
        if isinstance(a, dict):
            for k, v in _flatten(a).items():
                merged.setdefault(k, v)
        elif isinstance(a, str) and merged.get("text") is None:
            merged["text"] = a
    for k, v in (kwargs or {}).items():
        merged[k] = v
    merged["note_type"] = merged.get("note_type") or merged.get("noteType") or "event"
    return _notify_for_note_dict(merged, "event")


def notify_for_camera_note(note: Any = None, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Create ``@mention`` notifications for a camera-note dict."""
    merged: Dict[str, Any] = {}
    if isinstance(note, dict):
        merged.update(_flatten(note))
    elif note is not None:
        merged.update(_flatten(note))
    for a in args:
        if isinstance(a, dict):
            for k, v in _flatten(a).items():
                merged.setdefault(k, v)
        elif isinstance(a, str) and merged.get("text") is None:
            merged["text"] = a
    for k, v in (kwargs or {}).items():
        merged[k] = v
    merged["note_type"] = merged.get("note_type") or merged.get("noteType") or "camera"
    return _notify_for_note_dict(merged, "camera")


def list_notifications(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """List stored mention notifications, optionally filtered.

    Filters (all optional): ``user``/``username``/``mentioned``,
    ``author``, ``note_id``, ``note_type``, ``event_id``, ``camera_id``,
    ``read``/``unread_only``, ``q`` (substring of text), ``limit``,
    ``offset``.
    """
    data = _normalize_notify_inputs(*args, **kwargs)
    user = _first_present(data, ("user", "username", "mentioned", "mentioned_user",
                                 "mentionedUser", "recipient", "to"))
    # NOTE: "user"/"username" mean the *mentioned* user here, so author must
    # be extracted only from unambiguous author keys (not "user"/"username").
    author = _first_present(data, ("author", "created_by",
                                   "createdBy", "author_name", "authorName",
                                   "by", "actor"))
    note_id = _first_present(data, ("note_id", "noteId", "noteID"))
    note_type = _first_present(data, ("note_type", "noteType"))
    event_id = _first_present(data, ("event_id", "eventId", "event"))
    camera_id = _first_present(data, ("camera_id", "cameraId", "camera", "cam_id", "cam"))
    read_val = _first_present(data, ("read", "is_read", "isRead"))
    unread_flag = _first_present(data, ("unread", "unread_only",
                                        "unreadOnly", "unread_only_flag"))
    notif_id = _first_present(data, ("id", "mention_id", "mentionId", "notification_id"))
    query = _first_present(data, ("q", "query", "search", "filter", "text_search"))
    limit = _coerce_int(_first_present(data, ("limit", "n", "count", "size", "page_size")), None)
    offset = _coerce_int(_first_present(data, ("offset", "skip", "start_index")), 0) or 0
    with _lock:
        items = [_to_public(v) for v in (_NOTIFICATIONS[k] for k in sorted(_NOTIFICATIONS.keys()))]
    if notif_id is not None:
        nid = _coerce_int(notif_id, None)
        items = [n for n in items if n.get("id") == nid]
    if user is not None:
        us = str(user).strip().lower()
        if us:
            items = [n for n in items if str(n.get("user", "")).lower() == us]
    if author is not None:
        items = [n for n in items if (n.get("author") is not None and str(n.get("author")).lower() == str(author).lower())]
    if note_id is not None:
        try:
            items = [n for n in items if n.get("note_id") is not None and int(n.get("note_id")) == int(note_id)]
        except (TypeError, ValueError):
            items = [n for n in items if str(n.get("note_id")) == str(note_id)]
    if note_type is not None:
        items = [n for n in items if str(n.get("note_type", "")).lower() == str(note_type).lower()]
    if event_id is not None:
        try:
            items = [n for n in items if n.get("event_id") is not None and int(n.get("event_id")) == int(event_id)]
        except (TypeError, ValueError):
            items = [n for n in items if str(n.get("event_id")) == str(event_id)]
    if camera_id is not None:
        items = [n for n in items if n.get("camera_id") is not None and str(n.get("camera_id")) == str(camera_id)]
    if read_val is not None:
        if read_val is True or (isinstance(read_val, str) and read_val.strip().lower() in ("1", "true", "yes", "read", "is_read", "read_only")):
            items = [n for n in items if bool(n.get("read"))]
        elif read_val is False or (isinstance(read_val, str) and read_val.strip().lower() in ("0", "false", "no", "unread", "unread_only")):
            items = [n for n in items if not bool(n.get("read"))]
        elif isinstance(read_val, (int, float)) and not isinstance(read_val, bool):
            try:
                if int(read_val) == 1:
                    items = [n for n in items if bool(n.get("read"))]
                elif int(read_val) == 0:
                    items = [n for n in items if not bool(n.get("read"))]
            except (TypeError, ValueError):
                pass
    # unread_only boolean flag (separate key spellings).
    if unread_flag is True or (isinstance(unread_flag, str) and unread_flag.strip().lower() in ("1", "true", "yes", "unread", "unread_only")):
        items = [n for n in items if not bool(n.get("read"))]
    elif isinstance(unread_flag, (int, float)) and not isinstance(unread_flag, bool) and int(unread_flag) == 1:
        items = [n for n in items if not bool(n.get("read"))]
    if query is not None:
        qs = str(query).strip().lower()
        if qs:
            items = [n for n in items if qs in str(n.get("text", "")).lower() or qs in str(n.get("user", "")).lower()]
    if offset:
        items = items[offset:]
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items


def get_notification(notification_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    data = _normalize_notify_inputs(*args, **kwargs)
    if notification_id is None:
        notification_id = _first_present(data, ("id", "mention_id", "mentionId", "notification_id"))
    if notification_id is None and args:
        for a in args:
            if isinstance(a, (int, float, str)) and not isinstance(a, bool):
                try:
                    int(a)
                    notification_id = a
                    break
                except (TypeError, ValueError):
                    continue
    nid = _coerce_int(notification_id, None)
    if nid is None:
        return None
    with _lock:
        notif = _NOTIFICATIONS.get(int(nid))
        return _to_public(notif) if notif is not None else None


def mark_read(notification_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Mark one notification as read. Returns the updated dict or None."""
    data = _normalize_notify_inputs(*args, **kwargs)
    if notification_id is None:
        notification_id = _first_present(data, ("id", "mention_id", "mentionId", "notification_id"))
    if notification_id is None and args:
        for a in args:
            if isinstance(a, (int, float)):
                notification_id = a
                break
    nid = _coerce_int(notification_id, None)
    if nid is None:
        return None
    with _lock:
        notif = _NOTIFICATIONS.get(int(nid))
        if notif is None:
            return None
        notif["read"] = True
        notif["is_read"] = True
        notif["unread"] = False
        notif["updated_at"] = _now_iso()
        return _to_public(notif)


def mark_all_read(*args: Any, **kwargs: Any) -> int:
    """Mark matching notifications as read. Returns count marked."""
    targets = list_notifications(*args, **kwargs)
    n = 0
    with _lock:
        for t in targets:
            stored = _NOTIFICATIONS.get(int(t["id"]))
            if stored is not None and not bool(stored.get("read")):
                stored["read"] = True
                stored["is_read"] = True
                stored["unread"] = False
                stored["updated_at"] = _now_iso()
                n += 1
    return n


def delete_notification(notification_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    data = _normalize_notify_inputs(*args, **kwargs)
    if notification_id is None:
        notification_id = _first_present(data, ("id", "mention_id", "mentionId", "notification_id"))
    if notification_id is None and args:
        for a in args:
            if isinstance(a, (int, float)):
                notification_id = a
                break
    nid = _coerce_int(notification_id, None)
    if nid is None:
        return False
    with _lock:
        return _NOTIFICATIONS.pop(int(nid), None) is not None


def unread_count(*args: Any, **kwargs: Any) -> int:
    data = _normalize_notify_inputs(*args, **kwargs)
    data["unread_only"] = True
    return len(list_notifications(data))


def count(*args: Any, **kwargs: Any) -> int:
    return len(list_notifications(*args, **kwargs))


def clear(*args: Any, **kwargs: Any) -> int:
    with _lock:
        n = len(_NOTIFICATIONS)
        _NOTIFICATIONS.clear()
        return n


async def notify_for_text_async(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return notify_for_text(*args, **kwargs)


async def list_notifications_async(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_notifications(*args, **kwargs)


async def get_notification_async(notification_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_notification(notification_id, *args, **kwargs)


class MentionsService:
    """Object-oriented facade over the module-level store."""

    def parse(self, *args: Any, **kwargs: Any) -> List[str]:
        return parse_mentions(*args, **kwargs)

    def parse_mentions(self, *args: Any, **kwargs: Any) -> List[str]:
        return parse_mentions(*args, **kwargs)

    def notify(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return notify_for_text(*args, **kwargs)

    def notify_for_text(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return notify_for_text(*args, **kwargs)

    def notify_for_event_note(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return notify_for_event_note(*args, **kwargs)

    def notify_for_camera_note(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return notify_for_camera_note(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_notifications(*args, **kwargs)

    def list_notifications(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_notifications(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_notification(*args, **kwargs)

    def get_notification(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_notification(*args, **kwargs)

    def mark_read(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return mark_read(*args, **kwargs)

    def mark_all_read(self, *args: Any, **kwargs: Any) -> int:
        return mark_all_read(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return delete_notification(*args, **kwargs)

    def delete_notification(self, *args: Any, **kwargs: Any) -> bool:
        return delete_notification(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    def count(self, *args: Any, **kwargs: Any) -> int:
        return count(*args, **kwargs)

    def unread_count(self, *args: Any, **kwargs: Any) -> int:
        return unread_count(*args, **kwargs)

    # aliases
    extract = parse_mentions
    find = parse_mentions


mentions_service = MentionsService()
mention_service = mentions_service
service = mentions_service
manager = mentions_service
store = mentions_service


# Compatibility aliases (function level).
extract_mentions = parse_mentions
find_mentions = parse_mentions
get_mentions = parse_mentions
extract = parse_mentions
find = parse_mentions
parse = parse_mentions

notify = notify_for_text
create_notifications = notify_for_text
create_notifications_for_text = notify_for_text
notify_mentions = notify_for_text
create_mention_notifications = notify_for_text
record_mentions = notify_for_text
add_mentions = notify_for_text
create_mention = notify_for_text
record_mention = notify_for_text

notify_for_note = notify_for_text
create_notifications_for_note = notify_for_text

list_mentions = list_notifications
get_mentions_list = list_notifications
fetch_notifications = list_notifications
get_notifications = list_notifications
fetch_mentions = list_notifications
query_notifications = list_notifications
search_notifications = list_notifications

get_mention = get_notification
fetch_notification = get_notification
find_notification = get_notification
get = get_notification
fetch = get_notification

mark_as_read = mark_read
mark_notification_read = mark_read
read = mark_read

delete_mention = delete_notification
remove_notification = delete_notification
remove_mention = delete_notification
delete = delete_notification
remove = delete_notification

clear_notifications = clear
clear_mentions = clear
reset = clear
purge = clear

__all__ = [
    "parse_mentions",
    "notify_for_text",
    "notify_for_event_note",
    "notify_for_camera_note",
    "list_notifications",
    "get_notification",
    "mark_read",
    "mark_all_read",
    "delete_notification",
    "unread_count",
    "count",
    "clear",
    "MentionsService",
    "mentions_service",
]
