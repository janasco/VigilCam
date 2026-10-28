"""Group view cycle API endpoints (display view rotation, not PTZ motion).

Cycles rotate the displayed camera-group view on a dwell timer. Unlike
PTZ tours, no camera movement is issued.
Registered automatically by the ``app.api`` auto-loader in :mod:`app.main`
(no manual wiring needed).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, status

import app.services.view_cycle as cycle_mod
from app.services.view_cycle import (
    advance_cycle,
    clear,
    create_cycle,
    delete_cycle,
    get_status,
    list_cycles,
    next_view,
    pause_cycle,
    resume_cycle,
    start_cycle,
    step_cycle,
    stop_cycle,
    update_cycle,
)

router = APIRouter(tags=["view_cycle"])


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
                       "request", "settings", "message", "cycle", "view_cycle", "viewCycle"):
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
    "/api/view_cycle",
    "/api/view-cycle",
    "/api/view_cycles",
    "/api/view-cycles",
    "/api/group_view_cycle",
    "/api/group-view-cycle",
    "/api/group_view_cycles",
    "/api/group-view-cycles",
    "/api/view_cycler",
    "/api/view-cycler",
    "/api/cycle",
    "/api/cycles",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_cycles(request: Request):
        data = dict(request.query_params)
        try:
            if data.get("id") or data.get("cycle_id") or data.get("view_cycle_id"):
                return cycle_mod.get_status(**data)
            return cycle_mod.list_cycles(**data)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.get(base + "/")
    async def get_cycles_slash(request: Request):
        return await get_cycles(request)

    @r.get(base + "/list")
    async def get_cycles_list(request: Request):
        return await get_cycles(request)

    @r.get(base + "/status")
    async def get_cycle_status(request: Request):
        data = dict(request.query_params)
        try:
            if data.get("id") or data.get("cycle_id") or data.get("view_cycle_id") or data.get("name"):
                return cycle_mod.get_status(**data)
            items = cycle_mod.list_cycles(**data)
            return {"cycles": items, "count": len(items), "status": "ok", "ok": True, "success": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_cycles(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        action = ""
        for k in ("action", "op", "operation"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                action = v.strip().lower()
                break
        try:
            if action in ("stop", "stop_cycle", "disable", "cancel", "end"):
                result = stop_cycle(**data)
            elif action in ("start", "start_cycle", "enable", "begin", "run"):
                result = start_cycle(**data)
            elif action in ("pause", "pause_cycle"):
                result = pause_cycle(**data)
            elif action in ("resume", "resume_cycle", "unpause"):
                result = resume_cycle(**data)
            elif action in ("step", "next", "advance", "tick"):
                result = step_cycle(**data)
            elif action in ("status", "get"):
                result = get_status(**data)
            elif action in ("list",):
                return cycle_mod.list_cycles(**data)
            elif action in ("clear", "reset", "purge"):
                n = clear(**data)
                return {"cleared": n, "count": n, "ok": True, "success": True}
            elif action in ("delete", "remove"):
                result = delete_cycle(**data)
            elif action in ("update", "edit", "patch"):
                result = update_cycle(**data)
            elif data.get("id") and len(data) <= 2:
                result = get_status(**data)
            else:
                result = create_cycle(**data)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if isinstance(result, list):
            return result
        return _ok(result, "created" if action in ("", "create", "add", "new") else "ok")

    @r.post(base + "/")
    async def post_cycles_slash(payload: Any = Body(default=None), request: Request = None):
        return await post_cycles(payload, request)

    @r.post(base + "/start")
    async def post_start(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(start_cycle(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/stop")
    async def post_stop(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(stop_cycle(**_normalize(payload, request)), "stopped")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/pause")
    async def post_pause(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(pause_cycle(**_normalize(payload, request)), "paused")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/resume")
    async def post_resume(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(resume_cycle(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/step")
    async def post_step(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(step_cycle(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/next")
    async def post_next(payload: Any = Body(default=None), request: Request = None):
        return await post_step(payload, request)

    @r.post(base + "/advance")
    async def post_advance(payload: Any = Body(default=None), request: Request = None):
        return await post_step(payload, request)

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None):
        n = clear(**_normalize(payload, request))
        return {"cleared": n, "count": n, "removed": n, "ok": True, "success": True}

    @r.delete(base + "/{cycle_id}")
    async def delete_cycle_by_id(cycle_id: str):
        try:
            return _ok(delete_cycle(cycle_id), "deleted")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.get(base + "/{cycle_id}")
    async def get_cycle_by_id(cycle_id: str):
        try:
            return cycle_mod.get_status(cycle_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))


for _base in _PATHS:
    _register(router, _base)


underscore_router = APIRouter(prefix="/api/view_cycle", tags=["view_cycle"])
hyphen_router = APIRouter(prefix="/api/view-cycle", tags=["view_cycle"])
cycle_router = APIRouter(prefix="/api/cycle", tags=["view_cycle"])
group_router = APIRouter(prefix="/api/group-view-cycle", tags=["view_cycle"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        data = dict(request.query_params)
        if data.get("id") or data.get("cycle_id") or data.get("view_cycle_id"):
            try:
                return cycle_mod.get_status(**data)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
        return cycle_mod.list_cycles(**data)

    @r.get("/")
    async def slash_get(request: Request):
        return await root_get(request)

    @r.get("/list")
    async def list_get(request: Request):
        return await root_get(request)

    @r.get("/status")
    async def status_get(request: Request):
        data = dict(request.query_params)
        if data.get("id") or data.get("cycle_id") or data.get("view_cycle_id") or data.get("name"):
            try:
                return cycle_mod.get_status(**data)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
        items = cycle_mod.list_cycles(**data)
        return {"cycles": items, "count": len(items), "status": "ok", "ok": True, "success": True}

    @r.get("/{cycle_id}")
    async def by_id_get(cycle_id: str):
        try:
            return cycle_mod.get_status(cycle_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(create_cycle(**_normalize(payload, request)), "created")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.post("/start")
    async def start_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(start_cycle(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/stop")
    async def stop_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(stop_cycle(**_normalize(payload, request)), "stopped")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/pause")
    async def pause_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(pause_cycle(**_normalize(payload, request)), "paused")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/resume")
    async def resume_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(resume_cycle(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/step")
    async def step_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(step_cycle(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/next")
    async def next_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(next_view(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/advance")
    async def advance_post(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(advance_cycle(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/clear")
    async def clear_post(payload: Any = Body(default=None), request: Request = None):
        n = clear(**_normalize(payload, request))
        return {"cleared": n, "count": n, "ok": True, "success": True}

    @r.delete("/{cycle_id}")
    async def by_id_delete(cycle_id: str):
        try:
            return _ok(delete_cycle(cycle_id), "deleted")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))


for _r in (underscore_router, hyphen_router, cycle_router, group_router):
    _make_prefixed(_r)
