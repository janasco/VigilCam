"""Stale-event auto-acknowledge API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.

Verification note: ``app/services/event_ack.py`` and ``app/api/event_ack.py``
only handle single-event manual acknowledge/escalate, and
``app/services/bulk_ack.py`` / ``app/api/bulk_ack.py`` only handle manual
bulk acknowledge/escalate with explicit ``event_ids``. ``auto_rules`` only
reports an ``auto_acknowledge`` flag and never mutates ack state. This router
exposes the missing automatic stale-event path backed by
``app/services/auto_ack.py`` (which delegates per-event to ``event_ack``).

Endpoints:

- ``POST /api/events/auto-ack`` -- auto-acknowledge stale events (run)
- ``GET/POST /api/events/auto-ack/preview`` -- dry-run (no mutation)
- ``GET /api/events/auto-ack/status`` -- policy + stale counts (no mutation)

Hyphen/underscore and ``/api/event-ack/...`` aliases are registered for
every endpoint. Bodies and query params share tolerant alias keys
(``stale_after_seconds`` / ``stale_after_days`` / ``older_than_*`` /
``actor`` / ``reason`` / ``entries`` / ``events`` / ``event_ids`` /
``dry_run`` / ``preview``).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from app.services import auto_ack as svc

router = APIRouter(tags=["auto_ack"])


class AutoAckRequest(BaseModel):
    stale_after_seconds: Optional[Any] = None
    stale_after: Optional[Any] = None
    stale_after_days: Optional[Any] = None
    stale_after_hours: Optional[Any] = None
    older_than_days: Optional[Any] = None
    older_than_seconds: Optional[Any] = None
    threshold_seconds: Optional[Any] = None
    actor: Optional[Any] = None
    acknowledged_by: Optional[Any] = None
    reason: Optional[Any] = None
    note: Optional[Any] = None
    entries: Optional[Any] = None
    events: Optional[Any] = None
    event_ids: Optional[Any] = None
    ids: Optional[Any] = None
    event_id: Optional[Any] = None
    dry_run: Optional[Any] = None
    preview: Optional[Any] = None

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
    for nested_key in (
        "payload", "params", "data", "body", "request", "fields",
        "entry", "policy", "settings", "filter", "filters", "quota",
        "ack", "acknowledgement",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


async def _run_impl(data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return svc.auto_ack_stale(**data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


async def _preview_impl(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(data)
    payload["dry_run"] = True
    return await _run_impl(payload)


async def _status_impl(data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return svc.get_status(**data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


def _make_run_handler():
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
    ):
        data = _payload_dict(payload, request)
        return await _run_impl(data)

    _endpoint.__name__ = f"auto_ack_run_{abs(hash('run')) % 10_000_000}"
    return _endpoint


def _make_preview_handler():
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
    ):
        data = _payload_dict(payload, request)
        return await _preview_impl(data)

    _endpoint.__name__ = f"auto_ack_preview_{abs(hash('preview')) % 10_000_000}"
    return _endpoint


def _make_status_handler():
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
    ):
        data = _payload_dict(payload, request)
        return await _status_impl(data)

    _endpoint.__name__ = f"auto_ack_status_{abs(hash('status')) % 10_000_000}"
    return _endpoint


_RUN_PATHS = (
    "/api/events/auto-ack",
    "/api/events/auto_ack",
    "/api/events/auto-acknowledge",
    "/api/events/auto_acknowledge",
    "/api/events/stale-auto-ack",
    "/api/events/stale_auto_ack",
    "/api/events/ack-stale",
    "/api/events/ack_stale",
    "/api/events/acknowledge-stale",
    "/api/events/acknowledge_stale",
    "/api/event-ack/auto-ack",
    "/api/event_ack/auto_ack",
    "/api/event-ack/auto-acknowledge",
    "/api/event_ack/auto_acknowledge",
    "/api/event-acks/auto-ack",
    "/api/acknowledgements/auto-ack",
)

_PREVIEW_PATHS = (
    "/api/events/auto-ack/preview",
    "/api/events/auto_ack/preview",
    "/api/events/auto-ack/dry-run",
    "/api/events/auto-ack/dry_run",
    "/api/events/auto_ack/dry_run",
    "/api/event-ack/auto-ack/preview",
    "/api/event_ack/auto_ack/preview",
    "/api/event-ack/auto-ack/dry-run",
    "/api/acknowledgements/auto-ack/preview",
)

_STATUS_PATHS = (
    "/api/events/auto-ack/status",
    "/api/events/auto_ack/status",
    "/api/events/auto-ack/policy",
    "/api/events/auto_ack/policy",
    "/api/events/auto-ack/config",
    "/api/event-ack/auto-ack/status",
    "/api/event_ack/auto_ack/status",
    "/api/event-ack/auto-ack/policy",
    "/api/acknowledgements/auto-ack/status",
)


def _slug(p: str) -> str:
    return p.strip("/").replace("/", "_").replace("-", "_")


for _p in _RUN_PATHS:
    router.add_api_route(
        _p, _make_run_handler(), methods=["POST"],
        operation_id=f"auto_ack_run_{_slug(_p)}",
    )
for _p in _PREVIEW_PATHS:
    router.add_api_route(
        _p, _make_preview_handler(), methods=["GET", "POST"],
        operation_id=f"auto_ack_preview_{_slug(_p)}",
    )
for _p in _STATUS_PATHS:
    router.add_api_route(
        _p, _make_status_handler(), methods=["GET", "POST"],
        operation_id=f"auto_ack_status_{_slug(_p)}",
    )


# Compatibility aliases.
auto_ack_router = router
auto_acknowledge_router = router
stale_ack_router = router
event_auto_ack_router = router
api_router = router

__all__ = ["router", "AutoAckRequest"]
