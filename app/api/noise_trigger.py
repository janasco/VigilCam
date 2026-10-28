"""Noise-level trigger API router.

Exposes :mod:`app.services.noise_trigger` (threshold + cooldown -> event)
over HTTP. Auto-registered by ``app/main.py`` (it imports every ``app.api``
module and includes ``router``), so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.noise_trigger as svc_mod
from app.services.noise_trigger import (
    check,
    configure,
    evaluate,
    get_config,
    get_event,
    get_status,
    get_summary,
    list_events,
)

router = APIRouter(tags=["noise_trigger"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        s = payload.strip()
        try:
            data = {"level": float(s)}
        except (TypeError, ValueError):
            data = {"level": s} if s else {}
    elif isinstance(payload, (int, float)) and not isinstance(payload, bool):
        data = {"level": payload}
    elif isinstance(payload, list):
        data = {"level": payload[0]} if payload else {}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            import base64

            data = {"audio_base64": base64.b64encode(bytes(payload)).decode("ascii")}
        except Exception:  # noqa: BLE001
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass

    for nested_key in ("payload", "params", "data", "body", "config", "request", "options", "settings", "noise", "trigger"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # Source aliases -> camera_id so per-source cooldown keys line up.
    if data.get("camera_id") is None:
        for k in ("camera", "cam", "cameraId", "id", "microphone_id", "microphoneId", "mic_id", "micId", "mic", "device_id", "deviceId", "source"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break

    for k in ("request", "db", "session", "user", "current_user"):
        data.pop(k, None)

    return data


def _register(r: APIRouter, base: str) -> None:
    @r.get(base + "/config", summary="Get noise trigger config")
    @r.get(base + "/config/", include_in_schema=False)
    async def _get_config(request: Request):
        data = _normalize(None, request)
        return get_config(**data)

    @r.post(base + "/config", summary="Configure noise trigger threshold/cooldown")
    @r.post(base + "/config/", include_in_schema=False)
    @r.put(base + "/config", include_in_schema=False)
    @r.put(base + "/config/", include_in_schema=False)
    @r.patch(base + "/config", include_in_schema=False)
    @r.patch(base + "/config/", include_in_schema=False)
    async def _set_config(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        return configure(data)

    @r.post(base + "/check", summary="Threshold + cooldown noise check (produces event)")
    @r.post(base + "/check/", include_in_schema=False)
    @r.post(base + "/evaluate", include_in_schema=False)
    @r.post(base + "/evaluate/", include_in_schema=False)
    @r.post(base + "/process", include_in_schema=False)
    @r.post(base + "/process/", include_in_schema=False)
    @r.post(base + "/report", include_in_schema=False)
    @r.post(base + "/report/", include_in_schema=False)
    @r.post(base + "/trigger", include_in_schema=False)
    @r.post(base + "/trigger/", include_in_schema=False)
    @r.post(base, status_code=status.HTTP_201_CREATED, include_in_schema=False)
    @r.post(base + "/", status_code=status.HTTP_201_CREATED, include_in_schema=False)
    async def _check(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        return check(data)

    @r.post(base + "/peek", include_in_schema=False)
    @r.post(base + "/peek/", include_in_schema=False)
    async def _peek(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        """Pure threshold evaluation (no cooldown, no event stored)."""
        data = _normalize(payload, request)
        return evaluate(data)

    @r.get(base + "/events", summary="List noise trigger events")
    @r.get(base + "/events/", include_in_schema=False)
    async def _list_events(
        request: Request,
        camera_id: Optional[str] = Query(default=None),
        source: Optional[str] = Query(default=None),
    ):
        data = _normalize(None, request)
        if camera_id is not None:
            data.setdefault("camera_id", camera_id)
        if source is not None:
            data.setdefault("camera_id", source)
        items = list_events(data)
        return {"count": len(items), "events": items, "ok": True, "success": True}

    @r.get(base + "/events/{event_id}", summary="Get one noise trigger event")
    @r.get(base + "/events/{event_id}/", include_in_schema=False)
    async def _get_event(event_id: str):
        item = get_event(event_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Noise event not found")
        return item

    @r.delete(base + "/events", summary="Clear noise trigger events/cooldowns")
    @r.delete(base + "/events/", include_in_schema=False)
    async def _clear_events():
        return svc_mod.clear()

    @r.get(base + "/status", summary="Noise trigger status")
    @r.get(base + "/status/", include_in_schema=False)
    async def _status(
        request: Request,
        camera_id: Optional[str] = Query(default=None),
        source: Optional[str] = Query(default=None),
    ):
        data = _normalize(None, request)
        if camera_id is not None:
            data.setdefault("camera_id", camera_id)
        if source is not None:
            data.setdefault("camera_id", source)
        return get_status(data)

    @r.get(base, summary="Noise trigger summary")
    @r.get(base + "/", include_in_schema=False)
    async def _summary():
        return get_summary()


_BASES = (
    "/api/noise_trigger",
    "/api/noise-trigger",
    "/api/noise",
)

_seen: set = set()
for _base in _BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen:
        continue
    _seen.add(_norm)
    _register(router, _norm)


# Compatibility aliases.
noise_trigger_router = router
noise_router = router
trigger_router = router
api_router = router

__all__ = ["router"]

# Side-effect registration into the main app so the router is live even when
# the module is imported after app startup (mirrors tamper pattern).
# The main app also auto-discovers app.api.* routers at import time.
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    if id(router) not in _ids:
        _main_app.include_router(router)
        try:
            _main_app._registered_router_ids = _ids | {id(router)}
        except Exception:  # noqa: BLE001
            pass
except Exception:  # noqa: BLE001
    pass
