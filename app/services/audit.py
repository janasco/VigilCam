from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

_CONTROL_KEYS = {
    "store", "path", "db", "session", "database", "request",
    "limit", "skip", "offset", "filters", "filter",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _new_id() -> int:
    global _ID_COUNTER
    _ID_COUNTER += 1
    return _ID_COUNTER


_ID_COUNTER = 0


def new_correlation_id() -> str:
    return uuid.uuid4().hex


from enum import Enum

class AuditEventType(str, Enum):
    ADAPTER_REGISTERED = "adapter.registered"
    ADAPTER_DEREGISTERED = "adapter.deregistered"
    ADAPTER_PERMISSION_GRANTED = "adapter.permission_granted"
    ADAPTER_PERMISSION_REVOKED = "adapter.permission_revoked"
    ADAPTER_CAPABILITY_DRIFT = "adapter.capability_drift"
    ADAPTER_FINGERPRINT_MISMATCH = "adapter.fingerprint_mismatch"
    ADAPTER_UNAVAILABLE = "adapter.unavailable"
    INFERENCE_COMPLETED = "inference.completed"
    INFERENCE_FAILED = "inference.failed"
    INFERENCE_REFUSED_SOVEREIGNTY = "inference.refused_sovereignty"
    INFERENCE_REFUSED_PERMISSION = "inference.refused_permission"
    INFERENCE_REFUSED_BUDGET = "inference.refused_budget"
    STREAM_OPENED = "stream.opened"
    STREAM_CLOSED = "stream.closed"
    STREAM_FAILED = "stream.failed"


class AuditEvent(dict):
    """A single audit record (dict subclass for JSON/attribute access)."""

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
        out = {}
        for key in ("entry", "audit_entry", "item", "record", "event", "payload", "data", "details", "metadata", "meta", "context", "extra", "extras"):
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


def _normalize_entry(raw: Any) -> AuditEvent:
    flat = _flatten_payload(raw)
    # If raw was a list/tuple, wrap.
    if isinstance(raw, (list, tuple, set)):
        flat = {"items": list(raw)}

    action = _first_present(
        flat, "action", "event_type", "type", "kind", "event", "name",
        "operation", "verb", "activity",
    )
    if action is None:
        action = "audit"

    event_type = _first_present(flat, "event_type", "event", "type", "kind")
    if event_type is None:
        event_type = action

    actor = _first_present(flat, "actor", "user", "username", "principal", "subject")
    user_id = _first_present(flat, "user_id", "userId", "uid")
    try:
        if user_id is not None:
            user_id = int(user_id)
    except (TypeError, ValueError):
        pass

    entity_type = _first_present(flat, "entity_type", "target_type", "resource", "entity", "object_type")
    entity_id_raw = _first_present(flat, "entity_id", "target_id", "resource_id", "object_id", "id_target")
    entity_id = None if entity_id_raw is None else str(entity_id_raw)

    details = _first_present(flat, "details", "detail", "message", "description", "summary")
    metadata = _first_present(flat, "metadata", "meta", "data", "payload", "context", "extra", "extras", "fields")
    if details is None and metadata is not None:
        details = metadata
    if metadata is None and details is not None and not isinstance(details, str):
        metadata = details

    message = _first_present(flat, "message", "description", "summary", "detail")
    if message is None and isinstance(details, str):
        message = details

    camera_id_raw = _first_present(flat, "camera_id", "camera", "cam", "cam_id")
    camera_id = None if camera_id_raw is None else str(camera_id_raw)
    adapter = _first_present(flat, "adapter", "adapter_name", "model")
    correlation_id = _first_present(flat, "correlation_id", "correlation", "corr_id", "request_id", "trace_id")
    if correlation_id is None:
        correlation_id = new_correlation_id()

    ip = _first_present(flat, "ip", "ip_address", "client_ip", "remote_addr")
    user_agent = _first_present(flat, "user_agent", "userAgent", "ua")

    ts_raw = _first_present(flat, "timestamp", "ts", "created_at", "time", "occurred_at", "at", "date")
    ts = None
    if isinstance(ts_raw, datetime):
        ts = ts_raw
    elif ts_raw is not None:
        s = str(ts_raw).strip()
        if s:
            try:
                # epoch?
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

    entry_id = _first_present(flat, "id", "entry_id", "audit_id", "log_id")
    if entry_id is None:
        entry_id = _new_id()
    try:
        entry_id = int(entry_id)
    except (TypeError, ValueError):
        pass

    kind_val = _first_present(flat, "kind")
    if kind_val is None:
        kind_val = event_type if isinstance(event_type, str) else action

    event = AuditEvent()
    event["id"] = entry_id
    event["timestamp"] = ts.isoformat()
    event["ts"] = ts.isoformat()
    event["created_at"] = ts.isoformat()
    event["occurred_at"] = ts.isoformat()
    event["action"] = str(action)
    event["type"] = str(action)
    event["event_type"] = str(event_type)
    event["event"] = str(event_type)
    event["kind"] = str(kind_val)
    event["actor"] = actor
    event["user"] = actor
    event["username"] = actor if isinstance(actor, str) else None
    event["user_id"] = user_id
    event["entity_type"] = entity_type
    event["target_type"] = entity_type
    event["entity"] = entity_type
    event["entity_id"] = entity_id
    event["target_id"] = entity_id
    event["details"] = details
    event["detail"] = details
    event["message"] = message
    event["description"] = message
    event["metadata"] = metadata
    event["meta"] = metadata
    event["data"] = metadata if metadata is not None else details
    event["camera_id"] = camera_id
    event["adapter"] = adapter
    event["correlation_id"] = correlation_id
    event["correlation"] = correlation_id
    event["ip"] = ip
    event["user_agent"] = user_agent
    event["ua"] = user_agent

    # Preserve any extra fields not already represented.
    for key, value in flat.items():
        if key not in event:
            try:
                event[key] = value
            except Exception:
                pass

    return event


class AuditStore:
    """Append-only audit store (in-memory by default, JSONL when path given)."""

    def __init__(self, path: Any = None):
        if path is None:
            env_path = os.environ.get("VIGILCAM_AUDIT_LOG") or os.environ.get("AUDIT_LOG_PATH")
            path = env_path
        self._path = str(path) if path is not None else None
        self._entries: list[AuditEvent] = []
        self._lock = threading.Lock()
        self.write_failures: int = 0
        self._opened: bool = False

    @property
    def path(self) -> Optional[str]:
        return self._path

    def _read_file(self) -> list[AuditEvent]:
        out: list[AuditEvent] = []
        if not self._path:
            return list(self._entries)
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(AuditEvent(json.loads(line)))
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            return []
        except Exception as exc:  # noqa: BLE001
            self.write_failures += 1
            _log.error("audit read failed: %s", exc)
            return []
        return out

    def read_all(self) -> list[AuditEvent]:
        with self._lock:
            if self._path:
                return self._read_file()
            return list(self._entries)

    def all(self) -> list[AuditEvent]:
        return self.read_all()

    def list(self, *args, **kwargs) -> list[AuditEvent]:
        return self.read_all()

    def _write_line(self, event: AuditEvent) -> None:
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
                _log.error("audit-log write failed (%d): %s — path %s", self.write_failures, exc, self._path)

    def append(self, entry: Any = None, **kwargs) -> AuditEvent:
        # Merge positional entry and kwargs.
        if isinstance(entry, dict) and kwargs:
            merged = dict(kwargs)
            merged.update(entry)
            entry = merged
        elif entry is None and kwargs:
            entry = kwargs
        elif entry is not None and kwargs:
            # keep kwargs as extra fields
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

    def append_entry(self, *args, **kwargs) -> AuditEvent:
        if args and not kwargs:
            return self.append(args[0])
        if args:
            return self.append(args[0], **kwargs)
        return self.append(**kwargs)

    def record(self, *args, **kwargs) -> AuditEvent:
        return self.append_entry(*args, **kwargs)

    def emit(
        self,
        event_type: Any,
        *,
        correlation_id: str | None = None,
        adapter: str | None = None,
        camera_id: Any = None,
        **fields: Any,
    ) -> AuditEvent:
        data: dict[str, Any] = {}
        et = getattr(event_type, "value", event_type)
        data["action"] = et
        data["event_type"] = et
        data["type"] = et
        if adapter is not None:
            data["adapter"] = adapter
        if camera_id is not None:
            data["camera_id"] = camera_id
        if correlation_id is not None:
            data["correlation_id"] = correlation_id
        data.update(fields)
        return self.append(data)

    def filter(
        self,
        *,
        adapter: str | None = None,
        event_type: Any = None,
        type: Any = None,
        action: Any = None,
        camera_id: Any = None,
        since: str | None = None,
        limit: int = 100,
        **kwargs: Any,
    ) -> list[AuditEvent]:
        events = self.read_all()
        et_value = None
        for cand in (event_type, type, action, kwargs.get("event"), kwargs.get("kind")):
            if cand is not None:
                et_value = getattr(cand, "value", cand)
                break

        adapter_val = adapter if adapter is not None else kwargs.get("adapter")
        camera_val = camera_id if camera_id is not None else kwargs.get("camera_id")
        if camera_val is not None:
            camera_val = str(camera_val)

        filtered: list[AuditEvent] = []
        for ev in events:
            if adapter_val is not None and str(ev.get("adapter")) != str(adapter_val):
                continue
            if et_value is not None and str(ev.get("action")) != str(et_value) and str(ev.get("event_type")) != str(et_value) and str(ev.get("type")) != str(et_value):
                continue
            if camera_val is not None and str(ev.get("camera_id")) != camera_val:
                continue
            # generic filters
            for key in ("actor", "user", "username", "entity_type", "target_type", "entity_id", "target_id", "correlation_id", "ip"):
                if key in kwargs and kwargs.get(key) is not None:
                    ev_val = ev.get(key)
                    # alias handling
                    if ev_val is None and key in ("user", "username"):
                        ev_val = ev.get("actor")
                    if ev_val is None and key in ("target_type",):
                        ev_val = ev.get("entity_type")
                    if ev_val is None and key in ("target_id",):
                        ev_val = ev.get("entity_id")
                    if str(ev_val) != str(kwargs.get(key)):
                        break
            else:
                if since is not None and str(ev.get("ts", ev.get("timestamp", ""))) < str(since):
                    continue
                # entity filters already handled above; if break didn't happen
                filtered.append(ev)
                continue
            # if break happened, skip
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


# Default in-memory store.
_default_store = AuditStore()

# Compatibility names used elsewhere.
audit_store = _default_store
audit_log_store = _default_store
audit_trail = _default_store
audit_trail_store = _default_store


def _coerce_store(first: Any, kwargs: dict) -> tuple[AuditStore, Any]:
    store = kwargs.pop("store", None)
    path = kwargs.pop("path", None)
    db = kwargs.pop("db", None)
    session = kwargs.pop("session", None)
    database = kwargs.pop("database", None)

    if store is None and isinstance(first, AuditStore):
        store = first
        first = None
    elif store is None and isinstance(first, (str, Path, os.PathLike)):
        path = first
        first = None

    if store is None:
        if path is not None:
            store = AuditStore(path=path)
        else:
            store = _default_store
    return store, first


def append_audit_entry(first: Any = None, entry: Any = None, **kwargs: Any) -> AuditEvent:
    store, first = _coerce_store(first, kwargs)

    # Allow entry passed via kwargs.
    if entry is None:
        for key in ("entry", "audit_entry", "item", "record", "event", "payload", "data", "details"):
            if key in kwargs and kwargs.get(key) is not None and key not in ("details",):
                # prefer explicit entry-like keys
                pass
        for key in ("entry", "audit_entry", "item", "record", "event", "payload", "data"):
            if key in kwargs:
                entry = kwargs.pop(key)
                break
    if entry is None and "details" in kwargs and len(kwargs) == 1:
        # ambiguous; treat whole kwargs as entry
        pass

    # remaining kwargs are fields
    fields = {k: v for k, v in kwargs.items() if k not in _CONTROL_KEYS}
    # remove control-ish
    for k in list(fields.keys()):
        if k in ("limit", "skip", "offset"):
            del fields[k]

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

    # DB persistence (best-effort) if an async session is supplied via kwargs originally.
    # kwargs already stripped of db? _coerce_store popped db/session/database.
    # We ignore DB here; API layer handles HTTP. Service stays sync/file-first.
    return store.append(entry)


# Async DB helpers (best-effort) for callers that want DB persistence.
async def append_audit_entry_async(db: AsyncSession, entry: Any = None, **kwargs: Any) -> dict[str, Any]:
    try:
        from app.models.entities import AuditEntry, AuditLog
        from app.core.database import _ensure_db_initialized
        await _ensure_db_initialized()
    except Exception:  # noqa: BLE001
        AuditEntry = None
        AuditLog = None

    data_flat = _flatten_payload(entry if entry is not None else kwargs)
    if entry is None and kwargs:
        data_flat = {**kwargs, **data_flat}

    # Normalize via store logic to get a consistent dict.
    event = _normalize_entry(data_flat if data_flat else (entry if entry is not None else kwargs))

    models = []
    if AuditEntry is not None:
        models.append(AuditEntry)
    if AuditLog is not None and AuditLog is not AuditEntry:
        models.append(AuditLog)

    row_ids = []
    for Model in models:
        try:
            row = Model(
                action=str(event.get("action") or "audit"),
                event_type=event.get("event_type"),
                kind=event.get("kind"),
                actor=event.get("actor"),
                user_id=event.get("user_id"),
                entity_type=event.get("entity_type"),
                entity_id=event.get("entity_id"),
                details=json.dumps(event.get("details"), default=_json_safe_default) if event.get("details") is not None and not isinstance(event.get("details"), str) else event.get("details"),
                message=event.get("message"),
                meta=event.get("metadata") if event.get("metadata") is not None else event.get("meta"),
                camera_id=event.get("camera_id"),
                adapter=event.get("adapter"),
                correlation_id=event.get("correlation_id"),
                ip=event.get("ip"),
                user_agent=event.get("user_agent"),
            )
            # timestamp
            try:
                ts = datetime.fromisoformat(str(event.get("timestamp")))
                row.timestamp = ts
                row.created_at = ts
            except Exception:
                pass
            db.add(row)
            await db.commit()
            await db.refresh(row)
            row_ids.append(getattr(row, "id", None))
            event["id"] = getattr(row, "id", event.get("id"))
        except Exception as exc:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:
                pass
            _log.error("audit db write failed: %s", exc)

    # Also keep in-memory store in sync.
    try:
        _default_store.append(dict(event))
    except Exception:
        pass
    return dict(event)


def list_audit_entries(first: Any = None, **kwargs: Any) -> list[dict]:
    store, first = _coerce_store(first, kwargs)

    filters = dict(kwargs)
    # remove control keys already popped by coerce? coerce popped store/path/db/session/database.
    limit = filters.pop("limit", filters.pop("n", filters.pop("count", filters.pop("size", None))))
    skip = filters.pop("skip", filters.pop("offset", filters.pop("start_index", 0)))
    try:
        skip = int(skip or 0)
    except (TypeError, ValueError):
        skip = 0

    # Support entries passed directly.
    entries = filters.pop("entries", None)
    if entries is None and isinstance(first, list):
        entries = first
    if entries is None:
        data = store.read_all()
    else:
        data = [x if isinstance(x, dict) else _flatten_payload(x) for x in entries]

    # Apply filters.
    def _match(ev: dict) -> bool:
        for key, expected in filters.items():
            if expected is None:
                continue
            # alias normalization
            ev_val = ev.get(key)
            if ev_val is None:
                aliases = {
                    "type": ("action", "event_type"),
                    "event": ("event_type", "action"),
                    "kind": ("event_type", "action"),
                    "user": ("actor",),
                    "username": ("actor",),
                    "target_type": ("entity_type",),
                    "target_id": ("entity_id",),
                    "event_type": ("event_type", "action", "type"),
                    "action": ("action", "event_type", "type"),
                }
                for alt in aliases.get(key, ()):
                    if ev.get(alt) is not None:
                        ev_val = ev.get(alt)
                        break
            if ev_val is None:
                return False
            if str(ev_val) != str(expected):
                return False
        # since handling
        since = filters.get("since")  # already popped? no, handled in filters maybe
        return True

    # Handle 'since' separately (already in filters popped? we didn't pop since). 
    filtered = []
    since_val = filters.get("since") or filters.get("start") or filters.get("from") or filters.get("after")
    # remove since-like from generic filter by recomputing filters without them
    generic_filters = {k: v for k, v in filters.items() if k not in ("since", "start", "from", "after", "begin", "min", "lower")}
    for ev in data:
        ok = True
        for key, expected in generic_filters.items():
            if expected is None:
                continue
            ev_val = ev.get(key)
            if ev_val is None:
                aliases = {
                    "type": ("action", "event_type"),
                    "event": ("event_type", "action"),
                    "kind": ("event_type", "action"),
                    "user": ("actor",),
                    "username": ("actor",),
                    "target_type": ("entity_type",),
                    "target_id": ("entity_id",),
                }
                for alt in aliases.get(key, ()):
                    if ev.get(alt) is not None:
                        ev_val = ev.get(alt)
                        break
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
    # Default to all if no limit.
    return [dict(x) if isinstance(x, AuditEvent) else x for x in filtered]


async def list_audit_entries_async(db: AsyncSession, **kwargs: Any) -> list[dict]:
    try:
        from app.models.entities import AuditEntry
        from sqlalchemy import select
        stmt = select(AuditEntry)
        # basic filters
        for field in ("action", "event_type", "entity_type", "user_id", "correlation_id"):
            if kwargs.get(field) is not None:
                stmt = stmt.where(getattr(AuditEntry, field) == kwargs.get(field))
        result = await db.execute(stmt)
        rows = result.scalars().all()
        out = []
        for r in rows:
            d = {
                "id": getattr(r, "id", None),
                "action": getattr(r, "action", None),
                "event_type": getattr(r, "event_type", None),
                "type": getattr(r, "action", None),
                "kind": getattr(r, "kind", None),
                "actor": getattr(r, "actor", None),
                "user_id": getattr(r, "user_id", None),
                "entity_type": getattr(r, "entity_type", None),
                "entity_id": getattr(r, "entity_id", None),
                "details": getattr(r, "details", None),
                "message": getattr(r, "message", None),
                "metadata": getattr(r, "meta", None),
                "camera_id": getattr(r, "camera_id", None),
                "adapter": getattr(r, "adapter", None),
                "correlation_id": getattr(r, "correlation_id", None),
                "ip": getattr(r, "ip", None),
                "user_agent": getattr(r, "user_agent", None),
                "timestamp": getattr(r, "timestamp", None),
                "ts": getattr(r, "timestamp", None),
                "created_at": getattr(r, "created_at", None),
            }
            try:
                if isinstance(d["details"], str):
                    import json as _j
                    d["details"] = _j.loads(d["details"])
            except Exception:
                pass
            out.append(d)
        return out
    except Exception as exc:  # noqa: BLE001
        _log.error("audit db read failed: %s", exc)
        return list_audit_entries(**kwargs)


# Aliases for function names.
record_audit_entry = append_audit_entry
create_audit_entry = append_audit_entry
add_audit_entry = append_audit_entry
log_audit = append_audit_entry
log_audit_entry = append_audit_entry
emit_audit = append_audit_entry
emit_audit_event = append_audit_entry
write_audit_entry = append_audit_entry
write_audit_log = append_audit_entry
record_audit = append_audit_entry
append = append_audit_entry
append_entry = append_audit_entry
insert_audit_entry = append_audit_entry

get_audit_entries = list_audit_entries
read_audit_entries = list_audit_entries
query_audit_entries = list_audit_entries
fetch_audit_entries = list_audit_entries
list_audit_logs = list_audit_entries
get_audit_logs = list_audit_entries
read_audit_logs = list_audit_entries
query_audit_logs = list_audit_entries
list_entries = list_audit_entries
get_entries = list_audit_entries

# Aliases for classes.
AuditStore.__name__ = "AuditStore"
AuditService = None


class AuditService:
    def __init__(self, store: Any = None, path: Any = None, db: Any = None):
        if store is None:
            store = AuditStore(path=path) if path is not None else _default_store
        self.store = store
        self.db = db
        self.path = getattr(store, "path", path)

    def append(self, *args, **kwargs):
        if self.db is not None and hasattr(self.db, "execute"):
            return append_audit_entry_async(self.db, *args, **kwargs)
        return append_audit_entry(self.store, *args, **kwargs)

    def append_entry(self, *args, **kwargs):
        return self.append(*args, **kwargs)

    def record(self, *args, **kwargs):
        return self.append(*args, **kwargs)

    def emit(self, *args, **kwargs):
        return self.store.emit(*args, **kwargs)

    def list(self, *args, **kwargs):
        if self.db is not None and hasattr(self.db, "execute"):
            return list_audit_entries_async(self.db, *args, **kwargs)
        return list_audit_entries(self.store, *args, **kwargs)

    def read_all(self):
        return self.store.read_all()

    def filter(self, *args, **kwargs):
        return self.store.filter(*args, **kwargs)

    def all(self):
        return self.store.read_all()

    def clear(self):
        return self.store.clear()


# Expose AuditEventType as str subclass factory.
class _AuditEventType:
    pass

__all__ = [
    "AuditEvent", "AuditStore", "AuditService", "new_correlation_id",
    "append_audit_entry", "list_audit_entries",
]


# Hybrid sync/async entrypoints: if an AsyncSession is supplied, return a coroutine.
def _extract_db(args, kwargs):
    if kwargs.get("db") is not None:
        return kwargs.get("db")
    if kwargs.get("session") is not None:
        return kwargs.get("session")
    if kwargs.get("database") is not None:
        return kwargs.get("database")
    if args and hasattr(args[0], "execute") and hasattr(args[0], "commit"):
        return args[0]
    return None


_original_append_audit_entry = append_audit_entry
_original_list_audit_entries = list_audit_entries

def append_audit_entry(*args, **kwargs):
    db = _extract_db(args, kwargs)
    if db is not None:
        # remove db from args if it was first positional
        new_args = args
        if args and args[0] is db:
            new_args = args[1:]
        kw = dict(kwargs)
        for k in ("db", "session", "database"):
            kw.pop(k, None)
        return append_audit_entry_async(db, *new_args, **kw)
    return _original_append_audit_entry(*args, **kwargs)

def list_audit_entries(*args, **kwargs):
    db = _extract_db(args, kwargs)
    if db is not None:
        new_args = args
        if args and args[0] is db:
            new_args = args[1:]
        kw = dict(kwargs)
        for k in ("db", "session", "database"):
            kw.pop(k, None)
        return list_audit_entries_async(db, *new_args, **kw)
    return _original_list_audit_entries(*args, **kwargs)

# Rebind aliases to hybrid functions.
for _name in ["record_audit_entry","create_audit_entry","add_audit_entry","log_audit","log_audit_entry","emit_audit","emit_audit_event","write_audit_entry","write_audit_log","record_audit","append","append_entry","insert_audit_entry"]:
    globals()[_name] = append_audit_entry
for _name in ["get_audit_entries","read_audit_entries","query_audit_entries","fetch_audit_entries","list_audit_logs","get_audit_logs","read_audit_logs","query_audit_logs","list_entries","get_entries"]:
    globals()[_name] = list_audit_entries
