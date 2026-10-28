"""Bulk event acknowledge / escalate service.

Verification note (read-only):

- ``app/services/event_ack.py`` only implements single-event
  acknowledge/escalate (``acknowledge(event_id=...)`` /
  ``escalate(event_id=...)`` plus ``get_status``/``list_states``). There is
  no bulk operation there -- grep for ``acknowledge`` shows only per-event
  state helpers, and grep for ``bulk_ack`` / ``bulk.*ack`` across the repo
  shows no existing bulk-ack module.
- ``/opt/others/ispy`` (read-only reference) has no ``acknowledge``
  acknowledgement pattern to port -- grep finds no matches -- so this module
  follows the local ``event_ack`` in-memory semantics.

This module provides the bulk layer on top of ``event_ack`` so the
``app/api/bulk_ack.py`` router stays thin. ``db``/``session`` arguments are
accepted for forward-compatibility but ignored (in-memory store).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services import event_ack as _single

EVENT_ID_KEYS = (
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

ACTOR_KEYS = (
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

REASON_KEYS = (
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

LEVEL_KEYS = ("level", "priority", "severity", "escalation_level")

ACTION_ACK = {"acknowledge", "acknowledged", "ack", "acked", "ack_event"}
ACTION_ESCALATE = {"escalate", "escalated", "escalation", "escalate_event"}


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


def coerce_id_list(value: Any) -> List[int]:
    """Coerce ints / numeric strings / csv / lists / dicts to deduped ids."""
    out: List[int] = []
    seen = set()
    if value is None:
        return out
    iterable: Any
    if isinstance(value, (list, tuple, set)):
        iterable = value
    else:
        iterable = [value]
    for v in iterable:
        if v is None or isinstance(v, bool):
            continue
        if isinstance(v, dict):
            cand = None
            for k in ("id", "event_id", "eventId", "event", "eventid"):
                if v.get(k) is not None:
                    cand = v.get(k)
                    break
            if cand is None:
                continue
            v = cand
        if isinstance(v, str):
            for part in v.split(","):
                part = part.strip()
                if not part:
                    continue
                iv = _coerce_int(part)
                if iv is not None and iv not in seen:
                    seen.add(iv)
                    out.append(iv)
        else:
            iv = _coerce_int(v)
            if iv is not None and iv not in seen:
                seen.add(iv)
                out.append(iv)
    return out


def normalize_action(value: Any, default: str = "acknowledge") -> str:
    """Normalize action/mode to ``acknowledge`` or ``escalate``."""
    if value is None:
        return default
    s = str(value).strip().lower()
    if not s:
        return default
    if s in ACTION_ESCALATE:
        return "escalate"
    if s in ACTION_ACK:
        return "acknowledge"
    return default


def _extract_ids(data: Dict[str, Any]) -> Any:
    for key in EVENT_ID_KEYS:
        if data.get(key) is not None:
            return data.get(key)
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in ("event_ids", "eventids", "ids", "event_id", "eventid"):
        if lower.get(key) is not None:
            return lower.get(key)
    return None


def _extract_first(data: Dict[str, Any], keys: tuple) -> Optional[str]:
    for k in keys:
        if data.get(k) is not None:
            s = str(data.get(k)).strip()
            if s:
                return s
    lower = {str(k).lower(): v for k, v in data.items() if v is not None}
    for k in keys:
        if lower.get(k.lower()) is not None:
            s = str(lower[k.lower()]).strip()
            if s:
                return s
    return None


def _flatten_kwargs(event_ids: Any, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if event_ids is not None:
        data["event_ids"] = event_ids
    for k, v in (kwargs or {}).items():
        if k in ("db", "session"):
            continue
        data.setdefault(k, v)
    # nested payload tolerance (mirrors event_ack / bulk_tag routers)
    for nested_key in ("payload", "params", "data", "body", "request", "fields", "entry", "ack", "acknowledgement", "escalation"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def bulk_acknowledge(
    event_ids: Any = None,
    actor: Any = None,
    reason: Any = None,
    db: Any = None,
    session: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Acknowledge many events at once.

    Returns a summary dict with ``updated_event_ids`` / ``updated_count`` /
    ``per_event`` (mirrors ``bulk_tag`` response shape).
    """
    data = _flatten_kwargs(event_ids, kwargs)
    raw_ids = _extract_ids(data)
    ids = coerce_id_list(raw_ids)
    if not ids:
        raise ValueError("event_ids is required")
    actor_val = str(actor).strip() if actor is not None and str(actor).strip() else _extract_first(data, ACTOR_KEYS)
    reason_val = str(reason).strip() if reason is not None and str(reason).strip() else _extract_first(data, REASON_KEYS)
    per_event: Dict[str, Dict[str, Any]] = {}
    updated: List[int] = []
    for eid in ids:
        payload: Dict[str, Any] = {"event_id": int(eid)}
        if actor_val:
            payload["actor"] = actor_val
        if reason_val:
            payload["reason"] = reason_val
        state = _single.acknowledge(payload)
        per_event[str(int(eid))] = state
        updated.append(int(eid))
    return {
        "ok": True,
        "success": True,
        "action": "acknowledge",
        "mode": "acknowledge",
        "actor": actor_val,
        "acknowledged_by": actor_val,
        "reason": reason_val,
        "updated_event_ids": updated,
        "updated_events": list(updated),
        "updated": list(updated),
        "updated_count": len(updated),
        "missing_event_ids": [],
        "missing": [],
        "not_found": [],
        "per_event": per_event,
        "per_event_states": per_event,
    }


