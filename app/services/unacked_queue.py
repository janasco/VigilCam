"""Unacknowledged-events smart filter (oldest unacked first).

Verification note (read-only):

- ``app/services/bulk_ack.py`` already returns per-event results suitable for
  UI progress display: ``bulk_acknowledge`` / ``bulk_escalate`` /
  ``bulk_ack_events`` each return ``per_event`` + ``per_event_states`` dicts
  keyed by event id, plus ``updated_event_ids`` / ``updated_events`` /
  ``updated`` / ``updated_count`` counts (mirroring ``bulk_tag``). The
  ``app/api/bulk_ack.py`` router returns that dict directly, so progress UIs
  can render per-event rows. No ``ack_progress`` layer is needed.
- ``app/services/event_ack.py`` only tracks single-event ack/escalate state
  (``acknowledge`` / ``escalate`` / ``get_status`` / ``list_states`` /
  ``is_acknowledged`` / ``is_escalated``). There is no queue / smart-filter
  helper there -- grep for ``unacked`` / ``unacknowledged`` / ``pending`` /
  ``queue`` / ``oldest`` finds no queue module.

This module adds the missing layer: filter a set of event entries down to the
still-unacknowledged ones, sorted oldest-first so operators work the backlog
front-to-back. It reads ack state from ``app/services/event_ack.py``
(in-memory store) and never mutates it. ``db``/``session`` arguments are
accepted for forward-compatibility but ignored on the sync path
(best-effort no-op); the async entrypoints additionally support an
``AsyncSession`` over :class:`app.models.entities.Event`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.services import event_ack as _ack

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
DEFAULT_ORDER = "oldest"

_ORDER_OLDEST = {"oldest", "asc", "ascending", "chronological", "fifo", "forward", "oldest-first", "oldest_first"}
_ORDER_NEWEST = {"newest", "desc", "descending", "reverse", "newest-first", "newest_first", "lifo"}

_ENTRIES_KEYS = (
    "entries",
    "events",
    "items",
    "rows",
    "event_list",
    "data_events",
    "event_entries",
    "queue",
    "candidates",
)

_EVENT_ID_KEYS = (
    "event_ids",
    "eventIds",
    "events_ids",
    "ids",
    "id_list",
    "idList",
    "event_id_list",
    "eventIdList",
    "events",
    "event_id",
    "eventId",
    "event",
    "eventid",
    "id_event",
    "camera_event_id",
    "id",
)

_CAMERA_KEYS = (
    "camera_id",
    "camera_ids",
    "cameras",
    "camera",
    "cameraId",
    "cameraIds",
    "cam_id",
    "cam_ids",
    "cam",
)

_LABEL_KEYS = (
    "label",
    "labels",
    "detection_label",
    "detection_labels",
    "object",
    "objects",
    "kind",
    "kinds",
    "type",
    "types",
    "tag",
    "tags",
)

_LIMIT_KEYS = ("limit", "count", "n", "top", "first", "page_size", "pageSize", "per_page", "perPage", "size")

_OFFSET_KEYS = ("offset", "skip", "start", "from", "from_index", "fromIndex", "page_offset")


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    if isinstance(value, (int, float)):
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        if abs(num) > 1e12:
            num = num / 1000.0
        try:
            return datetime.fromtimestamp(num, tz=timezone.utc).replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in {"none", "null", "undefined", "all"}:
            return None
        try:
            num = float(s)
            if s.replace(".", "", 1).replace("e", "", 1).replace("E", "", 1).replace("-", "", 1).replace("+", "", 1).isdigit():
                if abs(num) > 1e12:
                    num = num / 1000.0
                if abs(num) > 1e8:
                    return datetime.fromtimestamp(num, tz=timezone.utc).replace(tzinfo=None)
        except ValueError:
            pass
        s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(s2)
        except ValueError:
            try:
                dt = datetime.fromisoformat(s + "T00:00:00")
            except ValueError:
                return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None


def _coerce_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    return None


def _flatten_options(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if a is None:
            continue
        if hasattr(a, "model_dump"):
            try:
                data.update(dict(a.model_dump()))
                continue
            except Exception:
                pass
        if isinstance(a, dict):
            for k, v in a.items():
                data.setdefault(k, v)
        elif isinstance(a, (list, tuple, set)):
            data.setdefault("entries", list(a))
    for k, v in (kwargs or {}).items():
        if k in ("db", "session"):
            continue
        data[k] = v
    for nested_key in (
        "payload", "options", "config", "params", "data", "body",
        "request", "fields", "entry", "filter", "filters", "query",
        "criteria", "ack", "acknowledgement",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _extract_first(data: Dict[str, Any], keys: tuple) -> Optional[Any]:
    for k in keys:
        if data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items() if v is not None}
    for k in keys:
        if lower.get(str(k).lower()) is not None:
            return lower[str(k).lower()]
    return None


def normalize_order(value: Any, default: str = DEFAULT_ORDER) -> str:
    """Normalize order/mode to ``oldest`` or ``newest`` (default oldest)."""
    if value is None:
        return default
    s = str(value).strip().lower().replace("_", "-")
    if not s:
        return default
    if s in _ORDER_NEWEST:
        return "newest"
    if s in _ORDER_OLDEST:
        return "oldest"
    return default


def _entry_id(ev: Any) -> Optional[int]:
    if ev is None:
        return None
    if isinstance(ev, (int, float)) and not isinstance(ev, bool):
        return _coerce_int(ev)
    if isinstance(ev, dict):
        for k in ("id", "event_id", "eventId", "ID", "event", "eventid"):
            if ev.get(k) is not None:
                eid = _coerce_int(ev.get(k))
                if eid is not None:
                    return eid
        return None
    for attr in ("id", "event_id"):
        v = getattr(ev, attr, None)
        if v is not None:
            eid = _coerce_int(v)
            if eid is not None:
                return eid
    return None


def _entry_time(ev: Any) -> Optional[datetime]:
    if isinstance(ev, dict):
        for key in (
            "created_at", "createdAt", "timestamp", "ts", "time",
            "occurred_at", "occurredAt", "at", "date", "created",
        ):
            if ev.get(key) is not None:
                dt = _parse_ts(ev.get(key))
                if dt is not None:
                    return dt
        return None
    for attr in ("created_at", "timestamp", "ts", "time", "occurred_at"):
        v = getattr(ev, attr, None)
        if v is None:
            continue
        dt = _parse_ts(v)
        if dt is not None:
            return dt
    return None


def _entry_camera(ev: Any) -> Optional[int]:
    if isinstance(ev, dict):
        for k in ("camera_id", "cameraId", "camera", "cam_id", "cam"):
            if ev.get(k) is not None:
                try:
                    return int(ev.get(k))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    try:
                        return int(float(str(ev.get(k)).strip()))
                    except (TypeError, ValueError):
                        continue
        return None
    for attr in ("camera_id", "cameraId", "cam_id"):
        v = getattr(ev, attr, None)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def _entry_labels(ev: Any) -> List[str]:
    vals: List[str] = []
    if isinstance(ev, dict):
        for k in ("label", "labels", "kind", "type", "tag", "tags", "detection_label", "object"):
            v = ev.get(k)
            if v is None:
                continue
            if isinstance(v, (list, tuple, set)):
                vals.extend(str(x).strip().lower() for x in v if str(x).strip())
            else:
                for part in str(v).split(","):
                    part = part.strip().lower()
                    if part:
                        vals.append(part)
    else:
        for attr in ("label", "kind", "tags"):
            v = getattr(ev, attr, None)
            if v is None:
                continue
            if isinstance(v, (list, tuple, set)):
                vals.extend(str(x).strip().lower() for x in v if str(x).strip())
            else:
                for part in str(v).split(","):
                    part = part.strip().lower()
                    if part:
                        vals.append(part)
    return vals


def _to_id_list(values: Any) -> List[int]:
    out: List[int] = []
    seen = set()
    if values is None:
        return out
    iterable: Any = values if isinstance(values, (list, tuple, set)) else [values]
    for v in iterable:
        if v is None or isinstance(v, bool):
            continue
        if isinstance(v, dict):
            eid = _entry_id(v)
            if eid is not None and eid not in seen:
                seen.add(eid)
                out.append(int(eid))
            continue
        if isinstance(v, str):
            for part in v.split(","):
                part = part.strip()
                if not part:
                    continue
                iv = _coerce_int(part)
                if iv is not None and iv not in seen:
                    seen.add(iv)
                    out.append(int(iv))
        else:
            iv = _coerce_int(v)
            if iv is not None and iv not in seen:
                seen.add(iv)
                out.append(int(iv))
    return out


def _to_str_list(value: Any) -> List[str]:
    out: List[str] = []
    if value is None:
        return out
    iterable: Any = value if isinstance(value, (list, tuple, set)) else [value]
    for v in iterable:
        if v is None:
            continue
        if isinstance(v, str):
            for part in v.split(","):
                part = part.strip().lower()
                if part and part not in {"all", "none", "null"}:
                    out.append(part)
        else:
            s = str(v).strip().lower()
            if s and s not in {"all", "none", "null"}:
                out.append(s)
    return out


def _resolve_entries(data: Dict[str, Any]) -> List[Any]:
    for k in _ENTRIES_KEYS:
        if data.get(k) is not None:
            v = data.get(k)
            if isinstance(v, (list, tuple, set)):
                return list(v)
            return [v]
    for k in _EVENT_ID_KEYS:
        if data.get(k) is not None:
            raw = data.get(k)
            if isinstance(raw, dict) and (_entry_time(raw) is not None or _entry_id(raw) is not None):
                # A single dict entry with id/time is an entry, not a bare id.
                if _entry_time(raw) is not None or "camera_id" in raw or "label" in raw:
                    return [raw]
            if isinstance(raw, (list, tuple, set)) and raw and all(isinstance(x, dict) for x in raw):
                return list(raw)
            return [{"id": i} for i in _to_id_list(raw)]
    return []


def _resolve_limit(data: Dict[str, Any], default: int = DEFAULT_LIMIT) -> int:
    raw = _extract_first(data, _LIMIT_KEYS)
    iv = _coerce_int(raw)
    if iv is None:
        return default
    if iv < 0:
        return 0
    return min(iv, MAX_LIMIT)


def _resolve_offset(data: Dict[str, Any]) -> int:
    raw = _extract_first(data, _OFFSET_KEYS)
    iv = _coerce_int(raw)
    if iv is None or iv < 0:
        # ``page``/``page_number`` convenience (1-based).
        page = _coerce_int(data.get("page", data.get("page_number", data.get("pageNumber"))))
        if page is not None and page >= 1:
            return (page - 1) * _resolve_limit(data)
        return 0
    return iv


def _resolve_camera_filter(data: Dict[str, Any]) -> List[int]:
    raw = _extract_first(data, _CAMERA_KEYS)
    if raw is None:
        return []
    return _to_id_list(raw)


def _resolve_label_filter(data: Dict[str, Any]) -> List[str]:
    raw = _extract_first(data, _LABEL_KEYS)
    if raw is None:
        return []
    return _to_str_list(raw)


def is_unacked(event_id: Any, include_escalated: Any = None, **kwargs: Any) -> bool:
    """Return True when the event has no ack state (or is neither acked nor escalated)."""
    eid = _coerce_int(event_id)
    if eid is None:
        flat: Dict[str, Any] = {}
        if isinstance(event_id, dict):
            flat.update(event_id)
        flat.update(kwargs or {})
        for k in ("event_id", "eventId", "event", "eventid", "id"):
            if flat.get(k) is not None:
                eid = _coerce_int(flat.get(k))
                if eid is not None:
                    break
    if eid is None:
        return False
    inc_esc = _coerce_bool(include_escalated)
    if inc_esc is None:
        inc_esc = _coerce_bool(kwargs.get("include_escalated", kwargs.get("includeEscalated")))
    if inc_esc is None:
        inc_esc = False
    if _ack.is_escalated(int(eid)):
        # Escalation implies acknowledgement in the ack store, so the
        # escalated check must come first.
        return bool(inc_esc)
    if _ack.is_acknowledged(int(eid)):
        return False
    return True


def select_unacked(
    *args: Any,
    entries: Any = None,
    events: Any = None,
    limit: Any = None,
    offset: Any = None,
    order: Any = None,
    camera_ids: Any = None,
    include_escalated: Any = None,
    db: Any = None,
    session: Any = None,
    **kwargs: Any,
) -> List[Any]:
    """Return unacknowledged entries sorted oldest-first (then lowest id).

    Already-acknowledged (and, by default, escalated -- escalation implies
    acknowledged) entries are dropped. Camera / label filters apply before
    sorting. ``limit``/``offset`` paginate the sorted queue.
    """
    data = _flatten_options(*args, **kwargs)
    if entries is not None:
        data["entries"] = entries
    if events is not None:
        data["events"] = events
    if limit is not None:
        data["limit"] = limit
    if offset is not None:
        data["offset"] = offset
    if order is not None:
        data["order"] = order
    if camera_ids is not None:
        data["camera_ids"] = camera_ids
    if include_escalated is not None:
        data["include_escalated"] = include_escalated
    if isinstance(db, (list, tuple)) and not _resolve_entries(data):
        data["entries"] = list(db)
    inline = _resolve_entries(data)
    inc_esc = _coerce_bool(
        _extract_first(data, ("include_escalated", "includeEscalated", "include_escalations", "with_escalated"))
    ) or False
    cam_filter = set(_to_id_list(camera_ids) if camera_ids is not None else _resolve_camera_filter(data))
    label_filter = set(_to_str_list(_extract_first(data, _LABEL_KEYS)))
    mode = normalize_order(data.get("order", data.get("sort", data.get("direction", data.get("dir")))))

    kept: List[Any] = []
    for ev in inline:
        eid = _entry_id(ev)
        if eid is None:
            continue
        if cam_filter:
            cam = _entry_camera(ev)
            if cam not in cam_filter:
                continue
        if label_filter:
            ev_labels = set(_entry_labels(ev))
            if not (label_filter & ev_labels):
                continue
        if _ack.is_escalated(int(eid)):
            # Escalation implies acknowledgement in the ack store, so check
            # escalated first: kept only when explicitly included.
            if not inc_esc:
                continue
        elif _ack.is_acknowledged(int(eid)):
            continue
        kept.append(ev)

    far_future = datetime.max.replace(tzinfo=None)

    def _sort_key(ev: Any) -> Any:
        ts = _entry_time(ev)
        return (ts is None, ts if ts is not None else far_future, _entry_id(ev) or 0)

    kept.sort(key=_sort_key, reverse=(mode == "newest"))

    lim = _resolve_limit(data)
    off = _resolve_offset(data)
    if off:
        kept = kept[off:]
    if lim is not None:
        kept = kept[:lim]
    return kept


def _public_entry(ev: Any) -> Dict[str, Any]:
    eid = _entry_id(ev)
    ts = _entry_time(ev)
    state = _ack.get_status(int(eid)) if eid is not None else None
    if isinstance(ev, dict):
        base: Dict[str, Any] = dict(ev)
    else:
        base = {}
        for attr in ("id", "event_id", "camera_id", "kind", "label", "confidence", "tags", "created_at"):
            v = getattr(ev, attr, None)
            if v is not None:
                base[attr] = v.isoformat() if isinstance(v, datetime) else v
    out: Dict[str, Any] = dict(base)
    if eid is not None:
        out.setdefault("id", int(eid))
        out.setdefault("event_id", int(eid))
        out.setdefault("eventId", int(eid))
    if ts is not None:
        out.setdefault("created_at", ts.isoformat())
    out["acknowledged"] = bool(state.get("acknowledged")) if state else False
    out["acked"] = bool(state.get("acked")) if state else False
    out["escalated"] = bool(state.get("escalated")) if state else False
    out["status"] = state.get("status", "open") if state else "open"
    return out


def get_queue(
    *args: Any,
    entries: Any = None,
    events: Any = None,
    limit: Any = None,
    offset: Any = None,
    order: Any = None,
    camera_ids: Any = None,
    include_escalated: Any = None,
    db: Any = None,
    session: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Build the paginated unacked queue (oldest-first) with UI-ready counts."""
    data = _flatten_options(*args, **kwargs)
    if entries is not None:
        data["entries"] = entries
    if events is not None:
        data["events"] = events
    if limit is not None:
        data["limit"] = limit
    if offset is not None:
        data["offset"] = offset
    if order is not None:
        data["order"] = order
    if camera_ids is not None:
        data["camera_ids"] = camera_ids
    if include_escalated is not None:
        data["include_escalated"] = include_escalated
    if isinstance(db, (list, tuple)) and not _resolve_entries(data):
        data["entries"] = list(db)
    inline = _resolve_entries(data)
    total = len(inline)
    mode = normalize_order(data.get("order", data.get("sort", data.get("direction", data.get("dir")))))
    lim = _resolve_limit(data)
    off = _resolve_offset(data)
    inc_esc = _coerce_bool(
        _extract_first(data, ("include_escalated", "includeEscalated", "include_escalations", "with_escalated"))
    ) or False
    full = select_unacked(data, limit=MAX_LIMIT * 10, offset=0)
    # ``select_unacked`` already filtered + sorted; re-derive unpaginated
    # full queue with a wide cap so counts stay correct under pagination.
    unacked_total = len(full)
    page = full[off: off + lim] if lim is not None else full[off:]
    items = [_public_entry(e) for e in page]
    ids = [int(_entry_id(e)) for e in page if _entry_id(e) is not None]
    all_ids = [int(_entry_id(e)) for e in full if _entry_id(e) is not None]
    per_event: Dict[str, Dict[str, Any]] = {}
    for e in page:
        eid = _entry_id(e)
        if eid is None:
            continue
        st = _ack.get_status(int(eid))
        per_event[str(int(eid))] = dict(st) if st is not None else {
            "event_id": int(eid),
            "eventId": int(eid),
            "id": int(eid),
            "status": "open",
            "acknowledged": False,
            "acked": False,
            "escalated": False,
        }
    times = [_entry_time(e) for e in full if _entry_time(e) is not None]
    oldest = min(times).isoformat() if times else None
    newest = max(times).isoformat() if times else None
    has_more = (off + len(page)) < unacked_total
    next_offset = off + len(page) if has_more else None
    result: Dict[str, Any] = {
        "status": "ok",
        "ok": True,
        "success": True,
        "order": mode,
        "sort": mode,
        "oldest_first": mode == "oldest",
        "limit": lim,
        "offset": off,
        "total": total,
        "total_entries": total,
        "scanned": total,
        "scanned_count": total,
        "unacked_count": unacked_total,
        "unacked_total": unacked_total,
        "pending_count": unacked_total,
        "count": len(page),
        "returned_count": len(page),
        "items": items,
        "queue": items,
        "events": items,
        "event_ids": list(ids),
        "unacked_ids": list(all_ids),
        "queue_ids": list(all_ids),
        "ids": list(ids),
        "has_more": has_more,
        "hasMore": has_more,
        "next_offset": next_offset,
        "nextOffset": next_offset,
        "include_escalated": bool(inc_esc),
        "oldest": oldest,
        "oldest_created_at": oldest,
        "newest": newest,
        "newest_created_at": newest,
        "per_event": per_event,
        "per_event_states": per_event,
    }
    if page:
        result["next"] = dict(items[0])
        result["next_id"] = ids[0] if ids else None
        result["next_event_id"] = ids[0] if ids else None
    else:
        result["next"] = None
        result["next_id"] = None
        result["next_event_id"] = None
    return result


