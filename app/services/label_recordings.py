"""Label-based recording search service.

Complements ``app.services.event_recordings`` (verified): that module finds
recordings covering a single event by **event id** only -- there is no
label-based recording search. This module adds exactly that: given an event
``label`` (e.g. ``"person"``), find the ``Recording`` rows whose
``[started_at, ended_at]`` interval overlaps any event carrying that label.

The sync entrypoint works without a DB when explicit ``events`` and
``recordings`` candidate lists are supplied. The async entrypoint accepts an
optional ``db``/``session`` (SQLAlchemy async session) and queries the
``Event``/``Recording`` tables. No edits to ``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.event_recordings import (
    DEFAULT_WINDOW_AFTER,
    DEFAULT_WINDOW_BEFORE,
    _event_to_dict,
    find_recordings_for_event,
)

LABEL_KEYS = (
    "label",
    "labels",
    "detection_label",
    "detectionLabel",
    "detection_labels",
    "object",
    "objects",
    "object_label",
    "objectLabel",
    "class",
    "classes",
)

EVENTS_KEYS = (
    "events",
    "events_list",
    "event_list",
    "eventRows",
    "candidates",
    "rows",
    "items",
)

MATCH_EXACT = "exact"
MATCH_CONTAINS = "contains"


def normalize_label(value: Any) -> Optional[str]:
    """Normalize a single label value (strip; ``None`` when empty)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            norm = normalize_label(item)
            if norm is not None:
                return norm
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "undefined", "all", "*"}:
        return None
    return text


