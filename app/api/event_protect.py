"""Event-driven recording protection API router -- flagged-event auto-protect.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no
``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.event_protect as protect_mod
from app.services.event_protect import (
    auto_protect_event,
    backfill_flagged,
    disable,
    enable,
    get_status,
    is_flagged,
    set_config,
    set_enabled,
    toggle,
)

router = APIRouter(tags=["event_protect"])


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
    for nested_key in ("event_protect", "auto_protect", "autoProtect", "protection",
                       "config", "settings", "payload", "options", "params",
                       "data", "body", "request"):
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
        return set_config(**data)

    async def _flag_check(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        inline = None
        for key in ("event", "source", "source_event"):
            if isinstance(data.get(key), dict):
                inline = dict(data.get(key))
                break
        if inline is not None:
            call_data = {k: v for k, v in data.items() if k not in ("event", "source", "source_event")}
            flagged = is_flagged(inline, **call_data)
            eid = _coerce_int_or_none(inline.get("id", inline.get("event_id", data.get("event_id"))))
        else:
            flagged = is_flagged(data)
            eid = _coerce_int_or_none(data.get("event_id", data.get("eventId", data.get("id"))))
        return {"flagged": bool(flagged), "is_flagged": bool(flagged),
                "event_id": eid, "ok": True, "success": True}

    async def _protect(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
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
        call_data = dict(data)
        for _k in ("event_id", "eventId", "id", "event", "source", "source_event"):
            call_data.pop(_k, None)
        # Explicit protect bypasses the disabled toggle when the event is
        # flagged; use force only when the caller asks for it.
        if inline is not None:
            inline = dict(inline)
            if eid is not None:
                inline.setdefault("id", eid)
                inline.setdefault("event_id", eid)
            out = auto_protect_event(inline, event_id=eid, **call_data)
        elif eid is not None:
            out = auto_protect_event(eid, **call_data)
        else:
            # Segment-only or inline dict without id: let the service validate.
            if not call_data and not data:
                raise HTTPException(status_code=400, detail="event_id is required")
            out = auto_protect_event(data if data else None, **call_data)
        if not out.get("success", True):
            raise HTTPException(status_code=400, detail=str(out.get("error", "protect failed")))
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
            out = await protect_mod.backfill_flagged_async(db=db, **data)
        except Exception:  # noqa: BLE001
            out = backfill_flagged(**data)
        return out

    async def _list_protected(request: Request):
        data = _normalize(None, request)
        items = protect_mod.list_protected(storage_root=data.get("storage_root"))
        return {"protected": items, "items": items, "count": len(items),
                "ok": True, "success": True}

    _get_status.__name__ = f"event_protect_status_{abs(hash(base)) % 10_000_000}"
    _get_config.__name__ = f"event_protect_config_{abs(hash(base)) % 10_000_000}"
    _enable.__name__ = f"event_protect_enable_{abs(hash(base)) % 10_000_000}"
    _disable.__name__ = f"event_protect_disable_{abs(hash(base)) % 10_000_000}"
    _toggle.__name__ = f"event_protect_toggle_{abs(hash(base)) % 10_000_000}"
    _configure.__name__ = f"event_protect_configure_{abs(hash(base)) % 10_000_000}"
    _flag_check.__name__ = f"event_protect_flagcheck_{abs(hash(base)) % 10_000_000}"
    _protect.__name__ = f"event_protect_protect_{abs(hash(base)) % 10_000_000}"
    _backfill.__name__ = f"event_protect_backfill_{abs(hash(base)) % 10_000_000}"
    _list_protected.__name__ = f"event_protect_list_{abs(hash(base)) % 10_000_000}"

    r.get(base, summary="Get event auto-protect status")(_get_status)
    r.get(base + "/", include_in_schema=False)(_get_status)
    r.get(base + "/status", summary="Get event auto-protect status")(_get_status)
    r.get(base + "/config", summary="Get event auto-protect config")(_get_config)
    r.get(base + "/settings", include_in_schema=False)(_get_config)
    r.get(base + "/list", summary="List protected segments")(_list_protected)
    r.get(base + "/protected", include_in_schema=False)(_list_protected)
    r.post(base + "/enable", summary="Enable flagged-event auto-protect")(_enable)
    r.post(base + "/disable", summary="Disable flagged-event auto-protect")(_disable)
    r.post(base + "/toggle", summary="Toggle flagged-event auto-protect")(_toggle)
    r.post(base + "/config", summary="Configure flagged-event auto-protect")(_configure)
    r.put(base + "/config", summary="Configure flagged-event auto-protect")(_configure)
    r.patch(base + "/config", include_in_schema=False)(_configure)
    r.post(base, status_code=status.HTTP_200_OK, include_in_schema=False)(_configure)
    r.post(base + "/check", summary="Check whether an event is flagged")(_flag_check)
    r.post(base + "/is-flagged", include_in_schema=False)(_flag_check)
    r.post(base + "/is_flagged", include_in_schema=False)(_flag_check)
    r.get(base + "/check", include_in_schema=False)(_flag_check)
    r.post(base + "/protect", summary="Auto-protect segments for one flagged event")(_protect)
    r.post(base + "/auto-protect", include_in_schema=False)(_protect)
    r.post(base + "/autoprotect", include_in_schema=False)(_protect)
    r.post(base + "/attach", include_in_schema=False)(_protect)
    r.post(base + "/backfill", summary="Bulk protect segments for flagged events")(_backfill)
    r.post(base + "/bulk-protect", include_in_schema=False)(_backfill)
    r.post(base + "/protect-flagged", include_in_schema=False)(_backfill)


_BASES = (
    "/api/event-protect",
    "/api/event_protect",
    "/api/events/protect",
    "/api/event-protection",
    "/api/rec-protect/event",
    "/api/rec_protect/event",
)
_seen: set = set()
for _base in _BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen:
        continue
    _seen.add(_norm)
    _register(router, _norm)


# Compatibility aliases.
event_protect_router = router
auto_protect_router = router
event_autoprotect_router = router
api_router = router

__all__ = ["router"]
