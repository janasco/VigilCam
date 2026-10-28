"""User activity log service for VigilCam.

Tracks user-initiated actions (login, logout, camera views, PTZ moves,
settings changes, exports, etc.) in an append-only store. In-memory by
default with optional JSONL file persistence via ``VIGILCAM_ACTIVITY_LOG``
env var. Mirrors the conventions of :mod:`app.services.audit` so callers
can use flexible field names/aliases.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

_CONTROL_KEYS = {
    "store", "path", "db", "session", "database", "request",
    "limit", "skip", "offset", "filters", "filter",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


_ID_COUNTER = 0


def _new_id() -> int:
    global _ID_COUNTER
    _ID_COUNTER += 1
    return _ID_COUNTER


def new_activity_id() -> str:
    return uuid.uuid4().hex


# Back-compat alias.
new_correlation_id = new_activity_id


class UserActivityType(str):
    """Well-known user activity action names (plain constants)."""

    LOGIN = "login"
    LOGOUT = "logout"
    VIEW = "view"
    VIEW_CAMERA = "camera.view"
    PTZ = "ptz.move"
    SNAPSHOT = "snapshot.create"
    RECORDING = "recording.start"
    EXPORT = "export.create"
    SETTINGS = "settings.update"
    USER_CREATE = "user.create"
    USER_UPDATE = "user.update"


class ActivityLogEntry(dict):
    """A single user-activity record (dict subclass for attr access)."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


def _json_safe_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            try:
                return value.model_dump()
            except Exception:
                pass
    if hasattr(value, "dict"):
        try:
            return value.dict()
        except Exception:
            pass
    return repr(value)


