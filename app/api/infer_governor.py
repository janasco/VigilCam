"""Inference governor API for VigilCam.

Compute-budget admission control in front of expensive inference
(cloud / detector-adapter / genai / face / plate). Exposes config, live
status/check/acquire/release, adaptive-budget observe, metrics, history, and
reset controls. Auto-registered via the ``app.main`` pkgutil loop, so no
``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

import app.services.infer_governor as svc

router = APIRouter(tags=["infer_governor"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"key": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"key": bytes(payload).decode("utf-8", errors="replace")}
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
                    try:
                        fv = float(v)
                        data[k] = int(fv) if float(fv).is_integer() else fv
                    except (TypeError, ValueError):
                        data[k] = v
        except Exception:  # noqa: BLE001
            pass

    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "settings", "governor", "gate"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _result(payload: Any, request: Request | None, fn) -> dict:
    data = _normalize(payload, request)
    result = fn(**data)
    if isinstance(result, bool):
        return {"ok": True, "success": True, "result": result}
    if isinstance(result, list):
        return {"ok": True, "success": True, "items": result, "count": len(result)}
    if not isinstance(result, dict):
        return {"ok": True, "success": True, "result": result}
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


_PATHS = (
    "/api/infer-governor",
    "/api/infer_governor",
    "/api/inference/governor",
)


def _register(r: APIRouter, base: str) -> None:
    tag = base.replace("/", "_")

    @r.get(base, operation_id=f"infer_governor_root{tag}")
    async def root_get(request: Request):
        return {
            "ok": True,
            "success": True,
            "summary": svc.get_summary(),
            "config": svc.get_config(),
            "status": svc.get_status(),
            "metrics": svc.get_metrics(),
        }

    @r.get(base + "/", operation_id=f"infer_governor_root_slash{tag}")
    async def root_get_slash(request: Request):
        return await root_get(request)

    @r.get(base + "/status", operation_id=f"infer_governor_status{tag}")
    async def status_get(request: Request):
        return _result(None, request, svc.get_status)

    @r.get(base + "/config", operation_id=f"infer_governor_get_config{tag}")
    async def config_get(request: Request):
        return _result(None, request, svc.get_config)

    @r.post(base + "/config", operation_id=f"infer_governor_post_config{tag}")
    async def config_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.configure)

    @r.get(base + "/metrics", operation_id=f"infer_governor_metrics{tag}")
    async def metrics_get(request: Request):
        return _result(None, request, svc.get_metrics)

    @r.get(base + "/summary", operation_id=f"infer_governor_summary{tag}")
    async def summary_get(request: Request):
        return _result(None, request, svc.get_summary)

    @r.get(base + "/history", operation_id=f"infer_governor_history{tag}")
    async def history_get(request: Request, limit: Optional[int] = Query(default=None)):
        params: dict = {}
        if limit is not None:
            params["limit"] = limit
        try:
            for k, v in request.query_params.items():
                if k not in params:
                    params[k] = v
        except Exception:  # noqa: BLE001
            pass
        return svc.get_history(**params)

    @r.get(base + "/check", operation_id=f"infer_governor_check_get{tag}")
    async def check_get(request: Request):
        return _result(None, request, svc.evaluate)

    @r.post(base + "/check", operation_id=f"infer_governor_check_post{tag}")
    async def check_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.evaluate)

    @r.post(base + "/acquire", operation_id=f"infer_governor_acquire{tag}")
    async def acquire_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.check_and_acquire)

    @r.post(base + "/release", operation_id=f"infer_governor_release{tag}")
    async def release_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.release)

    @r.post(base + "/observe", operation_id=f"infer_governor_observe{tag}")
    async def observe_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.observe)

    @r.post(base + "/evaluate", operation_id=f"infer_governor_evaluate{tag}")
    async def evaluate_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.evaluate)

    @r.post(base + "/enable", operation_id=f"infer_governor_enable{tag}")
    async def enable_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        return _result(data, None, svc.enable)

    @r.post(base + "/disable", operation_id=f"infer_governor_disable{tag}")
    async def disable_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        return _result(data, None, svc.disable)

    @r.post(base + "/reset", operation_id=f"infer_governor_reset{tag}")
    async def reset_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not data:
            out = svc.reset()
            out.setdefault("ok", True)
            return out
        return _result(data, None, svc.reset)

    @r.post(base + "/clear", operation_id=f"infer_governor_clear{tag}")
    async def clear_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not data:
            svc.clear()
            return {"ok": True, "success": True, "cleared": True}
        svc.clear(**data)
        return {"ok": True, "success": True, "cleared": True}

    @r.get(base + "/health", operation_id=f"infer_governor_health{tag}")
    async def health_get():
        return {"ok": True, "success": True, "service": "infer_governor"}


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix.
underscore_router = APIRouter(prefix="/api/infer_governor", tags=["infer_governor"])
hyphen_router = APIRouter(prefix="/api/infer-governor", tags=["infer_governor"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root(request: Request):
        return {"ok": True, "success": True, "summary": svc.get_summary(),
                "config": svc.get_config(), "status": svc.get_status()}

    @r.get("/")
    async def slash(request: Request):
        return await root(request)

    @r.get("/status")
    async def status(request: Request):
        data = _normalize(None, request)
        return svc.get_status(**data)

    @r.get("/summary")
    async def summary():
        return _result(None, None, svc.get_summary)

    @r.get("/config")
    async def cfg_get(request: Request):
        return _result(None, request, svc.get_config)

    @r.post("/config")
    async def cfg_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.configure)

    @r.get("/metrics")
    async def metrics():
        return _result(None, None, svc.get_metrics)

    @r.get("/health")
    async def health():
        return {"ok": True, "success": True, "service": "infer_governor"}


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

# Side-effect registration so the router is live even when imported after startup.
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    for _r in (router, underscore_router, hyphen_router):
        try:
            if id(_r) not in _ids:
                _main_app.include_router(_r)
                _ids = _ids | {id(_r)}
        except Exception:  # noqa: BLE001
            pass
    try:
        _main_app._registered_router_ids = _ids
    except Exception:  # noqa: BLE001
        pass
except Exception:  # noqa: BLE001
    pass

__all__ = ["router", "underscore_router", "hyphen_router"]
