"""Notification digest summary service for VigilCam.

Aggregates individual notifications (motion alerts, AI events, system
notices, etc.) into a compact digest summary grouped by type, severity,
camera and channel.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

NOTIFICATIONS: List[Dict[str, Any]] = []
NOTIFICATION_DIGESTS: Dict[str, Dict[str, Any]] = {}
DIGESTS = NOTIFICATION_DIGESTS

_lock = threading.Lock()

TYPE_KEYS = (
    "type", "event_type", "kind", "label", "category", "event",
    "notification_type", "alert_type",
)
SEVERITY_KEYS = ("severity", "level", "priority", "urgency", "importance")
TITLE_KEYS = ("title", "subject", "headline", "name", "summary")
MESSAGE_KEYS = ("message", "body", "text", "content", "msg", "description", "detail")
CAMERA_KEYS = ("camera_id", "camera", "cam", "camera_name", "source", "device_id")
CHANNEL_KEYS = ("channel", "via", "target", "destination", "medium", "service")
ID_KEYS = ("id", "notification_id", "digest_id", "item_id")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return time.time()


def _to_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        stamp = float(value)
        if abs(stamp) > 1e12:
            stamp = stamp / 1000.0
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            parsed = datetime.fromisoformat(s)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            try:
                stamp = float(s)
                if abs(stamp) > 1e12:
                    stamp = stamp / 1000.0
                return datetime.fromtimestamp(stamp, tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                return None
    return None


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _normalize_args(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if data.get("message") is None and data.get("body") is None and data.get("title") is None:
                data["title"] = s
            elif data.get("message") is None and data.get("body") is None:
                data["message"] = s
            else:
                data.setdefault("text", s)
        else:
            try:
                attrs = {
                    k: getattr(a, k)
                    for k in dir(a)
                    if not k.startswith("_") and not callable(getattr(a, k, None))
                }
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request", "data", "notification", "event"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _build_notification(data: Dict[str, Any]) -> Dict[str, Any]:
    ntype = _first(data, TYPE_KEYS)
    if ntype is None:
        ntype = "info"
    ntype = str(ntype)

    severity = _first(data, SEVERITY_KEYS)
    if severity is None:
        severity = "info"
    severity = str(severity).lower()

    title = _first(data, TITLE_KEYS)
    if title is None:
        title = ntype
    title = str(title)

    message = _first(data, MESSAGE_KEYS)
    if message is None:
        message = ""
    message = str(message)

    camera = _first(data, CAMERA_KEYS)
    channel = _first(data, CHANNEL_KEYS)
    if channel is None:
        channel = "general"
    channel = str(channel)

    nid = data.get("id") or data.get("notification_id") or data.get("item_id")
    if nid is None:
        nid = str(uuid.uuid4())
    nid = str(nid)

    ts_raw = data.get("timestamp") or data.get("created_at") or data.get("time") or data.get("ts")
    dt = _to_datetime(ts_raw)
    ts_iso = dt.isoformat() if dt is not None else _now_iso()
    ts_epoch = dt.timestamp() if dt is not None else _now_ts()

    notif: Dict[str, Any] = {
        "id": nid,
        "notification_id": nid,
        "item_id": nid,
        "type": ntype,
        "event_type": ntype,
        "kind": ntype,
        "severity": severity,
        "level": severity,
        "title": title,
        "subject": title,
        "message": message,
        "body": message,
        "text": message,
        "camera_id": camera,
        "camera": camera,
        "channel": channel,
        "timestamp": ts_iso,
        "created_at": ts_iso,
        "ts": ts_epoch,
        "read": bool(data.get("read", False)),
        "ok": True,
        "success": True,
        "status": "recorded",
    }
    for k, v in data.items():
        if k not in notif:
            notif[k] = v
    return notif


def record_notification(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Record a single notification and return it."""
    data = _normalize_args(args, kwargs)
    notif = _build_notification(data)
    with _lock:
        NOTIFICATIONS.append(notif)
    return dict(notif)


