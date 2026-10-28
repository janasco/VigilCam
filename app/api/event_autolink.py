"""Event auto-attach (autolink) API router -- toggle + backfill.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no
``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.event_autolink as autolink_mod
from app.services.event_autolink import (
    autolink_event,
    backfill,
    disable,
    enable,
    get_status,
    set_config,
    set_enabled,
    toggle,
)

router = APIRouter(tags=["event_autolink"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"enabled": payload}
    elif isinstance(payload, bool):
        data = {"enabled": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"enabled": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, (list, tuple)):
        data = {"events": list(payload)}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {"enabled": str(payload)}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    vals = request.query_params.getlist(k)
                    data[k] = vals[0] if len(vals) == 1 else vals
        except Exception:  # noqa: BLE001
            pass
    for nested_key in ("autolink", "auto_attach", "autoAttach", "config", "settings",
                       "payload", "options", "params", "data", "body", "request"):
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


def _register(r: APIRouter, base: str) -> None:
    from fastapi import Depends as _Depends  # noqa: E402

    from app.api.deps import db_session as _db_session  # noqa: E402

    async def _get_status(request: Request):
        data = _normalize(None, request)
        # Allow inline enable/disable via query for convenience.
        if any(k in data for k in ("enabled", "enable", "active", "on")):
            return set_enabled(data)
        return get_status()

    async def _get_config(request: Request):
        return get_status()

    async def _enable(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        return enable(**data) if data else enable()

    async def _disable(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        return disable(**data) if data else disable()

    async def _toggle(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        if not data:
            return toggle()
        return toggle(**data)

    async def _configure(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        if not data:
            return get_status()
        # If payload only carries the toggle, honour it; otherwise configure.
        config_keys = {"window_before", "windowBefore", "window_after", "windowAfter",
                       "window", "recording_dir", "recordingDir", "limit", "before", "after"}
        if any(k in data for k in config_keys):
            return set_config(**data)
        if any(k in data for k in ("enabled", "enable", "auto_attach", "autolink", "active", "on")):
            # set_enabled when only toggle-ish keys present, else full config
            if len([k for k in data if not k.startswith("_")]) == 1 or not any(k in data for k in config_keys):
                # route through set_config so combined payloads still work
                return set_config(**data)
        return set_config(**data)

    async def _attach(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        raw_eid = data.get("event_id", data.get("eventId", data.get("id", data.get("event"))))
        if isinstance(raw_eid, dict):
            raw_eid = raw_eid.get("id", raw_eid.get("event_id"))
        eid = _coerce_int_or_none(raw_eid)
        inline = None
        for key in ("event", "source", "source_event"):
            if isinstance(data.get(key), dict):
                inline = dict(data.get(key))
                break
        if eid is None and inline is not None:
            eid = _coerce_int_or_none(inline.get("id", inline.get("event_id")))
        if eid is None and inline is None:
            raise HTTPException(status_code=400, detail="event_id is required")
        # Explicit attach bypasses the toggle. Avoid duplicate event_id kwarg.
        call_data = dict(data)
        for _k in ("event_id", "eventId", "id", "event", "source", "source_event"):
            call_data.pop(_k, None)
        if inline is not None:
            inline = dict(inline)
            inline.setdefault("id", eid)
            inline.setdefault("event_id", eid)
            out = autolink_event(inline, event_id=eid, force=True, **call_data)
        else:
            out = autolink_event(eid, force=True, **call_data)
        if not out.get("success", True):
            raise HTTPException(status_code=400, detail=str(out.get("error", "attach failed")))
        out.setdefault("success", True)
        out.setdefault("ok", True)
        return out

    async def _backfill(
        payload: Any = Body(default=None),
        request: Request = None,  # type: ignore[assignment]
        db: Any = _Depends(_db_session),
        limit: Optional[int] = Query(default=None),
        dry_run: Optional[str] = Query(default=None),
        camera_id: Optional[str] = Query(default=None),
    ):
        data = _normalize(payload, request)
        if limit is not None:
            data.setdefault("limit", limit)
        if dry_run is not None:
            data.setdefault("dry_run", dry_run)
        if camera_id is not None:
            data.setdefault("camera_id", camera_id)
        try:
            out = await autolink_mod.backfill_async(db=db, **data)
        except Exception:  # noqa: BLE001
            out = backfill(**data)
        return out

    _get_status.__name__ = f"autolink_status_{abs(hash(base)) % 10_000_000}"
    _get_config.__name__ = f"autolink_config_{abs(hash(base)) % 10_000_000}"
    _enable.__name__ = f"autolink_enable_{abs(hash(base)) % 10_000_000}"
    _disable.__name__ = f"autolink_disable_{abs(hash(base)) % 10_000_000}"
    _toggle.__name__ = f"autolink_toggle_{abs(hash(base)) % 10_000_000}"
    _configure.__name__ = f"autolink_configure_{abs(hash(base)) % 10_000_000}"
    _attach.__name__ = f"autolink_attach_{abs(hash(base)) % 10_000_000}"
    _backfill.__name__ = f"autolink_backfill_{abs(hash(base)) % 10_000_000}"

    r.get(base, summary="Get event auto-attach status")(_get_status)
    r.get(base + "/", include_in_schema=False)(_get_status)
    r.get(base + "/status", summary="Get event auto-attach status")(_get_status)
    r.get(base + "/config", summary="Get event auto-attach config")(_get_config)
    r.get(base + "/settings", include_in_schema=False)(_get_config)
    r.post(base + "/enable", summary="Enable event auto-attach")(_enable)
    r.post(base + "/disable", summary="Disable event auto-attach")(_disable)
    r.post(base + "/toggle", summary="Toggle event auto-attach")(_toggle)
    r.post(base + "/config", summary="Configure event auto-attach")(_configure)
    r.put(base + "/config", summary="Configure event auto-attach")(_configure)
    r.patch(base + "/config", include_in_schema=False)(_configure)
    r.post(base, status_code=status.HTTP_200_OK, include_in_schema=False)(_configure)
    r.post(base + "/attach", summary="Attach segments for one event (explicit)")(_attach)
    r.post(base + "/autolink", include_in_schema=False)(_attach)
    r.post(base + "/auto-attach", include_in_schema=False)(_attach)
    r.post(base + "/backfill", summary="Backfill event-segment links")(_backfill)


_BASES = (
    "/api/event-autolink",
    "/api/event_autolink",
    "/api/events/autolink",
    "/api/event-segments/autolink",
)
_seen: set = set()
for _base in _BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen:
        continue
    _seen.add(_norm)
    _register(router, _norm)


# Compatibility aliases.
event_autolink_router = router
autolink_router = router
auto_attach_router = router
event_auto_attach_router = router
api_router = router

__all__ = ["router"]
