"""Unified alert inbox API for VigilCam.

Single inbox merging ``resource_watch`` + ``rate_anomaly`` +
``offline_alerts`` + ``bitrate_alerts`` alerts, sorted newest-first
with severity tiebreak. Auto-registered via the ``app.main`` pkgutil
loop, so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

import app.services.alert_inbox as svc

router = APIRouter(tags=["alert_inbox"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"sources": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (list, tuple)):
        data = {"events": list(payload)}
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
                        # Keep integer-looking values as-is when they are counts.
                        data[k] = fv
                    except (TypeError, ValueError):
                        data[k] = v
        except Exception:  # noqa: BLE001
            pass

    for nested_key in ("payload", "options", "config", "params", "data", "body",
                        "request", "settings", "filter", "filters"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _inbox_result(payload: Any, request: Request | None) -> dict:
    data = _normalize(payload, request)
    out = svc.get_inbox(**data)
    out.setdefault("ok", True)
    out.setdefault("success", True)
    return out


_PATHS = (
    "/api/alert-inbox",
    "/api/alert_inbox",
    "/api/alerts/inbox",
    "/api/inbox",
)


def _register(r: APIRouter, base: str) -> None:
    tag = base.replace("/", "_")

    @r.get(base, operation_id=f"alert_inbox_root{tag}")
    async def root_get(request: Request):
        return {
            "ok": True,
            "success": True,
            "summary": svc.get_summary(),
            "status": svc.get_status(),
            "inbox": svc.list_alerts(),
        }

    @r.get(base + "/", operation_id=f"alert_inbox_root_slash{tag}")
    async def root_get_slash(request: Request):
        return await root_get(request)

    @r.get(base + "/status", operation_id=f"alert_inbox_status{tag}")
    async def status_get(request: Request):
        data = _normalize(None, request)
        out = svc.get_status(**data)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        return out

    @r.get(base + "/summary", operation_id=f"alert_inbox_summary{tag}")
    async def summary_get(request: Request):
        data = _normalize(None, request)
        out = svc.get_summary(**data)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        return out

    @r.get(base + "/alerts", operation_id=f"alert_inbox_alerts{tag}")
    async def alerts_get(
        request: Request,
        sources: Optional[str] = Query(default=None),
        severity: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None),
    ):
        data = _normalize(None, request)
        if sources is not None and data.get("sources") is None:
            data["sources"] = sources
        if severity is not None and data.get("severity") is None:
            data["severity"] = severity
        if limit is not None and data.get("limit") is None:
            data["limit"] = limit
        return _inbox_result(data, None)

    @r.post(base + "/alerts", operation_id=f"alert_inbox_alerts_post{tag}")
    async def alerts_post(payload: Any = Body(default=None), request: Request = None):
        return _inbox_result(payload, request)

    @r.get(base + "/inbox", operation_id=f"alert_inbox_inbox{tag}")
    async def inbox_get(
        request: Request,
        sources: Optional[str] = Query(default=None),
        severity: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None),
    ):
        data = _normalize(None, request)
        if sources is not None and data.get("sources") is None:
            data["sources"] = sources
        if severity is not None and data.get("severity") is None:
            data["severity"] = severity
        if limit is not None and data.get("limit") is None:
            data["limit"] = limit
        return _inbox_result(data, None)

    @r.post(base + "/inbox", operation_id=f"alert_inbox_inbox_post{tag}")
    async def inbox_post(payload: Any = Body(default=None), request: Request = None):
        return _inbox_result(payload, request)

    @r.get(base + "/history", operation_id=f"alert_inbox_history{tag}")
    async def history_get(request: Request, limit: Optional[int] = Query(default=None)):
        data = _normalize(None, request)
        if limit is not None and data.get("limit") is None:
            data["limit"] = limit
        return svc.list_alerts(**data)

    @r.post(base + "/resolve", operation_id=f"alert_inbox_resolve{tag}")
    async def resolve_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        out = svc.resolve(**data)
        if isinstance(out, dict):
            out.setdefault("ok", True)
            out.setdefault("success", True)
        return out

    @r.get(base + "/sources", operation_id=f"alert_inbox_sources{tag}")
    async def sources_get():
        return {"ok": True, "success": True, "sources": list(svc.SOURCES)}

    @r.get(base + "/health", operation_id=f"alert_inbox_health{tag}")
    async def health_get():
        return {"ok": True, "success": True, "service": "alert_inbox"}


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix.
underscore_router = APIRouter(prefix="/api/alert_inbox", tags=["alert_inbox"])
hyphen_router = APIRouter(prefix="/api/alert-inbox", tags=["alert_inbox"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root(request: Request):
        return {"ok": True, "success": True, "summary": svc.get_summary(),
                "status": svc.get_status(), "inbox": svc.list_alerts()}

    @r.get("/")
    async def slash(request: Request):
        return await root(request)

    @r.get("/status")
    async def status(request: Request):
        return svc.get_status(**_normalize(None, request))

    @r.get("/summary")
    async def summary(request: Request):
        return svc.get_summary(**_normalize(None, request))

    @r.get("/alerts")
    async def alerts(request: Request):
        return _inbox_result(None, request)

    @r.post("/alerts")
    async def alerts_p(payload: Any = Body(default=None), request: Request = None):
        return _inbox_result(payload, request)

    @r.get("/inbox")
    async def inbox(request: Request):
        return _inbox_result(None, request)

    @r.post("/inbox")
    async def inbox_p(payload: Any = Body(default=None), request: Request = None):
        return _inbox_result(payload, request)

    @r.get("/health")
    async def health():
        return {"ok": True, "success": True, "service": "alert_inbox"}


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
