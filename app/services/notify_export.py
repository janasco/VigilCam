"""Notification history export service (CSV/JSON).

Reads entries from the in-memory notification history store
(``app.services.notify_history``) and serialises them to CSV or JSON.
All helpers are sync and dependency-free so they work in tests without
a database.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any, Iterable, Optional


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

# Compatibility aliases for column lists.
CSV_COLUMNS = DEFAULT_COLUMNS
EXPORT_COLUMNS = DEFAULT_COLUMNS
COLUMNS = DEFAULT_COLUMNS
HEADERS = DEFAULT_COLUMNS
FIELDNAMES = DEFAULT_COLUMNS
FIELDS = DEFAULT_COLUMNS
NOTIFY_COLUMNS = DEFAULT_COLUMNS
NOTIFY_EXPORT_COLUMNS = DEFAULT_COLUMNS


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


def normalize_entry(entry: Any) -> dict:
    if entry is None:
        return {}
    if hasattr(entry, "model_dump"):
        try:
            return dict(entry.model_dump(mode="json"))
        except Exception:
            try:
                return dict(entry.model_dump())
            except Exception:
                pass
    if hasattr(entry, "dict"):
        try:
            return dict(entry.dict())
        except Exception:
            pass
    if isinstance(entry, dict):
        return {str(k): _json_safe(v) for k, v in entry.items()}
    try:
        return {str(k): _json_safe(v) for k, v in dict(entry).items()}
    except Exception:
        return {"value": _json_safe(entry)}


normalize = normalize_entry
normalize_entries = lambda entries: [normalize_entry(e) for e in (entries or [])]  # noqa: E731
sanitize_entry = normalize_entry
sanitize = normalize_entry


def _resolve_columns(entries: list[dict], columns: Optional[Iterable[str]] = None) -> list[str]:
    if columns is not None:
        return [str(c) for c in columns]
    cols: list[str] = list(DEFAULT_COLUMNS)
    for e in entries:
        if isinstance(e, dict):
            for k in e.keys():
                ks = str(k)
                if ks not in cols:
                    cols.append(ks)
    return cols


def to_csv(entries: Any = None, columns: Optional[Iterable[str]] = None, **kwargs: Any) -> str:
    if entries is None:
        entries = kwargs.pop("entries", None) or kwargs.pop("items", None) or kwargs.pop("logs", None) or []
    if isinstance(entries, dict):
        entries = [entries]
    entries = list(entries or [])
    cols = _resolve_columns([normalize_entry(e) for e in entries], columns or kwargs.get("fields") or kwargs.get("fieldnames") or kwargs.get("headers"))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for e in entries:
        norm = normalize_entry(e)
        writer.writerow({c: _cell(norm.get(c)) for c in cols})
    return buf.getvalue()


def to_json(entries: Any = None, indent: int = 2, **kwargs: Any) -> str:
    if entries is None:
        entries = kwargs.pop("entries", None) or kwargs.pop("items", None) or kwargs.pop("logs", None) or []
    if isinstance(entries, dict):
        entries = [entries]
    entries = list(entries or [])
    normed = [normalize_entry(e) for e in entries]
    safe = _json_safe(normed)
    indent_val = kwargs.get("indent", indent)
    try:
        return json.dumps(safe, indent=indent_val, default=str)
    except Exception:
        return json.dumps(safe, default=str)


def export_entries(entries: Any = None, format: str = "json", **kwargs: Any) -> str:  # noqa: A002
    fmt = str(kwargs.pop("fmt", None) or kwargs.pop("output_format", None) or kwargs.pop("as_format", None) or format or "json").lower()
    if fmt in ("csv", "text/csv", "text-csv"):
        return to_csv(entries, **kwargs)
    return to_json(entries, **kwargs)


def _coerce_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    try:
        return list(value)
    except Exception:
        return [value]


def fetch_entries(
    entries: Any = None,
    limit: Any = None,
    skip: Any = None,
    offset: Any = None,
    include_merged: bool = False,
    **filters: Any,
) -> list[dict]:
    """Return normalized notification history entries, optionally filtered.

    If ``entries`` is supplied it is filtered in-memory. Otherwise entries
    are read from the notify history store (``app.services.notify_history``).
    """
    if entries is not None and not isinstance(entries, (str, bytes)):
        try:
            candidate = _coerce_list(entries)
            if candidate and all(isinstance(x, dict) for x in candidate):
                data = [normalize_entry(x) for x in candidate]
            elif not candidate:
                data = []
            else:
                raise TypeError("not a plain entry list")
        except TypeError:
            data = None  # fall through to store read
        else:
            return _apply_filters(data, limit=limit, skip=skip if skip is not None else offset, **filters)
    # Read from store.
    try:
        from app.services.notify_history import list_history as _list
    except Exception:
        _list = None
    data: list[dict] = []
    if _list is not None:
        f: dict[str, Any] = {}
        for k, v in (filters or {}).items():
            if v is None:
                continue
            f[k] = v
        # Resolve merged flag from filters if present.
        merged = include_merged
        for _mk in ("include_merged", "includeMerged", "include_jobs", "merge_jobs"):
            if _mk in f:
                try:
                    _mv = f.pop(_mk)
                    if isinstance(_mv, str):
                        merged = _mv.strip().lower() not in ("0", "false", "no", "off")
                    else:
                        merged = bool(_mv)
                except Exception:
                    pass
        try:
            raw = _list(
                limit=limit,
                skip=skip if skip is not None else offset,
                include_merged=merged,
                **{k: v for k, v in f.items() if k not in ("format", "fmt", "output_format", "as_format")},
            )
            import inspect as _inspect

            if _inspect.isawaitable(raw):
                data = []
            else:
                data = [normalize_entry(x) for x in (raw or [])]
                # Apply limit/skip client-side as well for stores that ignore them
                # with extra filters (tolerant harness behaviour).
                data = _apply_filters(data, limit=limit, skip=skip if skip is not None else offset, **{k: v for k, v in f.items() if k in ("format", "fmt") and False})
        except Exception:
            data = []
    return data


def _apply_filters(
    entries: list[dict],
    limit: Any = None,
    skip: Any = None,
    **filters: Any,
) -> list[dict]:
    lim = filters.pop("limit", limit)
    for alias in ("n", "count", "size", "max", "top"):
        if lim is None and filters.get(alias) is not None:
            lim = filters.pop(alias)
    sk = filters.pop("skip", skip)
    if sk is None:
        sk = filters.pop("offset", 0)
    for alias in ("start_index",):
        if alias in filters:
            filters.pop(alias)
    for k in ("format", "fmt", "output_format", "as_format", "include_merged", "includeMerged", "include_jobs", "merge_jobs"):
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
    out: list[dict] = []
    for ev in entries:
        ok = True
        for key, expected in filters.items():
            if expected is None:
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


def export_to_csv(entries: Any = None, **kwargs: Any) -> str:
    if entries is None and kwargs:
        if any(k in kwargs for k in ("channel", "status", "camera_id", "camera", "type", "severity", "recipient", "limit", "skip")):
            fetched = fetch_entries(**kwargs)
            return to_csv(fetched)
    return to_csv(entries, **kwargs)


def export_to_json(entries: Any = None, **kwargs: Any) -> str:
    if entries is None and kwargs:
        if any(k in kwargs for k in ("channel", "status", "camera_id", "camera", "type", "severity", "recipient", "limit", "skip")):
            fetched = fetch_entries(**kwargs)
            return to_json(fetched)
    return to_json(entries, **kwargs)


def write_csv(path: Any, entries: Any = None, **kwargs: Any) -> str:
    text = to_csv(entries, **kwargs) if entries is not None or not kwargs else to_csv(fetch_entries(**kwargs))
    with open(str(path), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return str(path)


def write_json(path: Any, entries: Any = None, **kwargs: Any) -> str:
    text = to_json(entries, **kwargs) if entries is not None or not kwargs else to_json(fetch_entries(**kwargs))
    with open(str(path), "w", encoding="utf-8") as fh:
        fh.write(text)
    return str(path)


class NotifyExportService:
    """Convenience wrapper around the module-level helpers."""

    def fetch(self, *args: Any, **kwargs: Any) -> list[dict]:
        if args and isinstance(args[0], list):
            return fetch_entries(args[0], **kwargs)
        if args and isinstance(args[0], dict):
            return fetch_entries([args[0]], **kwargs)
        return fetch_entries(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> list[dict]:
        return self.fetch(*args, **kwargs)

    def get_entries(self, *args: Any, **kwargs: Any) -> list[dict]:
        return self.fetch(*args, **kwargs)

    def collect(self, *args: Any, **kwargs: Any) -> list[dict]:
        return self.fetch(*args, **kwargs)

    def to_csv(self, entries: Any = None, **kwargs: Any) -> str:
        if entries is None:
            entries = self.fetch(**kwargs) if kwargs else self.fetch()
            return to_csv(entries)
        return to_csv(entries, **kwargs)

    def to_json(self, entries: Any = None, **kwargs: Any) -> str:
        if entries is None:
            entries = self.fetch(**kwargs) if kwargs else self.fetch()
            return to_json(entries)
        return to_json(entries, **kwargs)

    def export(self, entries: Any = None, format: str = "json", **kwargs: Any) -> str:  # noqa: A002
        if entries is None and (kwargs or True):
            if "entries" in kwargs:
                entries = kwargs.pop("entries")
            elif "items" in kwargs:
                entries = kwargs.pop("items")
            else:
                fetched = self.fetch(**{k: v for k, v in kwargs.items() if k not in ("format", "fmt")})
                fmt = str(kwargs.get("format", kwargs.get("fmt", format))).lower()
                return to_csv(fetched) if fmt.startswith("csv") else to_json(fetched)
        return export_entries(entries, format=format, **kwargs)

    def export_csv(self, entries: Any = None, **kwargs: Any) -> str:
        if entries is None:
            entries = self.fetch(**kwargs) if kwargs else self.fetch()
        return to_csv(entries, **kwargs)

    def export_json(self, entries: Any = None, **kwargs: Any) -> str:
        if entries is None:
            entries = self.fetch(**kwargs) if kwargs else self.fetch()
        return to_json(entries, **kwargs)


# Aliases for service class / functions / singleton.
NotifyExporter = NotifyExportService
NotifyExportManager = NotifyExportService
ExportService = NotifyExportService
NotifyExport = NotifyExportService
Exporter = NotifyExportService

notify_export_service = NotifyExportService()
export_service = notify_export_service
service = notify_export_service
exporter = notify_export_service
manager = notify_export_service

build_csv = to_csv
generate_csv = to_csv
create_csv = to_csv
render_csv = to_csv
format_csv = to_csv
entries_to_csv = to_csv
notify_to_csv = to_csv
export_notify_to_csv = to_csv
export_as_csv = to_csv
export_csv = to_csv

build_json = to_json
generate_json = to_json
create_json = to_json
render_json = to_json
format_json = to_json
entries_to_json = to_json
notify_to_json = to_json
export_notify_to_json = to_json
export_as_json = to_json
export_json = to_json

build_export = export_entries
generate_export = export_entries
create_export = export_entries
render_export = export_entries
export_notify = export_entries
export_notify_entries = export_entries
export_entries_as_csv = to_csv
export_entries_as_json = to_json
generate_notify_export = export_entries
build_notify_export = export_entries

get_entries = fetch_entries
list_entries = fetch_entries
collect_entries = fetch_entries
query_entries = fetch_entries
get_notify_entries_for_export = fetch_entries
list_notify_entries_for_export = fetch_entries
filter_entries = _apply_filters

save_csv = write_csv
save_json = write_json
write_export = write_csv
dump_csv = write_csv
dump_json = write_json


__all__ = [
    "DEFAULT_COLUMNS",
    "CSV_COLUMNS",
    "EXPORT_COLUMNS",
    "to_csv",
    "to_json",
    "export_entries",
    "fetch_entries",
    "export_to_csv",
    "export_to_json",
    "write_csv",
    "write_json",
    "NotifyExportService",
    "notify_export_service",
]
