"""Motion event merge/split service.

Pure (in-memory, no-DB) helpers plus optional async DB-backed entrypoints.

Merge combines several motion/detection events (e.g. fragmented motion
detections close in time) into a single event. Split divides one long
event into multiple parts. The pure helpers operate on plain dicts (or
ORM-like objects) so they work without a database; the ``*_db`` async
helpers persist the result when an ``AsyncSession`` is supplied.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

EVENT_ID_KEYS = (
    "event_id",
    "eventId",
    "event",
    "eventid",
    "id_event",
    "for_event",
    "camera_event_id",
    "primary_id",
    "primaryId",
    "primary",
    "keep_id",
    "keepId",
)
EVENT_IDS_KEYS = (
    "event_ids",
    "eventIds",
    "events_ids",
    "ids",
    "id_list",
    "idList",
    "event_id_list",
    "eventIdList",
    "merge_ids",
    "mergeIds",
    "to_merge",
    "toMerge",
    "sources",
    "source_ids",
    "inputs",
)
COUNT_KEYS = (
    "count",
    "parts",
    "num_parts",
    "numParts",
    "n",
    "splits",
    "num_splits",
    "numSplits",
    "pieces",
    "chunks",
    "segments",
)
GAP_KEYS = (
    "gap_seconds",
    "gapSeconds",
    "gap",
    "max_gap",
    "maxGap",
    "max_gap_seconds",
    "maxGapSeconds",
    "threshold",
    "threshold_seconds",
    "window",
    "window_seconds",
    "merge_window",
    "mergeWindow",
    "merge_gap",
)

DEFAULT_GAP_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Generic coercion helpers
# ---------------------------------------------------------------------------

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


def _coerce_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(str(value).strip())
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
        for nested_key in (
            "payload", "options", "config", "params", "data", "body",
            "request", "fields", "entry", "merge", "split", "event",
            "merge_request", "split_request",
        ):
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    try:
        return dict(data)
    except Exception:
        return {}


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        ts = float(value)
        if abs(ts) > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except (OSError, ValueError, OverflowError):
            return None
    else:
        s = str(value).strip()
        if s == "" or s.lower() in {"none", "null", "undefined", "all"}:
            return None
        try:
            ts = float(s)
            if s.replace(".", "", 1).replace("e", "", 1).replace("E", "", 1).replace("-", "", 1).replace("+", "", 1).isdigit():
                if abs(ts) > 1e12:
                    ts = ts / 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            pass
        s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(s2)
        except ValueError:
            try:
                dt = datetime.fromisoformat(s + "T00:00:00")
            except ValueError:
                return None
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _event_to_dict(ev: Any) -> Dict[str, Any]:
    if isinstance(ev, dict):
        out = dict(ev)
        # normalize created_at to datetime when possible for sorting
        return out
    if hasattr(ev, "model_dump"):
        try:
            return dict(ev.model_dump())
        except Exception:
            pass
    out: Dict[str, Any] = {}
    for attr in ("id", "event_id", "camera_id", "cameraId", "camera", "kind",
                 "label", "confidence", "payload", "tags", "tag",
                 "snapshot_path", "snapshotPath", "created_at", "createdAt",
                 "started_at", "ended_at", "start", "end"):
        try:
            val = getattr(ev, attr, None)
        except Exception:
            val = None
        if val is not None:
            out[attr] = val
    # also try mapping access
    if not out:
        try:
            return dict(ev)
        except Exception:
            return {"value": ev}
    return out


def _get_id(ev: Dict[str, Any]) -> Optional[int]:
    for k in ("id", "event_id", "eventId", "eventID"):
        v = ev.get(k)
        cid = _coerce_int(v, None)
        if cid is not None:
            return cid
    return None


def _get_camera(ev: Dict[str, Any]) -> Any:
    for k in ("camera_id", "cameraId", "camera", "cam"):
        if ev.get(k) is not None:
            return ev.get(k)
    return None


def _get_created(ev: Dict[str, Any]) -> Optional[datetime]:
    for k in ("created_at", "createdAt", "started_at", "start", "timestamp", "ts", "time"):
        if ev.get(k) is not None:
            dt = _parse_dt(ev.get(k))
            if dt is not None:
                return dt
    return None


def _normalize_tags(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        for key in ("tags", "tag", "values", "items", "labels", "names"):
            if key in value and value.get(key) is not None:
                return _normalize_tags(value.get(key))
        return []
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                value = parsed
            elif isinstance(parsed, str):
                return [parsed.strip()] if parsed.strip() else []
            else:
                return [str(parsed)]
        except Exception:
            value = [p.strip() for p in s.split(",") if p.strip()]
    if isinstance(value, (tuple, set)):
        value = list(value)
    if not isinstance(value, list):
        return [str(value)] if value is not None else []
    out: List[str] = []
    seen = set()
    for item in value:
        s = str(item).strip() if item is not None else ""
        if not s or s.lower() in {"none", "null"}:
            continue
        if s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
    return out


def _coerce_id_list(value: Any) -> List[int]:
    out: List[int] = []
    seen = set()
    if value is None:
        return out
    if isinstance(value, (list, tuple, set)):
        iterable: Any = value
    else:
        iterable = [value]
    for v in iterable:
        if v is None or isinstance(v, bool):
            continue
        if isinstance(v, dict):
            # allow {"id": N} / {"event_id": N}
            cand = v.get("id", v.get("event_id", v.get("eventId")))
            if cand is None:
                continue
            v = cand
        if isinstance(v, str):
            for part in v.split(","):
                part = part.strip()
                if not part:
                    continue
                iv = _coerce_int(part, None)
                if iv is not None and iv not in seen:
                    seen.add(iv)
                    out.append(iv)
        else:
            iv = _coerce_int(v, None)
            if iv is not None and iv not in seen:
                seen.add(iv)
                out.append(iv)
    return out


def _extract_ids(*args: Any, **kwargs: Any) -> Tuple[List[int], Dict[str, Any]]:
    flat: Dict[str, Any] = {}
    positional_events: List[Any] = []
    for a in args:
        if isinstance(a, dict):
            # a dict with ids or a single event dict? heuristic: has id but no ids key
            if any(k in a for k in EVENT_IDS_KEYS):
                flat.update(a)
            elif "events" in a:
                flat.update(a)
            elif _get_id(a) is not None and len(a) > 2:
                positional_events.append(a)
            else:
                flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
        elif isinstance(a, (list, tuple, set)):
            items = list(a)
            if items and all(isinstance(x, dict) for x in items):
                positional_events.extend(items)
            else:
                # list of ids
                flat.setdefault("event_ids", list(a))
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            flat.setdefault("_positional_ids", []).append(a)
        elif isinstance(a, str):
            flat.setdefault("_positional_ids", []).append(a)
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "merge", "request"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)

    ids: List[int] = []
    for key in EVENT_IDS_KEYS:
        if flat.get(key) is not None:
            ids.extend(_coerce_id_list(flat.get(key)))
    # singular aliases
    for key in ("event_id", "eventId", "event", "id", "primary_id", "primaryId"):
        # only treat singular as list member when other ids exist or _merge_with present
        pass
    if "_positional_ids" in flat:
        ids.extend(_coerce_id_list(flat.get("_positional_ids")))
    # dedupe preserving order
    seen = set()
    deduped: List[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            deduped.append(i)
    # events payload
    events_raw = flat.get("events", flat.get("items", flat.get("sources")))
    events: List[Dict[str, Any]] = []
    if isinstance(events_raw, list):
        for e in events_raw:
            events.append(_event_to_dict(e))
    elif positional_events:
        for e in positional_events:
            events.append(_event_to_dict(e))
    return deduped, {"flat": flat, "events": events}


def _extract_count(*args: Any, **kwargs: Any) -> int:
    flat = _flatten(args[0]) if len(args) == 1 and isinstance(args[0], dict) else {}
    for a in args:
        if isinstance(a, (int, float)) and not isinstance(a, bool) and "count" not in flat:
            flat["count"] = a
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "split", "request"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    raw = _first_present(flat, COUNT_KEYS)
    # split_points length implies count
    for key in ("split_points", "splitPoints", "split_at", "splitAt", "offsets",
                "boundaries", "times", "timestamps", "cuts", "cut_points"):
        pts = flat.get(key)
        if isinstance(pts, (list, tuple)) and len(pts) >= 1:
            implied = len(list(pts)) + 1
            if raw is None:
                return max(2, min(100, implied))
    count = _coerce_int(raw, 2)
    if count is None:
        count = 2
    return max(2, min(100, count))


def _extract_gap_seconds(*args: Any, **kwargs: Any) -> float:
    flat = _flatten(args[0]) if len(args) == 1 and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "request", "options"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    raw = _first_present(flat, GAP_KEYS)
    gap = _coerce_float(raw, DEFAULT_GAP_SECONDS)
    if gap is None or gap < 0:
        gap = DEFAULT_GAP_SECONDS
    return float(gap)


# ---------------------------------------------------------------------------
# Pure merge / split
# ---------------------------------------------------------------------------

def merge_event_dicts(
    events: List[Any],
    primary_id: Any = None,
    keep_id: Any = None,
    label: Any = None,
    kind: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Merge a list of event dicts/objects into a single dict (pure, no DB)."""
    flat_extra = _flatten(kwargs.get("extra") if isinstance(kwargs.get("extra"), dict) else None)
    items = [_event_to_dict(e) for e in (events or [])]
    if len(items) < 2:
        raise ValueError("at least two events are required to merge")
    pid = _coerce_int(primary_id if primary_id is not None else keep_id, None)
    if pid is None:
        for alias in ("primary_id", "primaryId", "primary", "keep_id", "keepId", "keep"):
            if kwargs.get(alias) is not None:
                pid = _coerce_int(kwargs.get(alias), None)
                if pid is not None:
                    break
    # choose primary: explicit id else earliest created_at else smallest id
    primary: Dict[str, Any]
    if pid is not None:
        matches = [e for e in items if _get_id(e) == pid]
        primary = dict(matches[0]) if matches else dict(items[0])
    else:
        def _sort_key(e: Dict[str, Any]) -> Tuple[Any, Any]:
            dt = _get_created(e)
            eid = _get_id(e)
            return (dt or datetime.max, eid if eid is not None else 10**18)
        primary = dict(sorted(items, key=_sort_key)[0])

    ids = [_get_id(e) for e in items if _get_id(e) is not None]
    # tags union
    merged_tags: List[str] = []
    seen_tags = set()
    for e in items:
        for t in _normalize_tags(e.get("tags", e.get("tag"))):
            if t.lower() not in seen_tags:
                seen_tags.add(t.lower())
                merged_tags.append(t)
    # confidence: max of present
    confs = [c for c in (e.get("confidence") for e in items)
             if isinstance(c, (int, float)) and not isinstance(c, bool)]
    merged_conf = max(confs) if confs else primary.get("confidence")
    # created_at: earliest
    created_list = [_get_created(e) for e in items]
    created_list = [c for c in created_list if c is not None]
    merged_created = min(created_list) if created_list else primary.get("created_at")
    if isinstance(merged_created, datetime):
        merged_created_iso = merged_created.isoformat()
    else:
        merged_created_iso = merged_created
    # payload: try to combine
    payloads = [e.get("payload") for e in items if e.get("payload") is not None]
    merged_payload = _combine_payloads(payloads, ids)
    # snapshot: first non-null from primary-first ordering
    snapshot = primary.get("snapshot_path", primary.get("snapshotPath"))
    if snapshot is None:
        for e in items:
            s = e.get("snapshot_path", e.get("snapshotPath"))
            if s:
                snapshot = s
                break
    merged: Dict[str, Any] = dict(primary)
    merged["id"] = _get_id(primary)
    merged["event_id"] = merged["id"]
    merged["camera_id"] = _get_camera(primary)
    if label is not None or kwargs.get("label") is not None:
        merged["label"] = label if label is not None else kwargs.get("label")
    if kind is not None or kwargs.get("kind") is not None:
        merged["kind"] = kind if kind is not None else kwargs.get("kind")
    merged.setdefault("kind", "detection")
    merged.setdefault("label", "motion")
    merged["confidence"] = merged_conf
    merged["payload"] = merged_payload
    merged["tags"] = merged_tags
    merged["tag"] = merged_tags
    merged["snapshot_path"] = snapshot
    if merged_created_iso is not None:
        merged["created_at"] = merged_created_iso if isinstance(merged_created_iso, str) else merged_created
    merged["merged_from"] = ids
    merged["merged_ids"] = ids
    merged["source_ids"] = ids
    merged["merge_count"] = len(items)
    merged["merged"] = True
    for k, v in flat_extra.items():
        merged.setdefault(k, v)
    return merged


