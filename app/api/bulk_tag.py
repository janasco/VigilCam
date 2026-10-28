"""Bulk event tagging API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.

Verification note: ``app/api/events.py`` only implements single-event
tagging (``POST /api/events/{event_id}/tags`` + ``/tag`` alias and the
matching ``DELETE``), plus ``GET /api/events/tags`` listing. The only bulk
operation there is bulk *delete*. This router adds the missing bulk
*tagging* endpoints.

Endpoints (POST, JSON bodies are tolerant of alias keys):

- ``/api/events/bulk-tag`` -- add (union) tags on many events (default)
- ``/api/events/bulk-set-tags`` -- replace tags on many events
- ``/api/events/bulk-remove-tags`` -- subtract tags on many events
- ``/api/events/bulk-clear-tags`` -- wipe tags on many events

Hyphen/underscore and ``/api/event-tags/...`` aliases are registered for
every endpoint. ``mode`` (``add``/``set``/``remove``/``clear``) may also be
sent explicitly to the generic endpoints.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi import Depends as _Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services import bulk_tag as svc

router = APIRouter(tags=["bulk_tag"])


class BulkTagRequest(BaseModel):
    event_ids: Optional[Any] = None
    ids: Optional[Any] = None
    event_id: Optional[Any] = None
    id: Optional[Any] = None
    events: Optional[Any] = None
    tags: Optional[Any] = None
    tag: Optional[Any] = None
    labels: Optional[Any] = None
    mode: Optional[Any] = None
    action: Optional[Any] = None
    operation: Optional[Any] = None

    model_config = {"extra": "allow"}


def _payload_dict(payload: Any, request: Request | None = None) -> Dict[str, Any]:
    if payload is None:
        data: Dict[str, Any] = {}
    elif isinstance(payload, str):
        data = {"tags": payload}
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
    for nested_key in ("payload", "params", "data", "body", "request", "fields", "entry"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _extract_event_ids(data: Dict[str, Any]) -> Any:
    for key in ("event_ids", "eventIds", "events_ids", "ids", "id_list", "idList",
                "event_id_list", "eventIdList", "events", "event_id", "eventId",
                "event", "id"):
        if data.get(key) is not None:
            return data.get(key)
    return None


def _extract_tags(data: Dict[str, Any]) -> Any:
    for key in ("tags", "tag", "tag_list", "tagList", "labels", "label",
                "values", "value", "items", "item", "names", "tagNames", "tag_names"):
        if data.get(key) is not None:
            return data.get(key)
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in ("tags", "tag"):
        if key in lower and lower[key] is not None:
            return lower[key]
    return None


def _extract_mode(data: Dict[str, Any], default: str) -> str:
    for key in ("mode", "action", "operation", "op"):
        if data.get(key) is not None:
            return svc.normalize_mode(data.get(key), default=default)
    return default


async def _bulk_impl(db: AsyncSession, data: Dict[str, Any], default_mode: str = "add") -> Dict[str, Any]:
    event_ids = _extract_event_ids(data)
    tags = _extract_tags(data)
    mode = _extract_mode(data, default_mode)
    # ``clear`` needs no tags; other modes require ids (service validates tags).
    if event_ids is None:
        raise HTTPException(status_code=400, detail="event_ids is required")
    try:
        return await svc.bulk_tag_events(db, event_ids=event_ids, tags=tags, mode=mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


def _make_bulk_handler(default_mode: str):
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = _Depends(db_session),
    ):
        data = _payload_dict(payload, request)
        return await _bulk_impl(db, data, default_mode=default_mode)

    _endpoint.__name__ = f"bulk_tag_{default_mode}_{abs(hash(default_mode)) % 10_000_000}"
    return _endpoint


# 3-segment paths avoid colliding with ``/{event_id}/tags`` (4 segments).
_ADD_PATHS = (
    "/api/events/bulk-tag",
    "/api/events/bulk_tag",
    "/api/events/bulk-tags",
    "/api/events/bulk_tags",
    "/api/events/tags-bulk",
    "/api/events/tags_bulk",
    "/api/events/bulk/add-tags",
    "/api/events/bulk-add-tags",
    "/api/events/bulk_add_tags",
    "/api/event-tags/bulk",
    "/api/event_tags/bulk",
    "/api/event-tags/bulk-tag",
    "/api/event_tags/bulk_tag",
)

_SET_PATHS = (
    "/api/events/bulk-set-tags",
    "/api/events/bulk_set_tags",
    "/api/events/bulk/set-tags",
    "/api/events/bulk-replace-tags",
    "/api/event-tags/bulk-set",
    "/api/event_tags/bulk_set",
)

_REMOVE_PATHS = (
    "/api/events/bulk-remove-tags",
    "/api/events/bulk_remove_tags",
    "/api/events/bulk/remove-tags",
    "/api/events/bulk-untag",
    "/api/events/bulk_untag",
    "/api/events/bulk-remove-tag",
    "/api/event-tags/bulk-remove",
    "/api/event_tags/bulk_remove",
)

_CLEAR_PATHS = (
    "/api/events/bulk-clear-tags",
    "/api/events/bulk_clear_tags",
    "/api/events/bulk/clear-tags",
    "/api/events/bulk-clear",
    "/api/event-tags/bulk-clear",
    "/api/event_tags/bulk_clear",
)


def _slug(p: str) -> str:
    return p.strip("/").replace("/", "_").replace("-", "_")


for _p in _ADD_PATHS:
    router.add_api_route(
        _p, _make_bulk_handler("add"), methods=["POST"],
        operation_id=f"bulk_tag_add_{_slug(_p)}",
    )
for _p in _SET_PATHS:
    router.add_api_route(
        _p, _make_bulk_handler("set"), methods=["POST"],
        operation_id=f"bulk_tag_set_{_slug(_p)}",
    )
for _p in _REMOVE_PATHS:
    router.add_api_route(
        _p, _make_bulk_handler("remove"), methods=["POST"],
        operation_id=f"bulk_tag_remove_{_slug(_p)}",
    )
for _p in _CLEAR_PATHS:
    router.add_api_route(
        _p, _make_bulk_handler("clear"), methods=["POST"],
        operation_id=f"bulk_tag_clear_{_slug(_p)}",
    )


# Compatibility aliases.
bulk_tag_router = router
bulk_tags_router = router
event_bulk_tag_router = router
api_router = router

__all__ = ["router", "BulkTagRequest"]
