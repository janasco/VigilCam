"""Event notes/comments API router (CRUD per event).

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.event_notes as notes_mod
from app.services.event_notes import (
    create_note,
    delete_note,
    delete_notes_for_event,
    get_note,
    list_notes,
    update_note,
)

router = APIRouter(tags=["event_notes"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"text": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"text": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {"text": str(payload)}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass

    for nested_key in ("note", "comment", "payload", "options", "config",
                       "params", "data", "body", "request", "fields"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _coerce_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _require_event_id(event_id: Any) -> int:
    eid = _coerce_int_or_none(event_id)
    if eid is None:
        raise HTTPException(status_code=400, detail="event_id is required")
    return eid


def _note_or_404(note: Optional[dict], note_id: Any = None) -> dict:
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


def _create_for_event(event_id: int, data: dict) -> dict:
    data = dict(data)
    data["event_id"] = event_id
    try:
        return create_note(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _get_scoped(event_id: int, note_id: int) -> dict:
    note = get_note(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    try:
        if int(note.get("event_id")) != int(event_id):
            raise HTTPException(status_code=404, detail="Note not found")
    except HTTPException:
        raise
    except (TypeError, ValueError):
        pass
    return note


def _update_scoped(event_id: int, note_id: int, data: dict) -> dict:
    _get_scoped(event_id, note_id)
    data = {k: v for k, v in dict(data).items() if k not in ("event_id", "eventId", "event")}
    updated = update_note(note_id, **data) if data else update_note(note_id)
    # update_note(note_id, **data): first positional is note_id; handle dict merge.
    if updated is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return updated


def _delete_scoped(event_id: int, note_id: int) -> dict:
    _get_scoped(event_id, note_id)
    ok = delete_note(note_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"success": True, "ok": True, "deleted": [int(note_id)], "id": int(note_id)}


def _register_event_routes(r: APIRouter, base: str) -> None:
    @r.get(base, summary="List notes for event")
    async def _list(
        event_id: int,
        request: Request,
        limit: Optional[int] = Query(None),
        offset: Optional[int] = Query(None),
        skip: Optional[int] = Query(None),
        q: Optional[str] = Query(None),
    ):
        eid = _require_event_id(event_id)
        params: dict = {"event_id": eid}
        if limit is not None:
            params["limit"] = limit
        params["offset"] = offset if offset is not None else (skip if skip is not None else 0)
        if q is not None:
            params["q"] = q
        # merge any extra query params (search, etc.)
        try:
            for k, v in request.query_params.items():
                params.setdefault(k, v)
        except Exception:
            pass
        return list_notes(**params)

    @r.post(base, status_code=status.HTTP_200_OK, summary="Create note for event")
    @r.post(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _create(
        event_id: int,
        payload: Any = Body(default=None),
        request: Request = None,
    ):
        eid = _require_event_id(event_id)
        data = _normalize(payload, request)
        return _create_for_event(eid, data)

    @r.get(base + "/{note_id}", summary="Get single note")
    async def _get(event_id: int, note_id: int):
        eid = _require_event_id(event_id)
        nid = _coerce_int_or_none(note_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="note_id is required")
        return _get_scoped(eid, nid)

    @r.put(base + "/{note_id}", summary="Update note")
    @r.patch(base + "/{note_id}", include_in_schema=False)
    async def _update(
        event_id: int,
        note_id: int,
        payload: Any = Body(default=None),
        request: Request = None,
    ):
        eid = _require_event_id(event_id)
        nid = _coerce_int_or_none(note_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="note_id is required")
        data = _normalize(payload, request)
        return _update_scoped(eid, nid, data)

    @r.delete(base + "/{note_id}", summary="Delete note")
    async def _delete(event_id: int, note_id: int):
        eid = _require_event_id(event_id)
        nid = _coerce_int_or_none(note_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="note_id is required")
        return _delete_scoped(eid, nid)

    @r.delete(base, include_in_schema=False)
    async def _delete_all(event_id: int):
        eid = _require_event_id(event_id)
        n = delete_notes_for_event(eid)
        return {"success": True, "ok": True, "deleted_count": n}


_EVENT_BASES = (
    "/api/events/{event_id}/notes",
    "/api/events/{event_id}/note",
    "/api/events/{event_id}/comments",
    "/api/events/{event_id}/comment",
)
for _base in _EVENT_BASES:
    _register_event_routes(router, _base)


def _register_top_level(r: APIRouter, base: str) -> None:
    @r.get(base, summary="List all event notes")
    @r.get(base + "/", include_in_schema=False)
    async def _list_all(
        request: Request,
        event_id: Optional[int] = Query(None),
        limit: Optional[int] = Query(None),
        offset: Optional[int] = Query(None),
        skip: Optional[int] = Query(None),
        q: Optional[str] = Query(None),
    ):
        params: dict = {}
        # prefer explicit query, fall back to any alias present in raw query params
        eid = event_id
        if eid is None:
            try:
                for alias in ("event_id", "eventId", "event"):
                    if alias in request.query_params:
                        eid = _coerce_int_or_none(request.query_params.get(alias))
                        break
            except Exception:
                pass
        if eid is not None:
            params["event_id"] = eid
        if limit is not None:
            params["limit"] = limit
        params["offset"] = offset if offset is not None else (skip if skip is not None else 0)
        if q is not None:
            params["q"] = q
        try:
            for k, v in request.query_params.items():
                params.setdefault(k, v)
        except Exception:
            pass
        return list_notes(**params)

    @r.post(base, status_code=status.HTTP_200_OK, summary="Create event note")
    @r.post(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _create_top(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        raw_eid = data.get("event_id", data.get("eventId", data.get("event")))
        eid = _coerce_int_or_none(raw_eid)
        if eid is None:
            raise HTTPException(status_code=400, detail="event_id is required")
        return _create_for_event(eid, data)

    @r.get(base + "/{note_id}", summary="Get event note")
    async def _get_top(note_id: int):
        nid = _coerce_int_or_none(note_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="note_id is required")
        return _note_or_404(get_note(nid), nid)

    @r.put(base + "/{note_id}", summary="Update event note")
    @r.patch(base + "/{note_id}", include_in_schema=False)
    async def _update_top(note_id: int, payload: Any = Body(default=None), request: Request = None):
        nid = _coerce_int_or_none(note_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="note_id is required")
        data = _normalize(payload, request)
        data = {k: v for k, v in data.items() if k not in ("event_id", "eventId", "event")}
        updated = update_note(nid, **data) if data else update_note(nid)
        return _note_or_404(updated, nid)

    @r.delete(base + "/{note_id}", summary="Delete event note")
    async def _delete_top(note_id: int):
        nid = _coerce_int_or_none(note_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="note_id is required")
        ok = delete_note(nid)
        if not ok:
            raise HTTPException(status_code=404, detail="Note not found")
        return {"success": True, "ok": True, "deleted": [int(nid)], "id": int(nid)}


_TOP_BASES = (
    "/api/event-notes",
    "/api/event_notes",
    "/api/event-notes/",
    "/api/notes",
)
_seen_top: set = set()
for _base in _TOP_BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen_top:
        continue
    _seen_top.add(_norm)
    _register_top_level(router, _norm)


# Compatibility aliases.
event_notes_router = router
notes_router = router
event_comments_router = router
api_router = router

__all__ = ["router"]