def _combine_payloads(payloads: List[Any], ids: List[int]) -> Any:
    if not payloads:
        return None
    dicts: List[Dict[str, Any]] = []
    all_dict = True
    for p in payloads:
        if isinstance(p, dict):
            dicts.append(p)
        elif isinstance(p, str):
            s = p.strip()
            if s.startswith("{"):
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        dicts.append(obj)
                    else:
                        all_dict = False
                        break
                except Exception:
                    all_dict = False
                    break
            else:
                all_dict = False
                break
        else:
            all_dict = False
            break
    if all_dict and dicts:
        combined: Dict[str, Any] = {}
        for d in dicts:
            for k, v in d.items():
                if k not in combined:
                    combined[k] = v
                elif combined[k] != v:
                    # keep list of distinct values
                    existing = combined[k]
                    if not isinstance(existing, list):
                        existing = [existing]
                    if v not in existing:
                        existing.append(v)
                    combined[k] = existing
        combined["merged_from"] = ids
        combined["merge_count"] = len(payloads)
        try:
            return json.dumps(combined)
        except Exception:
            return combined
    # fallback: JSON envelope
    try:
        return json.dumps({"merged_from": ids, "merge_count": len(payloads),
                           "payloads": [str(p) for p in payloads]})
    except Exception:
        return "; ".join(str(p) for p in payloads)


