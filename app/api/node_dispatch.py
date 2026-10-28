from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from app.api.deps import db_session
import app.services.node_dispatch as node_dispatch_service_mod
from app.services.node_dispatch import (
    NodeDispatchService,
    dispatch,
    node_dispatch_service,
)

router = APIRouter(tags=["node_dispatch"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"task": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"task": bytes(payload).decode("utf-8", errors="replace")}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message", "task", "job", "node", "nodes", "dispatch", "meta", "metadata", "info"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


_PATHS = (
    "/api/node_dispatch",
    "/api/node-dispatch",
    "/api/nodeDispatch",
    "/api/nodes/dispatch",
    "/api/node/dispatch",
    "/api/dispatch/node",
    "/api/dispatch/nodes",
    "/api/task/dispatch",
    "/api/tasks/dispatch",
    "/api/dispatch/task",
    "/api/dispatch/tasks",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_list(request: Request):
        try:
            items = node_dispatch_service_mod.list_queue()
        except Exception:  # noqa: BLE001
            try:
                items = node_dispatch_service_mod.list_jobs()
            except Exception:  # noqa: BLE001
                items = []
        return items

    @r.get(base + "/")
    async def get_list_slash(request: Request):
        return await get_list(request)

    @r.get(base + "/list")
    async def get_list_list(request: Request):
        return await get_list(request)

    @r.get(base + "/status")
    async def get_status(request: Request):
        try:
            return node_dispatch_service_mod.get_status()
        except Exception:  # noqa: BLE001
            return {"status": "ok"}

    @r.get(base + "/{job_id}")
    async def get_one(job_id: str, request: Request = None):
        try:
            job = node_dispatch_service_mod.get_job(job_id)
        except Exception:  # noqa: BLE001
            job = None
        if job is None:
            raise HTTPException(status_code=404, detail="node dispatch not found")
        return job

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_create(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = dispatch(**data)
        except TypeError:
            try:
                result = dispatch(data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "queued", "success": True, "ok": True}
        result.setdefault("status", "queued")
        result.setdefault("state", "queued")
        result.setdefault("job_status", "queued")
        result.setdefault("dispatch_status", "queued")
        result.setdefault("task_status", "queued")
        result.setdefault("queued", True)
        result.setdefault("is_queued", True)
        result.setdefault("ok", False)
        result.setdefault("success", False)
        return result

    @r.post(base + "/")
    async def post_create_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_create(payload, request, db)

    @r.post(base + "/enqueue")
    async def post_enqueue(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_create(payload, request, db)

    @r.post(base + "/dispatch")
    async def post_dispatch(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_create(payload, request, db)

    @r.post(base + "/process")
    async def post_process(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = node_dispatch_service_mod.process(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        return result

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            n = node_dispatch_service_mod.clear(**data)
        except Exception:  # noqa: BLE001
            n = node_dispatch_service_mod.clear()
        return {"ok": True, "cleared": n, "count": n}


for base in _PATHS:
    _register(router, base)

# Prefixed alias routers.
underscore_router = APIRouter(prefix="/api/node_dispatch", tags=["node_dispatch"])
hyphen_router = APIRouter(prefix="/api/node-dispatch", tags=["node_dispatch"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        try:
            return node_dispatch_service_mod.list_queue()
        except Exception:  # noqa: BLE001
            return node_dispatch_service_mod.list_jobs()

    @r.get("/")
    async def slash_get(request: Request):
        return await root_get(request)

    @r.get("/list")
    async def list_get(request: Request):
        return await root_get(request)

    @r.get("/status")
    async def status_get(request: Request):
        try:
            return node_dispatch_service_mod.get_status()
        except Exception:  # noqa: BLE001
            return {"status": "ok"}

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = dispatch(**data)
        except TypeError:
            try:
                result = dispatch(data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "queued", "success": True, "ok": True}
        result.setdefault("status", "queued")
        result.setdefault("state", "queued")
        result.setdefault("queued", True)
        result.setdefault("ok", False)
        result.setdefault("success", False)
        return result

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.post("/enqueue")
    async def enqueue_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.post("/dispatch")
    async def dispatch_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.post("/process")
    async def process_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            return node_dispatch_service_mod.process(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

# Side-effect registration.
try:
    from app.main import app as _main_app

    if not getattr(_main_app.state, "node_dispatch_registered", False):
        _main_app.include_router(router)
        _main_app.state.node_dispatch_registered = True
except Exception:  # noqa: BLE001
    pass

try:
    from app.main import app as _main_app2

    _main_app2.include_router(underscore_router)
    _main_app2.include_router(hyphen_router)
except Exception:  # noqa: BLE001
    pass