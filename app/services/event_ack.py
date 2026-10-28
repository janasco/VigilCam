"""Event acknowledge / escalate service (in-memory, per-event state).

Tracks operator workflow state per event id without DB migrations and
without requiring ``app/main.py`` edits. An optional ``db``/``session``
argument is accepted by the async entrypoints for forward-compatibility
but is currently ignored (best-effort no-op).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
_STATES: Dict[int, Dict[str, Any]] = {}

EVENT_ID_KEYS = (
    "event_id",
    "eventId",
    "event",
    "eventid",
    "id_event",
    "for_event",
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        for nested_key in (
            "payload",
            "options",
            "config",
            "params",
            "data",
            "body",
            "request",
            "fields",
            "entry",
            "ack",
            "acknowledgement",
            "escalation",
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


def _extract_event_id(*args: Any, **kwargs: Any) -> Optional[int]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif hasattr(a, "model_dump"):
            try:
                data.update(dict(a.model_dump()))
            except Exception:
                pass
        elif isinstance(a, (int, float)) and "event_id" not in data:
            data["event_id"] = a
    data.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "ack", "escalation"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    raw = _first_present(data, EVENT_ID_KEYS)
    if raw is None and len(args) == 1 and isinstance(args[0], (int, float)):
        raw = args[0]
    return _coerce_int(raw, None)


def _extract_actor(data: Dict[str, Any]) -> Optional[str]:
    raw = _first_present(data, ACTOR_KEYS)
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s else None


def _extract_reason(data: Dict[str, Any]) -> Optional[str]:
    raw = _first_present(data, REASON_KEYS)
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s else None


def _extract_level(data: Dict[str, Any]) -> Optional[str]:
    raw = _first_present(data, LEVEL_KEYS)
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s else None


def _to_public(state: Dict[str, Any]) -> Dict[str, Any]:
    return dict(state)


def _blank_state(event_id: int) -> Dict[str, Any]:
    now = _now_iso()
    return {
        "event_id": event_id,
        "eventId": event_id,
        "id": event_id,
        "status": "open",
        "acknowledged": False,
        "acked": False,
        "escalated": False,
        "acknowledged_by": None,
        "acked_by": None,
        "actor": None,
        "escalated_by": None,
        "reason": None,
        "note": None,
        "level": None,
        "acknowledged_at": None,
        "acked_at": None,
        "escalated_at": None,
        "created_at": now,
        "updated_at": now,
    }


def get_state(event_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    eid = _extract_event_id(*(([event_id] if event_id is not None else []) + list(args)), **kwargs)
    if eid is None:
        return None
    with _lock:
        state = _STATES.get(int(eid))
        return _to_public(state) if state is not None else None


def get_status(event_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_state(event_id, *args, **kwargs)


def is_acknowledged(event_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    state = get_state(event_id, *args, **kwargs)
    return bool(state and state.get("acknowledged"))


def is_escalated(event_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    state = get_state(event_id, *args, **kwargs)
    return bool(state and state.get("escalated"))


def acknowledge(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Mark an event as acknowledged.

    Accepts flexible inputs, e.g.::

        acknowledge(event_id=1)
        acknowledge(1, actor="op")
        acknowledge({"event_id": 1, "reason": "seen"})
    """
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
        elif isinstance(a, (int, float)) and "event_id" not in flat:
            flat["event_id"] = a
        elif isinstance(a, str):
            flat.setdefault("reason", a)
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "ack", "acknowledgement"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)

    eid = _coerce_int(_first_present(flat, EVENT_ID_KEYS), None)
    if eid is None:
        raise ValueError("event_id is required")
    eid = int(eid)
    actor = _extract_actor(flat)
    reason = _extract_reason(flat)
    now = _now_iso()
    with _lock:
        state = _STATES.get(eid)
        if state is None:
            state = _blank_state(eid)
            _STATES[eid] = state
        state["status"] = "escalated" if state.get("escalated") else "acknowledged"
        state["acknowledged"] = True
        state["acked"] = True
        if actor is not None:
            state["acknowledged_by"] = actor
            state["acked_by"] = actor
            state["actor"] = actor
        if reason is not None:
            state["reason"] = reason
            state["note"] = reason
        if state.get("acknowledged_at") is None:
            state["acknowledged_at"] = now
            state["acked_at"] = now
        state["updated_at"] = now
        for k, v in flat.items():
            if k not in state:
                state[k] = v
        return _to_public(state)


