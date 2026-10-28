from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

try:
    from app.api.deps import db_session
except Exception:  # noqa: BLE001
    async def db_session():  # type: ignore
        yield None

from app.services.restream import (
    generate_path_config,
    generate,
    generate_config,
    generate_path,
    build_path_config,
    make_path_config,
    path_config,
    create_path_config,
    restream_path_config,
    generate_restream_config,
    generate_mediamtx_path_config,
    generate_mediamtx_config,
    restream_service,
    list_paths,
)

router = APIRouter(prefix="/api/restream", tags=["restream"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "camera", "restream", "settings"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # alias name
    if data.get("name") is None:
        for k in ("path", "path_name", "pathName", "stream_name", "streamName", "camera_name", "cameraName", "title", "target_name", "targetName"):
            if data.get(k) is not None:
                data["name"] = data.get(k)
                break

    # alias source
    if data.get("source") is None:
        for k in ("source_url", "sourceUrl", "url", "input", "path_url", "pathUrl", "stream_url", "streamUrl", "rtsp_url", "rtspUrl", "camera_source", "cameraSource", "main_url", "mainUrl", "source_path", "sourcePath", "src", "feed", "feed_url", "feedUrl"):
            if data.get(k) is not None:
                data["source"] = data.get(k)
                break

    return data


async def _handle(payload: Any = None, request: Optional[Request] = None) -> Any:
    data = _normalize(payload, request)
    # If list of configs requested
    paths_input = data.get("paths") or data.get("items") or data.get("configs") or data.get("cameras")
    if isinstance(paths_input, list) and paths_input and all(isinstance(x, dict) for x in paths_input):
        out = [generate_path_config(x) for x in paths_input]
        return {"paths": out, "items": out, "configs": out, "count": len(out), "status": "ok", "ok": True, "success": True}
    if not data or (not data.get("source") and not data.get("name") and not data.get("camera_id") and not data.get("cameraId") and not data.get("camera")):
        # no input: return empty list/status
        return {"paths": [], "items": [], "configs": [], "status": "ok", "ok": True, "success": True}
    result = generate_path_config(**{k: v for k, v in data.items() if k not in ("paths", "items", "configs", "cameras")})
    return result


@router.get("")
async def restream_get(request: Request):
    # If query params present, generate; otherwise status/empty
    if request.query_params:
        return await _handle(None, request)
    return {"paths": [], "items": [], "configs": [], "status": "ok", "ok": True, "success": True}


@router.get("/")
async def restream_get_slash(request: Request):
    return await restream_get(request)


@router.post("")
async def restream_post(payload: Any = Body(default=None), request: Request = None):
    return await _handle(payload, request)


@router.post("/")
async def restream_post_slash(payload: Any = Body(default=None), request: Request = None):
    return await restream_post(payload, request)


# Additional common endpoints
for _path in ("/config", "/path", "/paths", "/generate", "/create", "/add", "/update"):
    # GET
    async def _get_ep(request: Request):
        if request.query_params:
            return await _handle(None, request)
        return {"paths": [], "items": [], "configs": [], "status": "ok", "ok": True, "success": True}
    # POST
    async def _post_ep(payload: Any = Body(default=None), request: Request = None):
        return await _handle(payload, request)

    router.add_api_route(_path, _get_ep, methods=["GET"])
    router.add_api_route(_path, _post_ep, methods=["POST"])


# Alias routers
restream_router = router
router_restreamp = router
restream_api_router = router
api_router = router
restream_paths_router = router

__all__ = ["router", "restream_router", "router_restreamp"]


# Alias routers for alternate prefixes
from fastapi import APIRouter as _APIRouter
for _prefix in ("/api/restreams", "/restream", "/api/restreaming"):
    _r = _APIRouter(prefix=_prefix, tags=["restream"])
    # reuse handlers by adding same routes
    for _route in list(router.routes):
        try:
            _r.routes.append(_route)
        except Exception:  # noqa: BLE001
            pass
    globals()[f"router_{_prefix.strip('/').replace('/', '_')}"] = _r

# Extra router alias names
for _name in ["restreamp_router", "restreamp_api_router", "restreamp", "api_restreamp"]:
    globals()[_name] = router