def normalize_labels(value: Any) -> List[str]:
    """Normalize a label value into a list (supports comma-separated strings)."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            if isinstance(item, (list, tuple)):
                out.extend(normalize_labels(item))
                continue
            norm = normalize_label(item)
            if norm is not None:
                # Split comma-separated entries inside list items too.
                for part in norm.split(","):
                    piece = normalize_label(part)
                    if piece is not None and piece not in out:
                        out.append(piece)
        return out
    text = str(value)
    out = []
    for part in text.split(","):
        piece = normalize_label(part)
        if piece is not None and piece not in out:
            out.append(piece)
    return out


def _extract_label(*args: Any, **kwargs: Any) -> Optional[Any]:
    flat: Dict[str, Any] = {}
    for arg in args:
        if isinstance(arg, dict):
            flat.update(arg)
        elif isinstance(arg, str) and "label" not in flat:
            flat["label"] = arg
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "options",
                       "request", "filter", "filters", "event"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for key, val in nested.items():
                flat.setdefault(key, val)
    for key in LABEL_KEYS:
        if flat.get(key) is not None:
            return flat.get(key)
        lowered = {str(k).lower(): v for k, v in flat.items()}
        if key.lower() in lowered and lowered[key.lower()] is not None:
            return lowered[key.lower()]
    return None


def _extract_events(*args: Any, **kwargs: Any) -> List[Any]:
    for key in EVENTS_KEYS:
        val = kwargs.get(key)
        if isinstance(val, list):
            return list(val)
        if val is not None:
            return [val]
    for arg in args:
        if isinstance(arg, (list, tuple)) and arg:
            first = arg[0]
            if isinstance(first, dict):
                if any(k in first for k in ("label", "kind", "created_at", "camera_id")):
                    return list(arg)
            elif any(hasattr(first, attr) for attr in ("label", "camera_id", "created_at")):
                # ORM event rows (e.g. app.models.entities.Event).
                return list(arg)
    flat: Dict[str, Any] = {}
    for arg in args:
        if isinstance(arg, dict):
            flat.update(arg)
    flat.update(kwargs or {})
    for key in EVENTS_KEYS:
        val = flat.get(key)
        if isinstance(val, list):
            return list(val)
        if val is not None:
            return [val]
    return []


def _extract_recordings(*args: Any, **kwargs: Any) -> List[Any]:
    for key in ("recordings", "recordings_list", "candidates", "rows", "items",
                "recording_list", "recordingRows"):
        val = kwargs.get(key)
        if isinstance(val, list):
            return list(val)
        if val is not None:
            return [val]
    flat: Dict[str, Any] = {}
    for arg in args:
        if isinstance(arg, dict):
            flat.update(arg)
    flat.update(kwargs or {})
    for key in ("recordings", "recordings_list", "candidates", "rows", "items"):
        val = flat.get(key)
        if isinstance(val, list):
            return list(val)
        if val is not None:
            return [val]
    return []


def _event_label(event: Any) -> Optional[str]:
    if isinstance(event, dict):
        raw = event.get("label", event.get("object", event.get("class")))
        return normalize_label(raw)
    for attr in ("label", "object", "class_"):
        try:
            val = getattr(event, attr, None)
        except Exception:  # noqa: BLE001
            val = None
        norm = normalize_label(val)
        if norm is not None:
            return norm
    if hasattr(event, "model_dump"):
        try:
            data = dict(event.model_dump())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            data = {}
        return normalize_label(data.get("label"))
    return None


def event_matches_label(event: Any, label: Any, match: str = MATCH_EXACT) -> bool:
    """Return ``True`` when ``event`` carries the given label (case-insensitive)."""
    wanted = normalize_labels(label)
    if not wanted:
        return False
    actual = _event_label(event)
    if actual is None:
        return False
    mode = str(match or MATCH_EXACT).strip().lower()
    lowered = actual.lower()
    for want in wanted:
        target = want.lower()
        if mode in (MATCH_CONTAINS, "substring", "like", "partial", "fuzzy"):
            if target in lowered:
                return True
        elif lowered == target:
            return True
    return False


def filter_events_by_label(
    events: List[Any],
    label: Any,
    match: str = MATCH_EXACT,
    camera_id: Any = None,
) -> List[Any]:
    """Filter an event list down to the entries carrying ``label``."""
    wanted = normalize_labels(label)
    if not wanted:
        return []
    out: List[Any] = []
    for event in events or []:
        if not event_matches_label(event, wanted, match=match):
            continue
        if camera_id is not None:
            cam: Any = None
            if isinstance(event, dict):
                cam = event.get("camera_id", event.get("cameraId", event.get("camera")))
            else:
                try:
                    cam = getattr(event, "camera_id", None)
                except Exception:  # noqa: BLE001
                    cam = None
            try:
                if cam is not None and int(cam) != int(camera_id):  # type: ignore[arg-type]
                    continue
            except (TypeError, ValueError):
                pass
        out.append(event)
    return out


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default


def _resolve_match(*args: Any, **kwargs: Any) -> str:
    flat: Dict[str, Any] = {}
    for arg in args:
        if isinstance(arg, dict):
            flat.update(arg)
    flat.update(kwargs or {})
    raw = flat.get("match", flat.get("mode", flat.get("match_mode")))
    if raw is None:
        return MATCH_EXACT
    mode = str(raw).strip().lower()
    if mode in (MATCH_CONTAINS, "substring", "like", "partial", "fuzzy"):
        return MATCH_CONTAINS
    return MATCH_EXACT


def find_recordings_for_label(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Find recordings overlapping events carrying a given label (sync, no DB).

    Example::

        find_recordings_for_label("person", events=[...], recordings=[...])
        find_recordings_for_label(label="person", events=[...], recordings=[...])

    ``events``/``recordings`` are lists of dicts (or ORM objects). When no
    candidate lists are supplied the sync path returns ``[]``; use the async
    entrypoint with a ``db`` session for DB-backed lookup. Matching is
    case-insensitive; ``match="contains"`` enables substring matching.
    """
    raw_label = _extract_label(*args, **kwargs)
    labels = normalize_labels(raw_label)
    if not labels:
        return []
    match = _resolve_match(*args, **kwargs)
    flat: Dict[str, Any] = {}
    for arg in args:
        if isinstance(arg, dict):
            flat.update(arg)
    flat.update(kwargs or {})
    camera_id = flat.get("camera_id", flat.get("cameraId", flat.get("camera")))
    events = filter_events_by_label(
        _extract_events(*args, **kwargs), labels, match=match, camera_id=camera_id,
    )
    if not events:
        return []
    recordings = _extract_recordings(*args, **kwargs)
    if not recordings:
        return []
    before = flat.get("window_before", flat.get("windowBefore", flat.get("before")))
    after = flat.get("window_after", flat.get("windowAfter", flat.get("after")))
    both = flat.get("window", flat.get("padding", flat.get("tolerance")))
    if before is None:
        before = both if both is not None else DEFAULT_WINDOW_BEFORE
    if after is None:
        after = both if both is not None else DEFAULT_WINDOW_AFTER
    try:
        window_before = float(before)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        window_before = DEFAULT_WINDOW_BEFORE
    try:
        window_after = float(after)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        window_after = DEFAULT_WINDOW_AFTER

    seen: Dict[Any, Dict[str, Any]] = {}
    order: List[Any] = []
    for event in events:
        label_value = _event_label(event)
        # find_recordings_for_event accepts dict events; normalize ORM rows
        # via the shared helper (mirrors the event_recordings async path).
        event_dict = event if isinstance(event, dict) else _event_to_dict(event)
        try:
            hits = find_recordings_for_event(
                event_dict,
                recordings=recordings,
                window_before=window_before,
                window_after=window_after,
            )
        except Exception:  # noqa: BLE001
            continue
        event_id: Any = None
        if isinstance(event, dict):
            event_id = event.get("id", event.get("event_id"))
        else:
            try:
                event_id = getattr(event, "id", None)
            except Exception:  # noqa: BLE001
                event_id = None
        for hit in hits:
            info = dict(hit)
            if event_id is not None:
                info.setdefault("matched_event_id", event_id)
            if label_value is not None:
                info.setdefault("matched_label", label_value)
            key = info.get("id", info.get("recording_id"))
            if key is None:
                key = len(order)
            if key in seen:
                continue
            seen[key] = info
            order.append(key)
    out = [seen[key] for key in order]
    out.sort(key=lambda d: (d.get("start") or 0))
    limit = _coerce_int(flat.get("limit", flat.get("n", flat.get("count"))), None)
    if limit is not None and limit >= 0:
        out = out[:limit]
    return out


