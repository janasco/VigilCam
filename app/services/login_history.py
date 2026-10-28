"""Login history service.

Tracks authentication attempts (successful and failed logins) in memory.
This is distinct from:

- :mod:`app.services.sessions` which manages live session tokens
  (creation, validation, revocation) and does not retain a history of
  login attempts, and
- :mod:`app.services.activity_log` which is a generic user-action log
  (camera views, PTZ, exports, ...) and has no dedicated login-attempt
  schema (success/failure, IP, user-agent, failure reason).

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

LOGIN_HISTORY: Dict[str, "LoginHistoryEntry"] = {}
login_history = LOGIN_HISTORY
history = LOGIN_HISTORY
HISTORY = LOGIN_HISTORY
STORE = LOGIN_HISTORY
ENTRIES = LOGIN_HISTORY
LOGIN_HISTORY_STORE = LOGIN_HISTORY
HISTORY_STORE = LOGIN_HISTORY
LOGIN_STORE = LOGIN_HISTORY
LOGINS = LOGIN_HISTORY
STORE_HISTORY = LOGIN_HISTORY

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
                       "entry", "login", "attempt", "attributes", "record"):
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


def _parse_success(data: Dict[str, Any]) -> tuple[bool, str]:
    """Return (success_bool, status_str) from flexible input fields."""
    raw = _pick(
        data,
        "success", "successful", "succeeded", "ok", "is_success",
        "status", "state", "result", "outcome",
    )
    if raw is None:
        # Default to success when nothing says otherwise.
        return True, "success"
    if isinstance(raw, bool):
        return raw, ("success" if raw else "failed")
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "y", "t", "ok", "success", "successful",
             "succeeded", "passed", "pass", "valid", "authenticated", "allow",
             "allowed", "200", "201"):
        return True, "success"
    if s in ("0", "false", "no", "n", "f", "fail", "failed", "failure",
             "error", "errors", "invalid", "unauthorized", "denied",
             "rejected", "bad", "wrong", "401", "403"):
        return False, "failed"
    # Unknown status strings: treat "success*" as success, "fail*"/"error*" as failure.
    if s.startswith("success") or s.startswith("ok"):
        return True, "success"
    if s.startswith("fail") or s.startswith("error") or s.startswith("invalid"):
        return False, "failed"
    # Any other explicit status: keep verbatim, infer success only if it
    # literally equals success.
    return (s == "success"), s or "success"


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "t", "ok", "success", "successful", "succeeded"):
        return True
    if s in ("0", "false", "no", "n", "f", "fail", "failed", "failure", "error"):
        return False
    return None


class LoginHistoryEntry(dict):
    """Dict-like login-attempt record with attribute access."""

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


LoginHistoryRecord = LoginHistoryEntry
HistoryEntry = LoginHistoryEntry
LoginRecord = LoginHistoryEntry
LoginEntry = LoginHistoryEntry
HistoryRecord = LoginHistoryEntry
LoginHistoryModel = LoginHistoryEntry
LoginHistoryItem = LoginHistoryEntry
AttemptEntry = LoginHistoryEntry


def _build_entry(data: Dict[str, Any]) -> LoginHistoryEntry:
    entry_id = _pick(data, "id", "entry_id", "history_id", "login_id",
                     "attempt_id", "record_id") or str(uuid.uuid4())
    entry_id = str(entry_id)

    username = _pick(data, "username", "user", "user_name", "name",
                     "account", "login", "principal")
    user_id = _pick(data, "user_id", "userId", "uid", "userID",
                    "account_id", "owner_id")
    if username is None and user_id is not None:
        username = str(user_id)
    if username is not None:
        username = str(username)

    success, status = _parse_success(data)
    # Keep explicit status alias in sync.
    status = str(status or ("success" if success else "failed"))

    ip = _pick(data, "ip", "ip_address", "ipAddress", "client_ip",
               "clientIp", "remote_addr", "remoteAddr", "remote_ip")
    user_agent = _pick(data, "user_agent", "userAgent", "ua",
                       "user-agent", "agent")
    device = _pick(data, "device", "device_name", "deviceName",
                   "client", "client_name")
    if user_agent is None:
        user_agent = device
    if device is None:
        device = user_agent

    ts = _pick(data, "timestamp", "ts", "created_at", "createdAt",
               "logged_at", "loggedAt", "occurred_at", "occurredAt",
               "time", "date", "at", "login_at", "attempted_at")
    if ts is None:
        ts = _now_iso()
    else:
        if isinstance(ts, datetime):
            ts = ts.isoformat()
        else:
            ts = str(ts)

    reason = _pick(data, "reason", "failure_reason", "failureReason",
                   "fail_reason", "message", "error", "detail",
                   "description", "cause")
    method = _pick(data, "method", "auth_method", "authMethod",
                   "provider", "auth_type", "login_method")
    session_id = _pick(data, "session_id", "sessionId", "sid", "session")

    entry = LoginHistoryEntry()
    entry["id"] = entry_id
    entry["entry_id"] = entry_id
    entry["history_id"] = entry_id
    entry["login_id"] = entry_id
    entry["attempt_id"] = entry_id
    entry["username"] = username
    entry["user"] = username
    entry["user_name"] = username
    entry["user_id"] = user_id
    entry["userId"] = user_id
    entry["uid"] = user_id
    entry["success"] = bool(success)
    entry["successful"] = bool(success)
    entry["ok"] = bool(success)
    entry["status"] = status
    entry["state"] = status
    entry["result"] = status
    entry["outcome"] = status
    entry["ip"] = ip
    entry["ip_address"] = ip
    entry["client_ip"] = ip
    entry["user_agent"] = user_agent
    entry["userAgent"] = user_agent
    entry["ua"] = user_agent
    entry["device"] = device
    entry["device_name"] = device
    entry["timestamp"] = ts
    entry["ts"] = ts
    entry["created_at"] = ts
    entry["logged_at"] = ts
    entry["occurred_at"] = ts
    entry["reason"] = reason
    entry["failure_reason"] = reason
    entry["message"] = reason
    entry["method"] = method
    entry["auth_method"] = method
    entry["session_id"] = session_id
    entry["sessionId"] = session_id
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


def _apply_filters(
    entries: List[Dict[str, Any]],
    limit: Any = None,
    skip: Any = None,
    offset: Any = None,
    since: Any = None,
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
    since_val = filters.pop("since", since)
    if since_val is None:
        for alias in ("after", "from", "from_time", "start", "begin", "min", "lower"):
            if filters.get(alias) is not None:
                since_val = filters.pop(alias)
                break
    for k in ("format", "fmt", "output_format", "include_jobs", "includeJobs",
              "merge_jobs", "format_filter", "format_"):
        # Serialization hints, not filters.
        if k in filters and str(filters.get(k)).lower() in ("json", "csv", ""):
            filters.pop(k, None)

    alias_map = {
        "user": ("username",),
        "user_name": ("username",),
        "name": ("username",),
        "account": ("username",),
        "userId": ("user_id",),
        "uid": ("user_id",),
        "successful": ("success",),
        "ok": ("success",),
        "state": ("status",),
        "result": ("status",),
        "outcome": ("status",),
        "ip_address": ("ip",),
        "client_ip": ("ip",),
        "remote_addr": ("ip",),
        "userAgent": ("user_agent",),
        "ua": ("user_agent",),
        "device_name": ("device",),
        "sessionId": ("session_id",),
        "sid": ("session_id",),
        "auth_method": ("method",),
    }
    out: List[Dict[str, Any]] = []
    for ev in entries:
        ok = True
        for key, expected in (filters or {}).items():
            if expected is None:
                continue
            if key in ("success", "successful", "ok"):
                want = _coerce_bool(expected)
                if want is None:
                    continue
                if bool(ev.get("success", ev.get("successful"))) is not want:
                    ok = False
                    break
                continue
            ev_val = ev.get(key)
            if ev_val is None:
                for alt in alias_map.get(key, ()):
                    if ev.get(alt) is not None:
                        ev_val = ev.get(alt)
                        break
            if ev_val is None:
                if key in alias_map or key in (
                    "username", "user", "user_name", "user_id", "userId", "uid",
                    "success", "successful", "status", "state", "result", "outcome",
                    "ip", "ip_address", "client_ip", "user_agent", "device",
                    "method", "session_id", "id", "entry_id", "login_id",
                ):
                    ok = False
                    break
                continue
            if str(ev_val) != str(expected):
                ok = False
                break
        if not ok:
            continue
        if since_val is not None:
            ev_ts = ev.get("timestamp") or ev.get("ts") or ev.get("created_at")
            if ev_ts is not None and str(ev_ts) < str(since_val):
                continue
        out.append(ev)
    # Newest first: entries are appended chronologically; reverse for listing
    # so recent logins surface first. Stable for equal timestamps.
    try:
        out = list(reversed(out))
    except Exception:
        pass
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


class LoginHistoryService:
    """In-memory login-attempt history store."""

    def __init__(self) -> None:
        self.store = LOGIN_HISTORY
        self.history = LOGIN_HISTORY
        self.entries = LOGIN_HISTORY
        self._lock = _jobs_lock

    # -- record --
    def record_login(self, *args: Any, **kwargs: Any) -> LoginHistoryEntry:
        data = _normalize_entry_data(*args, **kwargs)
        entry = _build_entry(data)
        with self._lock:
            LOGIN_HISTORY[entry["id"]] = entry
        return entry

    def log_login(self, *args: Any, **kwargs: Any) -> LoginHistoryEntry:
        return self.record_login(*args, **kwargs)

    def add_entry(self, *args: Any, **kwargs: Any) -> LoginHistoryEntry:
        return self.record_login(*args, **kwargs)

    def create_entry(self, *args: Any, **kwargs: Any) -> LoginHistoryEntry:
        return self.record_login(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> LoginHistoryEntry:
        return self.record_login(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> LoginHistoryEntry:
        return self.record_login(*args, **kwargs)

    def log(self, *args: Any, **kwargs: Any) -> LoginHistoryEntry:
        return self.record_login(*args, **kwargs)

    def append(self, *args: Any, **kwargs: Any) -> LoginHistoryEntry:
        return self.record_login(*args, **kwargs)

    def record(self, *args: Any, **kwargs: Any) -> LoginHistoryEntry:
        return self.record_login(*args, **kwargs)

    def record_attempt(self, *args: Any, **kwargs: Any) -> LoginHistoryEntry:
        return self.record_login(*args, **kwargs)

    def log_attempt(self, *args: Any, **kwargs: Any) -> LoginHistoryEntry:
        return self.record_login(*args, **kwargs)

    # -- get --
    def get_entry(self, entry_id: Any) -> Optional[LoginHistoryEntry]:
        if entry_id is None:
            return None
        key = str(entry_id)
        with self._lock:
            found = LOGIN_HISTORY.get(key)
            if found is not None:
                return found
            for e in LOGIN_HISTORY.values():
                if str(e.get("login_id")) == key or str(e.get("entry_id")) == key:
                    return e
        return None

    def get(self, entry_id: Any) -> Optional[LoginHistoryEntry]:
        return self.get_entry(entry_id)

    def fetch(self, entry_id: Any) -> Optional[LoginHistoryEntry]:
        return self.get_entry(entry_id)

    def fetch_entry(self, entry_id: Any) -> Optional[LoginHistoryEntry]:
        return self.get_entry(entry_id)

    # -- list --
    def list_history(
        self,
        username: Any = None,
        user_id: Any = None,
        success: Any = None,
        status: Any = None,
        ip: Any = None,
        limit: Any = None,
        offset: Any = None,
        skip: Any = None,
        since: Any = None,
        **filters: Any,
    ) -> List[LoginHistoryEntry]:
        if isinstance(username, dict):
            filters.update(username)
            username = None
        if username is None:
            username = filters.pop("user", filters.pop("user_name", filters.pop("account", None)))
        if user_id is None:
            user_id = filters.pop("userId", filters.pop("uid", None))
        if success is None:
            success = filters.pop("successful", filters.pop("ok", None))
        if status is None:
            status = filters.pop("state", filters.pop("result", filters.pop("outcome", None)))
        if ip is None:
            ip = filters.pop("ip_address", filters.pop("client_ip", filters.pop("remote_addr", None)))
        if since is None:
            since = filters.pop("after", filters.pop("from", None))
        with self._lock:
            items = [_entry_to_dict(e) for e in list(LOGIN_HISTORY.values())]
        query: Dict[str, Any] = {}
        if username is not None:
            query["username"] = username
        if user_id is not None:
            query["user_id"] = user_id
        if success is not None:
            query["success"] = success
        if status is not None:
            query["status"] = status
        if ip is not None:
            query["ip"] = ip
        query.update({k: v for k, v in filters.items() if v is not None})
        return _apply_filters(  # type: ignore[return-value]
            items, limit=limit, skip=skip if skip is not None else offset,
            since=since, **query,
        )

    def list(self, *args: Any, **kwargs: Any) -> List[LoginHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_history(self, *args: Any, **kwargs: Any) -> List[LoginHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_history(self, *args: Any, **kwargs: Any) -> List[LoginHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_entries(self, *args: Any, **kwargs: Any) -> List[LoginHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_entries(self, *args: Any, **kwargs: Any) -> List[LoginHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def all(self, *args: Any, **kwargs: Any) -> List[LoginHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def list_logins(self, *args: Any, **kwargs: Any) -> List[LoginHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_logins(self, *args: Any, **kwargs: Any) -> List[LoginHistoryEntry]:
        return self.list_history(*args, **kwargs)

    # -- delete / clear --
    def delete_entry(self, entry_id: Any) -> bool:
        if entry_id is None:
            return False
        key = str(entry_id)
        with self._lock:
            if LOGIN_HISTORY.pop(key, None) is not None:
                return True
            for k, e in list(LOGIN_HISTORY.items()):
                if str(e.get("login_id")) == key or str(e.get("entry_id")) == key:
                    LOGIN_HISTORY.pop(k, None)
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
                n = len(LOGIN_HISTORY)
                LOGIN_HISTORY.clear()
                return n
        targets = self.list_history(**filters)
        n = 0
        with self._lock:
            for t in targets:
                if LOGIN_HISTORY.pop(str(t.get("id")), None) is not None:
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

    def __len__(self) -> int:
        with self._lock:
            return len(LOGIN_HISTORY)

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            total = len(LOGIN_HISTORY)
            succeeded = sum(1 for r in LOGIN_HISTORY.values() if r.get("success"))
            failed = total - succeeded
        return {
            "ok": True,
            "service": "login_history",
            "status": "idle" if total == 0 else "active",
            "count": total,
            "total": total,
            "size": total,
            "succeeded": succeeded,
            "failed": failed,
        }

    def status(self) -> Dict[str, Any]:
        return self.get_status()

    def health(self) -> Dict[str, Any]:
        return self.get_status()


login_history_service = LoginHistoryService()
history_service = login_history_service
service = login_history_service
manager = login_history_service
login_manager = login_history_service
history_manager = login_history_service
store = login_history_service
default_service = login_history_service


def record_login(*args: Any, **kwargs: Any) -> LoginHistoryEntry:
    return login_history_service.record_login(*args, **kwargs)


def log_login(*args: Any, **kwargs: Any) -> LoginHistoryEntry:
    return login_history_service.log_login(*args, **kwargs)


def add_entry(*args: Any, **kwargs: Any) -> LoginHistoryEntry:
    return login_history_service.add_entry(*args, **kwargs)


def create_entry(*args: Any, **kwargs: Any) -> LoginHistoryEntry:
    return login_history_service.create_entry(*args, **kwargs)


def get_entry(entry_id: Any) -> Optional[LoginHistoryEntry]:
    return login_history_service.get_entry(entry_id)


def get_login_history(*args: Any, **kwargs: Any) -> List[LoginHistoryEntry]:
    return login_history_service.list_history(*args, **kwargs)


def list_history(*args: Any, **kwargs: Any) -> List[LoginHistoryEntry]:
    return login_history_service.list_history(*args, **kwargs)


def fetch_history(*args: Any, **kwargs: Any) -> List[LoginHistoryEntry]:
    return login_history_service.list_history(*args, **kwargs)


def list_entries(*args: Any, **kwargs: Any) -> List[LoginHistoryEntry]:
    return login_history_service.list_history(*args, **kwargs)


def fetch_entries(*args: Any, **kwargs: Any) -> List[LoginHistoryEntry]:
    return login_history_service.list_history(*args, **kwargs)


def delete_entry(entry_id: Any) -> bool:
    return login_history_service.delete_entry(entry_id)


def clear_history(**filters: Any) -> int:
    return login_history_service.clear_history(**filters)


def clear(**filters: Any) -> int:
    return login_history_service.clear_history(**filters)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return login_history_service.get_status(*args, **kwargs)


# CSV / JSON helpers for history dumps.
DEFAULT_COLUMNS = [
    "id",
    "username",
    "user_id",
    "success",
    "status",
    "ip",
    "user_agent",
    "method",
    "reason",
    "session_id",
    "timestamp",
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
record = record_login
log = log_login
add = add_entry
create = create_entry
get = get_entry
fetch = get_entry
list_all = list_history
all_entries = list_history
get_entries = list_entries
remove = delete_entry
delete = delete_entry
reset = clear
purge = clear
history_entries = list_history
login_history_entries = list_history
list_logins = list_history
get_logins = list_history
record_attempt = record_login
log_attempt = log_login
build_csv = to_csv
export_csv = to_csv
build_json = to_json
export_json = to_json

LoginHistoryServiceClass = LoginHistoryService
LoginHistoryManager = LoginHistoryService
HistoryService = LoginHistoryService
HistoryManager = LoginHistoryService
LoginService = LoginHistoryService
LoginManager = LoginHistoryService

__all__ = [
    "LOGIN_HISTORY",
    "LoginHistoryEntry",
    "LoginHistoryService",
    "login_history_service",
    "record_login",
    "log_login",
    "add_entry",
    "create_entry",
    "get_entry",
    "list_history",
    "fetch_history",
    "delete_entry",
    "clear_history",
    "get_status",
    "to_csv",
    "to_json",
    "export_entries",
]
