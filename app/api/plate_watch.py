"""Plate allowlist/blocklist watcher API for VigilCam.

Consumes already-recognized plate strings (recognition itself lives in
``app.api.license_plate`` / ``app.services.license_plate``) and matches
them against the ``app.services.plate_watch`` watchlists.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

import app.services.plate_watch as svc

router = APIRouter(tags=["plate_watch"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"plate": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
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
    for nested in ("payload", "options", "params", "data", "body", "request"):
        n = data.get(nested)
        if isinstance(n, dict):
            for k, v in n.items():
                data.setdefault(k, v)
    return data


def _ok(result: Any) -> Any:
    if isinstance(result, dict):
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result
    if isinstance(result, list):
        return result
    return {"ok": True, "success": True, "result": result}


_PATHS = (
    "/api/plate_watch",
    "/api/plate-watch",
    "/api/platewatch",
    "/api/plates/watch",
    "/api/watch/plates",
    "/api/watch/plate",
)


def _register(r: APIRouter, base: str) -> None:
    tag = base.replace("/", "_")

    @r.get(base, operation_id=f"plate_watch_root{tag}")
    async def root_get(request: Request):
        return {"ok": True, "success": True, "summary": svc.get_summary(),
                "watchlists": svc.get_watchlists()}

    @r.get(base + "/", operation_id=f"plate_watch_root_slash{tag}")
    async def root_get_slash(request: Request):
        return await root_get(request)

    @r.get(base + "/watchlists", operation_id=f"plate_watch_lists{tag}")
    async def lists_get():
        return _ok(svc.get_watchlists())

    @r.get(base + "/lists", operation_id=f"plate_watch_lists_alias{tag}")
    async def lists_alias():
        return _ok(svc.get_watchlists())

    @r.get(base + "/allowlist", operation_id=f"plate_watch_allow_get{tag}")
    async def allow_get():
        return _ok({"allowlist": svc.list_allowlist()})

    @r.get(base + "/blocklist", operation_id=f"plate_watch_block_get{tag}")
    async def block_get():
        return _ok({"blocklist": svc.list_blocklist()})

    @r.post(base + "/allowlist", operation_id=f"plate_watch_allow_add{tag}")
    async def allow_add(payload: Any = Body(default=None), request: Request = None):
        return _ok(svc.add_allowlist(_normalize(payload, request)))

    @r.post(base + "/blocklist", operation_id=f"plate_watch_block_add{tag}")
    async def block_add(payload: Any = Body(default=None), request: Request = None):
        return _ok(svc.add_blocklist(_normalize(payload, request)))

    @r.post(base + "/allowlist/remove", operation_id=f"plate_watch_allow_rm{tag}")
    async def allow_rm(payload: Any = Body(default=None), request: Request = None):
        return _ok(svc.remove_allowlist(_normalize(payload, request)))

    @r.post(base + "/blocklist/remove", operation_id=f"plate_watch_block_rm{tag}")
    async def block_rm(payload: Any = Body(default=None), request: Request = None):
        return _ok(svc.remove_blocklist(_normalize(payload, request)))

    @r.delete(base + "/allowlist", operation_id=f"plate_watch_allow_del{tag}")
    async def allow_del(payload: Any = Body(default=None), request: Request = None,
                        plate: Optional[str] = Query(default=None)):
        data = _normalize(payload, request)
        if plate is not None:
            data.setdefault("plate", plate)
        return _ok(svc.remove_allowlist(data))

    @r.delete(base + "/blocklist", operation_id=f"plate_watch_block_del{tag}")
    async def block_del(payload: Any = Body(default=None), request: Request = None,
                        plate: Optional[str] = Query(default=None)):
        data = _normalize(payload, request)
        if plate is not None:
            data.setdefault("plate", plate)
        return _ok(svc.remove_blocklist(data))

    @r.post(base + "/check", operation_id=f"plate_watch_check{tag}")
    async def check_post(payload: Any = Body(default=None), request: Request = None):
        return _ok(svc.check_plate(_normalize(payload, request)))

    @r.post(base + "/read", operation_id=f"plate_watch_read{tag}")
    async def read_post(payload: Any = Body(default=None), request: Request = None):
        return _ok(svc.check_plate(_normalize(payload, request)))

    @r.post(base + "/match", operation_id=f"plate_watch_match{tag}")
    async def match_post(payload: Any = Body(default=None), request: Request = None):
        return _ok(svc.check_plate(_normalize(payload, request)))

    @r.get(base + "/check", operation_id=f"plate_watch_check_get{tag}")
    async def check_get(request: Request, plate: Optional[str] = Query(default=None)):
        data = dict(request.query_params) if request is not None else {}
        if plate is not None:
            data["plate"] = plate
        return _ok(svc.check_plate(data))

    @r.get(base + "/reads", operation_id=f"plate_watch_reads{tag}")
    async def reads_get(request: Request):
        return _ok(svc.list_reads(dict(request.query_params)))

    @r.get(base + "/alerts", operation_id=f"plate_watch_alerts{tag}")
    async def alerts_get(request: Request):
        return _ok(svc.list_alerts(dict(request.query_params)))

    @r.get(base + "/summary", operation_id=f"plate_watch_summary{tag}")
    async def summary_get():
        return _ok(svc.get_summary())

    @r.get(base + "/config", operation_id=f"plate_watch_cfg_get{tag}")
    async def cfg_get():
        return _ok(svc.get_config())

    @r.post(base + "/config", operation_id=f"plate_watch_cfg_post{tag}")
    async def cfg_post(payload: Any = Body(default=None), request: Request = None):
        return _ok(svc.configure(_normalize(payload, request)))

    @r.post(base + "/clear", operation_id=f"plate_watch_clear{tag}")
    async def clear_post(payload: Any = Body(default=None), request: Request = None):
        return _ok(svc.clear(_normalize(payload, request)))

    @r.post(base + "/reset", operation_id=f"plate_watch_reset{tag}")
    async def reset_post(payload: Any = Body(default=None), request: Request = None):
        return _ok(svc.reset(_normalize(payload, request)))

    @r.get(base + "/health", operation_id=f"plate_watch_health{tag}")
    async def health_get():
        return {"ok": True, "success": True, "service": "plate_watch"}


for _base in _PATHS:
    _register(router, _base)


__all__ = ["router"]
