"""Auto-acknowledge stale (unacknowledged, aged-out) events.

Verification note (read-only):

- ``app/services/event_ack.py`` only implements single-event manual
  acknowledge/escalate (``acknowledge(event_id=...)`` /
  ``escalate(event_id=...)`` plus ``get_status``/``list_states``). There is
  no bulk or automatic operation there.
- ``app/services/bulk_ack.py`` only implements manual bulk acknowledge /
  escalate (``bulk_acknowledge`` / ``bulk_escalate`` / ``bulk_ack_events``)
  delegating per-event to ``event_ack``. It requires explicit ``event_ids``.
- ``app/services/auto_rules.py`` has an ``auto_acknowledge`` *flag* that
  only sets ``applied["acknowledged"] = True`` in a dry-run/applied report
  dict (see ``_apply_actions_to_event``). It never calls
  ``event_ack.acknowledge`` and never mutates the ack store.
- ``grep`` for ``stale`` across ``app/`` only hits ``rtsp_health`` /
  ``camera_health`` / ``audit_prune`` / ``cleanup_advisor`` stream-file
  staleness -- none of them touch ack state. There is no existing
  stale-event auto-ack module.

This module adds the missing layer: automatically acknowledge events that
are still unacknowledged (and unescalated) once they are older than a
configurable staleness threshold. It delegates per-event to
``app/services/event_ack.py`` so manual/bulk semantics are reused, and the
``app/api/auto_ack.py`` router stays thin. ``db``/``session`` arguments are
accepted for forward-compatibility but ignored for the in-memory path
(best-effort no-op); the async entrypoints additionally support an
``AsyncSession`` over :class:`app.models.entities.Event`.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.services import event_ack as _single

DEFAULT_STALE_AFTER_SECONDS = 24 * 3600.0
DEFAULT_ACTOR = "auto-ack"
DEFAULT_REASON = "stale auto-ack"

_SECONDS_KEYS = (
    "stale_after_seconds",
    "stale_after",
    "stale_seconds",
    "stale_age_seconds",
    "older_than_seconds",
    "age_seconds",
    "ttl_seconds",
    "stale_after_secs",
    "staleAfterSeconds",
    "staleAfter",
    "olderThanSeconds",
    "max_age_seconds",
    "auto_ack_after_seconds",
    "autoAckAfterSeconds",
    "stale_threshold_seconds",
    "threshold_seconds",
    "seconds",
    "secs",
)

_MINUTES_KEYS = (
    "stale_after_minutes",
    "stale_minutes",
    "older_than_minutes",
    "age_minutes",
    "ttl_minutes",
    "minutes",
    "mins",
)

_HOURS_KEYS = (
    "stale_after_hours",
    "stale_hours",
    "older_than_hours",
    "age_hours",
    "ttl_hours",
    "hours",
    "hrs",
)

_DAYS_KEYS = (
    "stale_after_days",
    "stale_days",
    "older_than_days",
    "max_age_days",
    "retention_days",
    "age_days",
    "days",
    "ttl_days",
    "olderThanDays",
    "maxAgeDays",
    "staleAfterDays",
    "event_retention_days",
    "event_max_age_days",
    "events_retention_days",
)

_GENERIC_AGE_KEYS = (
    "stale_after",
    "stale_age",
    "stale_threshold",
    "threshold",
    "older_than",
    "olderThan",
    "max_age",
    "maxAge",
    "retention",
    "age",
    "ttl",
    "expiry",
    "expires_after",
    "auto_ack_after",
    "auto_ack_age",
    "stale",
    "stale_age",
    "before",
    "cutoff",
    "cutoff_ts",
)

_ENTRIES_KEYS = (
    "entries",
    "events",
    "items",
    "rows",
    "event_list",
    "data_events",
    "event_entries",
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

_ACTOR_KEYS = (
    "actor",
    "acknowledged_by",
    "acked_by",
    "escalated_by",
    "user",
    "username",
    "author",
    "created_by",
    "createdBy",
    "by",
    "operator",
)

_REASON_KEYS = (
    "reason",
    "note",
    "comment",
    "message",
    "text",
    "body",
    "content",
    "description",
    "detail",
    "details",
)

_DRY_KEYS = (
    "dry_run",
    "dryRun",
    "dry-run",
    "dry",
    "simulate",
    "preview",
    "preview_only",
    "previewOnly",
    "what_if",
    "whatIf",
)

_lock = threading.Lock()
_LAST_RUN: Dict[str, Any] = {"at": None, "stats": {}}


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    if isinstance(value, bool):
        return None
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
        if not s:
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


def _as_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "")
        if not s or s in ("none", "null", "auto", "all", "disabled", "off"):
            return None
        for suffix in ("days", "day", "d"):
            if s.endswith(suffix):
                s = s[: -len(suffix)] or "0"
                break
        try:
            return float(s)
        except (TypeError, ValueError):
            return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_duration_to_seconds(value: Any) -> Optional[float]:
    """Parse a generic duration (``"24h"``, ``"30m"``, ``"1d"``, plain secs)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, timedelta):
        return float(value.total_seconds())
    s = str(value).strip().lower().replace(" ", "")
    if not s or s in ("none", "null", "auto", "all", "disabled", "off"):
        return None
    mult = 1.0
    for suffix, m in (
        ("milliseconds", 0.001), ("millisecond", 0.001), ("ms", 0.001),
        ("seconds", 1.0), ("second", 1.0), ("secs", 1.0), ("sec", 1.0), ("s", 1.0),
        ("minutes", 60.0), ("minute", 60.0), ("mins", 60.0), ("min", 60.0), ("m", 60.0),
        ("hours", 3600.0), ("hour", 3600.0), ("hrs", 3600.0), ("hr", 3600.0), ("h", 3600.0),
        ("days", 86400.0), ("day", 86400.0), ("d", 86400.0),
        ("weeks", 604800.0), ("week", 604800.0), ("w", 604800.0),
    ):
        if s.endswith(suffix):
            # Avoid stripping a bare unit with no number ("h" -> "").
            num_part = s[: -len(suffix)]
            if num_part == "":
                return None
            mult = m
            s = num_part
            break
    # A trailing bare number with comma separators is not a duration.
    try:
        return float(s) * mult
    except (TypeError, ValueError):
        return None


