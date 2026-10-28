"""Notification history log service for VigilCam.

Tracks sent/dispatched notification records (channel sends, webhook
deliveries, digest dispatches, ...) in memory so operators can audit
what was sent, to whom, via which channel and with what result.

Complements ``app.services.notify_digest`` (which *builds* digests) and
``app.services.webhook_queue`` (which *sends* webhooks): this module is
the persistent *history log* of dispatched notifications.

All helpers are sync and dependency-free so they work in tests without
a database.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

NOTIFY_HISTORY: Dict[str, "NotifyHistoryEntry"] = {}
notify_history = NOTIFY_HISTORY
history = NOTIFY_HISTORY
HISTORY = NOTIFY_HISTORY
STORE = NOTIFY_HISTORY
ENTRIES = NOTIFY_HISTORY
NOTIFY_HISTORY_STORE = NOTIFY_HISTORY
NOTIFICATION_HISTORY = NOTIFY_HISTORY
LOGS = NOTIFY_HISTORY
LOG_STORE = NOTIFY_HISTORY

_jobs_lock = threading.Lock()
_lock = _jobs_lock


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, set):
        return sorted((_json_safe(v) for v in value), key=repr)
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump(mode="json"))
        except Exception:
            try:
                return _json_safe(value.model_dump())
            except Exception:
                pass
    if hasattr(value, "dict"):
        try:
            return _json_safe(value.dict())
        except Exception:
            pass
    return repr(value)


def _normalize_entry_data(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            # Bare string -> treat as message unless message already set.
            if data.get("message") is None and data.get("body") is None and data.get("title") is None:
                data["title"] = s
            elif data.get("message") is None and data.get("body") is None:
                data["message"] = s
            else:
                data.setdefault("text", s)
        elif hasattr(a, "model_dump"):
            try:
                data.update(dict(a.model_dump()))
            except Exception:
                try:
                    data.update(dict(a.model_dump(mode="json")))  # type: ignore[call-arg]
                except Exception:
                    pass
        else:
            try:
                data.update(dict(a))
            except Exception:
                pass
    data.update(kwargs or {})
    # Unwrap common nested wrappers.
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "entry", "notification", "event", "history", "attributes",
                       "request"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _pick(data: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if lower.get(k.lower()) is not None:
            return lower.get(k.lower())
    return None


class NotifyHistoryEntry(dict):
    """Dict-like notification history record with attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)

    def dict(self) -> Dict[str, Any]:  # pydantic-style compat
        return self.to_dict()

    def model_dump(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()


NotifyHistoryRecord = NotifyHistoryEntry
HistoryEntry = NotifyHistoryEntry
NotificationHistoryEntry = NotifyHistoryEntry
NotificationHistoryRecord = NotifyHistoryEntry
NotifyHistoryModel = NotifyHistoryEntry
NotifyHistoryItem = NotifyHistoryEntry
HistoryRecord = NotifyHistoryEntry
LogEntry = NotifyHistoryEntry


def _build_entry(data: Dict[str, Any]) -> NotifyHistoryEntry:
    entry_id = _pick(data, "id", "entry_id", "history_id", "notification_id",
                    "log_id", "job_id", "jobId") or str(uuid.uuid4())
    entry_id = str(entry_id)
    channel = _pick(data, "channel", "via", "medium", "service",
                    "target_channel", "channel_name", "channelName") or "general"
    channel = str(channel)
    ntype = _pick(data, "type", "event_type", "eventType", "kind", "label",
                  "category", "event", "notification_type", "alert_type") or "info"
    ntype = str(ntype)
    severity = _pick(data, "severity", "level", "priority", "urgency",
                     "importance") or "info"
    severity = str(severity).lower()
    title = _pick(data, "title", "subject", "headline", "name", "summary")
    if title is None:
        title = ntype
    title = str(title)
    message = _pick(data, "message", "msg", "text", "content", "description",
                    "detail", "payload_text")
    if message is None:
        message = ""
    message = str(message)
    camera_id = _pick(data, "camera_id", "camera", "cam", "cameraId",
                      "cameraID", "cam_id", "source_id", "device_id")
    recipient = _pick(data, "recipient", "to", "target", "destination", "dest",
                      "address", "receiver", "subscriber", "email", "phone",
                      "webhook_url", "url", "endpoint")
    status_val = _pick(data, "status", "state", "stage", "result",
                       "delivery_status", "deliveryStatus", "send_status") or "sent"
    status_val = str(status_val)
    now = _pick(data, "created_at", "createdAt", "timestamp", "ts", "time",
                "date", "sent_at", "sentAt") or _now_iso()
    entry = NotifyHistoryEntry()
    entry["id"] = entry_id
    entry["entry_id"] = entry_id
    entry["history_id"] = entry_id
    entry["notification_id"] = str(_pick(data, "notification_id") or entry_id)
    entry["log_id"] = entry_id
    entry["channel"] = channel
    entry["via"] = channel
    entry["medium"] = channel
    entry["service"] = channel
    entry["type"] = ntype
    entry["event_type"] = ntype
    entry["kind"] = ntype
    entry["severity"] = severity
    entry["level"] = severity
    entry["title"] = title
    entry["subject"] = title
    entry["message"] = message
    entry["body"] = message
    entry["text"] = message
    entry["camera_id"] = camera_id
    entry["camera"] = camera_id
    entry["recipient"] = recipient
    entry["to"] = recipient
    entry["target"] = recipient if recipient is not None else _pick(data, "target")
    entry["destination"] = recipient
    entry["status"] = status_val
    entry["state"] = status_val
    entry["result"] = status_val
    entry["delivery_status"] = status_val
    ok_val = _pick(data, "ok", "success", "succeeded", "delivered")
    lowered = status_val.lower()
    if ok_val is None:
        ok_val = lowered in ("sent", "delivered", "success", "succeeded",
                             "completed", "ok", "done")
    entry["ok"] = bool(ok_val)
    entry["success"] = bool(ok_val)
    entry["created_at"] = now
    entry["updated_at"] = _pick(data, "updated_at", "updatedAt") or now
    entry["timestamp"] = now
    entry["sent_at"] = _pick(data, "sent_at", "sentAt") or now
    # Preserve extras.
    for k, v in data.items():
        if k not in entry:
            entry[k] = v
    return entry


def _entry_to_dict(entry: Any) -> Dict[str, Any]:
    if isinstance(entry, dict):
        return dict(entry)
    if hasattr(entry, "model_dump"):
        try:
            return dict(entry.model_dump(mode="json"))
        except Exception:
            try:
                return dict(entry.model_dump())
            except Exception:
                pass
    try:
        return dict(entry)
    except Exception:
        return {"value": _json_safe(entry)}


def _merged_snapshots() -> List[Dict[str, Any]]:
    """Best-effort snapshot of live webhook/digest records for merging."""
    out: List[Dict[str, Any]] = []
    try:
        from app.services.webhook_queue import WEBHOOK_JOBS as _wj  # lazy
        for v in list(_wj.values()):
            try:
                d = _entry_to_dict(v)
            except Exception:
                continue
            # Normalize webhook job -> history view (do not mutate source).
            mapped: Dict[str, Any] = dict(d)
            mapped.setdefault("channel", "webhook")
            mapped.setdefault("type", mapped.get("event_type") or "webhook")
            if mapped.get("status") is None:
                mapped["status"] = "queued"
            out.append(mapped)
    except Exception:
        pass
    try:
        from app.services.notify_digest import NOTIFICATIONS as _ns  # lazy
        for v in list(_ns):
            try:
                d = _entry_to_dict(v)
            except Exception:
                continue
            out.append(dict(d))
    except Exception:
        pass
    return out


def _apply_filters(
    entries: List[Dict[str, Any]],
    limit: Any = None,
    skip: Any = None,
    offset: Any = None,
    include_merged: bool = True,
    **filters: Any,
) -> List[Dict[str, Any]]:
    lim = filters.pop("limit", limit)
    for alias in ("n", "count", "size", "max", "top", "per_page", "page_size"):
        if lim is None and filters.get(alias) is not None:
            lim = filters.pop(alias)
    sk = filters.pop("skip", skip)
    if sk is None:
        sk = filters.pop("offset", offset if offset is not None else 0)
    for alias in ("start_index", "page"):
        filters.pop(alias, None)
    # Drop serialization / merge flags.
    for k in ("format_filter", "include_jobs", "includeJobs", "merge_jobs",
              "include_merged", "includeMerged", "format_"):
        filters.pop(k, None)
    alias_map = {
        "camera": ("camera_id",),
        "cam": ("camera_id",),
        "cameraId": ("camera_id",),
        "camera_name": ("camera_name",),
        "state": ("status",),
        "stage": ("status",),
        "result": ("status",),
        "delivery_status": ("status",),
        "via": ("channel",),
        "medium": ("channel",),
        "service": ("channel",),
        "level": ("severity",),
        "kind": ("type",),
        "event_type": ("type",),
        "to": ("recipient",),
        "target": ("recipient",),
        "destination": ("recipient",),
    }
    out: List[Dict[str, Any]] = []
    for ev in entries:
        ok = True
        for key, expected in (filters or {}).items():
            if expected is None:
                continue
            if key in ("format", "fmt", "output_format") and str(expected).lower() in ("json", "csv"):
                continue
            ev_val = ev.get(key)
            if ev_val is None:
                for alt in alias_map.get(key, ()):
                    if ev.get(alt) is not None:
                        ev_val = ev.get(alt)
                        break
            if ev_val is None:
                if key in alias_map or key in (
                    "channel", "via", "status", "state", "camera_id", "camera",
                    "type", "event_type", "kind", "severity", "level",
                    "recipient", "to", "target", "destination",
                    "id", "entry_id", "history_id", "notification_id",
                ):
                    ok = False
                    break
                continue
            if str(ev_val) != str(expected):
                ok = False
                break
        if ok:
            out.append(ev)
    try:
        sk_i = int(sk or 0)
    except (TypeError, ValueError):
        sk_i = 0
    if sk_i:
        out = out[sk_i:]
    if lim is not None:
        try:
            lim_i = int(lim)
        except (TypeError, ValueError):
            lim_i = None
        if lim_i is not None and lim_i >= 0:
            out = out[:lim_i]
    return out


class NotifyHistoryService:
    """In-memory sent-notification history store."""

    def __init__(self) -> None:
        self.store = NOTIFY_HISTORY
        self.history = NOTIFY_HISTORY
        self.entries = NOTIFY_HISTORY
        self.logs = NOTIFY_HISTORY
        self._lock = _jobs_lock

    # -- record --
    def record_history(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        data = _normalize_entry_data(*args, **kwargs)
        entry = _build_entry(data)
        with self._lock:
            NOTIFY_HISTORY[entry["id"]] = entry
        return entry

    def log_history(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def record_notification(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def log_notification(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def record_send(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def log_send(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def record_delivery(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def log_delivery(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def add_entry(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def create_entry(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def log(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def append(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def record(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def send(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def push(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    def notify(self, *args: Any, **kwargs: Any) -> NotifyHistoryEntry:
        return self.record_history(*args, **kwargs)

    # -- get --
    def get_entry(self, entry_id: Any) -> Optional[NotifyHistoryEntry]:
        if entry_id is None:
            return None
        key = str(entry_id)
        with self._lock:
            found = NOTIFY_HISTORY.get(key)
            if found is not None:
                return found
            for e in NOTIFY_HISTORY.values():
                if str(e.get("notification_id")) == key or str(e.get("history_id")) == key:
                    return e
        return None

    def get(self, entry_id: Any) -> Optional[NotifyHistoryEntry]:
        return self.get_entry(entry_id)

    def fetch_entry(self, entry_id: Any) -> Optional[NotifyHistoryEntry]:
        return self.get_entry(entry_id)

    def fetch(self, entry_id: Any) -> Optional[NotifyHistoryEntry]:
        return self.get_entry(entry_id)

    def find(self, entry_id: Any) -> Optional[NotifyHistoryEntry]:
        return self.get_entry(entry_id)

    def get_notification(self, entry_id: Any) -> Optional[NotifyHistoryEntry]:
        return self.get_entry(entry_id)

    # -- list --
    def list_history(
        self,
        channel: Any = None,
        status: Any = None,  # noqa: A002
        camera_id: Any = None,
        type: Any = None,  # noqa: A002
        severity: Any = None,
        recipient: Any = None,
        limit: Any = None,
        offset: Any = None,
        skip: Any = None,
        include_merged: bool = True,
        **filters: Any,
    ) -> List[NotifyHistoryEntry]:
        if isinstance(channel, dict):
            filters.update(channel)
            channel = None
        if channel is None:
            channel = filters.pop("via", filters.pop("medium", filters.pop("service", None)))
        if camera_id is None:
            camera_id = filters.pop("camera", filters.pop("cam", filters.pop("cameraId", None)))
        if status is None:
            status = filters.pop("state", filters.pop("stage", filters.pop("result", None)))
        if type is None:
            type = filters.pop("event_type", filters.pop("kind", None))
        if severity is None:
            severity = filters.pop("level", None)
        if recipient is None:
            recipient = filters.pop("to", filters.pop("target", filters.pop("destination", None)))
        # Merge flag may arrive via filters.
        if "include_merged" in filters:
            include_merged = filters.pop("include_merged")
        if "includeMerged" in filters:
            include_merged = filters.pop("includeMerged")
        if "include_jobs" in filters:
            include_merged = filters.pop("include_jobs")
        with self._lock:
            items = [_entry_to_dict(e) for e in list(NOTIFY_HISTORY.values())]
        if include_merged is not False:
            seen = {str(e.get("id")) for e in items}
            for job in _merged_snapshots():
                jid = str(job.get("id") or job.get("job_id") or job.get("notification_id") or "")
                if jid and jid not in seen:
                    # Only merge items that look dispatched; keep tolerant.
                    items.append(_entry_to_dict(_build_entry(job)))
                    seen.add(jid)
                elif not jid:
                    items.append(_entry_to_dict(_build_entry(job)))
        query: Dict[str, Any] = {}
        if channel is not None:
            query["channel"] = channel
        if status is not None:
            query["status"] = status
        if camera_id is not None:
            query["camera_id"] = camera_id
        if type is not None:
            query["type"] = type
        if severity is not None:
            query["severity"] = severity
        if recipient is not None:
            query["recipient"] = recipient
        query.update({k: v for k, v in filters.items() if v is not None})
        return _apply_filters(items, limit=limit, skip=skip if skip is not None else offset,
                              include_merged=include_merged, **query)  # type: ignore[return-value]

    def list(self, *args: Any, **kwargs: Any) -> List[NotifyHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_history(self, *args: Any, **kwargs: Any) -> List[NotifyHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_history(self, *args: Any, **kwargs: Any) -> List[NotifyHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_entries(self, *args: Any, **kwargs: Any) -> List[NotifyHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_entries(self, *args: Any, **kwargs: Any) -> List[NotifyHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def all(self, *args: Any, **kwargs: Any) -> List[NotifyHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def list_notifications(self, *args: Any, **kwargs: Any) -> List[NotifyHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_notifications(self, *args: Any, **kwargs: Any) -> List[NotifyHistoryEntry]:
        return self.list_history(*args, **kwargs)

    # -- delete / clear --
    def delete_entry(self, entry_id: Any) -> bool:
        if entry_id is None:
            return False
        key = str(entry_id)
        with self._lock:
            if NOTIFY_HISTORY.pop(key, None) is not None:
                return True
            for k, e in list(NOTIFY_HISTORY.items()):
                if str(e.get("notification_id")) == key or str(e.get("history_id")) == key:
                    NOTIFY_HISTORY.pop(k, None)
                    return True
        return False

    def delete(self, entry_id: Any) -> bool:
        return self.delete_entry(entry_id)

    def remove(self, entry_id: Any) -> bool:
        return self.delete_entry(entry_id)

    def remove_entry(self, entry_id: Any) -> bool:
        return self.delete_entry(entry_id)

    def clear_history(self, **filters: Any) -> int:
        with self._lock:
            if not filters:
                n = len(NOTIFY_HISTORY)
                NOTIFY_HISTORY.clear()
                return n
        targets = self.list_history(include_merged=False, **filters)
        n = 0
        with self._lock:
            for t in targets:
                if NOTIFY_HISTORY.pop(str(t.get("id")), None) is not None:
                    n += 1
        return n

    def clear(self, **filters: Any) -> int:
        return self.clear_history(**filters)

    def reset(self, **filters: Any) -> int:
        return self.clear_history(**filters)

    def purge(self, **filters: Any) -> int:
        return self.clear_history(**filters)

    def count(self, **filters: Any) -> int:
        return len(self.list_history(**filters))

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            total = len(NOTIFY_HISTORY)
            by_channel: Dict[str, int] = {}
            by_status: Dict[str, int] = {}
            for e in NOTIFY_HISTORY.values():
                ch = str(e.get("channel", "general"))
                by_channel[ch] = by_channel.get(ch, 0) + 1
                st = str(e.get("status", "sent"))
                by_status[st] = by_status.get(st, 0) + 1
        return {
            "ok": True,
            "success": True,
            "status": "ok",
            "service": "notify_history",
            "total": total,
            "count": total,
            "size": total,
            "entries": total,
            "logs": total,
            "by_channel": by_channel,
            "counts_by_channel": dict(by_channel),
            "by_status": by_status,
            "counts_by_status": dict(by_status),
        }

    def __len__(self) -> int:
        with self._lock:
            return len(NOTIFY_HISTORY)


notify_history_service = NotifyHistoryService()
history_service = notify_history_service
service = notify_history_service
manager = notify_history_service
history_manager = notify_history_service
store = notify_history_service
default_service = notify_history_service
log_service = notify_history_service
notification_history_service = notify_history_service


def record_history(*args: Any, **kwargs: Any) -> NotifyHistoryEntry:
    return notify_history_service.record_history(*args, **kwargs)


def log_history(*args: Any, **kwargs: Any) -> NotifyHistoryEntry:
    return notify_history_service.log_history(*args, **kwargs)


def record_notification(*args: Any, **kwargs: Any) -> NotifyHistoryEntry:
    return notify_history_service.record_notification(*args, **kwargs)


def log_notification(*args: Any, **kwargs: Any) -> NotifyHistoryEntry:
    return notify_history_service.log_notification(*args, **kwargs)


def record_send(*args: Any, **kwargs: Any) -> NotifyHistoryEntry:
    return notify_history_service.record_send(*args, **kwargs)


def log_send(*args: Any, **kwargs: Any) -> NotifyHistoryEntry:
    return notify_history_service.log_send(*args, **kwargs)


def add_entry(*args: Any, **kwargs: Any) -> NotifyHistoryEntry:
    return notify_history_service.add_entry(*args, **kwargs)


def create_entry(*args: Any, **kwargs: Any) -> NotifyHistoryEntry:
    return notify_history_service.create_entry(*args, **kwargs)


def get_entry(entry_id: Any) -> Optional[NotifyHistoryEntry]:
    return notify_history_service.get_entry(entry_id)


def list_history(*args: Any, **kwargs: Any) -> List[NotifyHistoryEntry]:
    return notify_history_service.list_history(*args, **kwargs)


def fetch_history(*args: Any, **kwargs: Any) -> List[NotifyHistoryEntry]:
    return notify_history_service.list_history(*args, **kwargs)


def list_entries(*args: Any, **kwargs: Any) -> List[NotifyHistoryEntry]:
    return notify_history_service.list_history(*args, **kwargs)


def fetch_entries(*args: Any, **kwargs: Any) -> List[NotifyHistoryEntry]:
    return notify_history_service.list_history(*args, **kwargs)


def delete_entry(entry_id: Any) -> bool:
    return notify_history_service.delete_entry(entry_id)


def clear_history(**filters: Any) -> int:
    return notify_history_service.clear_history(**filters)


def clear(**filters: Any) -> int:
    return notify_history_service.clear_history(**filters)


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return notify_history_service.status(*args, **kwargs)


def count(**filters: Any) -> int:
    return notify_history_service.count(**filters)


# CSV / JSON helpers for history dumps.
DEFAULT_COLUMNS = [
    "id",
    "channel",
    "recipient",
    "type",
    "severity",
    "title",
    "message",
    "camera_id",
    "status",
    "created_at",
]

CSV_COLUMNS = DEFAULT_COLUMNS
EXPORT_COLUMNS = DEFAULT_COLUMNS
COLUMNS = DEFAULT_COLUMNS
HEADERS = DEFAULT_COLUMNS
FIELDNAMES = DEFAULT_COLUMNS


def _cell(value: Any) -> str:
    safe = _json_safe(value)
    if safe is None:
        return ""
    if isinstance(safe, (dict, list)):
        try:
            return json.dumps(safe, default=str)
        except Exception:
            return str(safe)
    return str(safe)


def to_csv(entries: Any = None, columns: Any = None, **kwargs: Any) -> str:
    if entries is None:
        entries = kwargs.pop("entries", kwargs.pop("items", kwargs.pop("logs", [])))
    if isinstance(entries, dict):
        entries = [entries]
    entries = list(entries or [])
    cols = list(columns or kwargs.get("fields") or kwargs.get("fieldnames") or DEFAULT_COLUMNS)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for e in entries:
        norm = _entry_to_dict(e)
        writer.writerow({c: _cell(norm.get(c)) for c in cols})
    return buf.getvalue()


def to_json(entries: Any = None, indent: int = 2, **kwargs: Any) -> str:
    if entries is None:
        entries = kwargs.pop("entries", kwargs.pop("items", kwargs.pop("logs", [])))
    if isinstance(entries, dict):
        entries = [entries]
    entries = list(entries or [])
    normed = [_entry_to_dict(e) for e in entries]
    try:
        return json.dumps(_json_safe(normed), indent=kwargs.get("indent", indent), default=str)
    except Exception:
        return json.dumps(_json_safe(normed), default=str)


def export_entries(entries: Any = None, format: str = "json", **kwargs: Any) -> str:  # noqa: A002
    fmt = str(kwargs.pop("fmt", None) or kwargs.pop("output_format", None) or format or "json").lower()
    if fmt in ("csv", "text/csv", "text-csv"):
        return to_csv(entries, **kwargs)
    return to_json(entries, **kwargs)


# Compatibility aliases.
record = record_history
log = log_history
add = add_entry
create = create_entry
get = get_entry
fetch = get_entry
find = get_entry
list_all = list_history
all_entries = list_history
get_entries = list_entries
remove = delete_entry
delete = delete_entry
reset = clear
purge = clear
history_entries = list_history
notify_history_entries = list_history
notification_history_entries = list_history
build_csv = to_csv
export_csv = to_csv
build_json = to_json
export_json = to_json
get_status = status

NotifyHistoryServiceClass = NotifyHistoryService
NotifyHistoryManager = NotifyHistoryService
HistoryService = NotifyHistoryService
HistoryManager = NotifyHistoryService
NotificationHistoryService = NotifyHistoryService
NotificationHistoryManager = NotifyHistoryService

__all__ = [
    "NOTIFY_HISTORY",
    "NotifyHistoryEntry",
    "NotifyHistoryService",
    "notify_history_service",
    "record_history",
    "log_history",
    "record_notification",
    "log_notification",
    "record_send",
    "log_send",
    "add_entry",
    "create_entry",
    "get_entry",
    "list_history",
    "fetch_history",
    "delete_entry",
    "clear_history",
    "clear",
    "status",
    "count",
    "to_csv",
    "to_json",
    "export_entries",
]