def escalate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Mark an event as escalated (implies acknowledged).

    Accepts flexible inputs, e.g.::

        escalate(event_id=1, level="high")
        escalate(1, "needs review")
        escalate({"event_id": 1, "reason": "urgent"})
    """
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
        elif isinstance(a, (int, float)) and "event_id" not in flat:
            flat["event_id"] = a
        elif isinstance(a, str):
            flat.setdefault("reason", a)
    flat.update(kwargs or {})
    for nested_key in ("payload", "params", "data", "body", "escalation", "ack"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)

    eid = _coerce_int(_first_present(flat, EVENT_ID_KEYS), None)
    if eid is None:
        raise ValueError("event_id is required")
    eid = int(eid)
    actor = _extract_actor(flat)
    reason = _extract_reason(flat)
    level = _extract_level(flat)
    now = _now_iso()
    with _lock:
        state = _STATES.get(eid)
        if state is None:
            state = _blank_state(eid)
            _STATES[eid] = state
        state["status"] = "escalated"
        state["escalated"] = True
        # Escalation implies acknowledgement.
        state["acknowledged"] = True
        state["acked"] = True
        if actor is not None:
            state["escalated_by"] = actor
            state["acknowledged_by"] = state.get("acknowledged_by") or actor
            state["acked_by"] = state.get("acked_by") or actor
            state["actor"] = actor
        if reason is not None:
            state["reason"] = reason
            state["note"] = reason
        if level is not None:
            state["level"] = level
            state["priority"] = level
            state["severity"] = level
        if state.get("escalated_at") is None:
            state["escalated_at"] = now
        if state.get("acknowledged_at") is None:
            state["acknowledged_at"] = now
            state["acked_at"] = now
        state["updated_at"] = now
        for k, v in flat.items():
            if k not in state:
                state[k] = v
        return _to_public(state)


def unacknowledge(event_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    eid = _extract_event_id(*(([event_id] if event_id is not None else []) + list(args)), **kwargs)
    if eid is None:
        return None
    with _lock:
        state = _STATES.get(int(eid))
        if state is None:
            return None
        now = _now_iso()
        state["acknowledged"] = False
        state["acked"] = False
        state["status"] = "escalated" if state.get("escalated") else "open"
        state["updated_at"] = now
        return _to_public(state)


def deescalate(event_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    eid = _extract_event_id(*(([event_id] if event_id is not None else []) + list(args)), **kwargs)
    if eid is None:
        return None
    with _lock:
        state = _STATES.get(int(eid))
        if state is None:
            return None
        now = _now_iso()
        state["escalated"] = False
        state["status"] = "acknowledged" if state.get("acknowledged") else "open"
        state["updated_at"] = now
        return _to_public(state)


def list_states(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    flat = _flatten(args[0]) if len(args) == 1 and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    status_filter = flat.get("status")
    if status_filter is not None:
        status_filter = str(status_filter).strip().lower()
    with _lock:
        items = [_to_public(_STATES[k]) for k in sorted(_STATES.keys())]
    if status_filter:
        items = [s for s in items if str(s.get("status", "")).lower() == status_filter]
    return items


def clear(*args: Any, **kwargs: Any) -> int:
    with _lock:
        n = len(_STATES)
        _STATES.clear()
        return n


# --- Async entrypoints (db-optional, ignored) ---------------------------------

async def acknowledge_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    kwargs.pop("db", None)
    kwargs.pop("session", None)
    return acknowledge(*args, **kwargs)


async def escalate_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    kwargs.pop("db", None)
    kwargs.pop("session", None)
    return escalate(*args, **kwargs)


async def get_status_async(event_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_status(event_id, *args, **kwargs)


async def get_state_async(event_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_state(event_id, *args, **kwargs)


class EventAckService:
    """Object-oriented facade over the module-level store."""

    def acknowledge(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return acknowledge(*args, **kwargs)

    def escalate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return escalate(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_state(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_status(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_states(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    # aliases
    ack = acknowledge
    ack_event = acknowledge
    escalate_event = escalate


event_ack_service = EventAckService()
ack_service = event_ack_service
service = event_ack_service
manager = event_ack_service
store = event_ack_service


# Compatibility aliases (function level).
ack = acknowledge
acknowledge_event = acknowledge
ack_event = acknowledge
acknowledge_event_async = acknowledge_async
ack_async = acknowledge_async

escalate_event = escalate
escalation = escalate

get_ack = get_status
get_ack_status = get_status
fetch_status = get_status
get_event_ack = get_status
get_event_ack_status = get_status
fetch_ack = get_status

clear_ack = clear
clear_event_ack = clear
reset = clear
purge = clear

list_acknowledged = list_states
get_all = list_states

__all__ = [
    "acknowledge",
    "escalate",
    "get_state",
    "get_status",
    "is_acknowledged",
    "is_escalated",
    "unacknowledge",
    "deescalate",
    "list_states",
    "clear",
    "acknowledge_async",
    "escalate_async",
    "get_status_async",
    "get_state_async",
    "EventAckService",
    "event_ack_service",
]