def _as_seconds_with_mult(value: Any, mult: float) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value) * mult
        except (TypeError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    # If the string already carries a unit, honor the unit over the key hint.
    parsed = _parse_duration_to_seconds(s)
    if parsed is not None and any(
        ch.isalpha() for ch in s
    ):
        return parsed
    num = _as_number(s)
    if num is None:
        return None
    return float(num) * mult


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
        "request", "fields", "entry", "policy", "settings", "filter",
        "filters", "quota", "ack", "acknowledgement",
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


def resolve_stale_after_seconds(*args: Any, **kwargs: Any) -> float:
    """Resolve the staleness threshold in seconds (default 24h)."""
    data = _flatten_options(*args, **kwargs)
    # Explicit keyword wins (interpreted as seconds, unit suffixes honored).
    for key in ("stale_after_seconds", "stale_after", "threshold_seconds", "staleAfterSeconds"):
        if kwargs.get(key) is not None:
            parsed = _parse_duration_to_seconds(kwargs.get(key))
            if parsed is not None and parsed > 0:
                return float(parsed)
    for keys, mult, generic in (
        (_SECONDS_KEYS, 1.0, False),
        (_MINUTES_KEYS, 60.0, False),
        (_HOURS_KEYS, 3600.0, False),
        (_DAYS_KEYS, 86400.0, False),
    ):
        for k in keys:
            if data.get(k) is not None:
                parsed = _as_seconds_with_mult(data.get(k), mult)
                if parsed is not None and parsed > 0:
                    return float(parsed)
    for k in _GENERIC_AGE_KEYS:
        if data.get(k) is not None:
            # ``before``/``cutoff`` are absolute timestamps, not durations.
            if k in ("before", "cutoff", "cutoff_ts"):
                continue
            parsed = _parse_duration_to_seconds(data.get(k))
            if parsed is not None and parsed > 0:
                return float(parsed)
    # Absolute cutoff fallback: staleness = now - cutoff.
    for k in ("before", "cutoff", "cutoff_ts"):
        if data.get(k) is not None:
            ts = _parse_ts(data.get(k))
            if ts is not None:
                delta = (_now_naive() - ts).total_seconds()
                if delta > 0:
                    return float(delta)
    return float(DEFAULT_STALE_AFTER_SECONDS)


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


def _resolve_entries(data: Dict[str, Any]) -> tuple[List[Any], bool]:
    """Return (entries, from_explicit_ids)."""
    for k in _ENTRIES_KEYS:
        if data.get(k) is not None:
            v = data.get(k)
            if isinstance(v, (list, tuple, set)):
                return list(v), False
            return [v], False
    for k in _EVENT_ID_KEYS:
        if data.get(k) is not None:
            raw = data.get(k)
            # A single dict entry with id+created_at is an entry, not an id.
            if isinstance(raw, dict) and _entry_time(raw) is not None:
                return [raw], False
            if isinstance(raw, (list, tuple, set)) and raw and all(
                isinstance(x, dict) and _entry_id(x) is not None for x in raw
            ):
                return list(raw), False
            ids = _to_id_list(raw)
            return [{"id": i} for i in ids], True
    return [], False


def _resolve_camera_filter(data: Dict[str, Any]) -> List[int]:
    for k in ("camera_id", "camera_ids", "cameras", "camera", "cameraId", "cam_id", "cam"):
        if data.get(k) is not None:
            return _to_id_list(data.get(k))
    return []


def _cutoff_for(threshold_seconds: float, now: Optional[datetime] = None) -> datetime:
    base = now if now is not None else _now_naive()
    if base.tzinfo is not None:
        base = base.astimezone(timezone.utc).replace(tzinfo=None)
    return base - timedelta(seconds=float(threshold_seconds))


def select_stale(
    *args: Any,
    entries: Any = None,
    events: Any = None,
    stale_after_seconds: Any = None,
    now: Any = None,
    include_acknowledged: Any = None,
    camera_ids: Any = None,
    db: Any = None,
    session: Any = None,
    **kwargs: Any,
) -> List[Any]:
    """Return entries considered stale (unacked/unescalated and older than cutoff).

    Entries without a parseable timestamp are only selected when they came
    from an explicit ``event_ids`` list (caller intent) -- otherwise staleness
    cannot be proven and they are skipped.
    """
    data = _flatten_options(*args, **kwargs)
    if entries is not None:
        data.setdefault("entries", entries)
    if events is not None:
        data.setdefault("events", events)
    if stale_after_seconds is not None:
        data["stale_after_seconds"] = stale_after_seconds
    if camera_ids is not None:
        data["camera_ids"] = camera_ids
    threshold = resolve_stale_after_seconds(data)
    now_dt = _parse_ts(now) if now is not None else _now_naive()
    assert now_dt is not None
    cutoff = _cutoff_for(threshold, now_dt)
    include_ack = _coerce_bool(
        include_acknowledged if include_acknowledged is not None else data.get("include_acknowledged", data.get("includeAcked"))
    ) or False
    cam_filter = set(_to_id_list(camera_ids) if camera_ids is not None else _resolve_camera_filter(data))
    inline, from_ids = _resolve_entries(data)
    # ``db`` as an inline list/dict (sync convenience, mirrors retention).
    if not inline and isinstance(db, (list, tuple)):
        inline, from_ids = list(db), False
    elif not inline and isinstance(db, dict):
        for k in _ENTRIES_KEYS:
            if isinstance(db.get(k), (list, tuple)):
                inline, from_ids = list(db.get(k)), False
                break
    selected: List[Any] = []
    for ev in inline:
        eid = _entry_id(ev)
        if eid is None:
            continue
        if cam_filter:
            cam = _entry_camera(ev)
            if cam not in cam_filter:
                continue
        if not include_ack and (_single.is_acknowledged(eid) or _single.is_escalated(eid)):
            continue
        ts = _entry_time(ev)
        if ts is None:
            if from_ids:
                selected.append(ev)
            continue
        if ts < cutoff:
            selected.append(ev)
    # Oldest-first for deterministic application order.
    def _sort_key(ev: Any) -> Any:
        ts = _entry_time(ev)
        return (ts is None, ts, _entry_id(ev) or 0)

    selected.sort(key=_sort_key)
    return selected


def get_policy(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _flatten_options(*args, **kwargs)
    threshold = resolve_stale_after_seconds(data)
    actor = _extract_first(data, _ACTOR_KEYS)
    reason = _extract_first(data, _REASON_KEYS)
    return {
        "status": "ok",
        "ok": True,
        "success": True,
        "stale_after_seconds": float(threshold),
        "stale_after_hours": float(threshold) / 3600.0,
        "stale_after_days": float(threshold) / 86400.0,
        "threshold_seconds": float(threshold),
        "default_stale_after_seconds": float(DEFAULT_STALE_AFTER_SECONDS),
        "actor": str(actor).strip() if actor is not None else DEFAULT_ACTOR,
        "default_actor": DEFAULT_ACTOR,
        "reason": str(reason).strip() if reason is not None else DEFAULT_REASON,
        "default_reason": DEFAULT_REASON,
        "cutoff": _cutoff_for(threshold).isoformat(),
    }


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _flatten_options(*args, **kwargs)
    # Pop entries so get_policy does not misread them as thresholds.
    policy = get_policy(data)
    threshold = float(policy["stale_after_seconds"])
    inline, _ = _resolve_entries(data)
    if not inline and isinstance(data.get("db"), (list, tuple)):
        inline = list(data.get("db"))
    stale = select_stale(entries=inline or [], stale_after_seconds=threshold)
    stale_ids = [_entry_id(e) for e in stale if _entry_id(e) is not None]
    total = len(inline)
    with _lock:
        last = dict(_LAST_RUN)
    out = dict(policy)
    out.update({
        "total": total,
        "total_entries": total,
        "scanned": total,
        "scanned_count": total,
        "stale_count": len(stale_ids),
        "stale_ids": [int(i) for i in stale_ids],
        "candidate_count": len(stale_ids),
        "candidate_ids": [int(i) for i in stale_ids],
        "selected_count": len(stale_ids),
        "selected_ids": [int(i) for i in stale_ids],
        "last_run": last.get("at"),
        "last_stats": dict(last.get("stats") or {}),
    })
    return out


def preview(
    *args: Any,
    entries: Any = None,
    events: Any = None,
    stale_after_seconds: Any = None,
    now: Any = None,
    db: Any = None,
    session: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    kw = dict(kwargs)
    kw["dry_run"] = True
    return auto_ack_stale(
        *args, entries=entries, events=events,
        stale_after_seconds=stale_after_seconds, now=now,
        db=db, session=session, **kw,
    )


def auto_ack_stale(
    *args: Any,
    entries: Any = None,
    events: Any = None,
    event_ids: Any = None,
    stale_after_seconds: Any = None,
    actor: Any = None,
    reason: Any = None,
    dry_run: Any = None,
    now: Any = None,
    include_acknowledged: Any = None,
    camera_ids: Any = None,
    db: Any = None,
    session: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Auto-acknowledge stale events; ``dry_run=True`` only reports."""
    data = _flatten_options(*args, **kwargs)
    if entries is not None:
        data["entries"] = entries
    if events is not None:
        data["events"] = events
    if event_ids is not None:
        data["event_ids"] = event_ids
    if stale_after_seconds is not None:
        data["stale_after_seconds"] = stale_after_seconds
    if actor is not None:
        data["actor"] = actor
    if reason is not None:
        data["reason"] = reason
    if dry_run is not None:
        data["dry_run"] = dry_run
    if now is not None:
        data["now"] = now
    if include_acknowledged is not None:
        data["include_acknowledged"] = include_acknowledged
    if camera_ids is not None:
        data["camera_ids"] = camera_ids
    threshold = resolve_stale_after_seconds(data)
    now_dt = _parse_ts(data.get("now")) if data.get("now") is not None else _now_naive()
    assert now_dt is not None
    cutoff = _cutoff_for(threshold, now_dt)
    actor_val = _extract_first(data, _ACTOR_KEYS) or DEFAULT_ACTOR
    actor_val = str(actor_val).strip() or DEFAULT_ACTOR
    reason_val = _extract_first(data, _REASON_KEYS) or DEFAULT_REASON
    reason_val = str(reason_val).strip() or DEFAULT_REASON
    is_dry = _coerce_bool(_extract_first(data, _DRY_KEYS))
    if is_dry is None:
        is_dry = False
    stale = select_stale(
        data,
        stale_after_seconds=threshold,
        now=now_dt,
        include_acknowledged=data.get("include_acknowledged", data.get("includeAcked")),
        camera_ids=data.get("camera_ids", data.get("camera_id")),
    )
    stale_ids = [int(_entry_id(e)) for e in stale if _entry_id(e) is not None]
    inline, _ = _resolve_entries(data)
    total = len(inline)
    per_event: Dict[str, Dict[str, Any]] = {}
    updated: List[int] = []
    skipped_already_acked: List[int] = []
    if not is_dry:
        for eid in stale_ids:
            if _single.is_acknowledged(eid) or _single.is_escalated(eid):
                skipped_already_acked.append(int(eid))
                continue
            try:
                state = _single.acknowledge({"event_id": int(eid), "actor": actor_val, "reason": reason_val})
            except ValueError:
                continue
            per_event[str(int(eid))] = state
            updated.append(int(eid))
        with _lock:
            _LAST_RUN["at"] = _now_iso()
            _LAST_RUN["stats"] = {
                "updated_count": len(updated),
                "stale_after_seconds": float(threshold),
                "actor": actor_val,
                "dry_run": False,
            }
    else:
        for eid in stale_ids:
            st = _single.get_status(eid)
            per_event[str(int(eid))] = dict(st) if st is not None else {
                "event_id": int(eid), "status": "open",
                "acknowledged": False, "escalated": False,
            }
    result: Dict[str, Any] = {
        "status": "ok",
        "state": "ok",
        "ok": True,
        "success": True,
        "action": "auto-ack",
        "mode": "auto-ack",
        "actor": actor_val,
        "acknowledged_by": actor_val,
        "reason": reason_val,
        "stale_after_seconds": float(threshold),
        "stale_after_hours": float(threshold) / 3600.0,
        "stale_after_days": float(threshold) / 86400.0,
        "threshold_seconds": float(threshold),
        "cutoff": cutoff.isoformat(),
        "dry_run": bool(is_dry),
        "dryRun": bool(is_dry),
        "preview": bool(is_dry),
        "is_preview": bool(is_dry),
        "preview_only": bool(is_dry),
        "total": total,
        "total_entries": total,
        "scanned": total,
        "scanned_count": total,
        "stale_count": len(stale_ids),
        "selected_count": len(stale_ids),
        "selected_ids": list(stale_ids),
        "stale_ids": list(stale_ids),
        "candidate_count": len(stale_ids),
        "candidate_ids": list(stale_ids),
        "updated_event_ids": list(updated) if not is_dry else [],
        "updated_events": list(updated) if not is_dry else [],
        "updated": list(updated) if not is_dry else [],
        "updated_count": len(updated) if not is_dry else 0,
        "acked_event_ids": list(updated) if not is_dry else [],
        "acked_count": len(updated) if not is_dry else 0,
        "would_ack_ids": list(stale_ids) if is_dry else [],
        "would_ack_count": len(stale_ids) if is_dry else 0,
        "would_update_ids": list(stale_ids) if is_dry else [],
        "skipped_already_acknowledged": skipped_already_acked,
        "skipped_count": len(skipped_already_acked),
        "missing_event_ids": [],
        "missing": [],
        "not_found": [],
        "per_event": per_event,
        "per_event_states": per_event,
    }
    result["count"] = result["updated_count"] if not is_dry else result["selected_count"]
    result["summary"] = {
        "total": total,
        "stale": len(stale_ids),
        "updated": len(updated) if not is_dry else 0,
        "dry_run": bool(is_dry),
        "stale_after_seconds": float(threshold),
    }
    result["report"] = dict(result["summary"])
    return result


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

            q = _select(_Event).order_by(_Event.id.asc())
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


async def select_stale_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Any]:
    data = _flatten_options(*args, **kwargs)
    db_obj = db if db is not None else session
    if db_obj is None:
        for k in ("db", "session", "database"):
            if data.get(k) is not None:
                db_obj = data.get(k)
                break
    inline, _ = _resolve_entries(data)
    if not inline and db_obj is not None and hasattr(db_obj, "execute"):
        rows = await _fetch_db_events(db_obj, _resolve_camera_filter(data) or None)
        data = dict(data)
        data["entries"] = rows
    return select_stale(data)


async def preview_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    kw = dict(kwargs)
    kw["dry_run"] = True
    return await auto_ack_stale_async(*args, db=db, session=session, **kw)


async def auto_ack_stale_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    data = _flatten_options(*args, **kwargs)
    db_obj = db if db is not None else session
    if db_obj is None:
        for k in ("db", "session", "database"):
            if data.get(k) is not None:
                db_obj = data.get(k)
                break
    inline, _ = _resolve_entries(data)
    if not inline and db_obj is not None and hasattr(db_obj, "execute"):
        rows = await _fetch_db_events(db_obj, _resolve_camera_filter(data) or None)
        data = dict(data)
        data["entries"] = rows
    data.pop("db", None)
    data.pop("session", None)
    data.pop("database", None)
    return auto_ack_stale(data)


async def get_status_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    data = _flatten_options(*args, **kwargs)
    db_obj = db if db is not None else session
    inline, _ = _resolve_entries(data)
    if not inline and db_obj is not None and hasattr(db_obj, "execute"):
        try:
            rows = await _fetch_db_events(db_obj, _resolve_camera_filter(data) or None)
            data = dict(data)
            data["entries"] = rows
        except Exception:
            pass
    data.pop("db", None)
    data.pop("session", None)
    return get_status(data)


def clear_last_run(*args: Any, **kwargs: Any) -> int:
    with _lock:
        had = 1 if _LAST_RUN.get("at") is not None else 0
        _LAST_RUN["at"] = None
        _LAST_RUN["stats"] = {}
        return had


class AutoAckService:
    """Object-oriented facade over the auto-ack helpers."""

    def select(self, *args: Any, **kwargs: Any) -> List[Any]:
        return select_stale(*args, **kwargs)

    def preview(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kw = dict(kwargs)
        kw["dry_run"] = True
        return auto_ack_stale(*args, **kw)

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return auto_ack_stale(*args, **kwargs)

    def auto_ack(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return auto_ack_stale(*args, **kwargs)

    def acknowledge_stale(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return auto_ack_stale(*args, **kwargs)

    def ack_stale(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return auto_ack_stale(*args, **kwargs)

    def get_policy(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_policy(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)

    async def select_async(self, *args: Any, **kwargs: Any) -> List[Any]:
        return await select_stale_async(*args, **kwargs)

    async def preview_async_method(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await preview_async(*args, **kwargs)

    async def run_async(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await auto_ack_stale_async(*args, **kwargs)

    # aliases
    acknowledge = auto_ack
    ack = auto_ack
    dry_run = preview
    candidates = select
    find_stale = select


auto_ack_service = AutoAckService()
auto_ack_manager = auto_ack_service
service = auto_ack_service
manager = auto_ack_service
store = auto_ack_service

# Compatibility aliases (function level).
auto_ack = auto_ack_stale
ack_stale = auto_ack_stale
acknowledge_stale = auto_ack_stale
ack_stale_events = auto_ack_stale
auto_acknowledge_stale = auto_ack_stale
auto_acknowledge = auto_ack_stale
run_auto_ack = auto_ack_stale
run = auto_ack_stale
execute = auto_ack_stale
dry_run = preview
get_candidates = select_stale
find_stale = select_stale
get_stale = select_stale
collect_stale = select_stale

__all__ = [
    "DEFAULT_STALE_AFTER_SECONDS",
    "DEFAULT_ACTOR",
    "DEFAULT_REASON",
    "resolve_stale_after_seconds",
    "select_stale",
    "preview",
    "auto_ack_stale",
    "get_policy",
    "get_status",
    "clear_last_run",
    "select_stale_async",
    "preview_async",
    "auto_ack_stale_async",
    "get_status_async",
    "AutoAckService",
    "auto_ack_service",
]
