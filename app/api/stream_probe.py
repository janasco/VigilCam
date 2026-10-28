"""Stream health probe API for VigilCam.

Auto-registered by ``app.main`` (any module in ``app.api`` exposing
``router`` is included), so no ``app/main.py`` edit is needed.
Both ``/api/stream-probe`` (canonical) and ``/api/stream_probe``
(underscore alias) are served.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import app.services.stream_probe as svc

router = APIRouter(prefix="/api/stream-probe", tags=["stream-probe"])
underscore_router = APIRouter(prefix="/api/stream_probe", tags=["stream-probe"])


class ProbeRequest(BaseModel):
    url: Optional[str] = None
    target: Optional[str] = None
    stream_url: Optional[str] = None
    source: Optional[str] = None
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


def _probe_payload(payload: Any) -> dict:
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


def _do_probe(data: dict) -> dict:
    url = data.get("url") or data.get("stream_url") or data.get("source")
    target = data.get("target") or data.get("camera_id") or data.get("id")
    timeout = _timeout_from(data)
    if url:
        return svc.probe_url(url, timeout, target=target or url).to_dict()
    if target is not None:
        return svc.probe_camera(target, timeout, url=url).to_dict()
    raise HTTPException(status_code=422, detail="Provide 'url' or camera 'target'")


def _summary_payload() -> dict:
    summary = svc.get_summary()
    return {
        **summary,
        "results": svc.list_results_dicts(),
        "ok": True,
        "success": True,
    }


@router.get("/status")
@underscore_router.get("/status")
async def get_status():
    return _summary_payload()


@router.get("/summary")
@underscore_router.get("/summary")
async def get_summary():
    return {**svc.get_summary(), "ok": True, "success": True}


@router.get("/health")
@underscore_router.get("/health")
async def get_health():
    return _summary_payload()


@router.get("/results")
@underscore_router.get("/results")
async def list_results():
    return svc.list_results_dicts()


@router.delete("/results")
@underscore_router.delete("/results")
async def clear_results():
    svc.clear()
    return {"ok": True, "success": True, "cleared": True, **svc.get_summary()}


@router.get("/probe")
@underscore_router.get("/probe")
async def probe_via_query(
    url: Optional[str] = Query(default=None),
    target: Optional[str] = Query(default=None),
    timeout: Optional[float] = Query(default=None),
):
    data = {"url": url, "target": target, "timeout": timeout or 5.0}
    return _do_probe(data)


@router.post("/probe")
@underscore_router.post("/probe")
async def probe_stream(payload: Any = None):
    return _do_probe(_probe_payload(payload))


@router.get("/camera/{camera_id}")
@underscore_router.get("/camera/{camera_id}")
async def probe_camera_get(camera_id: int, url: Optional[str] = Query(default=None)):
    result = svc.probe_camera(camera_id, url=url)
    out = result.to_dict()
    out["camera_id"] = camera_id
    out["id"] = camera_id
    return out


@router.post("/camera/{camera_id}/probe")
@underscore_router.post("/camera/{camera_id}/probe")
async def probe_camera_post(camera_id: int, payload: Any = None):
    data = _probe_payload(payload)
    url = data.get("url") or data.get("stream_url") or data.get("source")
    timeout = _timeout_from(data)
    result = svc.probe_camera(camera_id, timeout, url=url)
    out = result.to_dict()
    out["camera_id"] = camera_id
    out["id"] = camera_id
    out["ok"] = True
    out["success"] = True
    return out


@router.get("/{target}")
@underscore_router.get("/{target}")
async def get_probe_target(target: str):
    st = svc.get_status(target)
    if st is None:
        # Fall back to camera-id interpretation for numeric targets.
        try:
            st = svc.get_status(f"camera:{int(target)}")
        except (TypeError, ValueError):
            st = None
    if st is None:
        raise HTTPException(status_code=404, detail="No probe result for target")
    return st.to_dict()


# Side-effect registration so the routers are live even when this module is
# imported after app startup (mirrors watchdog pattern). The main app also
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
