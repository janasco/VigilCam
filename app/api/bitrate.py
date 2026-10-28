"""Stream bitrate sampler API for VigilCam.

Auto-registered by ``app.main`` (any module in ``app.api`` exposing
``router`` is included), so no ``app/main.py`` edit is needed.
Canonical prefix is ``/api/bitrate``; ``/api/stream-bitrate`` (hyphen)
and ``/api/stream_bitrate`` (underscore) aliases are served as well.
Both ``/sample`` and ``/probe`` verbs are accepted for compatibility.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import app.services.bitrate as svc

router = APIRouter(prefix="/api/bitrate", tags=["bitrate"])
stream_hyphen_router = APIRouter(prefix="/api/stream-bitrate", tags=["bitrate"])
stream_underscore_router = APIRouter(prefix="/api/stream_bitrate", tags=["bitrate"])
# Back-compat singular alias object expected by some imports.
underscore_router = stream_underscore_router


class SampleRequest(BaseModel):
    url: Optional[str] = None
    target: Optional[str] = None
    stream_url: Optional[str] = None
    source: Optional[str] = None
    camera_id: Optional[Any] = None
    id: Optional[Any] = None
    duration: Optional[float] = None
    duration_seconds: Optional[float] = None
    sample_seconds: Optional[float] = None
    timeout: Optional[float] = None
    timeout_seconds: Optional[float] = None

    class Config:
        extra = "allow"


def _timeout_from(payload: dict, default: float = 5.0) -> float:
    for key in ("timeout", "timeout_seconds", "timeout_ms"):
        if payload.get(key) is not None:
            try:
                val = float(payload[key])
                if key == "timeout_ms":
                    val = val / 1000.0
                return val
            except (TypeError, ValueError):
                continue
    return default


def _duration_from(payload: dict, default: float = 2.0) -> float:
    for key in ("duration", "duration_seconds", "sample_seconds", "sample_duration", "duration_ms"):
        if payload.get(key) is not None:
            try:
                val = float(payload[key])
                if key == "duration_ms":
                    val = val / 1000.0
                return val
            except (TypeError, ValueError):
                continue
    return default


def _payload_of(payload: Any) -> dict:
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
    return {}


def _do_sample(data: dict) -> dict:
    url = data.get("url") or data.get("stream_url") or data.get("source")
    target = data.get("target") or data.get("camera_id") or data.get("id")
    duration = _duration_from(data)
    timeout = _timeout_from(data)
    if url:
        return svc.sample_url(url, duration, timeout, target=target or url).to_dict()
    if target is not None:
        return svc.sample_camera(target, duration, timeout, url=url).to_dict()
    raise HTTPException(status_code=422, detail="Provide 'url' or camera 'target'")


def _summary_payload() -> dict:
    summary = svc.get_summary()
    return {
        **summary,
        "results": svc.list_results_dicts(),
        "ok": True,
        "success": True,
    }


def _register_routes(target: APIRouter) -> None:
    @target.get("/status")
    async def get_status():
        return _summary_payload()

    @target.get("/summary")
    async def get_summary():
        return {**svc.get_summary(), "ok": True, "success": True}

    @target.get("/health")
    async def get_health():
        return _summary_payload()

    @target.get("/results")
    async def list_results():
        return svc.list_results_dicts()

    @target.delete("/results")
    async def clear_results():
        svc.clear()
        return {"ok": True, "success": True, "cleared": True, **svc.get_summary()}

    @target.get("/sample")
    async def sample_via_query(
        url: Optional[str] = Query(default=None),
        target: Optional[str] = Query(default=None),
        duration: Optional[float] = Query(default=None),
        timeout: Optional[float] = Query(default=None),
    ):
        data = {"url": url, "target": target, "duration": duration or 2.0, "timeout": timeout or 5.0}
        return _do_sample(data)

    @target.get("/probe")
    async def probe_via_query(
        url: Optional[str] = Query(default=None),
        target: Optional[str] = Query(default=None),
        duration: Optional[float] = Query(default=None),
        timeout: Optional[float] = Query(default=None),
    ):
        data = {"url": url, "target": target, "duration": duration or 2.0, "timeout": timeout or 5.0}
        return _do_sample(data)

    @target.post("/sample")
    async def sample_stream(payload: Any = None):
        return _do_sample(_payload_of(payload))

    @target.post("/probe")
    async def probe_stream(payload: Any = None):
        return _do_sample(_payload_of(payload))

    @target.get("/camera/{camera_id}")
    async def sample_camera_get(
        camera_id: int,
        url: Optional[str] = Query(default=None),
        duration: Optional[float] = Query(default=None),
    ):
        result = svc.sample_camera(camera_id, duration or 2.0, url=url)
        out = result.to_dict()
        out["camera_id"] = camera_id
        out["id"] = camera_id
        return out

    @target.post("/camera/{camera_id}/sample")
    async def sample_camera_post(camera_id: int, payload: Any = None):
        data = _payload_of(payload)
        url = data.get("url") or data.get("stream_url") or data.get("source")
        duration = _duration_from(data)
        timeout = _timeout_from(data)
        result = svc.sample_camera(camera_id, duration, timeout, url=url)
        out = result.to_dict()
        out["camera_id"] = camera_id
        out["id"] = camera_id
        out["ok"] = True
        out["success"] = True
        return out

    @target.post("/camera/{camera_id}/probe")
    async def probe_camera_post(camera_id: int, payload: Any = None):
        data = _payload_of(payload)
        url = data.get("url") or data.get("stream_url") or data.get("source")
        duration = _duration_from(data)
        timeout = _timeout_from(data)
        result = svc.sample_camera(camera_id, duration, timeout, url=url)
        out = result.to_dict()
        out["camera_id"] = camera_id
        out["id"] = camera_id
        out["ok"] = True
        out["success"] = True
        return out

    @target.get("/{target}")
    async def get_sample_target(target: str):
        st = svc.get_status(target)
        if st is None:
            try:
                st = svc.get_status(f"camera:{int(target)}")
            except (TypeError, ValueError):
                st = None
        if st is None:
            raise HTTPException(status_code=404, detail="No bitrate sample for target")
        return st.to_dict()


_register_routes(router)
_register_routes(stream_hyphen_router)
_register_routes(stream_underscore_router)

# Compatibility aliases.
bitrate_router = router
api_router = router

# Side-effect registration so the routers are live even when this module is
# imported after app startup (mirrors watchdog pattern). The main app also
# auto-discovers app.api.* routers at import time.
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    for _r in (router, stream_hyphen_router, stream_underscore_router):
        if id(_r) not in _ids:
            _main_app.include_router(_r)
            try:
                _main_app._registered_router_ids = getattr(_main_app, "_registered_router_ids", set()) | {id(_r)}
                _ids = _main_app._registered_router_ids
            except Exception:  # noqa: BLE001
                pass
except Exception:  # noqa: BLE001
    pass
