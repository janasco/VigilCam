"""PTZ tour live-progress API endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, status

import app.services.tour_progress as progress_mod
from app.services.tour_progress import get_progress, list_progress

router = APIRouter(tags=["tour_progress"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"id": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"id": bytes(payload).decode("utf-8", errors="replace")}
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
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "settings", "message", "tour", "patrol"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _ok(result: Any, default_status: str = "ok") -> dict:
    if not isinstance(result, dict):
        result = {"result": result, "status": default_status, "ok": True, "success": True}
    result.setdefault("status", default_status)
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


_PATHS = (
    "/api/tour_progress",
    "/api/tour-progress",
    "/api/tours/progress",
    "/api/tour/progress",
    "/api/ptz_tour/progress",
    "/api/ptz-tour/progress",
    "/api/patrol/progress",
    "/api/ptz/progress",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_progress_view(request: Request):
        data = dict(request.query_params)
        try:
            if data.get("id") or data.get("tour_id") or data.get("patrol_id") or data.get("camera_id"):
                return progress_mod.get_progress(**data)
            items = progress_mod.list_progress(**data)
            return {"items": items, "progress": items, "tours": items,
                    "count": len(items), "status": "ok", "ok": True, "success": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.get(base + "/")
    async def get_progress_view_slash(request: Request):
        return await get_progress_view(request)

    @r.get(base + "/list")
    async def get_progress_list(request: Request):
        data = dict(request.query_params)
        try:
            items = progress_mod.list_progress(**data)
            return {"items": items, "progress": items, "tours": items,
                    "count": len(items), "status": "ok", "ok": True, "success": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.get(base + "/status")
    async def get_progress_status(request: Request):
        data = dict(request.query_params)
        try:
            if data.get("id") or data.get("tour_id") or data.get("patrol_id") or data.get("camera_id"):
                return progress_mod.get_progress(**data)
            items = progress_mod.list_progress(**data)
            return {"items": items, "progress": items, "tours": items,
                    "count": len(items), "status": "ok", "ok": True, "success": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_progress(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            if data.get("id") or data.get("tour_id") or data.get("patrol_id") or data.get("camera_id"):
                return _ok(get_progress(**data), "ok")
            items = list_progress(**data)
            return {"items": items, "progress": items, "tours": items,
                    "count": len(items), "status": "ok", "ok": True, "success": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/")
    async def post_progress_slash(payload: Any = Body(default=None), request: Request = None):
        return await post_progress(payload, request)

    @r.get(base + "/{tour_id}")
    async def get_progress_by_id(tour_id: str):
        try:
            return progress_mod.get_progress(tour_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))


for _base in _PATHS:
    _register(router, _base)