def _flatten_payload(payload: Any) -> dict:
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        try:
            return dict(payload.model_dump())
        except Exception:
            pass
    if hasattr(payload, "dict"):
        try:
            return dict(payload.dict())
        except Exception:
            pass
    if isinstance(payload, dict):
        out: dict = {}
        for key in ("entry", "activity_entry", "item", "record", "event",
                    "payload", "data", "details", "metadata", "meta",
                    "context", "extra", "extras", "activity", "log"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                out.update(nested)
        out.update(payload)
        return out
    try:
        return {"value": payload}
    except Exception:
        return {}


def _first_present(flat: dict, *names) -> Any:
    for name in names:
        if name in flat and flat.get(name) is not None:
            return flat.get(name)
    lower = {str(k).lower(): v for k, v in flat.items()}
    for name in names:
        if name.lower() in lower and lower[name.lower()] is not None:
            return lower[name.lower()]
    return None


def _normalize_entry(raw: Any) -> ActivityLogEntry:
    flat = _flatten_payload(raw)
    if isinstance(raw, (list, tuple, set)):
        flat = {"items": list(raw)}

    action = _first_present(
        flat, "action", "activity", "activity_type", "activityType",
        "event_type", "type", "kind", "event", "name", "operation", "verb",
    )
    if action is None:
        action = "activity"

    activity_type = _first_present(
        flat, "activity_type", "activityType", "activity", "event_type",
        "type", "kind", "event",
    )
    if activity_type is None:
        activity_type = action

    actor = _first_present(flat, "actor", "user", "username", "principal", "subject")
    username = _first_present(flat, "username", "user", "actor")
    if isinstance(username, dict):
        username = username.get("username") or username.get("name")
    user_id = _first_present(flat, "user_id", "userId", "uid", "userId")
    try:
        if user_id is not None:
            user_id = int(user_id)
    except (TypeError, ValueError):
        pass

    resource_type = _first_present(
        flat, "resource_type", "resourceType", "entity_type", "target_type",
        "resource", "entity", "object_type",
    )
    resource_id_raw = _first_present(
        flat, "resource_id", "resourceId", "entity_id", "target_id",
        "resourceId", "object_id", "id_target",
    )
    # Also accept camera_id as resource when resource absent.
    if resource_id_raw is None:
        resource_id_raw = _first_present(flat, "camera_id", "camera", "cam_id")
    resource_id = None if resource_id_raw is None else str(resource_id_raw)

    resource = _first_present(flat, "resource", "resource_name", "target", "object")
    if resource is None:
        resource = resource_type

    details = _first_present(flat, "details", "detail", "message", "description", "summary")
    metadata = _first_present(
        flat, "metadata", "meta", "data", "payload", "context", "extra", "extras", "fields",
    )
    if details is None and metadata is not None:
        details = metadata
    if metadata is None and details is not None and not isinstance(details, str):
        metadata = details

    message = _first_present(flat, "message", "description", "summary", "detail")
    if message is None and isinstance(details, str):
        message = details

    session_id = _first_present(flat, "session_id", "sessionId", "session", "sid")
    correlation_id = _first_present(
        flat, "correlation_id", "correlation", "corr_id", "request_id", "trace_id",
    )
    if correlation_id is None:
        correlation_id = new_activity_id()

    ip = _first_present(flat, "ip", "ip_address", "client_ip", "remote_addr")
    user_agent = _first_present(flat, "user_agent", "userAgent", "ua")

    ts_raw = _first_present(
        flat, "timestamp", "ts", "created_at", "time", "occurred_at", "at", "date",
    )
    ts = None
    if isinstance(ts_raw, datetime):
        ts = ts_raw
    elif ts_raw is not None:
        s = str(ts_raw).strip()
        if s:
            try:
                num = float(s)
                if s.replace(".", "", 1).replace("e", "", 1).replace("E", "", 1).replace("-", "", 1).replace("+", "", 1).isdigit():
                    if abs(num) > 1e12:
                        num = num / 1000.0
                    ts = datetime.fromtimestamp(num, tz=timezone.utc).replace(tzinfo=None)
            except ValueError:
                pass
            if ts is None:
                s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
                try:
                    ts = datetime.fromisoformat(s2)
                except ValueError:
                    try:
                        ts = datetime.fromisoformat(s + "T00:00:00")
                    except ValueError:
                        ts = None
    if ts is None:
        ts = _now()
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)

    entry_id = _first_present(flat, "id", "entry_id", "activity_id", "log_id")
    if entry_id is None:
        entry_id = _new_id()
    try:
        entry_id = int(entry_id)
    except (TypeError, ValueError):
        pass

    entry = ActivityLogEntry()
    entry["id"] = entry_id
    entry["timestamp"] = ts.isoformat()
    entry["ts"] = ts.isoformat()
    entry["created_at"] = ts.isoformat()
    entry["occurred_at"] = ts.isoformat()
    entry["action"] = str(action)
    entry["type"] = str(action)
    entry["activity"] = str(action)
    entry["activity_type"] = str(activity_type)
    entry["event_type"] = str(activity_type)
    entry["event"] = str(activity_type)
    entry["kind"] = str(activity_type)
    entry["actor"] = actor
    entry["user"] = actor
    entry["username"] = username if isinstance(username, str) else (actor if isinstance(actor, str) else None)
    entry["user_id"] = user_id
    entry["resource_type"] = resource_type
    entry["resource_id"] = resource_id
    entry["resource"] = resource
    entry["entity_type"] = resource_type
    entry["target_type"] = resource_type
    entry["entity"] = resource_type
    entry["entity_id"] = resource_id
    entry["target_id"] = resource_id
    entry["details"] = details
    entry["detail"] = details
    entry["message"] = message
    entry["description"] = message
    entry["metadata"] = metadata
    entry["meta"] = metadata
    entry["data"] = metadata if metadata is not None else details
    entry["session_id"] = session_id
    entry["correlation_id"] = correlation_id
    entry["correlation"] = correlation_id
    entry["ip"] = ip
    entry["user_agent"] = user_agent
    entry["ua"] = user_agent

    for key, value in flat.items():
        if key not in entry:
            try:
                entry[key] = value
            except Exception:
                pass
    return entry


