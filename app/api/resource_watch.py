"""Resource watcher alerting API for VigilCam.

Threshold alerting on top of ``app.services.system_metrics`` (which only
reads values). Exposes config, live status/check, alerts, history, and
resolve/reset controls. Auto-registered via the ``app.main`` pkgutil loop,
so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

import app.services.resource_watch as svc

router = APIRouter(tags=["resource_watch"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"resource": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"resource": bytes(payload).decode("utf-8", errors="replace")}
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
                    # Coerce numeric query params.
                    try:
                        fv = float(v)
                        data[k] = fv
                    except (TypeError, ValueError):
                        data[k] = v
        except Exception:  # noqa: BLE001
            pass

    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "settings", "alert", "thresholds", "values"):
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
    "/api/resource-watch",
    "/api/resource_watch",
    "/api/system/resource-watch",
    "/api/system/resource_watch",
)


def _register(r: APIRouter, base: str) -> None:
    tag = base.replace("/", "_")

    @r.get(base, operation_id=f"resource_watch_root{tag}")
    async def root_get(request: Request):
        return {
            "ok": True,
            "success": True,
            "summary": svc.get_summary(),
            "config": svc.get_config(),
            "status": svc.get_status(),
            "alerts": svc.list_alerts(),
        }

    @r.get(base + "/", operation_id=f"resource_watch_root_slash{tag}")
    async def root_get_slash(request: Request):
        return await root_get(request)

    @r.get(base + "/status", operation_id=f"resource_watch_status{tag}")
    async def status_get(request: Request):
        data = _normalize(None, request)
        # Read-only live evaluation (no alert recording / no cooldown mutation).
        if not any(k in data for k in ("cpu", "memory", "mem", "disk",
                                       "cpu_percent", "memory_percent", "disk_percent",
                                       "metrics")):
            try:
                from app.services.system_metrics import collect_system_metrics

                metrics = await collect_system_metrics()
                live = svc.evaluate(metrics=metrics)
            except Exception as exc:  # noqa: BLE001
                live = {"ok": False, "success": False, "breached": False,
                        "alerts": [], "values": {}, "thresholds": svc.get_config().get("thresholds", {}),
                        "error": str(exc)}
            return {
                "ok": True, "success": True,
                "status": svc.get_status(),
                "evaluation": live,
                "values": live.get("values", {}),
                "thresholds": live.get("thresholds", {}),
                "breached": live.get("breached", False),
            }
        out = svc.evaluate(**data)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        return out

    @r.get(base + "/config", operation_id=f"resource_watch_get_config{tag}")
    async def config_get(request: Request):
        return _result(None, request, svc.get_config)

    @r.post(base + "/config", operation_id=f"resource_watch_post_config{tag}")
    async def config_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.configure)

    @r.get(base + "/thresholds", operation_id=f"resource_watch_get_thresholds{tag}")
    async def thresholds_get(request: Request):
        return _result(None, request, svc.get_thresholds)

    @r.post(base + "/thresholds", operation_id=f"resource_watch_post_thresholds{tag}")
    async def thresholds_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.configure)

    @r.post(base + "/enable", operation_id=f"resource_watch_enable{tag}")
    async def enable_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        return _result(data, None, svc.enable)

    @r.post(base + "/disable", operation_id=f"resource_watch_disable{tag}")
    async def disable_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        return _result(data, None, svc.disable)

    @r.get(base + "/summary", operation_id=f"resource_watch_summary{tag}")
    async def summary_get(request: Request):
        return _result(None, request, svc.get_summary)

    @r.get(base + "/alerts", operation_id=f"resource_watch_alerts{tag}")
    async def alerts_get(request: Request):
        return svc.list_alerts()

    @r.get(base + "/history", operation_id=f"resource_watch_history{tag}")
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

    @r.get(base + "/check", operation_id=f"resource_watch_check_get{tag}")
    async def check_get(request: Request):
        data = _normalize(None, request)
        if not data:
            return await svc.check_async()
        out = svc.check(**data)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        return out

    @r.post(base + "/check", operation_id=f"resource_watch_check_post{tag}")
    async def check_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not data:
            return await svc.check_async()
        # Explicit values (tests) stay sync; otherwise live-collect.
        has_values = any(k in data for k in ("cpu", "memory", "mem", "disk",
                                             "cpu_percent", "memory_percent",
                                             "disk_percent", "metrics"))
        if has_values:
            out = svc.check(**data)
        else:
            out = await svc.check_async(**data)
        if isinstance(out, dict):
            out.setdefault("ok", True)
            out.setdefault("success", True)
        return out

    @r.post(base + "/evaluate", operation_id=f"resource_watch_evaluate{tag}")
    async def evaluate_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.evaluate)

    @r.post(base + "/resolve", operation_id=f"resource_watch_resolve{tag}")
    async def resolve_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.resolve)

    @r.post(base + "/reset", operation_id=f"resource_watch_reset{tag}")
    async def reset_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not data:
            out = svc.reset()
            out.setdefault("ok", True)
            return out
        return _result(data, None, svc.reset)

    @r.post(base + "/clear", operation_id=f"resource_watch_clear{tag}")
    async def clear_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not data:
            svc.clear()
            return {"ok": True, "success": True, "cleared": True}
        svc.clear(**data)
        return {"ok": True, "success": True, "cleared": True}

    @r.get(base + "/health", operation_id=f"resource_watch_health{tag}")
    async def health_get():
        return {"ok": True, "success": True, "service": "resource_watch"}


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix.
underscore_router = APIRouter(prefix="/api/resource_watch", tags=["resource_watch"])
hyphen_router = APIRouter(prefix="/api/resource-watch", tags=["resource_watch"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root(request: Request):
        return {"ok": True, "success": True, "summary": svc.get_summary(),
                "config": svc.get_config(), "alerts": svc.list_alerts()}

    @r.get("/")
    async def slash(request: Request):
        return await root(request)

    @r.get("/status")
    async def status(request: Request):
        data = _normalize(None, request)
        if not data:
            try:
                from app.services.system_metrics import collect_system_metrics

                metrics = await collect_system_metrics()
                live = svc.evaluate(metrics=metrics)
            except Exception as exc:  # noqa: BLE001
                live = {"ok": False, "success": False, "breached": False,
                        "alerts": [], "values": {}, "error": str(exc)}
            return {"ok": True, "success": True, "status": svc.get_status(),
                    "evaluation": live, "values": live.get("values", {}),
                    "breached": live.get("breached", False)}
        return svc.evaluate(**data)

    @r.get("/summary")
    async def summary():
        return _result(None, None, svc.get_summary)

    @r.get("/config")
    async def cfg_get(request: Request):
        return _result(None, request, svc.get_config)

    @r.post("/config")
    async def cfg_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.configure)

    @r.get("/alerts")
    async def alerts():
        return svc.list_alerts()

    @r.get("/health")
    async def health():
        return {"ok": True, "success": True, "service": "resource_watch"}


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
