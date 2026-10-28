"""Camera service log service (in-memory, per-camera CRUD).

Structured per-camera service/maintenance history (cleaning, firmware,
repairs, inspections, ...). This is distinct from neighbouring stores:

* ``camera_notes`` is **freeform** operator annotations (``text/body`` only,
  no service_type / performed_at / performed_by structure).
* ``audit`` is a **system-wide** append-only event log, not a per-camera
  service history.
* ``maintenance`` manages **calendar suppression windows** (future
  ``[start, end]`` downtime during which alerts stay quiet), not a log of
  completed service work.

The store is intentionally in-memory (thread-safe) so the feature works
without DB migrations and without requiring ``app/main.py`` edits. An
optional ``db``/``session`` argument is accepted by the async entrypoints
for forward-compatibility but is currently ignored (best-effort no-op).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
_ENTRIES: Dict[int, Dict[str, Any]] = {}
_CAMERA_INDEX: Dict[str, List[int]] = {}
_ID_COUNTER = 0

SERVICE_TYPE_KEYS = (
    "service_type", "serviceType", "type", "kind", "category",
    "service", "work_type", "workType", "action",
)
SUMMARY_KEYS = (
    "summary", "title", "subject", "text", "body", "content",
    "message", "note", "comment", "description", "details",
    "detail", "value", "data",
)
DETAILS_KEYS = (
    "details", "detail", "description", "notes", "comment",
    "body", "text", "summary",
)
CAMERA_ID_KEYS = (
    "camera_id", "cameraId", "camera", "cameraid",
    "cam_id", "camId", "cam", "source_id", "sourceId",
    "id_camera", "for_camera",
)
PERFORMER_KEYS = (
    "performed_by", "performedBy", "performer", "technician",
    "author", "username", "user", "actor", "created_by",
    "createdBy", "by",
)
PERFORMED_AT_KEYS = (
    "performed_at", "performedAt", "performed_on", "service_date",
    "serviceDate", "date", "at", "timestamp", "ts", "time",
    "occurred_at", "done_at",
)
STATUS_KEYS = ("status", "state", "result", "outcome")
ID_KEYS = ("id", "entry_id", "entryId", "log_id", "logId")


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
        for nested_key in ("entry", "log", "record", "payload", "options",
                           "config", "params", "data", "body", "request",
                           "fields", "service"):
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    if isinstance(data, (str, bytes, bytearray)):
        s = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else str(data)
        return {"summary": s}
    try:
        return dict(data)
    except Exception:
        return {"summary": str(data)}


def _normalize_camera_key(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if not s:
        return None
    coerced = _coerce_int(value, None)
    if coerced is not None and str(coerced) == s.lstrip("0") or str(coerced) == s:
        return str(coerced)
    if coerced is not None:
        try:
            if float(str(value).strip()) == float(coerced):
                return str(coerced)
        except (TypeError, ValueError):
            pass
    return s


def _extract_camera_id(*args: Any, **kwargs: Any) -> Optional[str]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (int, float)) and not any(k in data for k in CAMERA_ID_KEYS):
            data["camera_id"] = a
        elif isinstance(a, str) and "camera_id" not in data:
            data.setdefault("camera_id", a)
    data.update(kwargs or {})
    for nested_key in ("entry", "log", "record", "payload", "params", "data", "body"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    raw = _first_present(data, CAMERA_ID_KEYS)
    if raw is None and len(args) == 1 and isinstance(args[0], (int, float, str)):
        if isinstance(args[0], (int, float)) or _coerce_int(args[0], None) is not None:
            raw = args[0]
        else:
            return None
    if raw is None:
        return None
    return _normalize_camera_key(raw)


def _extract_summary(data: Dict[str, Any]) -> Optional[str]:
    raw = _first_present(data, SUMMARY_KEYS)
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        return s if s else None
    s = str(raw).strip()
    return s if s else None


def _extract_performer(data: Dict[str, Any]) -> Optional[str]:
    raw = _first_present(data, PERFORMER_KEYS)
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s else None


def _extract_service_type(data: Dict[str, Any]) -> str:
    raw = _first_present(data, SERVICE_TYPE_KEYS)
    if raw is None:
        return "other"
    s = str(raw).strip()
    return s if s else "other"


def _extract_performed_at(data: Dict[str, Any]) -> str:
    raw = _first_present(data, PERFORMED_AT_KEYS)
    if raw is None:
        return _now_iso()
    if isinstance(raw, datetime):
        dt = raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    s = str(raw).strip()
    if not s:
        return _now_iso()
    # Accept epoch numbers as well.
    try:
        num = float(s)
        if s.replace(".", "", 1).replace("-", "", 1).replace("+", "", 1).replace("e", "", 1).replace("E", "", 1).isdigit():
            if abs(num) > 1e12:
                num = num / 1000.0
            return datetime.fromtimestamp(num, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    # Validate ISO-ish; fall back to raw string when unparseable so callers
    # keep their input rather than losing it.
    try:
        s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return s


def _to_public(entry: Dict[str, Any]) -> Dict[str, Any]:
    return dict(entry)


def create_entry(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create a service-log entry attached to a camera.

    Accepts flexible inputs, e.g.::

        create_entry(camera_id=1, service_type="cleaning", summary="lens cleaned")
        create_entry(1, "cleaning", "lens cleaned")
        create_entry({"camera_id": 1, "service_type": "firmware", "summary": "upgraded"})
    """
    flat: Dict[str, Any] = {}
    positionals: List[str] = []
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
        elif isinstance(a, (bytes, bytearray)):
            s = bytes(a).decode("utf-8", errors="replace")
            positionals.append(s)
        elif isinstance(a, (int, float)) and "camera_id" not in flat:
            flat["camera_id"] = a
        elif isinstance(a, str):
            positionals.append(a)
        else:
            try:
                flat.update(dict(a))
            except Exception:
                pass
    flat.update(kwargs or {})
    for nested_key in ("entry", "log", "record", "payload", "params", "data",
                       "body", "request", "service"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)

    camera_raw = _first_present(flat, CAMERA_ID_KEYS)
    camera_id: Optional[str] = None
    if camera_raw is not None:
        camera_id = _normalize_camera_key(camera_raw)
    if camera_id is None:
        camera_id = _extract_camera_id(flat)
    if camera_id is None and positionals:
        # First positional int-like is camera id when no camera yet.
        first = positionals[0]
        if _coerce_int(first, None) is not None:
            camera_id = _normalize_camera_key(first)
            positionals = positionals[1:]
    if camera_id is None:
        raise ValueError("camera_id is required")
    try:
        camera_id_int: Any = int(camera_id)
    except (TypeError, ValueError):
        camera_id_int = camera_id

    # Remaining positionals: [service_type, summary, ...].
    if _first_present(flat, SERVICE_TYPE_KEYS) is None and positionals:
        flat["service_type"] = positionals.pop(0)
    summary = _extract_summary(flat)
    if summary is None and positionals:
        summary = positionals.pop(0).strip() or None
    if summary is None:
        raise ValueError("summary/text/body is required")
    service_type = _extract_service_type(flat)
    performer = _extract_performer(flat)
    performed_at = _extract_performed_at(flat)
    status_raw = _first_present(flat, STATUS_KEYS)
    status = str(status_raw).strip() if status_raw is not None else "completed"
    if not status:
        status = "completed"
    details_raw = _first_present(flat, DETAILS_KEYS)
    details = str(details_raw).strip() if details_raw is not None else summary
    if not details:
        details = summary
    now = _now_iso()
    with _lock:
        nid = _next_id()
        entry: Dict[str, Any] = {
            "id": nid,
            "entry_id": nid,
            "log_id": nid,
            "camera_id": camera_id_int,
            "cameraId": camera_id_int,
            "camera": camera_id_int,
            "service_type": service_type,
            "serviceType": service_type,
            "type": service_type,
            "kind": service_type,
            "summary": summary,
            "title": summary,
            "text": summary,
            "details": details,
            "description": details,
            "performed_by": performer,
            "performedBy": performer,
            "technician": performer,
            "author": performer,
            "performed_at": performed_at,
            "performedAt": performed_at,
            "service_date": performed_at,
            "status": status,
            "state": status,
            "created_at": now,
            "updated_at": now,
        }
        for k, v in flat.items():
            if k not in entry:
                entry[k] = v
        _ENTRIES[nid] = entry
        _CAMERA_INDEX.setdefault(str(camera_id), []).append(nid)
        return _to_public(entry)