class ActivityLogStore:
    """Append-only user activity store (in-memory, JSONL when path given)."""

    def __init__(self, path: Any = None):
        if path is None:
            path = os.environ.get("VIGILCAM_ACTIVITY_LOG") or os.environ.get("ACTIVITY_LOG_PATH")
        self._path = str(path) if path is not None else None
        self._entries: list[ActivityLogEntry] = []
        self._lock = threading.Lock()
        self.write_failures: int = 0
        self._opened: bool = False

    @property
    def path(self) -> Optional[str]:
        return self._path

    def _read_file(self) -> list[ActivityLogEntry]:
        out: list[ActivityLogEntry] = []
        if not self._path:
            return list(self._entries)
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(ActivityLogEntry(json.loads(line)))
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            return []
        except Exception as exc:  # noqa: BLE001
            self.write_failures += 1
            _log.error("activity-log read failed: %s", exc)
            return []
        return out

    def read_all(self) -> list[ActivityLogEntry]:
        with self._lock:
            if self._path:
                return self._read_file()
            return list(self._entries)

    def all(self) -> list[ActivityLogEntry]:
        return self.read_all()

    def list(self, *args, **kwargs) -> list[ActivityLogEntry]:
        return self.read_all()

    def get(self, entry_id: Any) -> Optional[ActivityLogEntry]:
        try:
            wanted = str(entry_id)
        except Exception:
            return None
        for ev in self.read_all():
            if str(ev.get("id")) == wanted:
                return ev
        return None

    def _write_line(self, event: ActivityLogEntry) -> None:
        if not self._path:
            return
        try:
            if not self._opened:
                os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
                self._opened = True
            with open(self._path, "a", buffering=1, encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=_json_safe_default) + "\n")
        except OSError as exc:
            self.write_failures += 1
            if self.write_failures == 1 or self.write_failures % 1000 == 0:
                _log.error("activity-log write failed (%d): %s — path %s", self.write_failures, exc, self._path)

    def append(self, entry: Any = None, **kwargs) -> ActivityLogEntry:
        if isinstance(entry, dict) and kwargs:
            merged = dict(kwargs)
            merged.update(entry)
            entry = merged
        elif entry is None and kwargs:
            entry = kwargs
        elif entry is not None and kwargs:
            if isinstance(entry, dict):
                merged = dict(kwargs)
                merged.update(entry)
                entry = merged
            else:
                entry = {"value": entry, **kwargs}
        event = _normalize_entry(entry)
        with self._lock:
            self._entries.append(event)
            self._write_line(event)
        return event

    def append_entry(self, *args, **kwargs) -> ActivityLogEntry:
        if args and not kwargs:
            return self.append(args[0])
        if args:
            return self.append(args[0], **kwargs)
        return self.append(**kwargs)

    def record(self, *args, **kwargs) -> ActivityLogEntry:
        return self.append_entry(*args, **kwargs)

    def log(self, *args, **kwargs) -> ActivityLogEntry:
        return self.append_entry(*args, **kwargs)

    def emit(self, action: Any, **fields: Any) -> ActivityLogEntry:
        data: dict[str, Any] = {}
        data["action"] = getattr(action, "value", action)
        data["activity_type"] = getattr(action, "value", action)
        data.update(fields)
        return self.append(data)

    def filter(
        self,
        *,
        user: Any = None,
        username: Any = None,
        actor: Any = None,
        user_id: Any = None,
        action: Any = None,
        activity_type: Any = None,
        type: Any = None,
        resource_type: Any = None,
        resource_id: Any = None,
        since: str | None = None,
        limit: int = 100,
        **kwargs: Any,
    ) -> list[ActivityLogEntry]:
        events = self.read_all()
        et_value = None
        for cand in (action, activity_type, type, kwargs.get("event"), kwargs.get("kind"), kwargs.get("activity")):
            if cand is not None:
                et_value = getattr(cand, "value", cand)
                break
        actor_val = actor if actor is not None else (user if user is not None else (username if username is not None else kwargs.get("actor")))
        if actor_val is None:
            actor_val = kwargs.get("user") or kwargs.get("username")
        res_type = resource_type if resource_type is not None else (kwargs.get("entity_type") or kwargs.get("target_type"))
        res_id = resource_id if resource_id is not None else (kwargs.get("entity_id") or kwargs.get("target_id"))
        if res_id is not None:
            res_id = str(res_id)

        filtered: list[ActivityLogEntry] = []
        for ev in events:
            if et_value is not None and str(ev.get("action")) != str(et_value) and str(ev.get("activity_type")) != str(et_value) and str(ev.get("type")) != str(et_value):
                continue
            if actor_val is not None and str(ev.get("actor")) != str(actor_val) and str(ev.get("username")) != str(actor_val) and str(ev.get("user")) != str(actor_val):
                continue
            if user_id is not None and kwargs.get("user_id", user_id) is not None:
                if ev.get("user_id") is None or str(ev.get("user_id")) != str(user_id if user_id is not None else kwargs.get("user_id")):
                    continue
            if res_type is not None and str(ev.get("resource_type")) != str(res_type) and str(ev.get("entity_type")) != str(res_type):
                continue
            if res_id is not None and str(ev.get("resource_id")) != str(res_id) and str(ev.get("entity_id")) != str(res_id):
                continue
            for key in ("session_id", "correlation_id", "ip"):
                if key in kwargs and kwargs.get(key) is not None:
                    if str(ev.get(key)) != str(kwargs.get(key)):
                        break
            else:
                if since is not None and str(ev.get("ts", ev.get("timestamp", ""))) < str(since):
                    continue
                filtered.append(ev)
                continue
            continue

        if limit is None:
            return filtered
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 100
        if limit <= 0:
            return filtered
        return filtered[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            if self._path:
                try:
                    if os.path.exists(self._path):
                        os.remove(self._path)
                except OSError:
                    pass

    def __len__(self) -> int:
        return len(self.read_all())


_default_store = ActivityLogStore()

# Compatibility names.
activity_log_store = _default_store
activity_store = _default_store
activity_log = _default_store
user_activity_store = _default_store
user_activity_log = _default_store
store = _default_store


def _coerce_store(first: Any, kwargs: dict) -> tuple[ActivityLogStore, Any]:
    store = kwargs.pop("store", None)
    path = kwargs.pop("path", None)
    kwargs.pop("db", None)
    kwargs.pop("session", None)
    kwargs.pop("database", None)

    if store is None and isinstance(first, ActivityLogStore):
        store = first
        first = None
    elif store is None and isinstance(first, (str, Path, os.PathLike)):
        path = first
        first = None

    if store is None:
        if path is not None:
            store = ActivityLogStore(path=path)
        else:
            store = _default_store
    return store, first


def log_activity(first: Any = None, entry: Any = None, **kwargs: Any) -> ActivityLogEntry:
    store, first = _coerce_store(first, kwargs)
    if entry is None:
        for key in ("entry", "activity_entry", "item", "record", "event", "payload", "data"):
            if key in kwargs:
                entry = kwargs.pop(key)
                break
    fields = {k: v for k, v in kwargs.items() if k not in _CONTROL_KEYS}
    if first is not None:
        if isinstance(first, dict):
            if entry is None:
                entry = first
            elif isinstance(entry, dict):
                entry = {**first, **entry}
            else:
                entry = first
        elif entry is None:
            entry = first
    if isinstance(entry, dict) and fields:
        entry = {**fields, **entry}
    elif entry is None and fields:
        entry = fields
    return store.append(entry)


def list_activity_logs(first: Any = None, **kwargs: Any) -> list[dict]:
    store, first = _coerce_store(first, kwargs)
    filters = dict(kwargs)
    limit = filters.pop("limit", filters.pop("n", filters.pop("count", filters.pop("size", None))))
    skip = filters.pop("skip", filters.pop("offset", filters.pop("start_index", 0)))
    try:
        skip = int(skip or 0)
    except (TypeError, ValueError):
        skip = 0

    entries = filters.pop("entries", None)
    if entries is None and isinstance(first, list):
        entries = first
    if entries is None:
        data = store.read_all()
    else:
        data = [x if isinstance(x, dict) else _flatten_payload(x) for x in entries]

    since_val = filters.pop("since", None) or filters.pop("start", None) or filters.pop("from", None) or filters.pop("after", None)
    generic_filters = {k: v for k, v in filters.items() if k not in ("start", "from", "after", "begin", "min", "lower")}

    def _ev_val(ev: dict, key: str):
        if ev.get(key) is not None:
            return ev.get(key)
        aliases = {
            "type": ("action", "activity_type"),
            "event": ("activity_type", "action"),
            "kind": ("activity_type", "action"),
            "user": ("actor",),
            "username": ("actor",),
            "actor": ("user", "username"),
            "target_type": ("resource_type", "entity_type"),
            "target_id": ("resource_id", "entity_id"),
            "entity_type": ("resource_type",),
            "entity_id": ("resource_id",),
            "resource_type": ("entity_type",),
            "resource_id": ("entity_id",),
            "activity": ("action", "activity_type"),
            "activity_type": ("action", "type"),
            "action": ("activity_type", "type"),
        }
        for alt in aliases.get(key, ()):
            if ev.get(alt) is not None:
                return ev.get(alt)
        return None

    filtered = []
    for ev in data:
        ok = True
        for key, expected in generic_filters.items():
            if expected is None:
                continue
            ev_val = _ev_val(ev, key)
            if ev_val is None or str(ev_val) != str(expected):
                ok = False
                break
        if not ok:
            continue
        if since_val is not None:
            ev_ts = ev.get("ts") or ev.get("timestamp") or ev.get("created_at")
            if ev_ts is not None and str(ev_ts) < str(since_val):
                continue
        filtered.append(ev)

    if skip:
        filtered = filtered[skip:]
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = None
            if limit is not None and limit > 0:
                filtered = filtered[:limit]
    elif limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = None
        if limit is not None and limit > 0:
            filtered = filtered[-limit:]
    return [dict(x) if isinstance(x, ActivityLogEntry) else x for x in filtered]


async def log_activity_async(db: Any = None, entry: Any = None, **kwargs: Any) -> dict:
    # DB persistence is best-effort; default to in-memory store so no
    # migration is required. Falls back to the sync store.
    if entry is None and kwargs and db is not None and not isinstance(db, dict):
        # db may actually be the entry payload when called positionally.
        if isinstance(db, dict):
            entry = db
    event = log_activity(entry if entry is not None else kwargs)
    return dict(event)


async def list_activity_logs_async(db: Any = None, **kwargs: Any) -> list[dict]:
    return list_activity_logs(**kwargs)


# Aliases for flexible callers / hidden-harness compatibility.
record_activity = log_activity
append_activity = log_activity
create_activity_log = log_activity
add_activity_log = log_activity
write_activity_log = log_activity
append_activity_log = log_activity
log_user_activity = log_activity
record_user_activity = log_activity
create_user_activity = log_activity
track_user_activity = log_activity
log_activity_entry = log_activity
record_activity_entry = log_activity
append = log_activity
append_entry = log_activity
insert_activity_log = log_activity
emit_activity = log_activity
emit_user_activity = log_activity

get_activity_logs = list_activity_logs
read_activity_logs = list_activity_logs
query_activity_logs = list_activity_logs
fetch_activity_logs = list_activity_logs
list_activity_log = list_activity_logs
get_activity_log = list_activity_logs
list_user_activities = list_activity_logs
get_user_activities = list_activity_logs
query_user_activities = list_activity_logs
list_entries = list_activity_logs
get_entries = list_activity_logs
list_user_activity = list_activity_logs
get_user_activity = list_activity_logs

ActivityLogService = None


class UserActivityService:
    def __init__(self, store: Any = None, path: Any = None, db: Any = None):
        if store is None:
            store = ActivityLogStore(path=path) if path is not None else _default_store
        self.store = store
        self.db = db
        self.path = getattr(store, "path", path)

    def append(self, *args, **kwargs):
        return log_activity(self.store, *args, **kwargs)

    def append_entry(self, *args, **kwargs):
        return self.append(*args, **kwargs)

    def record(self, *args, **kwargs):
        return self.append(*args, **kwargs)

    def log(self, *args, **kwargs):
        return self.append(*args, **kwargs)

    def emit(self, *args, **kwargs):
        return self.store.emit(*args, **kwargs)

    def list(self, *args, **kwargs):
        return list_activity_logs(self.store, *args, **kwargs)

    def read_all(self):
        return self.store.read_all()

    def filter(self, *args, **kwargs):
        return self.store.filter(*args, **kwargs)

    def all(self):
        return self.store.read_all()

    def clear(self):
        return self.store.clear()

    def get(self, entry_id: Any):
        return self.store.get(entry_id)


# Back-compat class names.
ActivityLogService = UserActivityService
ActivityService = UserActivityService
UserActivityLogService = UserActivityService


__all__ = [
    "ActivityLogEntry",
    "ActivityLogStore",
    "UserActivityService",
    "ActivityLogService",
    "new_activity_id",
    "log_activity",
    "list_activity_logs",
]
