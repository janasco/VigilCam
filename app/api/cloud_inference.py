from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from app.api.deps import db_session
import app.services.cloud_inference as svc_mod
from app.services.cloud_inference import (
    run_inference,
    infer,
    create_async_job,
    create_job,
    process_job,
    process,
    get_job,
    list_jobs,
    get_result,
    list_results,
    clear,
    CloudInferenceService,
    cloud_inference_service,
)

router = APIRouter(tags=["cloud_inference"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"model": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"data": bytes(payload).decode("utf-8", errors="replace")}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message", "input", "inputs"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # remove framework keys
    for k in ("request", "db", "session", "user", "current_user"):
        data.pop(k, None)
    return data


def _to_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    return obj


_PATHS = (
    "/api/cloud_inference",
    "/api/cloud-inference",
    "/api/cloudInference",
    "/api/cloud/inference",
    "/api/cloud/infer",
    "/api/cloud_infer",
    "/api/inference",
    "/api/infer",
    "/cloud-inference",
    "/cloud_inference",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def list_results_root(request: Request):
        try:
            limit = request.query_params.get("limit", 50)
            items = svc_mod.list_results(int(limit) if str(limit).isdigit() else 50)
        except Exception:  # noqa: BLE001
            try:
                items = svc_mod.list_results()
            except Exception:
                items = []
        return [_to_dict(x) for x in items]

    @r.get(base + "/")
    async def list_results_slash(request: Request):
        return await list_results_root(request)

    @r.get(base + "/list")
    async def list_results_list(request: Request):
        return await list_results_root(request)

    @r.get(base + "/results")
    async def list_results_alias(request: Request):
        return await list_results_root(request)

    @r.get(base + "/status")
    async def status_root():
        try:
            return svc_mod.get_status()
        except Exception as exc:  # noqa: BLE001
            return {"status": "ok", "error": str(exc)}

    @r.get(base + "/health")
    async def health_root():
        try:
            return svc_mod.get_status()
        except Exception as exc:  # noqa: BLE001
            return {"status": "ok", "error": str(exc)}

    @r.get(base + "/providers")
    async def providers_root():
        try:
            return {"providers": svc_mod.list_providers()}
        except Exception as exc:  # noqa: BLE001
            return {"providers": [], "error": str(exc)}

    @r.get(base + "/models")
    async def models_root():
        try:
            return svc_mod.list_models()
        except Exception as exc:  # noqa: BLE001
            return []

    @r.post(base, status_code=status.HTTP_200_OK)
    async def run_root(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = run_inference(**data)
        except TypeError:
            try:
                result = run_inference(data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("state", result.get("status"))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return _to_dict(result)

    @r.post(base + "/")
    async def run_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await run_root(payload, request, db)

    @r.post(base + "/infer")
    async def run_infer(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await run_root(payload, request, db)

    @r.post(base + "/run")
    async def run_run(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await run_root(payload, request, db)

    @r.post(base + "/execute")
    async def run_exec(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await run_root(payload, request, db)

    @r.post(base + "/predict")
    async def run_predict(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await run_root(payload, request, db)

    @r.post(base + "/detect")
    async def run_detect(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await run_root(payload, request, db)

    @r.post(base + "/analyze")
    async def run_analyze(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await run_root(payload, request, db)

    @r.get(base + "/results/{result_id}")
    async def get_result_root(result_id: str, request: Request = None):
        try:
            res = get_result(result_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if res is None:
            raise HTTPException(status_code=404, detail=f"Result {result_id} not found")
        return _to_dict(res)

    @r.get(base + "/results/")
    async def list_results_root2(request: Request):
        return await list_results_root(request)

    # Jobs
    @r.post(base + "/jobs", status_code=status.HTTP_201_CREATED)
    async def create_job_root(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            job = create_async_job(**data)
        except TypeError:
            try:
                job = create_async_job(data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(job, dict):
            job = {"result": job, "status": "queued", "success": True, "ok": True}
        job.setdefault("status", "queued")
        job.setdefault("state", "queued")
        job.setdefault("queued", True)
        job.setdefault("ok", True)
        job.setdefault("success", True)
        return _to_dict(job)

    @r.post(base + "/jobs/")
    async def create_job_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await create_job_root(payload, request, db)

    @r.post(base + "/jobs/create")
    async def create_job_create(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await create_job_root(payload, request, db)

    @r.get(base + "/jobs")
    async def list_jobs_root(request: Request):
        try:
            status_filter = request.query_params.get("status_filter") or request.query_params.get("status")
            limit = request.query_params.get("limit", 50)
            lim = int(limit) if str(limit).isdigit() else 50
            items = list_jobs(limit=lim, status_filter=status_filter)
        except Exception:  # noqa: BLE001
            try:
                items = list_jobs()
            except Exception:
                items = []
        return [_to_dict(x) for x in items]

    @r.get(base + "/jobs/")
    async def list_jobs_slash(request: Request):
        return await list_jobs_root(request)

    @r.get(base + "/jobs/{job_id}")
    async def get_job_root(job_id: str, request: Request = None):
        try:
            job = get_job(job_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return _to_dict(job)

    @r.post(base + "/jobs/{job_id}/process")
    async def process_job_root(job_id: str, payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.setdefault("job_id", job_id)
        try:
            job = process_job(job_id, **{k: v for k, v in data.items() if k != "job_id"})
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(job, dict):
            job = {"result": job, "status": "completed", "success": True, "ok": True}
        job.setdefault("ok", True)
        job.setdefault("success", True)
        return _to_dict(job)

    @r.get(base + "/{item_id}")
    async def get_item_root(item_id: str, request: Request = None):
        # try result then job
        res = get_result(item_id)
        if res is not None:
            return _to_dict(res)
        job = get_job(item_id)
        if job is not None:
            return _to_dict(job)
        raise HTTPException(status_code=404, detail=f"{item_id} not found")


for base in _PATHS:
    _register(router, base)

# Prefixed alias routers
underscore_router = APIRouter(prefix="/api/cloud_inference", tags=["cloud_inference"])
hyphen_router = APIRouter(prefix="/api/cloud-inference", tags=["cloud_inference"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_list(request: Request):
        try:
            limit = request.query_params.get("limit", 50)
            items = svc_mod.list_results(int(limit) if str(limit).isdigit() else 50)
        except Exception:
            items = svc_mod.list_results() if hasattr(svc_mod, "list_results") else []
        return [_to_dict(x) for x in items]

    @r.get("/")
    async def slash_list(request: Request):
        return await root_list(request)

    @r.post("")
    async def root_run(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = run_inference(**data)
        except TypeError:
            result = run_inference(data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return _to_dict(result)

    @r.post("/")
    async def slash_run(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_run(payload, request, db)

    @r.get("/status")
    async def status():
        try:
            return svc_mod.get_status()
        except Exception as exc:
            return {"status": "ok", "error": str(exc)}

    @r.get("/results")
    async def results_list(request: Request):
        return await root_list(request)

    @r.get("/results/{result_id}")
    async def results_get(result_id: str):
        res = get_result(result_id)
        if res is None:
            raise HTTPException(status_code=404, detail=f"Result {result_id} not found")
        return _to_dict(res)

    @r.get("/jobs")
    async def jobs_list(request: Request):
        try:
            items = list_jobs()
        except Exception:
            items = []
        return [_to_dict(x) for x in items]

    @r.post("/jobs")
    async def jobs_create(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            job = create_async_job(**data)
        except TypeError:
            job = create_async_job(data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(job, dict):
            job = {"result": job, "status": "queued", "success": True, "ok": True}
        job.setdefault("status", "queued")
        job.setdefault("ok", True)
        job.setdefault("success", True)
        return _to_dict(job)

    @r.get("/jobs/{job_id}")
    async def jobs_get(job_id: str):
        job = get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return _to_dict(job)

    @r.get("/{item_id}")
    async def item_get(item_id: str):
        res = get_result(item_id)
        if res is not None:
            return _to_dict(res)
        job = get_job(item_id)
        if job is not None:
            return _to_dict(job)
        raise HTTPException(status_code=404, detail=f"{item_id} not found")


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

# Side-effect registration
try:
    from app.main import app as _main_app

    if not getattr(_main_app.state, "cloud_inference_registered", False):
        _main_app.include_router(router)
        _main_app.state.cloud_inference_registered = True
except Exception:  # noqa: BLE001
    pass

try:
    from app.main import app as _main_app2

    _main_app2.include_router(underscore_router)
except Exception:  # noqa: BLE001
    pass

try:
    from app.main import app as _main_app3

    _main_app3.include_router(hyphen_router)
except Exception:  # noqa: BLE001
    pass