def count_recordings_for_label(*args: Any, **kwargs: Any) -> int:
    """Count recordings overlapping events with the given label."""
    return len(find_recordings_for_label(*args, **kwargs))


def clear(*args: Any, **kwargs: Any) -> int:
    """No-op for API symmetry (recordings live in the DB, not memory)."""
    return 0


# --- Async entrypoints (db-backed) -------------------------------------------


async def _fetch_events(db: Any, camera_id: Any = None) -> List[Any]:
    try:
        from sqlalchemy import select as _select  # lazy import

        from app.models.entities import Event as _Event  # lazy import

        stmt = _select(_Event)
        if camera_id is not None:
            try:
                stmt = stmt.where(_Event.camera_id == int(camera_id))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
        result = await db.execute(stmt)
        return list(result.scalars().all())
    except Exception:  # noqa: BLE001
        return []


async def _fetch_recordings(db: Any, camera_id: Any = None) -> List[Any]:
    try:
        from sqlalchemy import select as _select  # lazy import

        from app.models.entities import Recording as _Recording  # lazy import

        stmt = _select(_Recording)
        if camera_id is not None:
            try:
                stmt = stmt.where(_Recording.camera_id == int(camera_id))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
        result = await db.execute(stmt)
        return list(result.scalars().all())
    except Exception:  # noqa: BLE001
        return []


async def find_recordings_for_label_async(
    *args: Any,
    db: Any = None,
    session: Any = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """DB-backed label search: events with ``label`` -> overlapping recordings."""
    sess = db if db is not None else session
    flat: Dict[str, Any] = {}
    for arg in args:
        if isinstance(arg, dict):
            flat.update(arg)
    flat.update(kwargs or {})
    raw_label = _extract_label(*args, **kwargs)
    labels = normalize_labels(raw_label)
    if not labels:
        return []
    match = _resolve_match(*args, **kwargs)
    camera_id = flat.get("camera_id", flat.get("cameraId", flat.get("camera")))
    events = _extract_events(*args, **kwargs)
    if not events and sess is not None:
        events = await _fetch_events(sess, camera_id)
    recordings = _extract_recordings(*args, **kwargs)
    if not recordings and sess is not None:
        recordings = await _fetch_recordings(sess, camera_id)
    if not events or not recordings:
        return []
    merged = dict(flat)
    merged["label"] = labels
    merged["labels"] = labels
    merged["events"] = events
    merged["recordings"] = recordings
    merged["match"] = match
    return find_recordings_for_label(merged)


async def get_recordings_for_label_async(
    *args: Any,
    db: Any = None,
    session: Any = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Alias for :func:`find_recordings_for_label_async`."""
    return await find_recordings_for_label_async(*args, db=db, session=session, **kwargs)


class LabelRecordingsService:
    """Object-oriented facade over the label-based recording search."""

    def find(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return find_recordings_for_label(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return find_recordings_for_label(*args, **kwargs)

    def count(self, *args: Any, **kwargs: Any) -> int:
        return count_recordings_for_label(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    # aliases
    find_recordings = find
    get_recordings = get
    recordings_for_label = get
    recordings = get
    for_label = get


label_recordings_service = LabelRecordingsService()
label_recording_service = label_recordings_service
service = label_recordings_service
manager = label_recordings_service
store = label_recordings_service
finder = label_recordings_service
label_finder = label_recordings_service


# Compatibility aliases (function level).
get_recordings_for_label = find_recordings_for_label
recordings_for_label = find_recordings_for_label
find_recordings = find_recordings_for_label
get_label_recordings = find_recordings_for_label
list_label_recordings = find_recordings_for_label
label_recordings = find_recordings_for_label
find = find_recordings_for_label
get = find_recordings_for_label

__all__ = [
    "find_recordings_for_label",
    "get_recordings_for_label",
    "count_recordings_for_label",
    "filter_events_by_label",
    "event_matches_label",
    "normalize_label",
    "normalize_labels",
    "clear",
    "find_recordings_for_label_async",
    "get_recordings_for_label_async",
    "LabelRecordingsService",
    "label_recordings_service",
]
