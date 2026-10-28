"""RTSP path brute-force API for the user's own cameras.

Auto-registered by ``app.main`` (any module in ``app.api`` exposing
``router`` is included), so no ``app/main.py`` edit is needed.
Both ``/api/rtsp-paths`` (canonical) and ``/api/rtsp_paths``
(underscore alias) are served.

Scope: single explicitly-provided host only (the user's own camera).
No subnet scanning happens here; that stays in ``network_discovery``.
All scans are capped (``MAX_PATHS`` paths, ``MAX_TIMEOUT_SECONDS``
per-path timeout) and use RTSP ``DESCRIBE`` probing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

import app.services.rtsp_paths as svc

router = APIRouter(prefix="/api/rtsp-paths", tags=["rtsp-paths"])
underscore_router = APIRouter(prefix="/api/rtsp_paths", tags=["rtsp-paths"])


class SingleProbeRequest(BaseModel):
    host: Optional[str] = None
    hostname: Optional[str] = None
    ip: Optional[str] = None
    port: Optional[int] = None
    path: Optional[str] = None
    username: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    timeout: Optional[float] = None
    timeout_seconds: Optional[float] = None

    class Config:
        extra = "allow"


class ScanRequest(BaseModel):
    host: Optional[str] = None
    hostname: Optional[str] = None
    ip: Optional[str] = None
    port: Optional[int] = Field(default=None)
    paths: Optional[List[str]] = None
    path_list: Optional[List[str]] = None
    username: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    timeout: Optional[float] = None
    timeout_seconds: Optional[float] = None
    max_paths: Optional[int] = None

    class Config:
        extra = "allow"


def _payload_dict(payload: Any) -> dict:
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
            return dict(data) if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, str):
        return {"host": payload}
    return {}


def _resolve_host(data: Dict[str, Any]) -> str:
    for key in ("host", "hostname", "ip", "address"):
        val = data.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _resolve_port(data: Dict[str, Any]) -> int:
    raw = data.get("port", data.get("rtsp_port"))
    if raw is None:
        return svc.DEFAULT_RTSP_PORT
    try:
        p = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="port must be an integer 1-65535")
    if not 1 <= p <= 65535:
        raise HTTPException(status_code=422, detail="port must be within 1-65535")
    return p


def _resolve_timeout(data: Dict[str, Any]) -> float:
    for key in ("timeout", "timeout_seconds", "timeoutSeconds"):
        if data.get(key) is not None:
            try:
                val = float(data[key])
            except (TypeError, ValueError):
                continue
            if val <= 0:
                continue
            return min(val, svc.MAX_TIMEOUT_SECONDS)
    if data.get("timeout_ms") is not None:
        try:
            return min(float(data["timeout_ms"]) / 1000.0, svc.MAX_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            pass
    return svc.DEFAULT_TIMEOUT_SECONDS


def _resolve_paths(data: Dict[str, Any]) -> List[str]:
    raw = data.get("paths", data.get("path_list", data.get("pathList", data.get("candidates"))))
    max_paths = data.get("max_paths", data.get("maxPaths", data.get("limit", svc.MAX_PATHS)))
    try:
        limit = int(max_paths) if max_paths is not None else svc.MAX_PATHS
    except (TypeError, ValueError):
        limit = svc.MAX_PATHS
    limit = max(1, min(limit, svc.MAX_PATHS))
    if raw is None:
        return svc._coerce_paths(None, limit)
    if isinstance(raw, str):
        raw = [raw]
    try:
        items = list(raw)
    except TypeError:
        items = [raw]
    return svc._coerce_paths(items, limit)


def _resolve_creds(data: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    user = data.get("username", data.get("user"))
    pwd = data.get("password", data.get("pass", data.get("pwd")))
    return user, pwd


def _result_payload(result: Any) -> dict:
    if hasattr(result, "to_dict"):
        try:
            d = result.to_dict()
            if isinstance(d, dict):
                return d
        except Exception:  # noqa: BLE001
            pass
    if isinstance(result, dict):
        return dict(result)
    return {"value": str(result)}


async def _do_single(data: Dict[str, Any]) -> dict:
    host = _resolve_host(data)
    if not host:
        raise HTTPException(status_code=422, detail="Provide 'host' (your camera's IP/hostname) to probe")
    path = data.get("path", data.get("stream_path", "/"))
    if path is None or (isinstance(path, str) and not path.strip()):
        path = "/"
    user, pwd = _resolve_creds(data)
    result = await svc.adescribe_path(
        host,
        _resolve_port(data),
        path,
        _resolve_timeout(data),
        user,
        pwd,
    )
    return {**_result_payload(result), "ok": bool(result.ok), "success": bool(result.ok)}


async def _do_scan(data: Dict[str, Any]) -> dict:
    host = _resolve_host(data)
    if not host:
        raise HTTPException(status_code=422, detail="Provide 'host' (your camera's IP/hostname) to scan")
    port = _resolve_port(data)
    timeout = _resolve_timeout(data)
    paths = _resolve_paths(data)
    user, pwd = _resolve_creds(data)
    results = await svc.abrute_force_paths(host, port, paths, timeout, user, pwd)
    items = [_result_payload(r) for r in results]
    found = [d for d in items if d.get("ok")]
    return {
        "host": host,
        "port": port,
        "timeout_seconds": timeout,
        "tried": len(items),
        "capped_to": svc.MAX_PATHS,
        "found": found,
        "found_count": len(found),
        "results": items,
        "paths": [d.get("path") for d in items],
        "ok": True,
        "success": True,
    }


@router.get("/paths")
@underscore_router.get("/paths")
async def list_common_paths():
    return {
        "paths": list(svc.COMMON_RTSP_PATHS),
        "count": len(svc.COMMON_RTSP_PATHS),
        "default_timeout_seconds": svc.DEFAULT_TIMEOUT_SECONDS,
        "max_timeout_seconds": svc.MAX_TIMEOUT_SECONDS,
        "max_paths": svc.MAX_PATHS,
        "default_port": svc.DEFAULT_RTSP_PORT,
        "ok": True,
        "success": True,
    }


@router.get("/status")
@underscore_router.get("/status")
async def get_status():
    return {**svc.get_summary(), "results": svc.list_results_dicts(), "ok": True, "success": True}


@router.get("/summary")
@underscore_router.get("/summary")
async def get_summary():
    return {**svc.get_summary(), "ok": True, "success": True}


@router.get("/health")
@underscore_router.get("/health")
async def get_health():
    return {**svc.get_summary(), "results": svc.list_results_dicts(), "ok": True, "success": True}


@router.get("/results")
@underscore_router.get("/results")
async def list_results():
    return svc.list_results_dicts()


@router.delete("/results")
@underscore_router.delete("/results")
async def clear_results():
    svc.clear()
    return {"cleared": True, **svc.get_summary(), "ok": True, "success": True}


@router.post("/probe")
@underscore_router.post("/probe")
async def probe_single(payload: Any = Body(default=None)):
    return await _do_single(_payload_dict(payload))


@router.post("/describe")
@underscore_router.post("/describe")
async def describe_single(payload: Any = Body(default=None)):
    return await _do_single(_payload_dict(payload))


@router.get("/probe")
@underscore_router.get("/probe")
async def probe_via_query(
    host: Optional[str] = Query(default=None),
    port: Optional[int] = Query(default=None),
    path: Optional[str] = Query(default=None),
    username: Optional[str] = Query(default=None),
    password: Optional[str] = Query(default=None),
    timeout: Optional[float] = Query(default=None),
):
    data: Dict[str, Any] = {
        "host": host, "port": port, "path": path or "/",
        "username": username, "password": password, "timeout": timeout,
    }
    return await _do_single(data)


@router.post("/scan")
@underscore_router.post("/scan")
async def scan_paths(payload: Any = Body(default=None)):
    return await _do_scan(_payload_dict(payload))


@router.post("/brute-force")
@underscore_router.post("/brute-force")
async def brute_force(payload: Any = Body(default=None)):
    return await _do_scan(_payload_dict(payload))


@router.post("/brute_force")
@underscore_router.post("/brute_force")
async def brute_force_underscore(payload: Any = Body(default=None)):
    return await _do_scan(_payload_dict(payload))


@router.get("/scan")
@underscore_router.get("/scan")
async def scan_via_query(
    host: Optional[str] = Query(default=None),
    port: Optional[int] = Query(default=None),
    username: Optional[str] = Query(default=None),
    password: Optional[str] = Query(default=None),
    timeout: Optional[float] = Query(default=None),
    max_paths: Optional[int] = Query(default=None),
    paths: Optional[str] = Query(default=None),
):
    path_list: Optional[List[str]] = None
    if paths is not None and paths.strip():
        path_list = [p.strip() for p in paths.replace(",", " ").split() if p.strip()]
    data: Dict[str, Any] = {
        "host": host, "port": port, "username": username, "password": password,
        "timeout": timeout, "max_paths": max_paths, "paths": path_list,
    }
    return await _do_scan(data)


# Side-effect registration so the routers are live even when this module is
# imported after app startup (mirrors stream-probe pattern). The main app also
# auto-discovers app.api.* routers at import time.
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    for _r in (router, underscore_router):
        if id(_r) not in _ids:
            _main_app.include_router(_r)
            try:
                _main_app._registered_router_ids = getattr(_main_app, "_registered_router_ids", set()) | {id(_r)}
                _ids = _main_app._registered_router_ids
            except Exception:  # noqa: BLE001
                pass
except Exception:  # noqa: BLE001
    pass