def bulk_escalate(
    event_ids: Any = None,
    actor: Any = None,
    reason: Any = None,
    level: Any = None,
    db: Any = None,
    session: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Escalate many events at once (implies acknowledged)."""
    data = _flatten_kwargs(event_ids, kwargs)
    raw_ids = _extract_ids(data)
    ids = coerce_id_list(raw_ids)
    if not ids:
        raise ValueError("event_ids is required")
    actor_val = str(actor).strip() if actor is not None and str(actor).strip() else _extract_first(data, ACTOR_KEYS)
    reason_val = str(reason).strip() if reason is not None and str(reason).strip() else _extract_first(data, REASON_KEYS)
    level_val = str(level).strip() if level is not None and str(level).strip() else _extract_first(data, LEVEL_KEYS)
    per_event: Dict[str, Dict[str, Any]] = {}
    updated: List[int] = []
    for eid in ids:
        payload: Dict[str, Any] = {"event_id": int(eid)}
        if actor_val:
            payload["actor"] = actor_val
        if reason_val:
            payload["reason"] = reason_val
        if level_val:
            payload["level"] = level_val
        state = _single.escalate(payload)
        per_event[str(int(eid))] = state
        updated.append(int(eid))
    return {
        "ok": True,
        "success": True,
        "action": "escalate",
        "mode": "escalate",
        "actor": actor_val,
        "escalated_by": actor_val,
        "reason": reason_val,
        "level": level_val,
        "updated_event_ids": updated,
        "updated_events": list(updated),
        "updated": list(updated),
        "updated_count": len(updated),
        "missing_event_ids": [],
        "missing": [],
        "not_found": [],
        "per_event": per_event,
        "per_event_states": per_event,
    }


def bulk_ack_events(
    event_ids: Any = None,
    action: Any = "acknowledge",
    actor: Any = None,
    reason: Any = None,
    level: Any = None,
    db: Any = None,
    session: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Generic bulk entrypoint dispatching on ``action``/``mode``."""
    data = _flatten_kwargs(event_ids, kwargs)
    # action may live in kwargs aliases
    raw_action = action
    if raw_action == "acknowledge" or raw_action is None:
        for key in ("action", "mode", "operation", "op", "type"):
            if data.get(key) is not None:
                raw_action = data.get(key)
                break
    mode = normalize_action(raw_action, default="acknowledge")
    # re-inject resolved pieces so downstream extractors see them
    if actor is not None:
        data.setdefault("actor", actor)
    if reason is not None:
        data.setdefault("reason", reason)
    if level is not None:
        data.setdefault("level", level)
    ids_val = data.get("event_ids", event_ids)
    forward = {k: v for k, v in data.items() if k not in EVENT_ID_KEYS}
    if mode == "escalate":
        return bulk_escalate(ids_val, db=db, session=session, **forward)
    return bulk_acknowledge(ids_val, db=db, session=session, **forward)


async def bulk_acknowledge_async(
    event_ids: Any = None, db: Any = None, session: Any = None, **kwargs: Any
) -> Dict[str, Any]:
    kwargs.pop("db", None)
    kwargs.pop("session", None)
    return bulk_acknowledge(event_ids, db=db, session=session, **kwargs)


async def bulk_escalate_async(
    event_ids: Any = None, db: Any = None, session: Any = None, **kwargs: Any
) -> Dict[str, Any]:
    kwargs.pop("db", None)
    kwargs.pop("session", None)
    return bulk_escalate(event_ids, db=db, session=session, **kwargs)


async def bulk_ack_events_async(
    event_ids: Any = None, db: Any = None, session: Any = None, **kwargs: Any
) -> Dict[str, Any]:
    kwargs.pop("db", None)
    kwargs.pop("session", None)
    return bulk_ack_events(event_ids, db=db, session=session, **kwargs)


class BulkAckService:
    """Object-oriented facade over the bulk-ack helpers."""

    def acknowledge(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if args and len(args) == 1 and "event_ids" not in kwargs:
            kwargs = dict(kwargs)
            kwargs.setdefault("event_ids", args[0])
        return bulk_acknowledge(**kwargs)

    def escalate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if args and len(args) == 1 and "event_ids" not in kwargs:
            kwargs = dict(kwargs)
            kwargs.setdefault("event_ids", args[0])
        return bulk_escalate(**kwargs)

    def ack(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.acknowledge(*args, **kwargs)

    async def acknowledge_async(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if args and len(args) == 1 and "event_ids" not in kwargs:
            kwargs = dict(kwargs)
            kwargs.setdefault("event_ids", args[0])
        return await bulk_acknowledge_async(**kwargs)

    async def escalate_async(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if args and len(args) == 1 and "event_ids" not in kwargs:
            kwargs = dict(kwargs)
            kwargs.setdefault("event_ids", args[0])
        return await bulk_escalate_async(**kwargs)

    def bulk(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if args and len(args) == 1 and "event_ids" not in kwargs:
            kwargs = dict(kwargs)
            kwargs.setdefault("event_ids", args[0])
        return bulk_ack_events(**kwargs)

    # aliases
    bulk_acknowledge = acknowledge
    bulk_escalate = escalate
    bulk_ack = bulk
    ack_events = acknowledge
    escalate_events = escalate


bulk_ack_service = BulkAckService()
bulk_service = bulk_ack_service
service = bulk_ack_service
manager = bulk_ack_service
store = bulk_ack_service

# Compatibility aliases (function level).
bulk_ack = bulk_ack_events
ack_events_bulk = bulk_acknowledge
acknowledge_events = bulk_acknowledge
ack_bulk = bulk_acknowledge
escalate_events_bulk = bulk_escalate
bulk_acknowledge_events = bulk_acknowledge
bulk_escalate_events = bulk_escalate

__all__ = [
    "EVENT_ID_KEYS",
    "coerce_id_list",
    "normalize_action",
    "bulk_acknowledge",
    "bulk_escalate",
    "bulk_ack_events",
    "bulk_acknowledge_async",
    "bulk_escalate_async",
    "bulk_ack_events_async",
    "BulkAckService",
    "bulk_ack_service",
]
