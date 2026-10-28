"""PTZ tour / patrol API endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, status

import app.services.ptz_tour as tour_mod
from app.services.ptz_tour import (
    clear,
    create_tour,
    delete_tour,
    get_status,
    list_tours,
    pause_tour,
    resume_tour,
    start_tour,
    step_tour,
    stop_tour,
    update_tour,
)

router = APIRouter(tags=["ptz_tour"])


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
    "/api/ptz_tour",
    "/api/ptz-tour",
    "/api/ptz_tours",
    "/api/ptz-tours",
    "/api/ptz/tour",
    "/api/ptz/tours",
    "/api/ptz/patrol",
    "/api/ptz/patrols",
    "/api/tour",
    "/api/tours",
    "/api/patrol",
    "/api/patrols",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_tours(request: Request):
        data = dict(request.query_params)
        try:
            if data.get("id") or data.get("tour_id") or data.get("patrol_id"):
                return tour_mod.get_status(**data)
            return tour_mod.list_tours(**data)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.get(base + "/")
    async def get_tours_slash(request: Request):
        return await get_tours(request)

    @r.get(base + "/list")
    async def get_tours_list(request: Request):
        return await get_tours(request)

    @r.get(base + "/status")
    async def get_tour_status(request: Request):
        data = dict(request.query_params)
        try:
            if data.get("id") or data.get("tour_id") or data.get("patrol_id") or data.get("camera_id"):
                return tour_mod.get_status(**data)
            items = tour_mod.list_tours(**data)
            return {"tours": items, "count": len(items), "status": "ok", "ok": True, "success": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_tours(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        action = ""
        for k in ("action", "op", "operation"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                action = v.strip().lower()
                break
        try:
            if action in ("stop", "stop_tour", "stop_patrol", "disable", "cancel", "end"):
                result = stop_tour(**data)
            elif action in ("start", "start_tour", "start_patrol", "enable", "begin", "run"):
                result = start_tour(**data)
            elif action in ("pause", "pause_tour"):
                result = pause_tour(**data)
            elif action in ("resume", "resume_tour", "unpause"):
                result = resume_tour(**data)
            elif action in ("step", "next", "advance", "tick"):
                result = step_tour(**data)
            elif action in ("status", "get"):
                result = get_status(**data)
            elif action in ("list",):
                return tour_mod.list_tours(**data)
            elif action in ("clear", "reset", "purge"):
                n = clear(**data)
                return {"cleared": n, "count": n, "ok": True, "success": True}
            elif action in ("delete", "remove"):
                result = delete_tour(**data)
            elif action in ("update", "edit", "patch"):
                result = update_tour(**data)
            elif data.get("id") and len(data) <= 2:
                result = get_status(**data)
            else:
                result = create_tour(**data)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if isinstance(result, list):
            return result
        return _ok(result, "created" if action in ("", "create", "add", "new") else "ok")

    @r.post(base + "/")
    async def post_tours_slash(payload: Any = Body(default=None), request: Request = None):
        return await post_tours(payload, request)

    @r.post(base + "/start")
    async def post_start(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(start_tour(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/stop")
    async def post_stop(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(stop_tour(**_normalize(payload, request)), "stopped")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/pause")
    async def post_pause(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(pause_tour(**_normalize(payload, request)), "paused")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/resume")
    async def post_resume(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(resume_tour(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/step")
    async def post_step(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(step_tour(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/next")
    async def post_next(payload: Any = Body(default=None), request: Request = None):
        return await post_step(payload, request)

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None):
        n = clear(**_normalize(payload, request))
        return {"cleared": n, "count": n, "removed": n, "ok": True, "success": True}

    @r.delete(base + "/{tour_id}")
    async def delete_tour_by_id(tour_id: str):
        try:
            return _ok(delete_tour(tour_id), "deleted")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.get(base + "/{tour_id}")
    async def get_tour_by_id(tour_id: str):
        try:
            return tour_mod.get_status(tour_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))


for _base in _PATHS:
    _register(router, _base)


underscore_router = APIRouter(prefix="/api/ptz_tour", tags=["ptz_tour"])
hyphen_router = APIRouter(prefix="/api/ptz-tour", tags=["ptz_tour"])
tour_router = APIRouter(prefix="/api/tour", tags=["ptz_tour"])
patrol_router = APIRouter(prefix="/api/patrol", tags=["ptz_tour"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        data = dict(request.query_params)
        if data.get("id") or data.get("tour_id"):
            try:
                return tour_mod.get_status(**data)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
        return tour_mod.list_tours(**data)

    @r.get("/")
    async def slash_get(request: Request):
        return await root_get(request)

    @r.get("/list")
    async def list_get(request: Request):
        return await root_get(request)

    @r.get("/status")
    async def status_get(request: Request):
        data = dict(request.query_params)
        if data.get("id") or data.get("tour_id") or data.get("camera_id"):
            try:
                return tour_mod.get_status(**data)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
        items = tour_mod.list_tours(**data)
        return {"tours": items, "count": len(items), "status": "ok", "ok": True, "success": True}

    @r.get("/{tour_id}")
    async def by_id_get(tour_id: str):
        try:
            return tour_mod.get_status(tour_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(create_tour(**_normalize(payload, request)), "created")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.post("/start")
    async def start_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(start_tour(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/stop")
    async def stop_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(stop_tour(**_normalize(payload, request)), "stopped")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/pause")
    async def pause_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(pause_tour(**_normalize(payload, request)), "paused")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/resume")
    async def resume_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(resume_tour(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/step")
    async def step_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(step_tour(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/clear")
    async def clear_post(payload: Any = Body(default=None), request: Request = None):
        n = clear(**_normalize(payload, request))
        return {"cleared": n, "count": n, "ok": True, "success": True}

    @r.delete("/{tour_id}")
    async def by_id_delete(tour_id: str):
        try:
            return _ok(delete_tour(tour_id), "deleted")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))


for _r in (underscore_router, hyphen_router, tour_router, patrol_router):
    _make_prefixed(_r)
