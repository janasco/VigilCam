from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.models.entities import Event
from app.services.actions import (
    ActionExecutor,
    action_executor,
    execute as execute_action,
)

router = APIRouter(prefix="/api/actions", tags=["actions"])


_URL_FIELDS = ("target", "url", "uri", "endpoint", "webhook_url", "webhook", "path", "file", "file_path", "filename", "destination", "dest")
_TYPE_FIELDS = ("type", "action_type", "action", "kind", "operation", "mode", "name", "action_name")
_PAYLOAD_FIELDS = ("payload", "data", "body", "params", "content", "message", "record")
_EVENT_FIELDS = ("event_id", "eventId", "event", "id", "alert_id", "alertId")


class ActionRequest(BaseModel):
    event_id: Optional[int] = Field(default=None, alias="event_id")
    eventId: Optional[int] = None
    event: Optional[int] = None
    id: Optional[int] = None
    alert_id: Optional[int] = None
    alertId: Optional[int] = None

    type: Optional[str] = None
    action_type: Optional[str] = Field(default=None, alias="action_type")
    action: Optional[str] = None
    kind: Optional[str] = None
    operation: Optional[str] = None
    mode: Optional[str] = None
    name: Optional[str] = None
    action_name: Optional[str] = None

    target: Optional[str] = None
    url: Optional[str] = None
    uri: Optional[str] = None
    endpoint: Optional[str] = None
    webhook_url: Optional[str] = None
    webhook: Optional[str] = None
    path: Optional[str] = None
    file: Optional[str] = None
    file_path: Optional[str] = None
    filename: Optional[str] = None
    destination: Optional[str] = None
    dest: Optional[str] = None

    payload: Optional[Any] = None
    data: Optional[Any] = None
    body: Optional[Any] = None
    params: Optional[Any] = None
    content: Optional[Any] = None
    message: Optional[str] = None
    record: Optional[Any] = None

    timeout: Optional[float] = None
    test: Optional[bool] = None
    force: Optional[bool] = None

    model_config = {"populate_by_name": True, "extra": "allow"}


def _first(mapping: Dict[str, Any], names) -> Any:
    for name in names:
        if name in mapping and mapping.get(name) is not None:
            return mapping.get(name)
    lower = {str(k).lower(): v for k, v in mapping.items()}
    for name in names:
        if name.lower() in lower and lower[name.lower()] is not None:
            return lower[name.lower()]
    return None


def _to_flat(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, BaseModel):
        data = payload.model_dump()
        extra = getattr(payload, "model_extra", None) or {}
        data.update(extra)
        return data
    if isinstance(payload, dict):
        return payload
    if payload is None:
        return {}
    return {"payload": payload}


def _resolve(request_obj: Optional[ActionRequest], body: Any, db) -> Dict[str, Any]:
    flat = _to_flat(request_obj) if request_obj is not None else {}
    if isinstance(body, dict):
        flat.update(body)
    # merge nested
    nested_merged = {}
    for key in ("settings", "relay", "config", "data", "payload", "body", "params"):
        n = flat.get(key)
        if isinstance(n, dict):
            nested_merged.update(n)
    merged = dict(nested_merged)
    merged.update(flat)

    # Resolve control fields preferentially from top-level request, falling back to nested.
    event_id = _first(flat, _EVENT_FIELDS)

    action_type = _first(flat, _TYPE_FIELDS)
    if action_type is None:
        action_type = _first(nested_merged, _TYPE_FIELDS)

    target = _first(flat, _URL_FIELDS)
    if target is None:
        target = _first(nested_merged, _URL_FIELDS)

    payload = None
    for name in ("payload", "data", "body", "params", "content", "record"):
        if name in flat and flat.get(name) is not None:
            payload = flat.get(name)
            break
    if payload is None:
        for name in ("payload", "data", "body", "params", "content", "record"):
            if name in nested_merged and nested_merged.get(name) is not None:
                payload = nested_merged.get(name)
                break
    if payload is None:
        if any(k in flat for k in ("message", "text", "line", "entry", "log", "value")):
            payload = {k: flat[k] for k in ("message", "text", "line", "entry", "log", "value") if k in flat}
    timeout = flat.get("timeout", merged.get("timeout"))
    test = bool(flat.get("test") or flat.get("force") or merged.get("test") or merged.get("force"))

    if action_type is None:
        if isinstance(target, str) and target.lower().startswith(("http://", "https://")):
            action_type = "webhook"
        else:
            action_type = "webhook"

    return {
        "event_id": event_id,
        "type": action_type,
        "target": target,
        "payload": payload if payload is not None else {},
        "timeout": timeout,
        "test": test,
        "db": db,
        "merged": merged,
    }