def peek_next(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return the single oldest unacked entry (or None when the queue is empty)."""
    data = _flatten_options(*args, **kwargs)
    data["limit"] = 1
    data["offset"] = 0
    queue = get_queue(data)
    items = queue.get("items") or []
    return dict(items[0]) if items else None


def get_next(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return peek_next(*args, **kwargs)


def get_count(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Return only queue counts (no items) for badge/counter UIs."""
    data = _flatten_options(*args, **kwargs)
    queue = get_queue(data, limit=0, offset=0)
    return {
        "status": "ok",
        "ok": True,
        "success": True,
        "order": queue.get("order"),
        "total": queue.get("total"),
        "total_entries": queue.get("total_entries"),
        "scanned": queue.get("scanned"),
        "scanned_count": queue.get("scanned_count"),
        "unacked_count": queue.get("unacked_count"),
        "unacked_total": queue.get("unacked_total"),
        "pending_count": queue.get("pending_count"),
        "count": queue.get("unacked_count"),
        "has_more": bool((queue.get("unacked_count") or 0) > 0),
        "oldest": queue.get("oldest"),
        "oldest_created_at": queue.get("oldest_created_at"),
        "newest": queue.get("newest"),
        "newest_created_at": queue.get("newest_created_at"),
        "next_id": queue.get("next_id"),
        "next_event_id": queue.get("next_event_id"),
    }


async def _fetch_db_events(db: Any, camera_ids: Optional[List[int]] = None) -> List[Any]:
    if db is None:
        return []
    if isinstance(db, (list, tuple)):
        return list(db)
    if isinstance(db, dict):
        for k in _ENTRIES_KEYS:
            if isinstance(db.get(k), (list, tuple)):
                return list(db.get(k))
        return []
    if hasattr(db, "execute"):
        try:
            from sqlalchemy import select as _select

            from app.models.entities import Event as _Event

            q = _select(_Event).order_by(_Event.created_at.asc(), _Event.id.asc())
            if camera_ids:
                q = q.where(_Event.camera_id.in_(list(camera_ids)))
            res = await db.execute(q)
            return list(res.scalars().all())
        except Exception:
            return []
    try:
        return list(db)  # type: ignore[arg-type]
    except Exception:
        return []


async def select_unacked_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Any]:
    data = _flatten_options(*args, **kwargs)
    db_obj = db if db is not None else session
    if db_obj is None:
        for k in ("db", "session", "database"):
            if data.get(k) is not None:
                db_obj = data.get(k)
                break
    inline = _resolve_entries(data)
    if not inline and db_obj is not None and hasattr(db_obj, "execute"):
        rows = await _fetch_db_events(db_obj, _resolve_camera_filter(data) or None)
        data = dict(data)
        data["entries"] = rows
    return select_unacked(data)


