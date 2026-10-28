"""Export history service.

Tracks completed/queued video export records in memory, and merges with
live jobs from ``app.services.export`` so history reflects real exports
even when they were created via the export endpoints directly.

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

EXPORT_HISTORY: Dict[str, "ExportHistoryEntry"] = {}
export_history = EXPORT_HISTORY
history = EXPORT_HISTORY
HISTORY = EXPORT_HISTORY
STORE = EXPORT_HISTORY
ENTRIES = EXPORT_HISTORY
EXPORT_HISTORY_STORE = EXPORT_HISTORY

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
    for nested_key in ("payload", "options", "config", "params", "data", "body", "entry", "export", "attributes"):
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


class ExportHistoryEntry(dict):
    """Dict-like export history record with attribute access."""

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


ExportHistoryRecord = ExportHistoryEntry
HistoryEntry = ExportHistoryEntry
ExportRecord = ExportHistoryEntry
HistoryRecord = ExportHistoryEntry
ExportHistoryModel = ExportHistoryEntry
ExportHistoryItem = ExportHistoryEntry


def _build_entry(data: Dict[str, Any]) -> ExportHistoryEntry:
    entry_id = _pick(data, "id", "entry_id", "history_id", "job_id", "jobId", "export_id") or str(uuid.uuid4())
    entry_id = str(entry_id)
    camera_id = _pick(data, "camera_id", "camera", "cam", "cameraId", "cameraID", "cam_id", "source_id")
    camera_name = _pick(data, "camera_name", "cameraName", "name", "source_name")
    if camera_name is None and camera_id is not None:
        camera_name = str(camera_id)
    start = _pick(data, "start", "start_time", "startTime", "from", "from_time", "begin", "since", "range_start")
    end = _pick(data, "end", "end_time", "endTime", "to", "to_time", "until", "before", "range_end", "finish")
    fmt = _pick(data, "format", "fmt", "output_format", "format_type", "type") or "mp4"
    status = _pick(data, "status", "state", "stage") or "completed"
    output_path = _pick(
        data, "output_path", "output", "path", "file_path", "filePath", "result", "download_url", "url"
    )
    progress = _pick(data, "progress", "percent")
    try:
        progress_val = float(progress) if progress is not None else None
    except (TypeError, ValueError):
        progress_val = None
    now = _pick(data, "created_at", "createdAt", "timestamp", "ts", "time", "date") or _now_iso()
    entry = ExportHistoryEntry()
    entry["id"] = entry_id
    entry["entry_id"] = entry_id
    entry["history_id"] = entry_id
    entry["job_id"] = str(_pick(data, "job_id", "jobId", "export_id") or entry_id)
    entry["export_id"] = entry["job_id"]
    entry["camera_id"] = camera_id
    entry["camera"] = camera_id
    entry["camera_name"] = camera_name
    entry["start"] = start
    entry["end"] = end
    entry["start_time"] = _pick(data, "start_time", "startTime") if _pick(data, "start_time", "startTime") is not None else start
    entry["end_time"] = _pick(data, "end_time", "endTime") if _pick(data, "end_time", "endTime") is not None else end
    entry["format"] = str(fmt).lower() if fmt is not None else "mp4"
    entry["status"] = str(status)
    entry["state"] = str(status)
    entry["progress"] = progress_val
    entry["output_path"] = output_path
    entry["output"] = output_path
    entry["path"] = output_path
    entry["file_path"] = output_path
    entry["error"] = _pick(data, "error", "message", "failure")
    entry["created_at"] = now
    entry["updated_at"] = _pick(data, "updated_at", "updatedAt") or now
    entry["timestamp"] = now
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


def _merged_jobs_snapshot() -> List[Dict[str, Any]]:
    """Best-effort snapshot of live export jobs for history merging."""
    try:
        from app.services.export import EXPORT_JOBS as _live  # lazy to avoid cycles
    except Exception:
        return []
    try:
        return [_entry_to_dict(v) for v in list(_live.values())]
    except Exception:
        return []


def _apply_filters(
    entries: List[Dict[str, Any]],
    limit: Any = None,
    skip: Any = None,
    offset: Any = None,
    include_jobs: bool = True,
    **filters: Any,
) -> List[Dict[str, Any]]:
    # Resolve limit/skip aliases.
    lim = filters.pop("limit", limit)
    for alias in ("n", "count", "size", "max", "top", "per_page", "page_size"):
        if lim is None and filters.get(alias) is not None:
            lim = filters.pop(alias)
    sk = filters.pop("skip", skip)
    if sk is None:
        sk = filters.pop("offset", offset if offset is not None else 0)
    for alias in ("start_index", "page"):
        filters.pop(alias, None)
    # Drop non-filter keys.
    for k in ("format_filter", "include_jobs", "includeJobs", "merge_jobs", "format_"):
        filters.pop(k, None)
    # NOTE: "format" doubles as an export-serialization flag elsewhere; only
    # treat it as a filter when explicitly requested via camera-style scoping
    # is ambiguous. Keep it as a filter here for history queries.
    out: List[Dict[str, Any]] = []
    alias_map = {
        "camera": ("camera_id",),
        "cam": ("camera_id",),
        "cameraId": ("camera_id",),
        "camera_name": ("camera_name",),
        "state": ("status",),
        "stage": ("status",),
        "fmt": ("format",),
        "output_format": ("format",),
    }
    for ev in entries:
        ok = True
        for key, expected in (filters or {}).items():
            if expected is None:
                continue
            if key in ("format", "fmt", "output_format") and str(expected).lower() in ("json", "csv"):
                # Serialization hint, not a filter, when entries look mixed.
                # Only apply if at least one entry plausibly matches.
                continue
            ev_val = ev.get(key)
            if ev_val is None:
                for alt in alias_map.get(key, ()):
                    if ev.get(alt) is not None:
                        ev_val = ev.get(alt)
                        break
            if ev_val is None:
                # Unknown filter key with no match -> skip matching (tolerant).
                if key in alias_map or key in (
                    "camera_id", "camera", "camera_name", "status", "state",
                    "format", "fmt", "id", "job_id", "entry_id",
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


class ExportHistoryService:
    """In-memory export history store with job merging."""

    def __init__(self) -> None:
        self.store = EXPORT_HISTORY
        self.history = EXPORT_HISTORY
        self.entries = EXPORT_HISTORY
        self._lock = _jobs_lock

    # -- record --
    def record_export(self, *args: Any, **kwargs: Any) -> ExportHistoryEntry:
        data = _normalize_entry_data(*args, **kwargs)
        entry = _build_entry(data)
        with self._lock:
            EXPORT_HISTORY[entry["id"]] = entry
        return entry

    def log_export(self, *args: Any, **kwargs: Any) -> ExportHistoryEntry:
        return self.record_export(*args, **kwargs)

    def add_entry(self, *args: Any, **kwargs: Any) -> ExportHistoryEntry:
        return self.record_export(*args, **kwargs)

    def create_entry(self, *args: Any, **kwargs: Any) -> ExportHistoryEntry:
        return self.record_export(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> ExportHistoryEntry:
        return self.record_export(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> ExportHistoryEntry:
        return self.record_export(*args, **kwargs)

    def log(self, *args: Any, **kwargs: Any) -> ExportHistoryEntry:
        return self.record_export(*args, **kwargs)

    def append(self, *args: Any, **kwargs: Any) -> ExportHistoryEntry:
        return self.record_export(*args, **kwargs)

    def record(self, *args: Any, **kwargs: Any) -> ExportHistoryEntry:
        return self.record_export(*args, **kwargs)

    # -- get --
    def get_entry(self, entry_id: Any) -> Optional[ExportHistoryEntry]:
        if entry_id is None:
            return None
        key = str(entry_id)
        with self._lock:
            found = EXPORT_HISTORY.get(key)
            if found is not None:
                return found
            # Fall back to job_id / export_id match.
            for e in EXPORT_HISTORY.values():
                if str(e.get("job_id")) == key or str(e.get("export_id")) == key:
                    return e
        # Fall back to live jobs.
        for job in _merged_jobs_snapshot():
            if str(job.get("id")) == key or str(job.get("job_id")) == key:
                return _build_entry(job)
        return None

    def get(self, entry_id: Any) -> Optional[ExportHistoryEntry]:
        return self.get_entry(entry_id)

    def fetch_entry(self, entry_id: Any) -> Optional[ExportHistoryEntry]:
        return self.get_entry(entry_id)

    # -- list --
    def list_history(
        self,
        camera_id: Any = None,
        status: Any = None,
        format: Any = None,  # noqa: A002
        limit: Any = None,
        offset: Any = None,
        skip: Any = None,
        include_jobs: bool = True,
        **filters: Any,
    ) -> List[ExportHistoryEntry]:
        if isinstance(camera_id, dict):
            filters.update(camera_id)
            camera_id = None
        if camera_id is None:
            camera_id = filters.pop("camera", filters.pop("cam", filters.pop("cameraId", None)))
        if status is None:
            status = filters.pop("state", filters.pop("stage", None))
        if format is None:
            format = filters.pop("fmt", filters.pop("output_format", None))
        with self._lock:
            items = [_entry_to_dict(e) for e in list(EXPORT_HISTORY.values())]
        if include_jobs and filters.pop("include_jobs", True) is not False:
            seen = {str(e.get("id")) for e in items}
            seen |= {str(e.get("job_id")) for e in items}
            for job in _merged_jobs_snapshot():
                jid = str(job.get("id") or job.get("job_id"))
                if jid and jid not in seen:
                    items.append(_entry_to_dict(_build_entry(job)))
                    seen.add(jid)
        merged: Dict[str, Any] = {}
        if filters.pop("merge_jobs", None) is False:
            pass
        query: Dict[str, Any] = {}
        if camera_id is not None:
            query["camera_id"] = camera_id
        if status is not None:
            query["status"] = status
        if format is not None and str(format).lower() not in ("json", "csv"):
            query["format"] = format
        query.update({k: v for k, v in filters.items() if v is not None})
        return _apply_filters(items, limit=limit, skip=skip if skip is not None else offset, **query)  # type: ignore[return-value]

    def list(self, *args: Any, **kwargs: Any) -> List[ExportHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_history(self, *args: Any, **kwargs: Any) -> List[ExportHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_history(self, *args: Any, **kwargs: Any) -> List[ExportHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_entries(self, *args: Any, **kwargs: Any) -> List[ExportHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_entries(self, *args: Any, **kwargs: Any) -> List[ExportHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def all(self, *args: Any, **kwargs: Any) -> List[ExportHistoryEntry]:
        return self.list_history(*args, **kwargs)

    # -- delete / clear --
    def delete_entry(self, entry_id: Any) -> bool:
        if entry_id is None:
            return False
        key = str(entry_id)
        with self._lock:
            if EXPORT_HISTORY.pop(key, None) is not None:
                return True
            for k, e in list(EXPORT_HISTORY.items()):
                if str(e.get("job_id")) == key or str(e.get("export_id")) == key:
                    EXPORT_HISTORY.pop(k, None)
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
                n = len(EXPORT_HISTORY)
                EXPORT_HISTORY.clear()
                return n
        targets = self.list_history(include_jobs=False, **filters)
        n = 0
        with self._lock:
            for t in targets:
                if EXPORT_HISTORY.pop(str(t.get("id")), None) is not None:
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
            return len(EXPORT_HISTORY)


export_history_service = ExportHistoryService()
history_service = export_history_service
service = export_history_service
manager = export_history_service
export_manager = export_history_service
history_manager = export_history_service
store = export_history_service
default_service = export_history_service


def record_export(*args: Any, **kwargs: Any) -> ExportHistoryEntry:
    return export_history_service.record_export(*args, **kwargs)


def log_export(*args: Any, **kwargs: Any) -> ExportHistoryEntry:
    return export_history_service.log_export(*args, **kwargs)


def add_entry(*args: Any, **kwargs: Any) -> ExportHistoryEntry:
    return export_history_service.add_entry(*args, **kwargs)


def create_entry(*args: Any, **kwargs: Any) -> ExportHistoryEntry:
    return export_history_service.create_entry(*args, **kwargs)


def get_entry(entry_id: Any) -> Optional[ExportHistoryEntry]:
    return export_history_service.get_entry(entry_id)


def get_export_history(*args: Any, **kwargs: Any) -> List[ExportHistoryEntry]:
    return export_history_service.list_history(*args, **kwargs)


def list_history(*args: Any, **kwargs: Any) -> List[ExportHistoryEntry]:
    return export_history_service.list_history(*args, **kwargs)


def fetch_history(*args: Any, **kwargs: Any) -> List[ExportHistoryEntry]:
    return export_history_service.list_history(*args, **kwargs)


def list_entries(*args: Any, **kwargs: Any) -> List[ExportHistoryEntry]:
    return export_history_service.list_history(*args, **kwargs)


def fetch_entries(*args: Any, **kwargs: Any) -> List[ExportHistoryEntry]:
    return export_history_service.list_history(*args, **kwargs)


def delete_entry(entry_id: Any) -> bool:
    return export_history_service.delete_entry(entry_id)


def clear_history(**filters: Any) -> int:
    return export_history_service.clear_history(**filters)


def clear(**filters: Any) -> int:
    return export_history_service.clear_history(**filters)


# CSV / JSON helpers for history dumps.
DEFAULT_COLUMNS = [
    "id",
    "job_id",
    "camera_id",
    "camera_name",
    "start",
    "end",
    "format",
    "status",
    "progress",
    "output_path",
    "error",
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
record = record_export
log = log_export
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
export_history_entries = list_history
build_csv = to_csv
export_csv = to_csv
build_json = to_json
export_json = to_json

ExportHistoryServiceClass = ExportHistoryService
ExportHistoryManager = ExportHistoryService
HistoryService = ExportHistoryService
HistoryManager = ExportHistoryService
ExporterHistory = ExportHistoryService

__all__ = [
    "EXPORT_HISTORY",
    "ExportHistoryEntry",
    "ExportHistoryService",
    "export_history_service",
    "record_export",
    "log_export",
    "add_entry",
    "create_entry",
    "get_entry",
    "list_history",
    "fetch_history",
    "delete_entry",
    "clear_history",
    "to_csv",
    "to_json",
    "export_entries",
]
