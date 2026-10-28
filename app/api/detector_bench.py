"""Detector-benchmark API router.

Exposes the synthetic detector benchmark runner over HTTP. Compatible
with the auto-loader in ``app.main`` (looks for ``router``).

Backend profiles mirror Frigate detector backends (Coral EdgeTPU USB/PCI
single-process, Hailo-8/8L HEF, OpenVINO CPU/GPU/NPU/AUTO, plus CPU/ONNX/
TensorRT/RKNN): benchmark dimensions are backend/device, model_type,
resolution, variant, iterations/warmup, threshold, and parallel
processes.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
import app.services.detector_bench as svc_mod
from app.services.detector_bench import (
    DetectorBenchService,
    detector_bench_service,
    get_backend,
    get_run,
    list_backends,
    list_runs,
    run_benchmark,
)

router = APIRouter(tags=["detector_bench"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"backend": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"backend": bytes(payload).decode("utf-8", "ignore")}
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
                       "request", "settings", "message", "input", "inputs",
                       "benchmark", "bench", "spec", "job"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    for k in ("request", "db", "session", "user", "current_user"):
        data.pop(k, None)
    return data


def _to_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return dict(obj)
    if isinstance(obj, list):
        return [dict(x) if isinstance(x, dict) else x for x in obj]
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:  # noqa: BLE001
            pass
    return obj


def _do_run(data: dict) -> dict:
    try:
        result = run_benchmark(**data)
    except TypeError:
        result = run_benchmark(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    if not isinstance(result, dict):
        result = {"success": True, "ok": True, "result": result}
    result.setdefault("success", True)
    result.setdefault("ok", True)
    return _to_dict(result)


_PATHS = (
    "/api/detector_bench",
    "/api/detector-bench",
    "/api/detector/bench",
    "/api/bench/detector",
    "/api/detector_benchmark",
    "/api/detector-benchmark",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def root_list(request: Request):
        try:
            runs = list_runs()
        except Exception:  # noqa: BLE001
            runs = []
        try:
            backends = list_backends()
        except Exception:  # noqa: BLE001
            backends = []
        return {"ok": True, "success": True, "backends": _to_dict(backends),
                "runs": _to_dict(runs), "count": len(runs),
                "backend_count": len(backends)}

    @r.get(base + "/")
    async def root_list_slash(request: Request):
        return await root_list(request)

    @r.get(base + "/backends")
    async def backends_list():
        try:
            backends = list_backends()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "success": True, "backends": _to_dict(backends),
                "count": len(backends)}

    @r.get(base + "/backends/list")
    async def backends_list_alias():
        return await backends_list()

    @r.get(base + "/status")
    async def status_root():
        try:
            return svc_mod.get_status()
        except Exception as exc:  # noqa: BLE001
            return {"status": "ok", "ok": True, "error": str(exc)}

    @r.get(base + "/health")
    async def health_root():
        try:
            return svc_mod.get_status()
        except Exception as exc:  # noqa: BLE001
            return {"status": "ok", "ok": True, "error": str(exc)}

    @r.get(base + "/runs")
    async def runs_list():
        try:
            runs = list_runs()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "success": True, "runs": _to_dict(runs),
                "count": len(runs)}

    @r.get(base + "/history")
    async def history_alias():
        return await runs_list()

    @r.get(base + "/list")
    async def list_alias():
        return await runs_list()

    @r.post(base + "/run", status_code=status.HTTP_200_OK)
    async def run_root(payload: Any = Body(default=None), request: Request = None,
                       db=Depends(db_session)):
        return _do_run(_normalize(payload, request))

    @r.post(base + "/run/", status_code=status.HTTP_200_OK)
    async def run_slash(payload: Any = Body(default=None), request: Request = None,
                        db=Depends(db_session)):
        return await run_root(payload, request, db)

    @r.post(base + "/benchmark", status_code=status.HTTP_200_OK)
    async def benchmark_alias(payload: Any = Body(default=None), request: Request = None,
                              db=Depends(db_session)):
        return await run_root(payload, request, db)

    @r.post(base + "/start", status_code=status.HTTP_200_OK)
    async def start_alias(payload: Any = Body(default=None), request: Request = None,
                          db=Depends(db_session)):
        return await run_root(payload, request, db)

    @r.post(base, status_code=status.HTTP_200_OK)
    async def infer_root(payload: Any = Body(default=None), request: Request = None,
                         db=Depends(db_session)):
        return _do_run(_normalize(payload, request))

    @r.post(base + "/", status_code=status.HTTP_200_OK)
    async def infer_slash(payload: Any = Body(default=None), request: Request = None,
                          db=Depends(db_session)):
        return await infer_root(payload, request, db)

    @r.post(base + "/clear", status_code=status.HTTP_200_OK)
    async def clear_root():
        try:
            n = svc_mod.clear()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "success": True, "cleared": n}

    @r.post(base + "/reset", status_code=status.HTTP_200_OK)
    async def reset_alias():
        return await clear_root()

    @r.get(base + "/backends/{backend_name}")
    async def get_one_backend(backend_name: str):
        rec = get_backend(backend_name)
        if rec is None:
            raise HTTPException(status_code=404,
                                detail=f"Backend {backend_name} not found")
        return _to_dict(rec)

    @r.get(base + "/{run_id}")
    async def get_one(run_id: str):
        rec = get_run(run_id)
        if rec is None:
            raise HTTPException(status_code=404,
                                detail=f"Benchmark run {run_id} not found")
        return _to_dict(rec)


for _base in _PATHS:
    _register(router, _base)
