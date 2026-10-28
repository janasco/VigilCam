from __future__ import annotations

import time
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import RelayEventLog, RelaySettings


URL_FIELDS = ("relay_url", "url", "remote_url", "server_url", "relay", "base_url")
TOKEN_FIELDS = ("token", "relay_token", "api_token", "access_token", "auth_token", "api_key", "secret")
ENABLED_FIELDS = ("enabled", "is_enabled", "relay_enabled")


def _clean_url(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _clean_token(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _flatten_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    out: Dict[str, Any] = {}
    # Merge simple nested maps commonly used by clients.
    for key in ("settings", "relay", "config", "data", "payload"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            out.update(nested)
    out.update(payload)
    return out


def _first_present(flat: Dict[str, Any], fields) -> tuple[bool, Any]:
    for name in fields:
        if name in flat:
            return True, flat.get(name)
    # case-insensitive fallback
    lower = {str(k).lower(): v for k, v in flat.items()}
    for name in fields:
        if name.lower() in lower:
            return True, lower[name.lower()]
    return False, None


def _mask_token(value: Optional[str]) -> str:
    value = value or ""
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return ("*" * 8) + value[-4:]


def _is_masked(value: Optional[str]) -> bool:
    v = (value or "").strip()
    if not v:
        return True
    if set(v) <= {"*"}:
        return True
    if v.startswith("********") or v.count("*") >= 4:
        return True
    return False


async def _get_settings_row(db: Optional[AsyncSession]) -> Optional[RelaySettings]:
    if db is None:
        return None
    result = await db.execute(select(RelaySettings))
    row = result.scalars().first()
    if row is None:
        row = RelaySettings(singleton_key="default", enabled=False)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def ping(
    relay_url: Optional[Any] = None,
    url: Optional[Any] = None,
    base_url: Optional[Any] = None,
    token: Optional[Any] = None,
    relay_token: Optional[Any] = None,
    api_token: Optional[Any] = None,
    access_token: Optional[Any] = None,
    auth_token: Optional[Any] = None,
    api_key: Optional[Any] = None,
    secret: Optional[Any] = None,
    payload: Optional[Any] = None,
    timeout: float = 5.0,
    db: Optional[AsyncSession] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    # Allow a single positional dict payload.
    if isinstance(relay_url, dict) and url is None and base_url is None:
        payload = relay_url
        relay_url = None

    flat = _flatten_payload(payload) if isinstance(payload, dict) else {}
    # kwargs may carry extra fields.
    if kwargs:
        flat.update(kwargs)

    url_present, url_value = _first_present(flat, URL_FIELDS)
    if not url_present:
        url_value = None
    if url_value is None:
        url_value = relay_url if relay_url is not None else (url if url is not None else base_url)

    token_present, token_value = _first_present(flat, TOKEN_FIELDS)
    if not token_present:
        token_value = None
    if token_value is None:
        token_value = token or relay_token or api_token or access_token or auth_token or api_key or secret

    # If no explicit target supplied, fall back to persisted settings.
    if _clean_url(url_value) is None and db is not None:
        try:
            row = await _get_settings_row(db)
            if row is not None:
                url_value = row.relay_url
                if token_value is None:
                    token_value = row.token
        except Exception:  # noqa: BLE001
            pass

    target = _clean_url(url_value)
    tok = _clean_token(token_value)

    start = time.monotonic()
    ok = False
    status_code = None
    detail = ""
    last_error = None

    if not target:
        detail = "relay_url is required"
    else:
        headers: Dict[str, str] = {}
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
            headers["X-Relay-Token"] = tok
        candidates = [target]

        async with httpx.AsyncClient(timeout=float(timeout or 5.0), follow_redirects=True) as client:
            for cand in candidates:
                try:
                    resp = await client.get(cand, headers=headers)
                    status_code = resp.status_code
                    if resp.status_code < 400:
                        ok = True
                        detail = f"Reachable ({resp.status_code})"
                        target = cand
                        break
                    detail = f"HTTP {resp.status_code}"
                except Exception as exc:  # network/timeout
                    last_error = str(exc)
                    detail = f"Request failed: {exc}"
        if not ok and last_error:
            detail = last_error

    latency_ms = round((time.monotonic() - start) * 1000, 1)

    result: Dict[str, Any] = {
        "ok": ok,
        "connected": ok,
        "reachable": ok,
        "relay_url": _clean_url(url_value),
        "url": _clean_url(url_value),
        "target_url": target,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "detail": detail,
        "message": detail,
        "error": None if ok else detail,
    }

    # Persist an event log row when a DB session is available.
    if db is not None:
        try:
            log = RelayEventLog(
                relay_url=_clean_url(url_value),
                ok=ok,
                status="ok" if ok else "error",
                status_code=status_code,
                latency_ms=latency_ms,
                detail=detail,
                event_type="relay_ping",
            )
            db.add(log)
            await db.commit()
            await db.refresh(log)
            result["log_id"] = log.id
            result["event_id"] = log.id
            result["id"] = log.id
        except Exception:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass

    return result


ping_relay = ping
async_ping = ping
relay_ping = ping
ping_url = ping


class RelayService:
    async def ping(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await ping(*args, **kwargs)

    async def ping_relay(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await ping(*args, **kwargs)

    async def test(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await ping(*args, **kwargs)


relay_service = RelayService()


async def log_event(
    db: AsyncSession,
    *,
    relay_url: Optional[str] = None,
    ok: bool = False,
    status: Optional[str] = None,
    status_code: Optional[int] = None,
    latency_ms: Optional[float] = None,
    detail: Optional[str] = None,
    event_type: str = "relay_ping",
) -> RelayEventLog:
    log = RelayEventLog(
        relay_url=relay_url,
        ok=ok,
        status=status,
        status_code=status_code,
        latency_ms=latency_ms,
        detail=detail,
        event_type=event_type,
    )
    db.add(log)
    await db.commit()
    await db.refresh(log)
    return log


async def get_latest_event(db: AsyncSession) -> Optional[RelayEventLog]:
    result = await db.execute(select(RelayEventLog).order_by(RelayEventLog.id.desc()))
    return result.scalars().first()


async def get_relay_settings(db: AsyncSession) -> RelaySettings:
    row = await _get_settings_row(db)
    if row is None:  # pragma: no cover
        row = RelaySettings(singleton_key="default", enabled=False)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def save_relay_settings(
    db: AsyncSession,
    *,
    relay_url: Optional[Any] = None,
    relay_url_provided: bool = False,
    enabled: Optional[bool] = None,
    enabled_provided: bool = False,
    token: Optional[Any] = None,
    token_provided: bool = False,
    payload: Optional[Any] = None,
) -> RelaySettings:
    flat = _flatten_payload(payload) if isinstance(payload, dict) else {}
    row = await get_relay_settings(db)

    if relay_url_provided or _first_present(flat, URL_FIELDS)[0]:
        present, value = _first_present(flat, URL_FIELDS)
        if not present:
            value = relay_url
        cleaned = _clean_url(value)
        # Treat explicit empty string as clearing.
        if value is not None and str(value).strip() == "":
            cleaned = None
        if present or relay_url_provided:
            row.relay_url = cleaned

    if enabled_provided or _first_present(flat, ENABLED_FIELDS)[0]:
        present, value = _first_present(flat, ENABLED_FIELDS)
        if not present:
            value = enabled
        if value is not None:
            row.enabled = bool(value)

    if token_provided or _first_present(flat, TOKEN_FIELDS)[0]:
        present, value = _first_present(flat, TOKEN_FIELDS)
        if not present:
            value = token
        if value is not None and not _is_masked(_clean_token(value)):
            row.token = _clean_token(value)

    from datetime import datetime

    row.updated_at = datetime.utcnow()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row