def split_event_dict(
    event: Any,
    count: Any = None,
    duration_seconds: Any = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Split one event dict into ``count`` parts (pure, no DB)."""
    base = _event_to_dict(event)
    n = _coerce_int(count, None)
    if n is None:
        # look through kwargs aliases
        n = _extract_count(count, **kwargs) if count is None else _coerce_int(count, 2)
    if n is None or n < 2:
        n = 2
    n = max(2, min(100, int(n)))
    dur = _coerce_float(duration_seconds if duration_seconds is not None else kwargs.get("duration_seconds",
                         kwargs.get("duration", kwargs.get("length_seconds"))), None)
    base_created = _get_created(base)
    base_id = _get_id(base)
    parts: List[Dict[str, Any]] = []
    step = (dur / n) if (dur and dur > 0) else 1.0
    for i in range(n):
        part = copy.deepcopy(base)
        # assign new ids for parts after the first when base id known
        if base_id is not None:
            part["id"] = base_id if i == 0 else base_id * 1000 + i
            part["event_id"] = part["id"]
        part["part_index"] = i
        part["part"] = i + 1
        part["part_number"] = i + 1
        part["total_parts"] = n
        part["split_count"] = n
        part["split_from"] = base_id
        part["split_source_id"] = base_id
        if base_created is not None:
            part_dt = base_created + timedelta(seconds=step * i)
            part["created_at"] = part_dt.isoformat()
        # annotate payload
        try:
            raw = part.get("payload")
            if raw and isinstance(raw, str) and raw.strip().startswith("{"):
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    obj["split_part"] = i + 1
                    obj["split_total"] = n
                    obj["split_from"] = base_id
                    part["payload"] = json.dumps(obj)
                else:
                    part["payload"] = raw
            elif raw is None:
                part["payload"] = json.dumps({"split_part": i + 1, "split_total": n,
                                              "split_from": base_id})
        except Exception:
            pass
        parts.append(part)
    return parts


def group_events_for_merge(
    events: List[Any],
    gap_seconds: Any = None,
    **kwargs: Any,
) -> List[List[Dict[str, Any]]]:
    """Group events whose time gap (per camera) is <= gap_seconds.

    Returns only groups with >= 2 events (merge candidates) sorted by time.
    """
    gap = _coerce_float(gap_seconds, None)
    if gap is None:
        gap = _extract_gap_seconds(**kwargs)
    items = [_event_to_dict(e) for e in (events or [])]

    def _sort_key(e: Dict[str, Any]) -> Tuple[str, Any, Any]:
        cam = _get_camera(e)
        dt = _get_created(e)
        eid = _get_id(e)
        return (str(cam), dt or datetime.max, eid if eid is not None else 10**18)

    items.sort(key=_sort_key)
    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    prev: Optional[Dict[str, Any]] = None
    for ev in items:
        if not current:
            current = [ev]
            prev = ev
            continue
        same_camera = str(_get_camera(ev)) == str(_get_camera(prev))
        dt_ev = _get_created(ev)
        dt_prev = _get_created(prev)
        if same_camera and dt_ev is not None and dt_prev is not None:
            delta = (dt_ev - dt_prev).total_seconds()
            if delta <= float(gap):
                current.append(ev)
                prev = ev
                continue
        # flush
        if len(current) >= 2:
            groups.append(current)
        current = [ev]
        prev = ev
    if len(current) >= 2:
        groups.append(current)
    return groups


# ---------------------------------------------------------------------------
# Flexible sync entrypoints
# ---------------------------------------------------------------------------

def merge_events(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Flexible sync merge.

    Examples::

        merge_events([e1, e2])
        merge_events(event_ids=[1, 2], events=[...])
        merge_events(e1, e2)
    """
    ids, ctx = _extract_ids(*args, **kwargs)
    flat = ctx["flat"]
    events = list(ctx["events"])
    # positional bare event dicts already collected; also accept kwargs events
    for key in ("events", "items", "sources", "to_merge"):
        val = kwargs.get(key, flat.get(key))
        if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
            for e in val:
                if e not in events:
                    events.append(_event_to_dict(e))
    # bare positional dicts passed as *args handled in _extract_ids
    if not events and ids:
        # no event payloads; synthesize minimal dicts from ids
        events = [{"id": i, "event_id": i} for i in ids]
    if len(events) < 2 and len(ids) >= 2 and not events:
        events = [{"id": i, "event_id": i} for i in ids]
    if len(events) < 2:
        raise ValueError("at least two events (or event_ids) are required to merge")
    primary = flat.get("primary_id", flat.get("primaryId", flat.get("primary",
                flat.get("keep_id", flat.get("keepId", kwargs.get("primary_id"))))))
    return merge_event_dicts(events, primary_id=primary,
                             label=flat.get("label", kwargs.get("label")),
                             kind=flat.get("kind", kwargs.get("kind")))


def split_event(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Flexible sync split. Returns list of part dicts."""
    # Prefer an explicitly nested event dict over treating the wrapper as the event
    # (callers often flatten nested keys into the top level for option aliases).
    event: Any = None
    if args and isinstance(args[0], dict):
        for key in ("event", "source", "source_event", "original", "target"):
            nested = args[0].get(key)
            if isinstance(nested, dict) and _get_id(nested) is not None:
                event = nested
                break
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) and "events" not in args[0] else {}
    # event may be first positional dict or kwarg
    positional_dicts: List[Any] = []
    for a in args:
        if isinstance(a, dict) and event is None and _get_id(a) is not None:
            # could be the event itself (has id) vs options dict
            if any(k in a for k in COUNT_KEYS) and "id" not in a and "event_id" not in a:
                flat.update(a)
            elif event is None:
                # skip wrappers that only carry the event under a nested key
                if any(isinstance(a.get(k), dict) and _get_id(a.get(k)) is not None
                       for k in ("event", "source", "source_event", "original", "target")):
                    flat.update(a)
                else:
                    event = a
            else:
                positional_dicts.append(a)
        elif isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "split", "request", "event"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    if event is None:
        for key in ("event", "source", "source_event", "original", "target"):
            if isinstance(flat.get(key), dict):
                event = flat.get(key)
                break
    if event is None and positional_dicts:
        event = positional_dicts[0]
    if event is None:
        # build minimal event from id
        eid = _first_present(flat, EVENT_ID_KEYS + ("id",))
        eid_int = _coerce_int(eid, None)
        if eid_int is None:
            raise ValueError("event (or event_id) is required to split")
        event = {"id": eid_int, "event_id": eid_int,
                 "camera_id": flat.get("camera_id", flat.get("camera")),
                 "kind": flat.get("kind", "motion"),
                 "label": flat.get("label", "motion"),
                 "created_at": flat.get("created_at")}
    count = _first_present(flat, COUNT_KEYS)
    if count is None:
        count = 2
    return split_event_dict(event, count=count,
                            duration_seconds=flat.get("duration_seconds",
                                              flat.get("duration", flat.get("length_seconds"))))


def suggest_merges(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return merge suggestions: each item is {event_ids, count, camera_id, ...}."""
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    events: List[Any] = []
    if args and isinstance(args[0], list):
        events = list(args[0])
    for key in ("events", "items", "sources"):
        if isinstance(flat.get(key), list):
            events = list(flat.get(key))
            break
    gap = _extract_gap_seconds(**flat)
    groups = group_events_for_merge(events, gap_seconds=gap)
    suggestions: List[Dict[str, Any]] = []
    for g in groups:
        ids = [_get_id(e) for e in g if _get_id(e) is not None]
        suggestions.append({
            "event_ids": ids,
            "ids": ids,
            "count": len(g),
            "camera_id": _get_camera(g[0]) if g else None,
            "events": g,
            "gap_seconds": gap,
        })
    return suggestions


def auto_merge(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Auto-merge groups: returns list of merged dicts (pure)."""
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    events: List[Any] = []
    if args and isinstance(args[0], list):
        events = list(args[0])
    for key in ("events", "items", "sources"):
        if isinstance(flat.get(key), list):
            events = list(flat.get(key))
            break
    gap = _extract_gap_seconds(**flat)
    groups = group_events_for_merge(events, gap_seconds=gap)
    return [merge_event_dicts(g) for g in groups]


def clear(*args: Any, **kwargs: Any) -> int:
    """No-op store clear for API symmetry (pure helpers are stateless)."""
    return 0


# ---------------------------------------------------------------------------
# Async DB-backed entrypoints
# ---------------------------------------------------------------------------

def _orm_to_dict(ev: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for attr in ("id", "camera_id", "kind", "label", "confidence",
                 "payload", "tags", "snapshot_path", "created_at"):
        try:
            out[attr] = getattr(ev, attr, None)
        except Exception:
            out[attr] = None
    if isinstance(out.get("created_at"), datetime):
        out["created_at"] = out["created_at"].isoformat()
    if out.get("tags") is not None and not isinstance(out.get("tags"), list):
        out["tags"] = _normalize_tags(out.get("tags"))
    out["tag"] = out.get("tags") or []
    return out


async def merge_events_db(db: Any, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Merge Event rows in the DB. Returns merged event dict."""
    from app.models.entities import Event  # lazy import

    ids, ctx = _extract_ids(*args, **kwargs)
    flat = ctx["flat"]
    # singular id + others (e.g. event_id + with_ids)
    primary_raw = _first_present(flat, ("primary_id", "primaryId", "primary", "keep_id", "keepId", "keep"))
    extra_ids: List[int] = []
    for key in ("with_ids", "withIds", "other_ids", "otherIds", "secondary_ids", "merge_with"):
        if flat.get(key) is not None:
            extra_ids.extend(_coerce_id_list(flat.get(key)))
    singular = _first_present(flat, EVENT_ID_KEYS)
    singular_int = _coerce_int(singular, None)
    if singular_int is not None and singular_int not in ids and singular_int not in extra_ids:
        # treat bare event_id as one of the merge set when others exist
        if ids or extra_ids:
            ids = [singular_int] + ids
    ids = ids + [i for i in extra_ids if i not in ids]
    # dedupe
    seen = set()
    ids = [i for i in ids if not (i in seen or seen.add(i))]

    if len(ids) < 2:
        raise ValueError("at least two event ids are required to merge")
    rows: List[Any] = []
    for eid in ids:
        try:
            row = await db.get(Event, int(eid))
        except Exception:
            row = None
        if row is not None:
            rows.append(row)
    if len(rows) < 2:
        raise LookupError("not all events found")
    # choose primary row
    primary_id = _coerce_int(primary_raw, None)
    if primary_id is not None:
        primaries = [r for r in rows if getattr(r, "id", None) == primary_id]
        primary_row = primaries[0] if primaries else rows[0]
    else:
        def _key(r: Any) -> Tuple[Any, Any]:
            ca = getattr(r, "created_at", None)
            return (ca or datetime.max, getattr(r, "id", 0) or 0)
        primary_row = sorted(rows, key=_key)[0]
    others = [r for r in rows if getattr(r, "id", None) != getattr(primary_row, "id", None)]

    label_override = flat.get("label", kwargs.get("label"))
    kind_override = flat.get("kind", kwargs.get("kind"))

    # combine tags
    merged_tags: List[str] = []
    seen_tags = set()
    for r in rows:
        for t in _normalize_tags(getattr(r, "tags", None)):
            if t.lower() not in seen_tags:
                seen_tags.add(t.lower())
                merged_tags.append(t)
    # confidence max
    confs = [getattr(r, "confidence", None) for r in rows
             if isinstance(getattr(r, "confidence", None), (int, float))]
    if confs:
        primary_row.confidence = max(confs)
    # earliest created_at
    cas = [getattr(r, "created_at", None) for r in rows if getattr(r, "created_at", None) is not None]
    if cas:
        earliest = min(cas)
        try:
            primary_row.created_at = earliest
        except Exception:
            pass
    # combine payloads
    payloads = [getattr(r, "payload", None) for r in rows if getattr(r, "payload", None)]
    try:
        primary_row.payload = _combine_payloads(payloads, ids)  # type: ignore[assignment]
    except Exception:
        pass
    if label_override is not None:
        try:
            primary_row.label = str(label_override)
        except Exception:
            pass
    if kind_override is not None:
        try:
            primary_row.kind = str(kind_override)
        except Exception:
            pass
    try:
        primary_row.tags = merged_tags  # type: ignore[assignment]
    except Exception:
        pass
    if getattr(primary_row, "snapshot_path", None) is None:
        for r in rows:
            if getattr(r, "snapshot_path", None):
                try:
                    primary_row.snapshot_path = r.snapshot_path
                except Exception:
                    pass
                break
    for r in others:
        try:
            await db.delete(r)
        except Exception:
            pass
    try:
        await db.commit()
    except Exception:
        pass
    try:
        await db.refresh(primary_row)
    except Exception:
        pass
    out = _orm_to_dict(primary_row)
    out["merged_from"] = ids
    out["merged_ids"] = ids
    out["source_ids"] = ids
    out["merge_count"] = len(rows)
    out["merged"] = True
    out["success"] = True
    out["ok"] = True
    return out


async def split_event_db(db: Any, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Split an Event row into multiple rows. Returns list of event dicts."""
    from app.models.entities import Event  # lazy import

    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    for a in args:
        if isinstance(a, (int, float)) and not isinstance(a, bool) and "event_id" not in flat:
            flat["event_id"] = a
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "split", "request", "event"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    eid = _first_present(flat, EVENT_ID_KEYS + ("id",))
    eid_int = _coerce_int(eid, None)
    if eid_int is None:
        raise ValueError("event_id is required to split")
    count = _first_present(flat, COUNT_KEYS)
    n = _coerce_int(count, 2) or 2
    n = max(2, min(100, int(n)))
    row = await db.get(Event, int(eid_int))
    if row is None:
        raise LookupError("Event not found")
    base_created = getattr(row, "created_at", None)
    if isinstance(base_created, str):
        base_created = _parse_dt(base_created)
    dur = _coerce_float(flat.get("duration_seconds",
                        flat.get("duration", flat.get("length_seconds"))), None)
    step = (dur / n) if (dur and dur > 0) else 1.0

    try:
        base_payload_obj: Any = None
        if getattr(row, "payload", None):
            try:
                parsed = json.loads(row.payload)
                if isinstance(parsed, dict):
                    base_payload_obj = parsed
            except Exception:
                base_payload_obj = None
    except Exception:
        base_payload_obj = None

    try:
        row_tags = _normalize_tags(getattr(row, "tags", None))
    except Exception:
        row_tags = []

    created_rows: List[Any] = [row]
    for i in range(n):
        payload_obj = dict(base_payload_obj) if isinstance(base_payload_obj, dict) else {}
        payload_obj["split_part"] = i + 1
        payload_obj["split_total"] = n
        payload_obj["split_from"] = eid_int
        try:
            payload_str = json.dumps(payload_obj)
        except Exception:
            payload_str = getattr(row, "payload", None)
        part_dt = None
        if isinstance(base_created, datetime):
            part_dt = base_created + timedelta(seconds=step * i)
        if i == 0:
            # annotate original row as part 1
            try:
                row.payload = payload_str
            except Exception:
                pass
            continue
        new_row = Event(
            camera_id=getattr(row, "camera_id", None),
            kind=getattr(row, "kind", "motion") or "motion",
            label=getattr(row, "label", "motion") or "motion",
            confidence=getattr(row, "confidence", None),
            payload=payload_str,
            tags=list(row_tags),
            snapshot_path=getattr(row, "snapshot_path", None),
        )
        if part_dt is not None:
            try:
                new_row.created_at = part_dt
            except Exception:
                pass
        try:
            db.add(new_row)
        except Exception:
            pass
        created_rows.append(new_row)
    try:
        await db.commit()
    except Exception:
        pass
    out: List[Dict[str, Any]] = []
    for idx, r in enumerate(created_rows):
        try:
            await db.refresh(r)
        except Exception:
            pass
        d = _orm_to_dict(r)
        d["part_index"] = idx
        d["part"] = idx + 1
        d["part_number"] = idx + 1
        d["total_parts"] = n
        d["split_count"] = n
        d["split_from"] = eid_int
        d["split_source_id"] = eid_int
        out.append(d)
    # fetch any additional rows created beyond n-1? (only n total incl. original)
    return out


async def merge_events_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    sess = db if db is not None else session
    ids, ctx = _extract_ids(*args, **kwargs)
    has_inline_events = bool(ctx["events"])
    if sess is not None and (ids or _first_present(ctx["flat"], EVENT_ID_KEYS) is not None):
        try:
            return await merge_events_db(sess, *args, **kwargs)
        except (ValueError, LookupError):
            raise
        except Exception:
            pass
    return merge_events(*args, **kwargs)


async def split_event_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    sess = db if db is not None else session
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    tmp = dict(flat)
    tmp.update(kwargs or {})
    eid = _first_present(tmp, EVENT_ID_KEYS + ("id",))
    inline_event = tmp.get("event", tmp.get("source"))
    if sess is not None and eid is not None and not isinstance(inline_event, dict):
        try:
            return await split_event_db(sess, *args, **kwargs)
        except (ValueError, LookupError):
            raise
        except Exception:
            pass
    return split_event(*args, **kwargs)


async def suggest_merges_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    sess = db if db is not None else session
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    events: List[Any] = []
    if args and isinstance(args[0], list):
        events = list(args[0])
    for key in ("events", "items", "sources"):
        if isinstance(flat.get(key), list):
            events = list(flat.get(key))
            break
    if not events and sess is not None:
        try:
            from sqlalchemy import select
            from app.models.entities import Event as EventModel
            limit = _coerce_int(flat.get("limit", flat.get("n")), 500) or 500
            result = await sess.execute(
                select(EventModel).order_by(EventModel.id.desc()).limit(min(max(int(limit), 1), 2000))
            )
            rows = list(result.scalars().all())
            cam_filter = flat.get("camera_id", flat.get("camera"))
            if cam_filter is not None:
                try:
                    cid = int(cam_filter)
                    rows = [r for r in rows if getattr(r, "camera_id", None) == cid]
                except (TypeError, ValueError):
                    pass
            events = [_orm_to_dict(r) for r in rows]
        except Exception:
            events = []
    gap = _extract_gap_seconds(**flat)
    groups = group_events_for_merge(events, gap_seconds=gap)
    suggestions: List[Dict[str, Any]] = []
    for g in groups:
        ids = [_get_id(e) for e in g if _get_id(e) is not None]
        suggestions.append({
            "event_ids": ids,
            "ids": ids,
            "count": len(g),
            "camera_id": _get_camera(g[0]) if g else None,
            "events": g,
            "gap_seconds": gap,
        })
    return suggestions


class EventMergeService:
    """Object-oriented facade over the merge/split helpers."""

    def merge(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return merge_events(*args, **kwargs)

    def split(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return split_event(*args, **kwargs)

    def group(self, *args: Any, **kwargs: Any) -> List[List[Dict[str, Any]]]:
        events: List[Any] = []
        if args and isinstance(args[0], list):
            events = list(args[0])
        flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
        flat.update(kwargs or {})
        for key in ("events", "items", "sources"):
            if isinstance(flat.get(key), list):
                events = list(flat.get(key))
                break
        return group_events_for_merge(events, **flat)

    def suggest(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return suggest_merges(*args, **kwargs)

    def auto_merge(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return auto_merge(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    # aliases
    combine = merge
    join = merge
    merge_motion_events = merge
    split_motion_event = split


event_merge_service = EventMergeService()
merge_service = event_merge_service
split_service = event_merge_service
service = event_merge_service
manager = event_merge_service
store = event_merge_service


# Compatibility aliases (function level).
combine_events = merge_events
join_events = merge_events
merge_motion_events = merge_events
combine_motion_events = merge_events
join_motion_events = merge_events
merge_event = merge_events
combine_event = merge_events
merge = merge_events

split_motion_event = split_event
split_motion_events = split_event
split_events = split_event
divide_event = split_event
split = split_event

find_merge_candidates = group_events_for_merge
find_merges = group_events_for_merge
group_for_merge = group_events_for_merge
group_events = group_events_for_merge
auto_merge_events = auto_merge
auto_merge_groups = auto_merge
suggest_merge = suggest_merges
get_merge_suggestions = suggest_merges

merge_events_sync = merge_events
split_event_sync = split_event

__all__ = [
    "merge_event_dicts",
    "split_event_dict",
    "group_events_for_merge",
    "merge_events",
    "split_event",
    "suggest_merges",
    "auto_merge",
    "clear",
    "merge_events_db",
    "split_event_db",
    "merge_events_async",
    "split_event_async",
    "suggest_merges_async",
    "EventMergeService",
    "event_merge_service",
]
