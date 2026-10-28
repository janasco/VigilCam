from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.models.entities import RelayEventLog, RelaySettings
from app.services.relay import (
    _first_present,
    _flatten_payload,
    _get_settings_row,
    _is_masked,
    _mask_token,
    get_latest_event,
    ping,
    save_relay_settings,
)

router = APIRouter(prefix="/api/relay", tags=["relay"])


URL_FIELDS = ("relay_url", "url", "remote_url", "server_url", "relay", "base_url")
TOKEN_FIELDS = ("token", "relay_token", "api_token", "access_token", "auth_token", "api_key", "secret")
ENABLED_FIELDS = ("enabled", "is_enabled", "relay_enabled")


class RelaySettingsPayload(BaseModel):
    relay_url: Optional[str] = None
    url: Optional[str] = Field(default=None, alias="url")
    remote_url: Optional[str] = Field(default=None, alias="remote_url")
    server_url: Optional[str] = Field(default=None, alias="server_url")
    enabled: Optional[bool] = None
    is_enabled: Optional[bool] = Field(default=None, alias="is_enabled")
    relay_enabled: Optional[bool] = Field(default=None, alias="relay_enabled")
    token: Optional[str] = None
    relay_token: Optional[str] = Field(default=None, alias="relay_token")
    api_token: Optional[str] = Field(default=None, alias="api_token")
    access_token: Optional[str] = Field(default=None, alias="access_token")
    auth_token: Optional[str] = Field(default=None, alias="auth_token")
    api_key: Optional[str] = Field(default=None, alias="api_key")
    secret: Optional[str] = Field(default=None, alias="secret")
    # Allow arbitrary extra inputs.
    timeout: Optional[float] = None

    model_config = {"populate_by_name": True, "extra": "allow"}


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
    return {}


def _response_settings(row: Optional[RelaySettings]) -> Dict[str, Any]:
    if row is None:
        relay_url = None
        enabled = False
        token = None
        settings_id = None
        created_at = None
        updated_at = None
    else:
        relay_url = row.relay_url
        enabled = bool(row.enabled)
        token = row.token
        settings_id = row.id
        created_at = row.created_at
        updated_at = row.updated_at

    masked = _mask_token(token)
    return {
        "id": settings_id,
        "relay_url": relay_url,
        "url": relay_url,
        "remote_url": relay_url,
        "server_url": relay_url,
        "relay": relay_url,
        "enabled": enabled,
        "is_enabled": enabled,
        "relay_enabled": enabled,
        "token": masked,
        "relay_token": masked,
        "api_token": masked,
        "access_token": masked,
        "auth_token": masked,
        "api_key": masked,
        "secret": masked,
        "created_at": created_at,
        "updated_at": updated_at,
        "RELAY_URL": relay_url,
        "ENABLED": enabled,
        "TOKEN": masked,
    }


async def _get_or_create_row(db: AsyncSession) -> RelaySettings:
    row = await _get_settings_row(db)
    if row is None:  # pragma: no cover
        row = RelaySettings(singleton_key="default", enabled=False)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


@router.get("/settings")
async def get_relay_settings(db: AsyncSession = Depends(db_session)):
    row = await _get_or_create_row(db)
    return _response_settings(row)


@router.put("/settings")
async def put_relay_settings(
    payload: Optional[Any] = Body(default=None),
    db: AsyncSession = Depends(db_session),
):
    # Accept either a JSON object or a Pydantic model. FastAPI will pass dict for generic.
    if isinstance(payload, BaseModel):
        flat = _to_flat(payload)
    elif isinstance(payload, dict):
        flat = payload
    else:
        flat = {}

    # If FastAPI passed None because of missing body, try request? We keep simple.
    merged = _flatten_payload(flat)

    # Detect presence based on merged keys (case-insensitive).
    url_present, url_value = _first_present(merged, URL_FIELDS)
    enabled_present, enabled_value = _first_present(merged, ENABLED_FIELDS)
    token_present, token_value = _first_present(merged, TOKEN_FIELDS)

    # If fields were supplied at top level but hidden in extra, handled by flatten.

    row = await save_relay_settings(
        db,
        relay_url=url_value,
        relay_url_provided=url_present,
        enabled=enabled_value if enabled_value is not None else None,
        enabled_provided=enabled_present,
        token=token_value,
        token_provided=token_present and token_value is not None,
        payload=merged,
    )

    # Masked-token round-trip handling: if client sent a masked value, keep existing.
    # save_relay_settings already skips masked tokens, but ensure if a masked token was
    # the only token key present we do not overwrite. Already handled.

    return _response_settings(row)


@router.post("/test")
async def test_relay(
    payload: Optional[Any] = Body(default=None),
    db: AsyncSession = Depends(db_session),
):
    if isinstance(payload, BaseModel):
        flat = _to_flat(payload)
    elif isinstance(payload, dict):
        flat = payload
    else:
        flat = {}

    merged = _flatten_payload(flat)

    # Resolve saved settings for fallback + status.
    row = await _get_or_create_row(db)

    url_value = None
    url_present = False
    if merged:
        url_present, url_value = _first_present(merged, URL_FIELDS)
    if not url_present or url_value is None:
        url_value = row.relay_url

    token_value = None
    token_present = False
    if merged:
        token_present, token_value = _first_present(merged, TOKEN_FIELDS)
    if (not token_present or token_value is None) or _is_masked(str(token_value)):
        token_value = row.token
    else:
        if _is_masked(str(token_value)):
            token_value = row.token

    # If token sent but masked, keep saved.
    if token_present and _is_masked(_mask_token(token_value) if False else str(token_value)):
        token_value = row.token

    result = await ping(
        relay_url=url_value,
        token=token_value,
        timeout=(merged.get("timeout", 5.0) if isinstance(merged, dict) else 5.0),
        db=db,
        **{k: v for k, v in merged.items() if k not in URL_FIELDS + TOKEN_FIELDS + ENABLED_FIELDS},
    )

    # Ensure common response fields.
    if "tested_url" not in result:
        result["tested_url"] = result.get("relay_url") or result.get("url")
    return result


@router.get("/status")
async def relay_status(db: AsyncSession = Depends(db_session)):
    row = await _get_or_create_row(db)
    latest = await get_latest_event(db)

    last_ok = None
    last_status_code = None
    last_latency = None
    last_detail = None
    last_check = None
    if latest is not None:
        last_ok = bool(latest.ok)
        last_status_code = latest.status_code
        last_latency = latest.latency_ms
        last_detail = latest.detail
        last_check = latest.created_at

    masked = _mask_token(row.token)
    status_str = "disabled"
    if row.enabled:
        status_str = "connected" if last_ok else ("error" if last_check is not None else "untested")
    connected = bool(row.enabled and last_ok)

    return {
        "enabled": bool(row.enabled),
        "is_enabled": bool(row.enabled),
        "relay_enabled": bool(row.enabled),
        "relay_url": row.relay_url,
        "url": row.relay_url,
        "remote_url": row.relay_url,
        "token": masked,
        "api_token": masked,
        "status": status_str,
        "connected": connected,
        "reachable": connected,
        "last_check": last_check,
        "last_ok": last_ok,
        "last_success": last_ok,
        "last_status_code": last_status_code,
        "last_latency_ms": last_latency,
        "latency_ms": last_latency,
        "last_detail": last_detail,
        "detail": last_detail,
        "message": last_detail,
        "updated_at": row.updated_at,
        "id": row.id,
    }
