"""Per-camera inactivity (stillness / silence) alerting API for VigilCam."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

import app.services.inactivity_alerts as svc

router = APIRouter(tags=["inactivity_alerts"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"camera_id": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"camera_id": bytes(payload).decode("utf-8", errors="replace")}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "alert"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _serialize(state: Any) -> Any:
    if state is None:
        return None
    if isinstance(state, dict):
        return state
    if isinstance(state, list):
        return [_serialize(s) for s in state]
    for meth in ("to_dict", "model_dump", "dict", "as_dict"):
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


def _result(payload: Any, request: Request | None, fn) -> dict:
    data = _normalize(payload, request)
    result = fn(**data)
    if isinstance(result, bool):
        return {"ok": True, "success": True, "result": result}
    if not isinstance(result, dict):
        ser = _serialize(result)
        if isinstance(ser, dict):
            ser.setdefault("ok", True)
            ser.setdefault("success", True)
            return ser
        return {"ok": True, "success": True, "result": ser}
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


_PATHS = (
    "/api/inactivity-alerts",
    "/api/inactivity_alerts",
    "/api/alerts/inactivity",
    "/api/alerts/inactivity-alerts",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base, operation_id=f"inactivity_alerts_root_{base.replace('/', '_')}")
    async def root_get(request: Request):
        return {
            "ok": True,
            "success": True,
            "summary": svc.get_summary(),
            "config": svc.get_config(),
            "alerts": [_serialize(s) for s in svc.list_status()],
        }

    @r.get(base + "/", operation_id=f"inactivity_alerts_root_slash_{base.replace('/', '_')}")
    async def root_get_slash(request: Request):
        return await root_get(request)

    @r.get(base + "/status", operation_id=f"inactivity_alerts_status_{base.replace('/', '_')}")
    async def status_get(request: Request, camera_id: Optional[str] = Query(default=None)):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            qp = dict(request.query_params)
        except Exception:  # noqa: BLE001
            qp = {}
        params.update({k: v for k, v in qp.items() if k not in params})
        if "camera_id" in params or "camera" in params or "id" in params:
            st = svc.get_status(**params)
            if st is None:
                from fastapi import HTTPException

                raise HTTPException(status_code=404, detail="Camera not tracked")
            out = _serialize(st)
            out.setdefault("ok", True)
            return out
        return {
            "ok": True,
            "success": True,
            **svc.get_config(),
            "summary": svc.get_summary(),
            "cameras": [_serialize(s) for s in svc.list_status()],
        }

    @r.get(base + "/config", operation_id=f"inactivity_alerts_get_config_{base.replace('/', '_')}")
    async def config_get(request: Request):
        return _result(None, request, svc.get_config)

    @r.post(base + "/config", operation_id=f"inactivity_alerts_post_config_{base.replace('/', '_')}")
    async def config_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.configure)

    @r.post(base + "/enable", operation_id=f"inactivity_alerts_enable_{base.replace('/', '_')}")
    async def enable_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        return _result(data, None, svc.enable)

    @r.post(base + "/disable", operation_id=f"inactivity_alerts_disable_{base.replace('/', '_')}")
    async def disable_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        return _result(data, None, svc.disable)

    @r.get(base + "/summary", operation_id=f"inactivity_alerts_summary_{base.replace('/', '_')}")
    async def summary_get(request: Request):
        return _result(None, request, svc.get_summary)

    @r.get(base + "/alerts", operation_id=f"inactivity_alerts_list_{base.replace('/', '_')}")
    async def alerts_get(request: Request):
        return [_serialize(s) for s in svc.list_status()]

    @r.get(base + "/cameras", operation_id=f"inactivity_alerts_cameras_{base.replace('/', '_')}")
    async def cameras_get(request: Request):
        return [_serialize(s) for s in svc.list_status()]

    @r.get(base + "/cameras/{camera_id}", operation_id=f"inactivity_alerts_camera_{base.replace('/', '_')}")
    async def camera_get(camera_id: int, request: Request):
        st = svc.get_status(camera_id)
        if st is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Camera not tracked")
        out = _serialize(st)
        out.setdefault("ok", True)
        return out

    @r.get(base + "/check", operation_id=f"inactivity_alerts_check_get_{base.replace('/', '_')}")
    async def check_get(request: Request, camera_id: Optional[str] = Query(default=None)):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            qp = dict(request.query_params)
        except Exception:  # noqa: BLE001
            qp = {}
        params.update({k: v for k, v in qp.items() if k not in params})
        if "camera_id" in params or "camera" in params or "id" in params:
            out = _serialize(svc.check_camera(**params))
            if isinstance(out, dict):
                out.setdefault("ok", True)
            return out
        return {
            "ok": True,
            "success": True,
            "checked": len(svc.check_all(**params)),
            "cameras": [_serialize(s) for s in svc.list_status()],
            "summary": svc.get_summary(),
        }

    @r.post(base + "/check", operation_id=f"inactivity_alerts_check_post_{base.replace('/', '_')}")
    async def check_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if "camera_id" in data or "camera" in data or "id" in data:
            out = _serialize(svc.check_camera(**data))
            if isinstance(out, dict):
                out.setdefault("ok", True)
                out.setdefault("success", True)
            return out
        results = svc.check_all(**data)
        return {
            "ok": True,
            "success": True,
            "checked": len(results),
            "count": len(results),
            "cameras": _serialize(results),
            "summary": svc.get_summary(),
        }

    @r.post(base + "/check/{camera_id}", operation_id=f"inactivity_alerts_check_one_{base.replace('/', '_')}")
    async def check_one_post(camera_id: int, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("camera_id", camera_id)
        out = _serialize(svc.check_camera(**data))
        if isinstance(out, dict):
            out.setdefault("ok", True)
            out.setdefault("success", True)
        return out

    @r.post(base + "/report", operation_id=f"inactivity_alerts_report_{base.replace('/', '_')}")
    async def report_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        kind = str(data.get("kind", data.get("type", "both"))).strip().lower()
        if kind in ("motion", "stillness", "still", "video"):
            return _result(data, None, svc.report_motion)
        if kind in ("sound", "silence", "silent", "audio"):
            return _result(data, None, svc.report_sound)
        return _result(data, None, svc.report_activity)

    @r.post(base + "/report/{camera_id}", operation_id=f"inactivity_alerts_report_one_{base.replace('/', '_')}")
    async def report_one_post(camera_id: int, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("camera_id", camera_id)
        kind = str(data.get("kind", data.get("type", "both"))).strip().lower()
        if kind in ("motion", "stillness", "still", "video"):
            return _result(data, None, svc.report_motion)
        if kind in ("sound", "silence", "silent", "audio"):
            return _result(data, None, svc.report_sound)
        return _result(data, None, svc.report_activity)

    @r.post(base + "/motion", operation_id=f"inactivity_alerts_motion_{base.replace('/', '_')}")
    async def motion_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.report_motion)

    @r.post(base + "/motion/{camera_id}", operation_id=f"inactivity_alerts_motion_one_{base.replace('/', '_')}")
    async def motion_one_post(camera_id: int, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("camera_id", camera_id)
        return _result(data, None, svc.report_motion)

    @r.post(base + "/sound", operation_id=f"inactivity_alerts_sound_{base.replace('/', '_')}")
    async def sound_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.report_sound)

    @r.post(base + "/sound/{camera_id}", operation_id=f"inactivity_alerts_sound_one_{base.replace('/', '_')}")
    async def sound_one_post(camera_id: int, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("camera_id", camera_id)
        return _result(data, None, svc.report_sound)

    @r.post(base + "/alert", operation_id=f"inactivity_alerts_alert_{base.replace('/', '_')}")
    async def alert_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.send_inactivity_alert)

    @r.post(base + "/notify", operation_id=f"inactivity_alerts_notify_{base.replace('/', '_')}")
    async def notify_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.send_inactivity_alert)

    @r.post(base + "/alert/{camera_id}", operation_id=f"inactivity_alerts_alert_one_{base.replace('/', '_')}")
    async def alert_one_post(camera_id: int, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("camera_id", camera_id)
        return _result(data, None, svc.send_inactivity_alert)

    @r.post(base + "/notify/{camera_id}", operation_id=f"inactivity_alerts_notify_one_{base.replace('/', '_')}")
    async def notify_one_post(camera_id: int, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("camera_id", camera_id)
        return _result(data, None, svc.send_inactivity_alert)

    @r.post(base + "/cameras/{camera_id}/alert", operation_id=f"inactivity_alerts_cam_alert_{base.replace('/', '_')}")
    async def cam_alert_post(camera_id: int, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("camera_id", camera_id)
        return _result(data, None, svc.send_inactivity_alert)

    @r.post(base + "/resolve/{camera_id}", operation_id=f"inactivity_alerts_resolve_{base.replace('/', '_')}")
    async def resolve_post(camera_id: int, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("camera_id", camera_id)
        out = _serialize(svc.resolve(**data))
        if isinstance(out, dict):
            out.setdefault("ok", True)
            out.setdefault("success", True)
        return out

    @r.post(base + "/reset", operation_id=f"inactivity_alerts_reset_{base.replace('/', '_')}")
    async def reset_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not data:
            result = svc.reset()
            if isinstance(result, dict):
                result.setdefault("ok", True)
                return result
            return {"ok": True, "success": True, "summary": _serialize(result)}
        result = svc.reset(**data)
        ser = _serialize(result)
        if isinstance(ser, dict):
            ser.setdefault("ok", True)
            return ser
        return {"ok": True, "success": True, "result": ser}

    @r.post(base + "/reset/{camera_id}", operation_id=f"inactivity_alerts_reset_one_{base.replace('/', '_')}")
    async def reset_one_post(camera_id: int):
        out = _serialize(svc.reset(camera_id))
        if isinstance(out, dict):
            out.setdefault("ok", True)
        return out

    @r.post(base + "/clear", operation_id=f"inactivity_alerts_clear_{base.replace('/', '_')}")
    async def clear_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not data:
            svc.clear()
            return {"ok": True, "success": True, "cleared": True}
        if "camera_id" in data or "camera" in data or "id" in data:
            svc.clear(**data)
            return {"ok": True, "success": True, "cleared": True}
        svc.clear()
        return {"ok": True, "success": True, "cleared": True}

    @r.get(base + "/health", operation_id=f"inactivity_alerts_health_{base.replace('/', '_')}")
    async def health_get():
        return {"ok": True, "success": True, "service": "inactivity_alerts"}


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix.
underscore_router = APIRouter(prefix="/api/inactivity_alerts", tags=["inactivity_alerts"])
hyphen_router = APIRouter(prefix="/api/inactivity-alerts", tags=["inactivity_alerts"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root(request: Request):
        return {
            "ok": True,
            "success": True,
            "summary": svc.get_summary(),
            "config": svc.get_config(),
            "alerts": [_serialize(s) for s in svc.list_status()],
        }

    @r.get("/")
    async def slash(request: Request):
        return await root(request)

    @r.get("/status")
    async def status(request: Request, camera_id: Optional[str] = Query(default=None)):
        params: dict = {}
        if camera_id is not None:
            params["camera_id"] = camera_id
        try:
            params.update({k: v for k, v in request.query_params.items() if k not in params})
        except Exception:  # noqa: BLE001
            pass
        if "camera_id" in params or "camera" in params or "id" in params:
            st = svc.get_status(**params)
            if st is None:
                from fastapi import HTTPException

                raise HTTPException(status_code=404, detail="Camera not tracked")
            out = _serialize(st)
            out.setdefault("ok", True)
            return out
        return {"ok": True, "success": True, **svc.get_config(), "summary": svc.get_summary()}

    @r.get("/summary")
    async def summary():
        return _result(None, None, svc.get_summary)

    @r.get("/config")
    async def cfg_get(request: Request):
        return _result(None, request, svc.get_config)

    @r.post("/config")
    async def cfg_post(payload: Any = Body(default=None), request: Request = None):
        return _result(payload, request, svc.configure)

    @r.post("/enable")
    async def en(payload: Any = Body(default=None), request: Request = None):
        return _result(_normalize(payload, request), None, svc.enable)

    @r.post("/disable")
    async def dis(payload: Any = Body(default=None), request: Request = None):
        return _result(_normalize(payload, request), None, svc.disable)

    @r.get("/health")
    async def health():
        return {"ok": True, "success": True, "service": "inactivity_alerts"}


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