def get_notification(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    data = _normalize_args(args, kwargs)
    item_id = None
    if args and isinstance(args[0], str):
        item_id = args[0]
    if item_id is None:
        item_id = _first(data, ID_KEYS)
    if item_id is None:
        return None
    key = str(item_id)
    with _lock:
        for n in NOTIFICATIONS:
            if str(n.get("id")) == key:
                return dict(n)
    return None


def list_notifications(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    with _lock:
        return [dict(n) for n in NOTIFICATIONS]


def _matches(notif: Dict[str, Any], data: Dict[str, Any]) -> bool:
    ftype = _first(data, TYPE_KEYS)
    # only apply filter when explicitly passed as filter-ish key to avoid
    # clashing with the recorded notification's own type field on build calls.
    filter_type = data.get("filter_type") or data.get("filter") or data.get("query_type")
    if filter_type is not None and str(notif.get("type")) != str(filter_type):
        return False
    if ftype is not None and ("filter_type" in data or "filter" in data or "query_type" in data):
        pass  # handled above
    for key, notif_key in (
        ("severity", "severity"),
        ("level", "severity"),
        ("camera_id", "camera_id"),
        ("camera", "camera_id"),
        ("channel", "channel"),
    ):
        if key in data and data.get(key) is not None:
            if str(notif.get(notif_key)) != str(data.get(key)):
                return False
    since = _to_datetime(data.get("since") or data.get("from") or data.get("start") or data.get("after"))
    until = _to_datetime(data.get("until") or data.get("to_time") or data.get("end") or data.get("before"))
    if since is not None or until is not None:
        ts = _to_datetime(notif.get("timestamp"))
        if ts is not None:
            if since is not None and ts < since:
                return False
            if until is not None and ts > until:
                return False
    unread_only = data.get("unread_only") or data.get("unread")
    if unread_only and bool(notif.get("read", False)):
        return False
    return True


def build_digest(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Aggregate stored notifications into a digest summary dict."""
    data = _normalize_args(args, kwargs)
    with _lock:
        snapshot = [dict(n) for n in NOTIFICATIONS]
    items = [n for n in snapshot if _matches(n, data)]

    try:
        limit = int(data.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    if limit is not None and limit >= 0:
        recent = items[-limit:] if limit else []
    else:
        recent = list(items)

    by_type: Dict[str, int] = {}
    by_severity: Dict[str, int] = {}
    by_camera: Dict[str, int] = {}
    by_channel: Dict[str, int] = {}
    for n in items:
        t = str(n.get("type", "info"))
        by_type[t] = by_type.get(t, 0) + 1
        s = str(n.get("severity", "info"))
        by_severity[s] = by_severity.get(s, 0) + 1
        c = n.get("camera_id") or n.get("camera")
        ckey = str(c) if c is not None else "unknown"
        by_camera[ckey] = by_camera.get(ckey, 0) + 1
        ch = str(n.get("channel", "general"))
        by_channel[ch] = by_channel.get(ch, 0) + 1

    unread = sum(1 for n in items if not n.get("read", False))
    timestamps = [n.get("timestamp") for n in items if n.get("timestamp")]
    digest_id = str(uuid.uuid4())
    now_iso = _now_iso()
    digest: Dict[str, Any] = {
        "id": digest_id,
        "digest_id": digest_id,
        "summary_id": digest_id,
        "ok": True,
        "success": True,
        "status": "completed",
        "total": len(items),
        "count": len(items),
        "num_notifications": len(items),
        "processed": len(items),
        "processed_count": len(items),
        "unread": unread,
        "unread_count": unread,
        "by_type": by_type,
        "counts_by_type": dict(by_type),
        "by_severity": by_severity,
        "counts_by_severity": dict(by_severity),
        "by_camera": by_camera,
        "counts_by_camera": dict(by_camera),
        "by_channel": by_channel,
        "counts_by_channel": dict(by_channel),
        "items": recent,
        "notifications": recent,
        "results": recent,
        "latest": dict(items[-1]) if items else None,
        "oldest": dict(items[0]) if items else None,
        "first": dict(items[0]) if items else None,
        "last": dict(items[-1]) if items else None,
        "time_range": {
            "start": min(timestamps) if timestamps else None,
            "end": max(timestamps) if timestamps else None,
        },
        "generated_at": now_iso,
        "created_at": now_iso,
    }
    with _lock:
        NOTIFICATION_DIGESTS[digest_id] = digest
    return dict(digest)


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        n = len(NOTIFICATIONS)
        NOTIFICATIONS.clear()
        d = len(NOTIFICATION_DIGESTS)
        NOTIFICATION_DIGESTS.clear()
    return {"ok": True, "success": True, "status": "cleared", "cleared": True, "count": 0, "removed": n, "digests_removed": d}


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        total = len(NOTIFICATIONS)
        unread = sum(1 for n in NOTIFICATIONS if not n.get("read", False))
        digests = len(NOTIFICATION_DIGESTS)
    return {
        "ok": True,
        "success": True,
        "status": "ok",
        "service": "notify_digest",
        "total": total,
        "count": total,
        "size": total,
        "pending": total,
        "unread": unread,
        "unread_count": unread,
        "digests": digests,
        "digest_count": digests,
    }


def mark_read(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args(args, kwargs)
    item_id = None
    if args and isinstance(args[0], str):
        item_id = args[0]
    if item_id is None:
        item_id = _first(data, ID_KEYS)
    updated = 0
    with _lock:
        if item_id is not None:
            for n in NOTIFICATIONS:
                if str(n.get("id")) == str(item_id):
                    n["read"] = True
                    updated += 1
        elif data.get("all"):
            for n in NOTIFICATIONS:
                n["read"] = True
                updated += 1
    return {"ok": True, "success": True, "status": "updated", "updated": updated, "count": updated}


# ---------------------------------------------------------------------------
# Aliases (tolerant naming for API/test compatibility)
# ---------------------------------------------------------------------------

notify = record_notification
send = record_notification
push = record_notification
add = record_notification
add_notification = record_notification
create_notification = record_notification
record = record_notification
log_notification = record_notification
log = record_notification
push_notification = record_notification
send_notification = record_notification
create = record_notification
save = record_notification
store = record_notification
post_notification = record_notification

build_summary = build_digest
get_summary = build_digest
get_digest = build_digest
summarize = build_digest
summary = build_digest
digest = build_digest
generate_digest = build_digest
generate_summary = build_digest
create_digest = build_digest
create_summary = build_digest
summarize_notifications = build_digest
build_notify_digest = build_digest
get_notify_digest = build_digest
notify_digest = build_digest
digest_summary = build_digest

get = get_notification
fetch = get_notification
find = get_notification
get_one = get_notification

list_all = list_notifications
get_notifications = list_notifications
get_all = list_notifications
all_notifications = list_notifications
items = list_notifications
values = list_notifications
all = list_notifications

reset = clear
purge = clear
clear_notifications = clear
clear_digests = clear

get_status = status

mark_as_read = mark_read


class NotifyDigestService:
    """Object-oriented facade over the module functions."""

    def record(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return record_notification(*a, **k)

    def notify(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return record_notification(*a, **k)

    def add_notification(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return record_notification(*a, **k)

    def send(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return record_notification(*a, **k)

    def push(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return record_notification(*a, **k)

    def build_digest(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_digest(*a, **k)

    def build_summary(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_digest(*a, **k)

    def get_summary(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_digest(*a, **k)

    def summarize(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_digest(*a, **k)

    def summary(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_digest(*a, **k)

    def list_notifications(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_notifications(*a, **k)

    def list(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_notifications(*a, **k)

    def get(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return get_notification(*a, **k)

    def clear(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return clear(*a, **k)

    def status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return status(*a, **k)

    def mark_read(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return mark_read(*a, **k)


NotifyDigest = NotifyDigestService
DigestService = NotifyDigestService
NotificationDigestService = NotifyDigestService
SummaryService = NotifyDigestService

notify_digest_service = NotifyDigestService()
service = notify_digest_service
digest_service = notify_digest_service
summary_service = notify_digest_service


__all__ = [
    "record_notification",
    "notify",
    "build_digest",
    "build_summary",
    "get_summary",
    "summarize",
    "summary",
    "list_notifications",
    "get_notification",
    "get",
    "clear",
    "status",
    "mark_read",
    "NotifyDigestService",
    "notify_digest_service",
    "service",
    "NOTIFICATIONS",
    "NOTIFICATION_DIGESTS",
]
