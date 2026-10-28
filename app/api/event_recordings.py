"""Event-to-recording finder API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.

Answers "which recordings cover event X time": given an event id (DB
lookup) or inline event/camera/timestamp hints, returns the ``Recording``
rows whose ``[started_at, ended_at]`` overlaps the event window. A
``POST .../discover`` endpoint also accepts an explicit ``recordings``
candidate list for offline use (no DB required).
"""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.event_recordings as rec_mod
from app.services.event_recordings import find_recordings_for_event

router = APIRouter(tags=["event_recordings"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"event": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"event": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, (list, tuple)):
        data = {"recordings": list(payload)}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {"event": str(payload)}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    vals = request.query_params.getlist(k)
                    data[k] = vals[0] if len(vals) == 1 else vals
        except Exception:  # noqa: BLE001
            pass
    for nested_key in ("event", "link", "payload", "options", "config",
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


def _has_candidates(data: dict) -> bool:
    for k in ("recordings", "recordings_list", "candidates", "rows", "items"):
        v = data.get(k)
        if isinstance(v, (list, tuple)) and len(v) > 0:
            return True
        if v is not None and not isinstance(v, (list, tuple)):
            return True
    return False


def _has_inline_hints(data: dict) -> bool:
    return any(data.get(k) is not None for k in (
        "camera_id", "cameraId", "camera", "camera_name", "cameraName",
        "created_at", "createdAt", "timestamp", "time", "event_time",
        "start", "start_time", "end", "end_time",
    ))


async def _do_find(event_id: int, data: dict, db: Any) -> List[dict]:
    eid = _require_event_id(event_id)
    # Offline path: explicit candidate list needs no DB.
    if _has_candidates(data):
        inline: Optional[dict] = None
        for key in ("event", "source", "source_event"):
            if isinstance(data.get(key), dict):
                inline = dict(data.get(key))
                break
        if inline is None and _has_inline_hints(data):
            inline = {"id": eid, "event_id": eid}
            for k in ("camera_id", "cameraId", "camera", "camera_name", "cameraName",
                      "created_at", "createdAt", "timestamp", "time", "event_time"):
                if data.get(k) is not None:
                    inline[k] = data.get(k)
        if inline is not None:
            return find_recordings_for_event(inline, **data)
        return find_recordings_for_event(eid, **data)
    if db is not None:
        try:
            out = await rec_mod.find_recordings_for_event_async(eid, db=db, **data)
            if out:
                return out
            # Empty: 404 unless the event row exists or inline hints were given.
            try:
                row = await rec_mod._fetch_event(db, int(eid))  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                row = None
            if row is not None or _has_inline_hints(data):
                return []
            raise HTTPException(status_code=404, detail="Event not found")
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            pass
        # If the event row exists but nothing overlaps, return [] (not 404).
        try:
            row = await rec_mod._fetch_event(db, int(eid))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            row = None
        if row is not None:
            return []
    else:
        # No DB: try pure sync hints before 404.
        if _has_inline_hints(data):
            try:
                return find_recordings_for_event({"id": eid, "event_id": eid, **data}, **data)
            except Exception:  # noqa: BLE001
                return []
    raise HTTPException(status_code=404, detail="Event not found")


def _register_event_routes(r: APIRouter, base: str) -> None:
    from fastapi import Depends as _Depends  # noqa: E402

    from app.api.deps import db_session as _db_session  # noqa: E402

    async def _list(
        event_id: int,
        request: Request,
        db: Any = _Depends(_db_session),
    ):
        return await _do_find(event_id, _normalize(None, request), db)

    async def _discover(
        event_id: int,
        payload: Any = Body(default=None),
        request: Request = None,  # type: ignore[assignment]
        db: Any = _Depends(_db_session),
    ):
        return await _do_find(event_id, _normalize(payload, request), db)

    _list.__name__ = f"list_event_recordings_{abs(hash(base)) % 10_000_000}"
    _discover.__name__ = f"discover_event_recordings_{abs(hash(base)) % 10_000_000}"

    r.get(base, summary="List recordings covering event")(_list)
    r.get(base + "/", include_in_schema=False)(_list)
    r.post(base, status_code=status.HTTP_200_OK, summary="Find recordings covering event")(_discover)
    r.post(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)(_discover)
    r.post(base + "/discover", summary="Discover recordings for event")(_discover)
    r.post(base + "/find", include_in_schema=False)(_discover)


_EVENT_BASES = (
    "/api/events/{event_id}/recordings",
    "/api/events/{event_id}/event-recordings",
    "/api/events/{event_id}/event_recordings",
    "/api/events/{event_id}/covering-recordings",
)
for _base in _EVENT_BASES:
    _register_event_routes(router, _base)


def _register_top_level(r: APIRouter, base: str) -> None:
    from fastapi import Depends as _Depends  # noqa: E402

    from app.api.deps import db_session as _db_session  # noqa: E402

    async def _list_all(
        request: Request,
        event_id: Optional[int] = Query(default=None),
        db: Any = _Depends(_db_session),
    ):
        data = _normalize(None, request)
        eid: Optional[int] = event_id
        if eid is None:
            try:
                for alias in ("event_id", "eventId", "event"):
                    if alias in request.query_params:
                        eid = _coerce_int_or_none(request.query_params.get(alias))
                        break
            except Exception:  # noqa: BLE001
                pass
        if eid is not None:
            return await _do_find(int(eid), data, db)
        # Time-window query without an event id: needs camera + start/end.
        has_time = any(data.get(k) is not None for k in (
            "camera_id", "cameraId", "camera", "start", "start_time",
            "end", "end_time",
        ))
        has_candidates = _has_candidates(data)
        if has_candidates or (has_time and db is not None):
            if db is not None:
                try:
                    return await rec_mod.find_recordings_for_event_async(**data, db=db)
                except Exception:  # noqa: BLE001
                    pass
            try:
                return find_recordings_for_event(**data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc))
        if has_time and has_candidates:
            try:
                return find_recordings_for_event(**data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc))
        raise HTTPException(status_code=400, detail="event_id is required")

    async def _discover_top(
        payload: Any = Body(default=None),
        request: Request = None,  # type: ignore[assignment]
        db: Any = _Depends(_db_session),
    ):
        data = _normalize(payload, request)
        raw_eid = data.get("event_id", data.get("eventId", data.get("event")))
        if isinstance(raw_eid, dict):
            raw_eid = raw_eid.get("id", raw_eid.get("event_id"))
        eid = _coerce_int_or_none(raw_eid)
        inline: Optional[dict] = None
        for key in ("event", "source", "source_event"):
            if isinstance(data.get(key), dict):
                inline = dict(data.get(key))
                break
        has_candidates = _has_candidates(data)
        if eid is None and inline is None:
            if has_candidates or any(data.get(k) is not None for k in (
                "camera_id", "camera", "camera_name", "created_at", "timestamp", "time",
                "start", "start_time", "end", "end_time",
            )):
                if db is not None and not has_candidates:
                    try:
                        return await rec_mod.find_recordings_for_event_async(**data, db=db)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    return find_recordings_for_event(data, **data)
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(status_code=400, detail=str(exc))
            raise HTTPException(status_code=400, detail="event_id is required")
        if eid is not None:
            return await _do_find(int(eid), data, db)
        assert inline is not None
        inline.setdefault("id", 0)
        if db is not None and not has_candidates:
            try:
                return await rec_mod.find_recordings_for_event_async(inline, db=db, **data)
            except Exception:  # noqa: BLE001
                pass
        return find_recordings_for_event(inline, **data)

    _list_all.__name__ = f"list_event_recordings_top_{abs(hash(base)) % 10_000_000}"
    _discover_top.__name__ = f"discover_event_recordings_top_{abs(hash(base)) % 10_000_000}"

    r.get(base, summary="List recordings for event")(_list_all)
    r.get(base + "/", include_in_schema=False)(_list_all)
    r.post(base + "/discover", summary="Discover recordings for event")(_discover_top)
    r.post(base + "/find", include_in_schema=False)(_discover_top)
    r.post(base, status_code=status.HTTP_200_OK, include_in_schema=False)(_discover_top)


_TOP_BASES = (
    "/api/event-recordings",
    "/api/event_recordings",
    "/api/eventrecordings",
)
_seen_top: set = set()
for _base in _TOP_BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen_top:
        continue
    _seen_top.add(_norm)
    _register_top_level(router, _base)


# Compatibility aliases.
event_recordings_router = router
recordings_router = router
event_recording_router = router
api_router = router

__all__ = ["router"]