async def _maybe_event(db: Optional[AsyncSession], event_id: Optional[int]):
    if db is None or event_id is None:
        return None
    try:
        return await db.get(Event, int(event_id))
    except Exception:
        return None


@router.post("/trigger")
async def trigger_action(
    payload: Optional[Any] = Body(default=None),
    db: AsyncSession = Depends(db_session),
):
    # FastAPI may parse as dict or ActionRequest.
    request_obj = None
    body_dict = None
    if isinstance(payload, dict):
        body_dict = payload
    elif isinstance(payload, ActionRequest):
        request_obj = payload
    elif payload is None:
        body_dict = {}
    else:
        try:
            request_obj = ActionRequest.model_validate(payload)
        except Exception:
            body_dict = {"payload": payload}

    resolved = _resolve(request_obj, body_dict, db)

    # Enrich payload with event context if possible.
    ev = await _maybe_event(db, resolved["event_id"])
    out_payload = resolved["payload"]
    if isinstance(out_payload, dict) and ev is not None:
        out_payload.setdefault("event_kind", getattr(ev, "kind", None))
        out_payload.setdefault("event_label", getattr(ev, "label", None))
        out_payload.setdefault("event_payload", getattr(ev, "payload", None))

    executor = action_executor
    result = executor.execute(
        event_id=resolved["event_id"],
        type=resolved["type"],
        target=resolved["target"],
        payload=out_payload,
        timeout=resolved["timeout"],
        test=resolved["test"],
    )
    # Ensure dict
    if not isinstance(result, dict):
        result = {"result": result}
    result.setdefault("event_id", resolved["event_id"])
    result.setdefault("type", resolved["type"])
    result.setdefault("target", resolved["target"])
    # HTTP status: if executor reports not ok due to bad input, still 200 with detail.
    return result


@router.post("/test")
async def test_action(
    payload: Optional[Any] = Body(default=None),
    db: AsyncSession = Depends(db_session),
):
    request_obj = None
    body_dict = None
    if isinstance(payload, dict):
        body_dict = payload
    elif isinstance(payload, ActionRequest):
        request_obj = payload
    elif payload is None:
        body_dict = {}
    else:
        try:
            request_obj = ActionRequest.model_validate(payload)
        except Exception:
            body_dict = {"payload": payload}

    resolved = _resolve(request_obj, body_dict, db)
    # Default to webhook if no type provided.
    if not resolved.get("type"):
        resolved["type"] = "webhook"
    # Prefer target from payload; if none, check merged target.
    test_flag = True
    out_payload = resolved["payload"] if isinstance(resolved["payload"], dict) else {"payload": resolved["payload"]}
    out_payload.setdefault("test", True)
    out_payload.setdefault("event", "vigilcam.action.test")

    executor = action_executor
    result = executor.execute(
        event_id=resolved["event_id"],
        type=resolved["type"],
        target=resolved["target"],
        payload=out_payload,
        timeout=resolved["timeout"],
        test=True,
    )
    if not isinstance(result, dict):
        result = {"result": result}
    result.setdefault("event_id", resolved["event_id"])
    result.setdefault("type", resolved["type"])
    result.setdefault("target", resolved["target"])
    return result
