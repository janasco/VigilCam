"""Event acknowledge / escalate API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

from app.services.event_ack import acknowledge, escalate, get_status, list_states

router = APIRouter(tags=["event_ack"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"reason": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"reason": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {"reason": str(payload)}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass

    for nested_key in ("payload", "options", "config", "params", "data",
                        "body", "request", "fields", "ack",
                        "acknowledgement", "escalation"):
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


def _event_id_from(data: dict, fallback: Any = None) -> Optional[int]:
    for key in ("event_id", "eventId", "event", "eventid", "id", "id_event"):
        if data.get(key) is not None:
            eid = _coerce_int_or_none(data.get(key))
            if eid is not None:
                return eid
    # case-insensitive fallback
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in ("event_id", "eventid", "event"):
        if lower.get(key) is not None:
            eid = _coerce_int_or_none(lower.get(key))
            if eid is not None:
                return eid
    return _coerce_int_or_none(fallback)


def _require_event_id(event_id: Any) -> int:
    eid = _coerce_int_or_none(event_id)
    if eid is None:
        raise HTTPException(status_code=400, detail="event_id is required")
    return eid


def _do_acknowledge(event_id: int, data: dict) -> dict:
    payload = dict(data)
    payload["event_id"] = event_id
    try:
        return acknowledge(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _do_escalate(event_id: int, data: dict) -> dict:
    payload = dict(data)
    payload["event_id"] = event_id
    try:
        return escalate(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _register_event_routes(r: APIRouter, base: str) -> None:
    @r.post(base + "/acknowledge", status_code=status.HTTP_200_OK, summary="Acknowledge event")
    @r.post(base + "/acknowledge/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _ack(
        event_id: int,
        payload: Any = Body(default=None),
        request: Request = None,
    ):
        eid = _require_event_id(event_id)
        return _do_acknowledge(eid, _normalize(payload, request))

    @r.post(base + "/ack", status_code=status.HTTP_200_OK, include_in_schema=False)
    @r.post(base + "/ack/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _ack_alias(
        event_id: int,
        payload: Any = Body(default=None),
        request: Request = None,
    ):
        eid = _require_event_id(event_id)
        return _do_acknowledge(eid, _normalize(payload, request))

    @r.post(base + "/escalate", status_code=status.HTTP_200_OK, summary="Escalate event")
    @r.post(base + "/escalate/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _esc(
        event_id: int,
        payload: Any = Body(default=None),
        request: Request = None,
    ):
        eid = _require_event_id(event_id)
        return _do_escalate(eid, _normalize(payload, request))

    @r.post(base + "/escalation", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _esc_alias(
        event_id: int,
        payload: Any = Body(default=None),
        request: Request = None,
    ):
        eid = _require_event_id(event_id)
        return _do_escalate(eid, _normalize(payload, request))

    @r.get(base + "/ack-status", summary="Get event ack/escalation status")
    @r.get(base + "/status", summary="Get event ack/escalation status", include_in_schema=False)
    @r.get(base + "/acknowledge", summary="Get event ack/escalation status", include_in_schema=False)
    async def _status(event_id: int):
        eid = _require_event_id(event_id)
        state = get_status(eid)
        if state is None:
            raise HTTPException(status_code=404, detail="No ack state for event")
        return state


_EVENT_BASES = (
    "/api/events/{event_id}/ack",
    "/api/events/{event_id}",
)
# Primary canonical base; the generic second base registers sub-paths.
_register_event_routes(router, "/api/events/{event_id}")
for _base in ("/api/events/{event_id}/ack",):
    # Also expose /api/events/{event_id}/ack/acknowledge style aliases.
    @router.post(_base + "/acknowledge", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _ack_nested(event_id: int, payload: Any = Body(default=None), request: Request = None):
        eid = _require_event_id(event_id)
        return _do_acknowledge(eid, _normalize(payload, request))

    @router.post(_base + "/escalate", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _esc_nested(event_id: int, payload: Any = Body(default=None), request: Request = None):
        eid = _require_event_id(event_id)
        return _do_escalate(eid, _normalize(payload, request))


def _register_top_level(r: APIRouter, base: str) -> None:
    _tag = base.replace("/", "_").replace("-", "_").strip("_") or "root"

    @r.post(base + "/acknowledge", status_code=status.HTTP_200_OK, summary="Acknowledge event", operation_id=f"ack_{_tag}_acknowledge")
    @r.post(base + "/acknowledge/", status_code=status.HTTP_200_OK, include_in_schema=False, operation_id=f"ack_{_tag}_acknowledge_slash")
    async def _ack_top(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        eid = _event_id_from(data)
        if eid is None:
            raise HTTPException(status_code=400, detail="event_id is required")
        return _do_acknowledge(eid, data)

    @r.post(base + "/ack", status_code=status.HTTP_200_OK, include_in_schema=False, operation_id=f"ack_{_tag}_ack")
    async def _ack_top_alias(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        eid = _event_id_from(data)
        if eid is None:
            raise HTTPException(status_code=400, detail="event_id is required")
        return _do_acknowledge(eid, data)

    @r.post(base + "/escalate", status_code=status.HTTP_200_OK, summary="Escalate event", operation_id=f"ack_{_tag}_escalate")
    @r.post(base + "/escalate/", status_code=status.HTTP_200_OK, include_in_schema=False, operation_id=f"ack_{_tag}_escalate_slash")
    async def _esc_top(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        eid = _event_id_from(data)
        if eid is None:
            raise HTTPException(status_code=400, detail="event_id is required")
        return _do_escalate(eid, data)

    @r.post(base + "/escalation", status_code=status.HTTP_200_OK, include_in_schema=False, operation_id=f"ack_{_tag}_escalation")
    async def _esc_top_alias(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        eid = _event_id_from(data)
        if eid is None:
            raise HTTPException(status_code=400, detail="event_id is required")
        return _do_escalate(eid, data)

    @r.get(base + "/status", summary="Get event ack status", operation_id=f"ack_{_tag}_status")
    async def _status_top(
        request: Request,
        event_id: Optional[int] = Query(None),
    ):
        data = _normalize(None, request)
        if event_id is not None:
            data["event_id"] = event_id
        eid = _event_id_from(data)
        if eid is None:
            raise HTTPException(status_code=400, detail="event_id is required")
        state = get_status(eid)
        if state is None:
            raise HTTPException(status_code=404, detail="No ack state for event")
        return state

    @r.get(base, summary="List event ack states", operation_id=f"ack_{_tag}_list")
    @r.get(base + "/", include_in_schema=False, operation_id=f"ack_{_tag}_list_slash")
    async def _list_top(
        request: Request,
        event_id: Optional[int] = Query(None),
        status_: Optional[str] = Query(None, alias="status"),
    ):
        params: dict = {}
        try:
            for k, v in request.query_params.items():
                params.setdefault(k, v)
        except Exception:
            pass
        if event_id is not None:
            params["event_id"] = event_id
        if status_ is not None:
            params["status"] = status_
        if "event_id" in params or "eventId" in params or "event" in params:
            eid = _event_id_from(params)
            if eid is None:
                raise HTTPException(status_code=400, detail="event_id is required")
            state = get_status(eid)
            if state is None:
                raise HTTPException(status_code=404, detail="No ack state for event")
            return state
        if params.get("status"):
            return list_states(status=params.get("status"))
        return list_states()

    @r.get(base + "/{event_id}", summary="Get event ack status by id", operation_id=f"ack_{_tag}_get")
    async def _get_top(event_id: int):
        eid = _require_event_id(event_id)
        state = get_status(eid)
        if state is None:
            raise HTTPException(status_code=404, detail="No ack state for event")
        return state


_TOP_BASES = (
    "/api/event-ack",
    "/api/event_ack",
    "/api/event-acks",
    "/api/acknowledgements",
    "/api/acknowledgments",
)
_seen: set = set()
for _base in _TOP_BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen:
        continue
    _seen.add(_norm)
    _register_top_level(router, _norm)


# Compatibility aliases.
event_ack_router = router
ack_router = router
acknowledge_router = router
escalate_router = router
escalation_router = router
api_router = router

__all__ = ["router"]
