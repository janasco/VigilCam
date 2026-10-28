"""INBOUND external trigger API (camera / doorbell webhooks).

Receives webhook calls *from* external devices and persists ``Event`` rows.
This is the inverse of the OUTBOUND ``webhook_queue`` API, which *sends*
webhooks to third-party URLs — the two must not be confused.

Routers defined here are picked up automatically by the ``app.api`` auto
loader in ``app.main`` (no ``main.py`` edit required).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services import inbound_trigger as inbound_service

router = APIRouter(tags=["inbound_trigger"])


def _body_to_dict(payload: Any) -> dict:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return dict(payload)
    if hasattr(payload, "model_dump"):
        try:
            dumped = payload.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:  # noqa: BLE001
            pass
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            text = bytes(payload).decode("utf-8", errors="replace") if isinstance(payload, (bytes, bytearray)) else payload
            import json as _json

            parsed = _json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            return {"label": text}
        except Exception:  # noqa: BLE001
            return {"label": str(payload)}
    try:
        return dict(payload)
    except Exception:  # noqa: BLE001
        return {}


def _provided_token(request: Optional[Request], data: dict) -> Optional[Any]:
    candidates = []
    if request is not None:
        try:
            qp = request.query_params
            for key in ("token", "auth_token", "trigger_token", "inbound_token", "api_token", "api_key", "key"):
                if key in qp and qp.get(key):
                    candidates.append(qp.get(key))
        except Exception:  # noqa: BLE001
            pass
        try:
            for header in ("x-trigger-token", "x-inbound-token", "x-api-token", "x-auth-token"):
                val = request.headers.get(header)
                if val:
                    candidates.append(val)
            auth = request.headers.get("authorization")
            if auth and auth.strip():
                candidates.append(auth.strip())
        except Exception:  # noqa: BLE001
            pass
    body_token = inbound_service.extract_token(data)
    if body_token is not None:
        candidates.append(body_token)
    for cand in candidates:
        if cand is not None and str(cand).strip():
            return cand
    return None


def _merged_data(payload: Any, request: Optional[Request], extra: Optional[dict] = None) -> dict:
    data = _body_to_dict(payload)
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    if extra:
        for k, v in extra.items():
            if v is not None and (k not in data or data.get(k) is None):
                data[k] = v
    return data


async def _handle(
    payload: Any,
    request: Optional[Request],
    db: AsyncSession,
    defaults: Optional[dict] = None,
) -> dict:
    data = _merged_data(payload, request, defaults)
    provided = _provided_token(request, data)
    try:
        event = await inbound_service.handle_inbound_trigger(db, data, token=provided)
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing trigger token")
    return {
        "success": True,
        "status": "triggered",
        "ok": True,
        "event": inbound_service.event_to_dict(event),
        **inbound_service.event_to_dict(event),
    }


@router.get("/api/inbound-trigger/status")
@router.get("/api/inbound_trigger/status")
@router.get("/api/inbound-triggers/status")
@router.get("/api/triggers/inbound/status")
async def inbound_status():
    expected = inbound_service.get_expected_token()
    return {
        "enabled": True,
        "token_required": bool(expected),
        "token_configured": bool(expected),
        "ok": True,
        "success": True,
    }


@router.post("/api/inbound-trigger", status_code=status.HTTP_201_CREATED)
@router.post("/api/inbound_trigger", status_code=status.HTTP_201_CREATED)
@router.post("/api/inbound-triggers", status_code=status.HTTP_201_CREATED)
@router.post("/api/inbound_triggers", status_code=status.HTTP_201_CREATED)
@router.post("/api/inbound/triggers", status_code=status.HTTP_201_CREATED)
@router.post("/api/triggers/inbound", status_code=status.HTTP_201_CREATED)
@router.post("/api/external-trigger", status_code=status.HTTP_201_CREATED)
@router.post("/api/external_trigger", status_code=status.HTTP_201_CREATED)
async def post_inbound_trigger(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = Depends(db_session),
):
    return await _handle(payload, request, db)


@router.post("/api/doorbell", status_code=status.HTTP_201_CREATED)
@router.post("/api/doorbells", status_code=status.HTTP_201_CREATED)
@router.post("/api/doorbell/press", status_code=status.HTTP_201_CREATED)
@router.post("/api/doorbell/ring", status_code=status.HTTP_201_CREATED)
async def post_doorbell(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = Depends(db_session),
):
    return await _handle(payload, request, db, defaults={"kind": "doorbell", "label": "doorbell"})


@router.post("/api/cameras/{camera_id}/inbound-trigger", status_code=status.HTTP_201_CREATED)
@router.post("/api/cameras/{camera_id}/inbound_trigger", status_code=status.HTTP_201_CREATED)
@router.post("/api/cameras/{camera_id}/trigger", status_code=status.HTTP_201_CREATED)
@router.post("/api/cameras/{camera_id}/external-trigger", status_code=status.HTTP_201_CREATED)
async def post_camera_inbound_trigger(
    camera_id: int,
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = Depends(db_session),
):
    return await _handle(payload, request, db, defaults={"camera_id": camera_id})