async def get_queue_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    data = _flatten_options(*args, **kwargs)
    db_obj = db if db is not None else session
    if db_obj is None:
        for k in ("db", "session", "database"):
            if data.get(k) is not None:
                db_obj = data.get(k)
                break
    inline = _resolve_entries(data)
    if not inline and db_obj is not None and hasattr(db_obj, "execute"):
        rows = await _fetch_db_events(db_obj, _resolve_camera_filter(data) or None)
        data = dict(data)
        data["entries"] = rows
    data.pop("db", None)
    data.pop("session", None)
    data.pop("database", None)
    return get_queue(data)


async def peek_next_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    queue = await get_queue_async(*args, db=db, session=session, limit=1, offset=0, **kwargs)
    items = queue.get("items") or []
    return dict(items[0]) if items else None


async def get_count_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    data = _flatten_options(*args, **kwargs)
    db_obj = db if db is not None else session
    if db_obj is None:
        for k in ("db", "session", "database"):
            if data.get(k) is not None:
                db_obj = data.get(k)
                break
    inline = _resolve_entries(data)
    if not inline and db_obj is not None and hasattr(db_obj, "execute"):
        rows = await _fetch_db_events(db_obj, _resolve_camera_filter(data) or None)
        data = dict(data)
        data["entries"] = rows
    data.pop("db", None)
    data.pop("session", None)
    data.pop("database", None)
    return get_count(data)


