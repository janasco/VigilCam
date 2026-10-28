"""Unacknowledged-events smart-filter API router (oldest unacked first).

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.

Verification note: ``app/services/bulk_ack.py`` already returns per-event
results (``per_event`` / ``per_event_states`` / ``updated_event_ids`` /
``updated_count``) suitable for UI progress display, so no ``ack_progress``
layer was built. This router exposes the complementary read path backed by
``app/services/unacked_queue.py`` (which itself reads ack state from
``app/services/event_ack.py`` without mutating it).

Endpoints (GET and POST; JSON bodies and query params share tolerant keys):

- ``/api/events/unacked`` -- paginated queue, oldest-first (default)
- ``/api/events/unacked/next`` -- single oldest unacked entry (operator peek)
- ``/api/events/unacked/count`` -- counts only (badge/counter UIs)

Hyphen/underscore and ``/api/event-ack/...`` / ``/api/acknowledgements/...``
aliases are registered for every endpoint. Supported keys include
``entries`` / ``events`` / ``event_ids`` (inline scan set), ``limit``,
``offset`` / ``page``, ``order`` (``oldest`` default, ``newest`` opt-in),
``camera_id(s)``, ``label(s)``, and ``include_escalated``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from app.services import unacked_queue as svc

router = APIRouter(tags=["unacked_queue"])


class UnackedQueueRequest(BaseModel):
    entries: Optional[Any] = None
    events: Optional[Any] = None
    items: Optional[Any] = None
    event_ids: Optional[Any] = None
    ids: Optional[Any] = None
    event_id: Optional[Any] = None
    limit: Optional[Any] = None
    count: Optional[Any] = None
    offset: Optional[Any] = None
    skip: Optional[Any] = None
    page: Optional[Any] = None
    order: Optional[Any] = None
    sort: Optional[Any] = None
    direction: Optional[Any] = None
    camera_id: Optional[Any] = None
    camera_ids: Optional[Any] = None
    camera: Optional[Any] = None
    label: Optional[Any] = None
    labels: Optional[Any] = None
    kind: Optional[Any] = None
    include_escalated: Optional[Any] = None

    model_config = {"extra": "allow"}


def _payload_dict(payload: Any, request: Request | None = None) -> Dict[str, Any]:
    if payload is None:
        data: Dict[str, Any] = {}
    elif isinstance(payload, str):
        data = {"label": payload}
    elif isinstance(payload, (list, tuple, set)):
        data = {"entries": list(payload)}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump(exclude_unset=False))
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}
    data = {k: v for k, v in data.items() if v is not None}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                vals = request.query_params.getlist(k)
                val: Any = vals[0] if len(vals) == 1 else vals
                if k not in data or data.get(k) is None:
                    data[k] = val
        except Exception:  # noqa: BLE001
            pass
    for nested_key in (
        "payload", "params", "data", "body", "request", "fields",
        "entry", "filter", "filters", "query", "criteria",
        "ack", "acknowledgement",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


async def _queue_impl(data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        db = data.pop("db", None)
        session = data.pop("session", None)
        if db is not None or session is not None:
            return await svc.get_queue_async(db=db, session=session, **data)
        return svc.get_queue(data)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


async def _next_impl(data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        db = data.pop("db", None)
        session = data.pop("session", None)
        if db is not None or session is not None:
            item = await svc.peek_next_async(db=db, session=session, **data)
        else:
            item = svc.peek_next(data)
        return {
            "status": "ok",
            "ok": True,
            "success": True,
            "next": item,
            "item": item,
            "event": item,
            "empty": item is None,
            "next_id": (item.get("id") if isinstance(item, dict) else None),
            "next_event_id": (item.get("event_id") if isinstance(item, dict) else None),
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


async def _count_impl(data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        db = data.pop("db", None)
        session = data.pop("session", None)
        if db is not None or session is not None:
            return await svc.get_count_async(db=db, session=session, **data)
        return svc.get_count(data)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


def _make_queue_handler():
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
    ):
        data = _payload_dict(payload, request)
        return await _queue_impl(data)

    _endpoint.__name__ = f"unacked_queue_{abs(hash('queue')) % 10_000_000}"
    return _endpoint


def _make_next_handler():
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
    ):
        data = _payload_dict(payload, request)
        return await _next_impl(data)

    _endpoint.__name__ = f"unacked_next_{abs(hash('next')) % 10_000_000}"
    return _endpoint


def _make_count_handler():
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
    ):
        data = _payload_dict(payload, request)
        return await _count_impl(data)

    _endpoint.__name__ = f"unacked_count_{abs(hash('count')) % 10_000_000}"
    return _endpoint


_QUEUE_PATHS = (
    "/api/events/unacked",
    "/api/events/unacked-queue",
    "/api/events/unacked_queue",
    "/api/events/unacknowledged",
    "/api/events/unacknowledged-queue",
    "/api/events/unacknowledged_queue",
    "/api/events/pending-ack",
    "/api/events/pending_ack",
    "/api/events/ack-queue",
    "/api/events/ack_queue",
    "/api/events/ack-pending",
    "/api/events/ack_pending",
    "/api/event-ack/unacked",
    "/api/event_ack/unacked",
    "/api/event-ack/unacked-queue",
    "/api/event_ack/unacked_queue",
    "/api/event-ack/pending-ack",
    "/api/event-acks/unacked",
    "/api/acknowledgements/unacked",
    "/api/acknowledgments/unacked",
)

_NEXT_PATHS = (
    "/api/events/unacked/next",
    "/api/events/unacked-queue/next",
    "/api/events/unacked_queue/next",
    "/api/events/unacknowledged/next",
    "/api/events/pending-ack/next",
    "/api/events/pending_ack/next",
    "/api/events/ack-queue/next",
    "/api/event-ack/unacked/next",
    "/api/event_ack/unacked/next",
    "/api/acknowledgements/unacked/next",
)

_COUNT_PATHS = (
    "/api/events/unacked/count",
    "/api/events/unacked-queue/count",
    "/api/events/unacked_queue/count",
    "/api/events/unacknowledged/count",
    "/api/events/pending-ack/count",
    "/api/events/pending_ack/count",
    "/api/events/ack-queue/count",
    "/api/event-ack/unacked/count",
    "/api/event_ack/unacked/count",
    "/api/acknowledgements/unacked/count",
)


def _slug(p: str) -> str:
    return p.strip("/").replace("/", "_").replace("-", "_")


for _p in _QUEUE_PATHS:
    router.add_api_route(
        _p, _make_queue_handler(), methods=["GET", "POST"],
        operation_id=f"unacked_queue_{_slug(_p)}",
    )
for _p in _NEXT_PATHS:
    router.add_api_route(
        _p, _make_next_handler(), methods=["GET", "POST"],
        operation_id=f"unacked_next_{_slug(_p)}",
    )
for _p in _COUNT_PATHS:
    router.add_api_route(
        _p, _make_count_handler(), methods=["GET", "POST"],
        operation_id=f"unacked_count_{_slug(_p)}",
    )


# Compatibility aliases.
unacked_router = router
unacked_queue_router = router
unacknowledged_router = router
pending_ack_router = router
ack_queue_router = router
event_unacked_router = router
api_router = router

__all__ = ["router", "UnackedQueueRequest"]
