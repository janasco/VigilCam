"""Persistent disconnect/reconnect event emitter for VigilCam.

Complements :mod:`app.services.offline_alerts` (verified: in-memory
throttled alerts + ``event_bus`` publish only, with no ``Event`` row,
no ``SessionLocal`` / ``get_session`` / ``Base`` usage).

This module persists connectivity transitions as ``Event`` rows
(``kind="disconnect"`` / ``kind="reconnect"``) so they survive restarts
and show up in ``/api/events`` timelines. All writes go through an
``AsyncSession``; when ``db`` is ``None`` a short-lived ``SessionLocal``
session is opened internally.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.models.entities import Event

DISCONNECT_KIND = "disconnect"
RECONNECT_KIND = "reconnect"
CONN_KINDS = (DISCONNECT_KIND, RECONNECT_KIND)

CONNECTIVITY_TAG = "connectivity"


def _coerce_camera_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        if hasattr(value, "id") and not isinstance(value, (int, str, float)):
            value = getattr(value, "id")
        return int(value)
    except (TypeError, ValueError):
        s = str(value).strip()
        if not s:
            return None
        import re

        m = re.search(r"(\d+)$", s)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        return None


def _coerce_tags(value: Any, kind: str) -> List[str]:
    base = [CONNECTIVITY_TAG, kind]
    if value is None:
        return base
    if isinstance(value, str):
        extra = [p.strip() for p in value.split(",") if p.strip()]
    elif isinstance(value, (list, tuple, set)):
        extra = [str(v).strip() for v in value if str(v).strip()]
    else:
        extra = [str(value).strip()]
    out = list(base)
    seen = {t.lower() for t in out}
    for t in extra:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _build_payload(reason: Optional[str], detail: Optional[str], extra: Optional[Dict[str, Any]]) -> Optional[str]:
    obj: Dict[str, Any] = {"trigger": "conn_events"}
    if reason:
        obj["reason"] = str(reason)
    if detail:
        obj["detail"] = str(detail)
    if isinstance(extra, dict):
        for k, v in extra.items():
            obj.setdefault(k, v)
    try:
        return json.dumps(obj)
    except Exception:  # noqa: BLE001
        return json.dumps({"trigger": "conn_events"})


def event_to_dict(event: Event) -> Dict[str, Any]:
    tags = getattr(event, "tags", None)
    if isinstance(tags, str):
        try:
            parsed = json.loads(tags)
            tags = parsed if isinstance(parsed, list) else [parsed]
        except Exception:  # noqa: BLE001
            tags = [p.strip() for p in tags.split(",") if p.strip()]
    if not isinstance(tags, list):
        tags = [] if tags is None else [str(tags)]
    created = getattr(event, "created_at", None)
    return {
        "id": getattr(event, "id", None),
        "camera_id": getattr(event, "camera_id", None),
        "kind": getattr(event, "kind", None),
        "label": getattr(event, "label", None),
        "confidence": getattr(event, "confidence", None),
        "payload": getattr(event, "payload", None),
        "tags": list(tags),
        "created_at": created.isoformat() if created else None,
    }


async def _get_last(db, camera_id: int) -> Optional[Event]:
    result = await db.execute(
        select(Event)
        .where(Event.camera_id == camera_id, Event.kind.in_(CONN_KINDS))
        .order_by(Event.id.desc())
        .limit(1)
    )
    return result.scalars().first()


async def get_last_conn_event(db, camera_id: Any) -> Optional[Event]:
    """Return the most recent disconnect/reconnect Event for a camera."""
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        return None
    if db is None:
        from app.core.database import SessionLocal as _SessionLocal

        async with _SessionLocal() as session:
            last = await _get_last(session, cid)
            if last is not None:
                try:
                    session.expunge(last)
                except Exception:  # noqa: BLE001
                    pass
            return last
    return await _get_last(db, cid)


async def get_conn_state(db, camera_id: Any) -> Optional[str]:
    """Return 'offline' / 'online' / None from the last conn Event."""
    last = await get_last_conn_event(db, camera_id)
    if last is None:
        return None
    if last.kind == DISCONNECT_KIND:
        return "offline"
    if last.kind == RECONNECT_KIND:
        return "online"
    return None


async def _persist(db, camera_id: int, kind: str, label: Optional[str],
                   reason: Optional[str], detail: Optional[str],
                   tags: Any, payload_extra: Optional[Dict[str, Any]]) -> Event:
    if kind == DISCONNECT_KIND:
        default_label = f"Camera {camera_id} disconnected"
    else:
        default_label = f"Camera {camera_id} reconnected"
    event = Event(
        camera_id=camera_id,
        kind=kind,
        label=(label or default_label)[:120],
        confidence=None,
        payload=_build_payload(reason, detail, payload_extra),
        tags=_coerce_tags(tags, kind),
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


def _resolve_kwargs(camera_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    # Bare positional camera id.
    positional = [camera_id, *args]
    for candidate in positional:
        if candidate is not None and not isinstance(candidate, dict):
            cid = _coerce_camera_id(candidate)
            if cid is not None and "camera_id" not in data:
                data["camera_id"] = cid
                continue
            if isinstance(candidate, str) and "reason" not in data:
                data["reason"] = candidate
        elif isinstance(candidate, dict):
            for k, v in candidate.items():
                data.setdefault(k, v)
    for nested_key in ("payload", "options", "params", "data", "body", "request", "event"):
        nested = kwargs.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    for k, v in kwargs.items():
        if k not in data:
            data[k] = v
    # Alias keys for camera id.
    if "camera_id" not in data:
        for alias in ("camera", "cam", "cam_id", "id", "device_id", "source_id"):
            if data.get(alias) is not None:
                cid = _coerce_camera_id(data.get(alias))
                if cid is not None:
                    data["camera_id"] = cid
                    break
    return data


async def emit_disconnect(db=None, camera_id: Any = None, *args: Any, **kwargs: Any) -> Event:
    """Persist a ``disconnect`` Event row. Dedups only when asked."""
    data = _resolve_kwargs(camera_id, *args, **kwargs)
    cid = _coerce_camera_id(data.get("camera_id"))
    if cid is None:
        raise ValueError("camera_id is required")
    reason = data.get("reason", "offline")
    detail = data.get("detail") or data.get("message") or data.get("description")
    label = data.get("label")
    tags = data.get("tags", data.get("tag"))
    dedup = bool(data.get("dedup", False))
    force = bool(data.get("force", False))
    opened_here = False
    session = db
    if session is None:
        from app.core.database import SessionLocal as _SessionLocal

        session = _SessionLocal()
        opened_here = True
    try:
        if dedup and not force:
            last = await _get_last(session, cid)
            if last is not None and last.kind == DISCONNECT_KIND:
                return last
        return await _persist(session, cid, DISCONNECT_KIND, label, reason, detail, tags, None)
    finally:
        if opened_here:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass


async def emit_reconnect(db=None, camera_id: Any = None, *args: Any, **kwargs: Any) -> Event:
    """Persist a ``reconnect`` Event row. Dedups only when asked."""
    data = _resolve_kwargs(camera_id, *args, **kwargs)
    cid = _coerce_camera_id(data.get("camera_id"))
    if cid is None:
        raise ValueError("camera_id is required")
    reason = data.get("reason", "online")
    detail = data.get("detail") or data.get("message") or data.get("description")
    label = data.get("label")
    tags = data.get("tags", data.get("tag"))
    dedup = bool(data.get("dedup", False))
    force = bool(data.get("force", False))
    opened_here = False
    session = db
    if session is None:
        from app.core.database import SessionLocal as _SessionLocal

        session = _SessionLocal()
        opened_here = True
    try:
        if dedup and not force:
            last = await _get_last(session, cid)
            if last is not None and last.kind == RECONNECT_KIND:
                return last
        return await _persist(session, cid, RECONNECT_KIND, label, reason, detail, tags, None)
    finally:
        if opened_here:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass


async def record_transition(db=None, camera_id: Any = None, online: Any = None,
                            *args: Any, **kwargs: Any) -> Optional[Event]:
    """Persist a transition Event, skipping duplicates (no state change).

    ``online=False`` -> ``disconnect`` row; ``online=True`` -> ``reconnect``
    row. When the last conn Event already matches the desired state the
    existing row is returned without inserting a duplicate, unless
    ``force=True`` is given.
    """
    data = _resolve_kwargs(camera_id, *args, **kwargs)
    cid = _coerce_camera_id(data.get("camera_id"))
    if cid is None:
        raise ValueError("camera_id is required")
    # Resolve online flag from multiple aliases.
    flag = online
    if flag is None:
        for key in ("online", "is_online", "connected", "up", "reconnected"):
            if data.get(key) is not None:
                flag = data.get(key)
                break
    if flag is None:
        for key in ("offline", "is_offline", "disconnected", "down"):
            if data.get(key) is not None:
                try:
                    flag = not bool(data.get(key))
                except Exception:  # noqa: BLE001
                    flag = False
                break
    if flag is None:
        kind_hint = str(data.get("kind", "") or data.get("event", "") or data.get("type", "")).lower()
        if kind_hint in (DISCONNECT_KIND, "offline", "disconnected", "camera_offline"):
            flag = False
        elif kind_hint in (RECONNECT_KIND, "online", "reconnected", "camera_online"):
            flag = True
    if flag is None:
        raise ValueError("online flag is required (True=reconnect, False=disconnect)")
    desired = RECONNECT_KIND if bool(flag) else DISCONNECT_KIND
    force = bool(data.get("force", False))
    reason = data.get("reason") or ("online" if desired == RECONNECT_KIND else "offline")
    detail = data.get("detail") or data.get("message") or data.get("description")
    label = data.get("label")
    tags = data.get("tags", data.get("tag"))
    opened_here = False
    session = db
    if session is None:
        from app.core.database import SessionLocal as _SessionLocal

        session = _SessionLocal()
        opened_here = True
    try:
        if not force:
            last = await _get_last(session, cid)
            if last is not None and last.kind == desired:
                return last
        return await _persist(session, cid, desired, label, reason, detail, tags, None)
    finally:
        if opened_here:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass


async def list_conn_events(db=None, camera_id: Any = None, kind: Optional[str] = None,
                           limit: int = 100) -> List[Event]:
    """List persistent disconnect/reconnect Event rows, newest first."""
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = 100
    lim = max(1, min(lim, 1000))
    kinds = list(CONN_KINDS)
    if kind is not None:
        k = str(kind).strip().lower()
        if k in CONN_KINDS:
            kinds = [k]
    cid = _coerce_camera_id(camera_id) if camera_id is not None else None
    opened_here = False
    session = db
    if session is None:
        from app.core.database import SessionLocal as _SessionLocal

        session = _SessionLocal()
        opened_here = True
    try:
        stmt = select(Event).where(Event.kind.in_(kinds)).order_by(Event.id.desc()).limit(lim)
        if cid is not None:
            stmt = (
                select(Event)
                .where(Event.camera_id == cid, Event.kind.in_(kinds))
                .order_by(Event.id.desc())
                .limit(lim)
            )
        result = await session.execute(stmt)
        return list(result.scalars().all())
    finally:
        if opened_here:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass


async def get_summary(db=None, camera_id: Any = None) -> Dict[str, Any]:
    """Count persistent disconnect/reconnect rows (optionally per camera)."""
    events = await list_conn_events(db, camera_id=camera_id, limit=1000)
    disconnects = sum(1 for e in events if e.kind == DISCONNECT_KIND)
    reconnects = sum(1 for e in events if e.kind == RECONNECT_KIND)
    return {
        "ok": True,
        "success": True,
        "total": len(events),
        "disconnects": disconnects,
        "reconnects": reconnects,
        "camera_id": _coerce_camera_id(camera_id),
        "kinds": list(CONN_KINDS),
    }


# Aliases for flexible callers.
emit = record_transition
emit_event = record_transition
record = record_transition
record_event = record_transition
transition = record_transition
handle_state_change = record_transition
disconnect = emit_disconnect
reconnect = emit_reconnect
emit_offline = emit_disconnect
emit_online = emit_reconnect
last_event = get_last_conn_event
last = get_last_conn_event
state = get_conn_state
conn_state = get_conn_state
list_events = list_conn_events
all_events = list_conn_events
summary = get_summary

__all__ = [
    "DISCONNECT_KIND",
    "RECONNECT_KIND",
    "CONN_KINDS",
    "event_to_dict",
    "emit_disconnect",
    "emit_reconnect",
    "record_transition",
    "list_conn_events",
    "get_last_conn_event",
    "get_conn_state",
    "get_summary",
]
