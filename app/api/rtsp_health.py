"""RTSP resilience API for VigilCam.

Exposes the ``app.services.rtsp_health`` keepalive / stale-stream / backoff
state over HTTP. Auto-discovered by ``app.main`` (no ``app/main.py`` edit);
a side-effect registration block below mirrors the ``watchdog`` pattern so
the routes are live even when imported after startup.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

import app.services.rtsp_health as rtsp_health_mod

router = APIRouter(prefix="/api/rtsp-health", tags=["rtsp-health"])
underscore_router = APIRouter(prefix="/api/rtsp_health", tags=["rtsp-health"])


class RtspStreamRegister(BaseModel):
    stream_id: Optional[Any] = None
    id: Optional[Any] = None
    camera_id: Optional[Any] = None
    url: Optional[str] = None
    transport: Optional[str] = None
    session_id: Optional[str] = None
    session_header: Optional[str] = None
    public_header: Optional[str] = None
    session_timeout_seconds: Optional[float] = None

    class Config:
        extra = "allow"


def _serialize(state: Any) -> Any:
    if state is None:
        return None
    if isinstance(state, dict):
        return state
    if isinstance(state, list):
        return [_serialize(s) for s in state]
    for meth in ("to_dict", "model_dump", "dict"):
        fn = getattr(state, meth, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                continue
    try:
        return dict(state)
    except Exception:  # noqa: BLE001
        return {"value": str(state)}


def _payload(data: Any) -> dict:
    out: dict = {}
    if data is None:
        return out
    if hasattr(data, "model_dump"):
        try:
            raw = data.model_dump()
            out = {k: v for k, v in raw.items() if v is not None}
        except Exception:  # noqa: BLE001
            out = {}
    elif isinstance(data, dict):
        out = {k: v for k, v in data.items() if v is not None}
    for nested_key in ("stream", "config", "settings", "params", "options", "data", "body"):
        nested = out.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                out.setdefault(k, v)
    return out


def _resolve_id(stream_id: Any, data: dict) -> Any:
    for key in ("stream_id", "id", "camera_id"):
        if stream_id is not None:
            break
        stream_id = data.get(key)
    return stream_id if stream_id is not None else "default"


def _status_payload() -> dict:
    return {
        **rtsp_health_mod.get_config(),
        "summary": rtsp_health_mod.get_summary(),
        "streams": _serialize(rtsp_health_mod.list_health()),
    }


def _register_routes(target: APIRouter) -> None:
    @target.get("/status")
    async def get_rtsp_health_status():
        return _status_payload()

    @target.get("/config")
    async def get_rtsp_health_config():
        return rtsp_health_mod.get_config()

    @target.post("/config")
    async def update_rtsp_health_config(payload: Any = Body(default=None)):
        data = _payload(payload)
        # Config is constant in this revision; echo requested overrides.
        cfg = rtsp_health_mod.get_config()
        cfg.update({k: v for k, v in data.items() if k in cfg})
        cfg.setdefault("ok", True)
        return cfg

    @target.get("/summary")
    async def get_rtsp_health_summary():
        return rtsp_health_mod.get_summary()

    @target.get("/streams")
    async def list_rtsp_streams():
        return _serialize(rtsp_health_mod.list_health())

    @target.post("/streams")
    async def register_rtsp_stream(payload: Any = Body(default=None)):
        data = _payload(payload)
        sid = _resolve_id(None, data)
        for key in ("stream_id", "id", "camera_id"):
            data.pop(key, None)
        state = rtsp_health_mod.register_stream(sid, **data)
        out = _serialize(state)
        out.setdefault("ok", True)
        return out

    @target.get("/streams/{stream_id}")
    async def get_rtsp_stream(stream_id: str):
        state = rtsp_health_mod.get_health(stream_id, create=False)
        if state is None:
            raise HTTPException(status_code=404, detail="Unknown RTSP stream")
        return _serialize(state)

    @target.post("/streams/{stream_id}/frame")
    async def mark_rtsp_stream_frame(stream_id: str, payload: Any = Body(default=None)):
        data = _payload(payload)
        state = rtsp_health_mod.mark_frame_received(stream_id, **data)
        out = _serialize(state)
        out.setdefault("ok", True)
        return out

    @target.post("/streams/{stream_id}/keepalive")
    async def record_rtsp_stream_keepalive(stream_id: str, payload: Any = Body(default=None)):
        data = _payload(payload)
        sent = rtsp_health_mod.mark_keepalive_sent(stream_id)
        if "ok" in data or "success" in data or "error" in data:
            ok = data.get("ok", data.get("success", True))
            state = rtsp_health_mod.record_keepalive_response(stream_id, ok=ok, **data)
        else:
            state = sent
        out = _serialize(state)
        out.setdefault("ok", True)
        out.setdefault("keepalive_due", False)
        return out

    @target.post("/streams/{stream_id}/failure")
    async def record_rtsp_stream_failure(stream_id: str, payload: Any = Body(default=None)):
        data = _payload(payload)
        state = rtsp_health_mod.record_failure(stream_id, **data)
        out = _serialize(state)
        out.setdefault("ok", True)
        return out

    @target.post("/streams/{stream_id}/success")
    async def record_rtsp_stream_success(stream_id: str, payload: Any = Body(default=None)):
        data = _payload(payload)
        state = rtsp_health_mod.record_success(stream_id, **data)
        out = _serialize(state)
        out.setdefault("ok", True)
        return out

    @target.post("/streams/{stream_id}/reconnect")
    async def reconnect_rtsp_stream(stream_id: str, payload: Any = Body(default=None)):
        data = _payload(payload)
        due = rtsp_health_mod.should_reconnect(stream_id)
        state = rtsp_health_mod.get_health(stream_id)
        out = _serialize(state)
        out.setdefault("ok", True)
        out["reconnect_due"] = bool(due)
        out["should_reconnect"] = bool(due)
        out["retry_in_seconds"] = rtsp_health_mod.next_retry_in(stream_id)
        if data.get("record", data.get("record_failure", False)):
            failed = rtsp_health_mod.record_failure(stream_id, **data)
            out = _serialize(failed)
            out.setdefault("ok", True)
        return out

    @target.get("/streams/{stream_id}/keepalive-schedule")
    async def get_rtsp_keepalive_schedule(stream_id: str):
        state = rtsp_health_mod.get_health(stream_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Unknown RTSP stream")
        interval = rtsp_health_mod.get_next_keepalive_interval(state.session_timeout_seconds)
        return {
            "stream_id": state.stream_id,
            "session_timeout_seconds": state.session_timeout_seconds,
            "next_interval_seconds": interval,
            "next_keepalive_at": state.next_keepalive_at.isoformat() if state.next_keepalive_at else None,
            "keepalive_due": rtsp_health_mod.should_send_keepalive(state),
            "server_supports_get_parameter": state.server_supports_get_parameter,
        }

    @target.post("/check")
    async def run_rtsp_health_check(payload: Any = Body(default=None)):
        data = _payload(payload)
        sid = data.get("stream_id", data.get("id", data.get("camera_id")))
        if sid is not None:
            state = rtsp_health_mod.evaluate(stream_id=sid)
            states = [state] if state is not None else []
        else:
            states = [rtsp_health_mod.evaluate(s) for s in rtsp_health_mod.list_health()]
        return {
            "checked": len(states),
            "count": len(states),
            "streams": _serialize(states),
            "summary": rtsp_health_mod.get_summary(),
            "ok": True,
            "success": True,
        }

    @target.post("/reset")
    async def reset_rtsp_health_all():
        rtsp_health_mod.clear()
        return {"ok": True, "success": True, "summary": rtsp_health_mod.get_summary()}

    @target.post("/reset/{stream_id}")
    async def reset_rtsp_stream(stream_id: str):
        state = rtsp_health_mod.reset_backoff(stream_id)
        out = _serialize(state)
        out.setdefault("ok", True)
        return out

    @target.delete("/streams/{stream_id}")
    async def delete_rtsp_stream(stream_id: str):
        ok = rtsp_health_mod.remove(stream_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Unknown RTSP stream")
        return {"ok": True, "removed": True, "stream_id": stream_id}


_register_routes(router)
_register_routes(underscore_router)

# Side-effect registration into the main app (mirrors watchdog pattern).
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    for _r in (router, underscore_router):
        if id(_r) not in _ids:
            _main_app.include_router(_r)
            _ids = _ids | {id(_r)}
    try:
        _main_app._registered_router_ids = _ids
    except Exception:  # noqa: BLE001
        pass
except Exception:  # noqa: BLE001
    pass
