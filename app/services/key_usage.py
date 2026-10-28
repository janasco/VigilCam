"""API key usage log service.

Tracks per-request API key usage events in memory. This is distinct from
:mod:`app.services.api_keys`, which only maintains inline counters
(``usage_count`` / ``last_used_at``) updated on ``verify_api_key`` and
retains no per-request history (no endpoint, method, IP, user-agent or
timestamp log, and no queryable history endpoint).

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

KEY_USAGE: Dict[str, "KeyUsageEntry"] = {}
key_usage = KEY_USAGE
usage = KEY_USAGE
USAGE = KEY_USAGE
STORE = KEY_USAGE
ENTRIES = KEY_USAGE
KEY_USAGE_STORE = KEY_USAGE
HISTORY = KEY_USAGE
USAGE_HISTORY = KEY_USAGE
LOG = KEY_USAGE

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
    for nested_key in (
        "payload", "options", "config", "params", "data", "body",
        "entry", "usage", "record", "attributes", "request", "context",
    ):
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
    raw = _pick(
        data,
        "success", "successful", "succeeded", "ok", "is_success",
        "status", "state", "result", "outcome",
    )
    if raw is None:
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
    if s.startswith("success") or s.startswith("ok"):
        return True, "success"
    if s.startswith("fail") or s.startswith("error") or s.startswith("invalid"):
        return False, "failed"
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


def _try_resolve_key_name(key_id: Any) -> Optional[str]:
    if key_id is None:
        return None
    try:
        from app.services import api_keys as _api_keys

        record = _api_keys.get_api_key(key_id)
        if record is not None:
            name = record.get("name") or record.get("label") or record.get("key_name")
            if name is not None:
                return str(name)
    except Exception:  # noqa: BLE001
        pass
    return None


class KeyUsageEntry(dict):
    """Dict-like API key usage record with attribute access."""

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


KeyUsageRecord = KeyUsageEntry
UsageEntry = KeyUsageEntry
UsageRecord = KeyUsageEntry
KeyUsage = KeyUsageEntry
Usage = KeyUsageEntry
HistoryEntry = KeyUsageEntry


def _build_entry(data: Dict[str, Any]) -> KeyUsageEntry:
    entry_id = _pick(data, "id", "entry_id", "usage_id", "record_id",
                     "log_id", "history_id") or str(uuid.uuid4())
    entry_id = str(entry_id)

    key_id = _pick(data, "key_id", "keyId", "kid", "api_key_id",
                   "apiKeyId", "id", "keyID")
    # ``id`` doubles as key_id alias only when it does not look like the
    # entry id itself; prefer explicit key fields.
    explicit_key = _pick(data, "key_id", "keyId", "kid", "api_key_id",
                         "apiKeyId", "keyID")
    if explicit_key is not None:
        key_id = explicit_key
    if key_id is not None:
        key_id = str(key_id)

    key_name = _pick(data, "key_name", "keyName", "name", "label",
                     "key_label", "keyLabel")
    if key_name is not None:
        key_name = str(key_name)
    elif key_id is not None:
        key_name = _try_resolve_key_name(key_id)

    endpoint = _pick(data, "endpoint", "path", "route", "url", "resource",
                     "api", "api_path", "request_path", "requestPath")
    if endpoint is not None:
        endpoint = str(endpoint)

    method = _pick(data, "method", "http_method", "httpMethod", "verb")
    if method is not None:
        method = str(method).upper()

    ip = _pick(data, "ip", "ip_address", "ipAddress", "client_ip",
               "clientIp", "remote_addr", "remoteAddr", "remote_ip")
    if ip is not None:
        ip = str(ip)
    user_agent = _pick(data, "user_agent", "userAgent", "ua",
                       "user-agent", "agent")
    if user_agent is not None:
        user_agent = str(user_agent)

    success, status = _parse_success(data)
    status = str(status or ("success" if success else "failed"))

    ts = _pick(data, "timestamp", "ts", "created_at", "createdAt",
               "logged_at", "loggedAt", "occurred_at", "occurredAt",
               "time", "date", "at", "used_at", "usedAt")
    if ts is None:
        ts = _now_iso()
    else:
        if isinstance(ts, datetime):
            ts = ts.isoformat()
        else:
            ts = str(ts)

    scopes = _pick(data, "scopes", "scope", "permissions", "perms")
    detail = _pick(data, "detail", "details", "description", "message",
                   "reason", "note", "extra")

    entry = KeyUsageEntry()
    entry["id"] = entry_id
    entry["entry_id"] = entry_id
    entry["usage_id"] = entry_id
    entry["record_id"] = entry_id
    entry["log_id"] = entry_id
    entry["key_id"] = key_id
    entry["keyId"] = key_id
    entry["kid"] = key_id
    entry["api_key_id"] = key_id
    entry["key_name"] = key_name
    entry["name"] = key_name
    entry["label"] = key_name
    entry["endpoint"] = endpoint
    entry["path"] = endpoint
    entry["route"] = endpoint
    entry["url"] = endpoint
    entry["method"] = method
    entry["http_method"] = method
    entry["ip"] = ip
    entry["ip_address"] = ip
    entry["client_ip"] = ip
    entry["user_agent"] = user_agent
    entry["userAgent"] = user_agent
    entry["ua"] = user_agent
    entry["success"] = bool(success)
    entry["successful"] = bool(success)
    entry["ok"] = bool(success)
    entry["status"] = status
    entry["state"] = status
    entry["result"] = status
    entry["outcome"] = status
    entry["timestamp"] = ts
    entry["ts"] = ts
    entry["created_at"] = ts
    entry["logged_at"] = ts
    entry["occurred_at"] = ts
    entry["used_at"] = ts
    entry["scopes"] = scopes
    entry["detail"] = detail
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
        if k in filters and str(filters.get(k)).lower() in ("json", "csv", ""):
            filters.pop(k, None)

    alias_map = {
        "keyId": ("key_id",),
        "kid": ("key_id",),
        "api_key_id": ("key_id",),
        "key_name": ("key_name",),
        "keyName": ("key_name",),
        "name": ("key_name",),
        "label": ("key_name",),
        "path": ("endpoint",),
        "route": ("endpoint",),
        "url": ("endpoint",),
        "http_method": ("method",),
        "verb": ("method",),
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
                    "key_id", "keyId", "kid", "api_key_id", "key_name",
                    "endpoint", "path", "route", "url", "method",
                    "success", "successful", "status", "state", "result",
                    "ip", "user_agent", "id", "entry_id", "usage_id",
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


class KeyUsageService:
    """In-memory API key usage history store."""

    def __init__(self) -> None:
        self.store = KEY_USAGE
        self.history = KEY_USAGE
        self.entries = KEY_USAGE
        self._lock = _jobs_lock

    # -- record --
    def record_usage(self, *args: Any, **kwargs: Any) -> KeyUsageEntry:
        data = _normalize_entry_data(*args, **kwargs)
        entry = _build_entry(data)
        with self._lock:
            KEY_USAGE[entry["id"]] = entry
        return entry

    def log_usage(self, *args: Any, **kwargs: Any) -> KeyUsageEntry:
        return self.record_usage(*args, **kwargs)

    def add_entry(self, *args: Any, **kwargs: Any) -> KeyUsageEntry:
        return self.record_usage(*args, **kwargs)

    def create_entry(self, *args: Any, **kwargs: Any) -> KeyUsageEntry:
        return self.record_usage(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> KeyUsageEntry:
        return self.record_usage(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> KeyUsageEntry:
        return self.record_usage(*args, **kwargs)

    def log(self, *args: Any, **kwargs: Any) -> KeyUsageEntry:
        return self.record_usage(*args, **kwargs)

    def append(self, *args: Any, **kwargs: Any) -> KeyUsageEntry:
        return self.record_usage(*args, **kwargs)

    def record(self, *args: Any, **kwargs: Any) -> KeyUsageEntry:
        return self.record_usage(*args, **kwargs)

    # -- get --
    def get_entry(self, entry_id: Any) -> Optional[KeyUsageEntry]:
        if entry_id is None:
            return None
        key = str(entry_id)
        with self._lock:
            found = KEY_USAGE.get(key)
            if found is not None:
                return found
            for e in KEY_USAGE.values():
                if str(e.get("usage_id")) == key or str(e.get("entry_id")) == key:
                    return e
        return None

    def get(self, entry_id: Any) -> Optional[KeyUsageEntry]:
        return self.get_entry(entry_id)

    def fetch(self, entry_id: Any) -> Optional[KeyUsageEntry]:
        return self.get_entry(entry_id)

    def fetch_entry(self, entry_id: Any) -> Optional[KeyUsageEntry]:
        return self.get_entry(entry_id)

    # -- list --
    def list_usage(
        self,
        key_id: Any = None,
        endpoint: Any = None,
        method: Any = None,
        success: Any = None,
        status: Any = None,
        ip: Any = None,
        limit: Any = None,
        offset: Any = None,
        skip: Any = None,
        since: Any = None,
        **filters: Any,
    ) -> List[KeyUsageEntry]:
        if isinstance(key_id, dict):
            filters.update(key_id)
            key_id = None
        if key_id is None:
            key_id = filters.pop("keyId", filters.pop("kid", filters.pop("api_key_id", None)))
        if endpoint is None:
            endpoint = filters.pop("path", filters.pop("route", filters.pop("url", None)))
        if method is None:
            method = filters.pop("http_method", filters.pop("verb", None))
        if success is None:
            success = filters.pop("successful", filters.pop("ok", None))
        if status is None:
            status = filters.pop("state", filters.pop("result", filters.pop("outcome", None)))
        if ip is None:
            ip = filters.pop("ip_address", filters.pop("client_ip", filters.pop("remote_addr", None)))
        if since is None:
            since = filters.pop("after", filters.pop("from", None))
        with self._lock:
            items = [_entry_to_dict(e) for e in list(KEY_USAGE.values())]
        query: Dict[str, Any] = {}
        if key_id is not None:
            query["key_id"] = key_id
        if endpoint is not None:
            query["endpoint"] = endpoint
        if method is not None:
            # Normalize method case for filtering.
            try:
                query["method"] = str(method).upper()
            except Exception:
                query["method"] = method
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

    def list(self, *args: Any, **kwargs: Any) -> List[KeyUsageEntry]:
        return self.list_usage(*args, **kwargs)

    def get_usage(self, *args: Any, **kwargs: Any) -> List[KeyUsageEntry]:
        return self.list_usage(*args, **kwargs)

    def fetch_usage(self, *args: Any, **kwargs: Any) -> List[KeyUsageEntry]:
        return self.list_usage(*args, **kwargs)

    def get_entries(self, *args: Any, **kwargs: Any) -> List[KeyUsageEntry]:
        return self.list_usage(*args, **kwargs)

    def fetch_entries(self, *args: Any, **kwargs: Any) -> List[KeyUsageEntry]:
        return self.list_usage(*args, **kwargs)

    def all(self, *args: Any, **kwargs: Any) -> List[KeyUsageEntry]:
        return self.list_usage(*args, **kwargs)

    def list_history(self, *args: Any, **kwargs: Any) -> List[KeyUsageEntry]:
        return self.list_usage(*args, **kwargs)

    def get_history(self, *args: Any, **kwargs: Any) -> List[KeyUsageEntry]:
        return self.list_usage(*args, **kwargs)

    def list_for_key(self, key_id: Any, **kwargs: Any) -> List[KeyUsageEntry]:
        return self.list_usage(key_id=key_id, **kwargs)

    def get_for_key(self, key_id: Any, **kwargs: Any) -> List[KeyUsageEntry]:
        return self.list_usage(key_id=key_id, **kwargs)

    def get_summary(self, key_id: Any = None, **filters: Any) -> Dict[str, Any]:
        if key_id is None:
            key_id = filters.pop("keyId", filters.pop("kid", filters.pop("api_key_id", None)))
        with self._lock:
            items = list(KEY_USAGE.values())
        if key_id is not None:
            items = [e for e in items if str(e.get("key_id")) == str(key_id)]
        else:
            extra = {k: v for k, v in filters.items() if v is not None and k not in ("limit", "skip", "offset", "since")}
            if extra:
                dicts = [_entry_to_dict(e) for e in items]
                dicts = _apply_filters(dicts, **extra)
                ids = {str(d.get("id")) for d in dicts}
                items = [e for e in items if str(e.get("id")) in ids]
        total = len(items)
        succeeded = sum(1 for e in items if e.get("success"))
        failed = total - succeeded
        endpoints: Dict[str, int] = {}
        for e in items:
            ep = e.get("endpoint") or e.get("path") or "unknown"
            endpoints[str(ep)] = endpoints.get(str(ep), 0) + 1
        last_used = None
        try:
            stamps = sorted(str(e.get("timestamp") or "") for e in items if e.get("timestamp"))
            last_used = stamps[-1] if stamps else None
        except Exception:  # noqa: BLE001
            last_used = None
        return {
            "ok": True,
            "key_id": str(key_id) if key_id is not None else None,
            "total": total,
            "count": total,
            "size": total,
            "succeeded": succeeded,
            "failed": failed,
            "success": succeeded,
            "endpoints": endpoints,
            "last_used_at": last_used,
            "last_used": last_used,
        }

    def summary(self, key_id: Any = None, **filters: Any) -> Dict[str, Any]:
        return self.get_summary(key_id, **filters)

    def summary_for_key(self, key_id: Any, **filters: Any) -> Dict[str, Any]:
        return self.get_summary(key_id, **filters)

    # -- delete / clear --
    def delete_entry(self, entry_id: Any) -> bool:
        if entry_id is None:
            return False
        key = str(entry_id)
        with self._lock:
            if KEY_USAGE.pop(key, None) is not None:
                return True
            for k, e in list(KEY_USAGE.items()):
                if str(e.get("usage_id")) == key or str(e.get("entry_id")) == key:
                    KEY_USAGE.pop(k, None)
                    return True
        return False

    def delete(self, entry_id: Any) -> bool:
        return self.delete_entry(entry_id)

    def remove(self, entry_id: Any) -> bool:
        return self.delete_entry(entry_id)

    def remove_entry(self, entry_id: Any) -> bool:
        return self.delete_entry(entry_id)

    def clear_usage(self, **filters: Any) -> int:
        with self._lock:
            if not filters:
                n = len(KEY_USAGE)
                KEY_USAGE.clear()
                return n
        targets = self.list_usage(**filters)
        n = 0
        with self._lock:
            for t in targets:
                if KEY_USAGE.pop(str(t.get("id")), None) is not None:
                    n += 1
        return n

    def clear(self, **filters: Any) -> int:
        return self.clear_usage(**filters)

    def reset(self, **filters: Any) -> int:
        return self.clear_usage(**filters)

    def purge(self, **filters: Any) -> int:
        return self.clear_usage(**filters)

    def count(self, **filters: Any) -> int:
        return len(self.list_usage(**filters))

    def __len__(self) -> int:
        with self._lock:
            return len(KEY_USAGE)

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            total = len(KEY_USAGE)
            succeeded = sum(1 for r in KEY_USAGE.values() if r.get("success"))
            failed = total - succeeded
        return {
            "ok": True,
            "service": "key_usage",
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


key_usage_service = KeyUsageService()
usage_service = key_usage_service
service = key_usage_service
manager = key_usage_service
usage_manager = key_usage_service
store = key_usage_service
default_service = key_usage_service


def record_usage(*args: Any, **kwargs: Any) -> KeyUsageEntry:
    return key_usage_service.record_usage(*args, **kwargs)


def log_usage(*args: Any, **kwargs: Any) -> KeyUsageEntry:
    return key_usage_service.log_usage(*args, **kwargs)


def add_entry(*args: Any, **kwargs: Any) -> KeyUsageEntry:
    return key_usage_service.add_entry(*args, **kwargs)


def create_entry(*args: Any, **kwargs: Any) -> KeyUsageEntry:
    return key_usage_service.create_entry(*args, **kwargs)


def get_entry(entry_id: Any) -> Optional[KeyUsageEntry]:
    return key_usage_service.get_entry(entry_id)


def list_usage(*args: Any, **kwargs: Any) -> List[KeyUsageEntry]:
    return key_usage_service.list_usage(*args, **kwargs)


def fetch_usage(*args: Any, **kwargs: Any) -> List[KeyUsageEntry]:
    return key_usage_service.list_usage(*args, **kwargs)


def list_entries(*args: Any, **kwargs: Any) -> List[KeyUsageEntry]:
    return key_usage_service.list_usage(*args, **kwargs)


def fetch_entries(*args: Any, **kwargs: Any) -> List[KeyUsageEntry]:
    return key_usage_service.list_usage(*args, **kwargs)


def delete_entry(entry_id: Any) -> bool:
    return key_usage_service.delete_entry(entry_id)


def clear_usage(**filters: Any) -> int:
    return key_usage_service.clear_usage(**filters)


def clear(**filters: Any) -> int:
    return key_usage_service.clear_usage(**filters)


def get_summary(key_id: Any = None, **filters: Any) -> Dict[str, Any]:
    return key_usage_service.get_summary(key_id, **filters)


def summary_for_key(key_id: Any, **filters: Any) -> Dict[str, Any]:
    return key_usage_service.summary_for_key(key_id, **filters)


def list_for_key(key_id: Any, **kwargs: Any) -> List[KeyUsageEntry]:
    return key_usage_service.list_for_key(key_id, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return key_usage_service.get_status(*args, **kwargs)


DEFAULT_COLUMNS = [
    "id",
    "key_id",
    "key_name",
    "endpoint",
    "method",
    "success",
    "status",
    "ip",
    "user_agent",
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


record = record_usage
log = log_usage
add = add_entry
create = create_entry
get = get_entry
fetch = get_entry
list_all = list_usage
all_entries = list_usage
get_entries = list_entries
remove = delete_entry
delete = delete_entry
reset = clear
purge = clear
usage_entries = list_usage
key_usage_entries = list_usage
list_history = list_usage
get_history = list_usage
summary = get_summary
build_csv = to_csv
export_csv = to_csv
build_json = to_json
export_json = to_json

KeyUsageServiceClass = KeyUsageService
KeyUsageManager = KeyUsageService
UsageService = KeyUsageService
UsageManager = KeyUsageService

__all__ = [
    "KEY_USAGE",
    "KeyUsageEntry",
    "KeyUsageService",
    "key_usage_service",
    "record_usage",
    "log_usage",
    "add_entry",
    "create_entry",
    "get_entry",
    "list_usage",
    "fetch_usage",
    "delete_entry",
    "clear_usage",
    "get_summary",
    "get_status",
    "to_csv",
    "to_json",
    "export_entries",
]
