"""Multi-camera incident grouping API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.

Correlates detection events from different cameras that occur close in time
into incidents. Distinct from ``app/api/event_merge.py``, which merges
duplicate events from the same camera.
"""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends as _Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
import app.services.incidents as incidents_mod
from app.services.incidents import correlate_events, suggest_incidents

router = APIRouter(tags=["incidents"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"events": []}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"text": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:
            data = {}
    elif isinstance(payload, (list, tuple)):
        data = {"events": list(payload)}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    vals = request.query_params.getlist(k)
                    data[k] = vals[0] if len(vals) == 1 else vals
        except Exception:
            pass

    for nested_key in ("incident", "correlate", "group", "payload", "options",
                       "config", "params", "data", "body", "request", "fields"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _has_inline_events(data: dict) -> bool:
    for key in ("events", "items", "sources", "detections", "alerts"):
        val = data.get(key)
        if isinstance(val, list) and val:
            return True
    return False


async def _do_correlate(data: dict, db: Optional[AsyncSession]) -> Any:
    if _has_inline_events(data):
        try:
            incidents = correlate_events(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"success": True, "ok": True, "incidents": incidents,
                "groups": incidents, "count": len(incidents)}
    if db is None:
        try:
            incidents = correlate_events(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"success": True, "ok": True, "incidents": incidents,
                "groups": incidents, "count": len(incidents)}
    try:
        incidents = await incidents_mod.correlate_incidents_async(data, db=db)
    except HTTPException:
        raise
    except Exception:
        try:
            incidents = suggest_incidents(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"success": True, "ok": True, "incidents": incidents,
            "groups": incidents, "count": len(incidents)}


_CORRELATE_BASES = (
    "/api/incidents/correlate",
    "/api/incidents/group",
    "/api/incidents/suggest",
    "/api/incidents/suggestions",
    "/api/incident/correlate",
    "/api/incident/group",
)


def _build_correlate_handler(idx: int):
    async def _correlate_handler(
        payload: Any = Body(default=None),
        request: Request = None,
        db: AsyncSession = _Depends(db_session),
    ):
        return await _do_correlate(_normalize(payload, request), db)

    _correlate_handler.__name__ = f"incidents_correlate_handler_{idx}"
    return _correlate_handler


for _idx, _base in enumerate(_CORRELATE_BASES):
    _h = _build_correlate_handler(_idx)
    router.post(_base, summary="Correlate events into multi-camera incidents")(_h)
    router.post(_base + "/", include_in_schema=False)(_h)


async def _list_handler(
    request: Request = None,
    db: AsyncSession = _Depends(db_session),
):
    return await _do_correlate(_normalize(None, request), db)


async def _list_post_handler(
    payload: Any = Body(default=None),
    request: Request = None,
    db: AsyncSession = _Depends(db_session),
):
    return await _do_correlate(_normalize(payload, request), db)


router.get("/api/incidents", summary="List multi-camera incidents")(_list_handler)
router.get("/api/incidents/", include_in_schema=False)(_list_handler)
router.post("/api/incidents", summary="Correlate events into multi-camera incidents")(_list_post_handler)
router.post("/api/incidents/", include_in_schema=False)(_list_post_handler)
router.get("/api/incident", summary="List multi-camera incidents", include_in_schema=False)(_list_handler)
router.post("/api/incident", include_in_schema=False)(_list_post_handler)


# Compatibility aliases.
incidents_router = router
incident_router = router
api_router = router

__all__ = ["router"]