class UnackedQueueService:
    """Object-oriented facade over the unacked-queue helpers."""

    def select(self, *args: Any, **kwargs: Any) -> List[Any]:
        return select_unacked(*args, **kwargs)

    def queue(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_queue(*args, **kwargs)

    def next(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return peek_next(*args, **kwargs)

    def count(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_count(*args, **kwargs)

    async def select_async(self, *args: Any, **kwargs: Any) -> List[Any]:
        return await select_unacked_async(*args, **kwargs)

    async def queue_async(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await get_queue_async(*args, **kwargs)

    async def count_async(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await get_count_async(*args, **kwargs)

    # aliases
    get_queue = queue
    getQueue = queue  # noqa: N815
    list = select
    peek = next
    peek_next = next


unacked_queue_service = UnackedQueueService()
unacked_service = unacked_queue_service
queue_service = unacked_queue_service
service = unacked_queue_service
manager = unacked_queue_service
store = unacked_queue_service

# Compatibility aliases (function level).
unacked_queue = get_queue
get_unacked = get_queue
get_unacked_queue = get_queue
list_unacked = select_unacked
select_unacknowledged = select_unacked
get_pending = get_queue
pending_queue = get_queue
ack_queue = get_queue
oldest_unacked = peek_next
next_unacked = peek_next
peek_unacked = peek_next

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "DEFAULT_ORDER",
    "normalize_order",
    "is_unacked",
    "select_unacked",
    "get_queue",
    "peek_next",
    "get_next",
    "get_count",
    "select_unacked_async",
    "get_queue_async",
    "peek_next_async",
    "get_count_async",
    "UnackedQueueService",
    "unacked_queue_service",
]