def list_entries(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """List entries, optionally filtered by camera_id / service_type."""
    flat = _flatten(args[0]) if len(args) == 1 and isinstance(args[0], dict) else {}
    for a in args:
        if isinstance(a, (int, float)) and "camera_id" not in flat:
            flat["camera_id"] = a
        elif isinstance(a, str) and "camera_id" not in flat and _coerce_int(a, None) is not None:
            flat["camera_id"] = a
    flat.update(kwargs or {})
    camera_raw = _first_present(flat, CAMERA_ID_KEYS)
    camera_key = _normalize_camera_key(camera_raw) if camera_raw is not None else None
    if camera_key is None and len(args) == 1 and isinstance(args[0], (int, float)):
        camera_key = _normalize_camera_key(args[0])
    service_type_raw = _first_present(flat, SERVICE_TYPE_KEYS)
    limit = _coerce_int(_first_present(flat, ("limit", "n", "count", "size", "page_size")), None)
    offset = _coerce_int(_first_present(flat, ("offset", "skip", "start_index")), 0) or 0
    query = _first_present(flat, ("q", "query", "search", "filter", "text_search"))
    with _lock:
        if camera_key is None:
            items = [_to_public(_ENTRIES[k]) for k in sorted(_ENTRIES.keys())]
        else:
            ids = list(_CAMERA_INDEX.get(str(camera_key), []))
            items = [_to_public(_ENTRIES[k]) for k in sorted(ids) if k in _ENTRIES]
    if service_type_raw is not None:
        want = str(service_type_raw).strip().lower()
        if want:
            items = [e for e in items if str(e.get("service_type", "")).lower() == want]
    if query is not None:
        qs = str(query).strip().lower()
        if qs:
            items = [e for e in items
                     if qs in str(e.get("summary", "")).lower()
                     or qs in str(e.get("details", "")).lower()
                     or qs in str(e.get("service_type", "")).lower()]
    if offset:
        items = items[offset:]
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items


def get_entry(entry_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if entry_id is None:
        entry_id = _first_present(flat, ID_KEYS)
    if entry_id is None and args:
        for a in args:
            if isinstance(a, (int, float, str)) and not isinstance(a, bool):
                try:
                    int(a)  # noqa: F841
                    entry_id = a
                    break
                except (TypeError, ValueError):
                    continue
    nid = _coerce_int(entry_id, None)
    if nid is None:
        return None
    with _lock:
        entry = _ENTRIES.get(int(nid))
        return _to_public(entry) if entry is not None else None


def update_entry(entry_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
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
            flat.setdefault("summary", s)
    flat.update(kwargs or {})
    if entry_id is None:
        entry_id = _first_present(flat, ID_KEYS)
    nid = _coerce_int(entry_id, None)
    if nid is None:
        return None
    with _lock:
        entry = _ENTRIES.get(int(nid))
        if entry is None:
            return None
        summary = _extract_summary(flat)
        if summary is not None:
            for k in ("summary", "title", "text", "details", "description"):
                entry[k] = summary if k != "details" and k != "description" else str(
                    _first_present(flat, DETAILS_KEYS) or summary)
        st_raw = _first_present(flat, SERVICE_TYPE_KEYS)
        if st_raw is not None and str(st_raw).strip():
            st = str(st_raw).strip()
            entry["service_type"] = st
            entry["serviceType"] = st
            entry["type"] = st
            entry["kind"] = st
        performer = _extract_performer(flat)
        if performer is not None:
            entry["performed_by"] = performer
            entry["performedBy"] = performer
            entry["technician"] = performer
            entry["author"] = performer
        if _first_present(flat, PERFORMED_AT_KEYS) is not None:
            pa = _extract_performed_at(flat)
            entry["performed_at"] = pa
            entry["performedAt"] = pa
            entry["service_date"] = pa
        status_raw = _first_present(flat, STATUS_KEYS)
        if status_raw is not None and str(status_raw).strip():
            entry["status"] = str(status_raw).strip()
            entry["state"] = str(status_raw).strip()
        for k, v in flat.items():
            if k.lower() in {str(x).lower() for x in ID_KEYS + CAMERA_ID_KEYS}:
                continue
            if k not in ("summary", "title", "subject", "text", "body", "content",
                         "message", "note", "comment", "description", "details",
                         "detail", "service_type", "serviceType", "type", "kind",
                         "category", "service", "performed_by", "performedBy",
                         "performer", "technician", "author", "username", "user",
                         "actor", "created_by", "performed_at", "performedAt",
                         "status", "state", "value", "data"):
                entry[k] = v
        entry["updated_at"] = _now_iso()
        return _to_public(entry)


def delete_entry(entry_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    if entry_id is None:
        entry_id = _first_present(flat, ID_KEYS)
    if entry_id is None and args:
        for a in args:
            if isinstance(a, (int, float)):
                entry_id = a
                break
    nid = _coerce_int(entry_id, None)
    if nid is None:
        return False
    with _lock:
        entry = _ENTRIES.pop(int(nid), None)
        if entry is None:
            return False
        cid = entry.get("camera_id")
        try:
            lst = _CAMERA_INDEX.get(str(cid), [])
            if int(nid) in lst:
                lst.remove(int(nid))
        except (TypeError, ValueError):
            pass
        return True


def delete_entries_for_camera(camera_id: Any = None, *args: Any, **kwargs: Any) -> int:
    cid = _extract_camera_id(*(([camera_id] if camera_id is not None else []) + list(args)), **kwargs)
    if cid is None:
        return 0
    with _lock:
        ids = list(_CAMERA_INDEX.get(str(cid), []))
    n = 0
    for nid in ids:
        if delete_entry(nid):
            n += 1
    return n


def count_entries(camera_id: Any = None, *args: Any, **kwargs: Any) -> int:
    return len(list_entries(*(([camera_id] if camera_id is not None else []) + list(args)), **kwargs))


def clear(*args: Any, **kwargs: Any) -> int:
    with _lock:
        n = len(_ENTRIES)
        _ENTRIES.clear()
        _CAMERA_INDEX.clear()
        return n


# --- Async entrypoints (db-optional, ignored) --------------------------------

async def _maybe_await(value: Any) -> Any:
    return value


async def create_entry_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    kwargs.pop("db", None)
    kwargs.pop("session", None)
    return create_entry(*args, **kwargs)


async def list_entries_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_entries(*args, **kwargs)


async def get_entry_async(entry_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_entry(entry_id, *args, **kwargs)


async def update_entry_async(entry_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return update_entry(entry_id, *args, **kwargs)


async def delete_entry_async(entry_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> bool:
    return delete_entry(entry_id, *args, **kwargs)


class ServiceLogService:
    """Object-oriented facade over the module-level store."""

    def create(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_entry(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_entries(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_entry(*args, **kwargs)

    def update(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return update_entry(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return delete_entry(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    def count(self, *args: Any, **kwargs: Any) -> int:
        return count_entries(*args, **kwargs)

    # aliases
    create_entry = create
    add_entry = create
    list_entries = list
    get_entries = list
    get_entry = get
    update_entry = update
    delete_entry = delete
    clear_entries = clear


service_log_service = ServiceLogService()
service_service = service_log_service
log_service = service_log_service
service = service_log_service
manager = service_log_service
store = service_log_service


# Compatibility aliases (function level).
add_entry = create_entry
create_service_entry = create_entry
add_service_entry = create_entry
create_log_entry = create_entry
log_service_entry = create_entry
record_service = create_entry

get_entries = list_entries
get_service_entries = list_entries
list_service_entries = list_entries
fetch_entries = list_entries
fetch_service_entries = list_entries
query_entries = list_entries
search_entries = list_entries

get_service_entry = get_entry
fetch_entry = get_entry
find_entry = get_entry

edit_entry = update_entry
update_service_entry = update_entry
edit_service_entry = update_entry
patch_entry = update_entry

remove_entry = delete_entry
delete_service_entry = delete_entry
remove_service_entry = delete_entry

clear_entries = clear
clear_service_log = clear
reset = clear
purge = clear

__all__ = [
    "create_entry", "list_entries", "get_entry", "update_entry",
    "delete_entry", "delete_entries_for_camera", "count_entries", "clear",
    "create_entry_async", "list_entries_async", "get_entry_async",
    "update_entry_async", "delete_entry_async",
    "ServiceLogService", "service_log_service",
]
