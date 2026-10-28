"""Bulk event acknowledge / escalate API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.

Verification note: ``app/services/event_ack.py`` and ``app/api/event_ack.py``
only implement single-event acknowledge/escalate
(``POST /api/events/{event_id}/acknowledge`` etc. plus top-level
``POST /api/event-ack/acknowledge``). There is no bulk endpoint. This router
adds the missing bulk acknowledge / escalate endpoints backed by
``app/services/bulk_ack.py`` (which itself delegates per-event to
``app/services/event_ack.py``).

Endpoints (POST, JSON bodies are tolerant of alias keys):

- ``/api/events/bulk-acknowledge`` -- acknowledge many events (default)
- ``/api/events/bulk-escalate`` -- escalate many events
- ``/api/events/bulk-ack`` -- generic (``action``/``mode`` selects)

Hyphen/underscore and ``/api/event-ack/...`` aliases are registered for
every endpoint. ``action`` (``acknowledge``/``escalate``) may also be sent
explicitly to the generic endpoint.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from app.services import bulk_ack as svc

router = APIRouter(tags=["bulk_ack"])


class BulkAckRequest(BaseModel):
    event_ids: Optional[Any] = None
    ids: Optional[Any] = None
    event_id: Optional[Any] = None
    id: Optional[Any] = None
    events: Optional[Any] = None
    actor: Optional[Any] = None
    acknowledged_by: Optional[Any] = None
    reason: Optional[Any] = None
    note: Optional[Any] = None
    level: Optional[Any] = None
    action: Optional[Any] = None
    mode: Optional[Any] = None
    operation: Optional[Any] = None

    model_config = {"extra": "allow"}


def _payload_dict(payload: Any, request: Request | None = None) -> Dict[str, Any]:
    if payload is None:
        data: Dict[str, Any] = {}
    elif isinstance(payload, str):
        data = {"reason": payload}
    elif isinstance(payload, (list, tuple, set)):
        data = {"event_ids": list(payload)}
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
    for nested_key in ("payload", "params", "data", "body", "request", "fields", "entry", "ack", "acknowledgement", "escalation"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _extract_action(data: Dict[str, Any], default: str) -> str:
    for key in ("action", "mode", "operation", "op", "type"):
        if data.get(key) is not None:
            return svc.normalize_action(data.get(key), default=default)
    return default


async def _bulk_impl(data: Dict[str, Any], default_action: str = "acknowledge") -> Dict[str, Any]:
    raw_ids = None
    for key in svc.EVENT_ID_KEYS:
        if data.get(key) is not None:
            raw_ids = data.get(key)
            break
    if raw_ids is None:
        raise HTTPException(status_code=400, detail="event_ids is required")
    action = _extract_action(data, default_action)
    forward = {k: v for k, v in data.items() if k not in svc.EVENT_ID_KEYS and k not in ("action", "actor", "reason", "level", "event_ids")}
    try:
        return svc.bulk_ack_events(
            event_ids=raw_ids,
            action=action,
            actor=data.get("actor", data.get("acknowledged_by", data.get("acked_by", data.get("user", data.get("operator"))))),
            reason=data.get("reason", data.get("note", data.get("comment", data.get("message")))),
            level=data.get("level", data.get("priority", data.get("severity"))),
            **forward,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


def _make_bulk_handler(default_action: str):
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
    ):
        data = _payload_dict(payload, request)
        return await _bulk_impl(data, default_action=default_action)

    _endpoint.__name__ = f"bulk_ack_{default_action}_{abs(hash(default_action)) % 10_000_000}"
    return _endpoint


# 3-segment paths avoid colliding with ``/{event_id}/ack`` (4 segments).
_ACK_PATHS = (
    "/api/events/bulk-acknowledge",
    "/api/events/bulk_acknowledge",
    "/api/events/bulk-ack-acknowledge",
    "/api/events/bulk_ack_acknowledge",
    "/api/events/bulk-ack",
    "/api/events/bulk_ack",
    "/api/events/acknowledge-bulk",
    "/api/events/acknowledge_bulk",
    "/api/event-ack/bulk-acknowledge",
    "/api/event_ack/bulk_acknowledge",
    "/api/event-ack/bulk-ack",
    "/api/event_ack/bulk_ack",
    "/api/event-acks/bulk-acknowledge",
    "/api/acknowledgements/bulk-acknowledge",
)

_ESCALATE_PATHS = (
    "/api/events/bulk-escalate",
    "/api/events/bulk_escalate",
    "/api/events/bulk-escalation",
    "/api/events/bulk_escalation",
    "/api/events/escalate-bulk",
    "/api/event-ack/bulk-escalate",
    "/api/event_ack/bulk_escalate",
    "/api/event-ack/bulk-escalation",
    "/api/acknowledgements/bulk-escalate",
)


def _slug(p: str) -> str:
    return p.strip("/").replace("/", "_").replace("-", "_")


for _p in _ACK_PATHS:
    router.add_api_route(
        _p, _make_bulk_handler("acknowledge"), methods=["POST"],
        operation_id=f"bulk_ack_acknowledge_{_slug(_p)}",
    )
for _p in _ESCALATE_PATHS:
    router.add_api_route(
        _p, _make_bulk_handler("escalate"), methods=["POST"],
        operation_id=f"bulk_ack_escalate_{_slug(_p)}",
    )


# Compatibility aliases.
bulk_ack_router = router
bulk_acknowledge_router = router
bulk_escalate_router = router
event_bulk_ack_router = router
api_router = router

__all__ = ["router", "BulkAckRequest"]
