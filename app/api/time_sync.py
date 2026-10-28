"""NTP / camera time-sync status API.

Auto-registered by ``app/main.py``'s router auto-loader (no ``main.py``
edit required). Read-only status plus ONVIF clock push via
:mod:`app.services.time_sync` (which uses
:mod:`app.services.onvif_digest_service` where available, else reports).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

from app.services import time_sync as time_sync_service


router = APIRouter(prefix="/api/time-sync", tags=["time-sync"])
underscore_router = APIRouter(prefix="/api/time_sync", tags=["time-sync"])


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
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "camera"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _pick(data: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None and data[key] != "":
            return data[key]
    return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _conn_from(data: dict) -> tuple[str, str, str, int, str]:
    ip = str(_pick(data, "ip", "host", "onvif_host", "address", default="") or "").strip()
    username = str(_pick(data, "username", "user", "login", default="") or "")
    password = str(_pick(data, "password", "pass_", "pass", default="") or "")
    port = _coerce_int(_pick(data, "port", "onvif_port", "control_port", default=80), 80)
    scheme = str(_pick(data, "scheme", "control_scheme", "protocol", default="http") or "http").strip().lower() or "http"
    return ip, username, password, port, scheme


def _register_routes(target: APIRouter) -> None:
    @target.get("/status")
    async def get_status(
        request: Request,
        ip: Optional[str] = Query(default=None),
        host: Optional[str] = Query(default=None),
        port: Optional[str] = Query(default=None),
        scheme: Optional[str] = Query(default=None),
        tolerance_seconds: Optional[str] = Query(default=None),
        tolerance: Optional[str] = Query(default=None),
        timeout: Optional[str] = Query(default=None),
    ):
        data: dict = {}
        try:
            for k, v in request.query_params.items():
                data[k] = v
        except Exception:  # noqa: BLE001
            pass
        tolerance_v = _pick(data, "tolerance_seconds", "tolerance", "toleranceSecs", default=time_sync_service.DEFAULT_TOLERANCE_SECONDS)
        return await time_sync_service.get_time_sync_status(
            ip=str(_pick(data, "ip", "host", "onvif_host", default="") or ""),
            port=_coerce_int(_pick(data, "port", "onvif_port", default=80), 80),
            scheme=str(_pick(data, "scheme", "control_scheme", default="http") or "http"),
            tolerance_seconds=_coerce_float(tolerance_v, time_sync_service.DEFAULT_TOLERANCE_SECONDS),
            timeout=_coerce_float(_pick(data, "timeout", default=time_sync_service.DEFAULT_TIMEOUT), time_sync_service.DEFAULT_TIMEOUT),
        )

    @target.post("/status")
    async def post_status(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        tolerance_v = _pick(data, "tolerance_seconds", "tolerance", default=time_sync_service.DEFAULT_TOLERANCE_SECONDS)
        return await time_sync_service.get_time_sync_status(
            ip=str(_pick(data, "ip", "host", "onvif_host", default="") or ""),
            port=_coerce_int(_pick(data, "port", "onvif_port", default=80), 80),
            scheme=str(_pick(data, "scheme", "control_scheme", default="http") or "http"),
            tolerance_seconds=_coerce_float(tolerance_v, time_sync_service.DEFAULT_TOLERANCE_SECONDS),
            timeout=_coerce_float(_pick(data, "timeout", default=time_sync_service.DEFAULT_TIMEOUT), time_sync_service.DEFAULT_TIMEOUT),
        )

    @target.get("/nvr-time")
    async def get_nvr_time():
        return time_sync_service.get_nvr_time()

    @target.get("/ntp")
    async def get_ntp():
        return time_sync_service.get_ntp_status()

    @target.get("/camera-time")
    async def get_camera_time(
        request: Request,
        ip: Optional[str] = Query(default=None),
        host: Optional[str] = Query(default=None),
        port: Optional[str] = Query(default=None),
        scheme: Optional[str] = Query(default=None),
        timeout: Optional[str] = Query(default=None),
    ):
        data: dict = {}
        try:
            for k, v in request.query_params.items():
                data[k] = v
        except Exception:  # noqa: BLE001
            pass
        return await time_sync_service.get_camera_time(
            ip=str(_pick(data, "ip", "host", "onvif_host", default="") or ""),
            port=_coerce_int(_pick(data, "port", "onvif_port", default=80), 80),
            scheme=str(_pick(data, "scheme", "control_scheme", default="http") or "http"),
            timeout=_coerce_float(_pick(data, "timeout", default=time_sync_service.DEFAULT_TIMEOUT), time_sync_service.DEFAULT_TIMEOUT),
        )

    async def _sync_impl(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        ip, username, password, port, scheme = _conn_from(data)
        result = await time_sync_service.sync_camera_time(
            ip=ip,
            username=username,
            password=password,
            port=port,
            scheme=scheme,
            timeout=_coerce_float(_pick(data, "timeout", default=time_sync_service.DEFAULT_TIMEOUT), time_sync_service.DEFAULT_TIMEOUT),
        )
        result.setdefault("ok", bool(result.get("ok")))
        result.setdefault("success", bool(result.get("ok")))
        return result

    for _path in ("/sync", "/camera/sync", "/camera-time/sync", "/push"):
        target.post(_path)(_sync_impl)

    @target.post("/check")
    async def check_sync(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        tolerance_v = _pick(data, "tolerance_seconds", "tolerance", default=time_sync_service.DEFAULT_TOLERANCE_SECONDS)
        return await time_sync_service.get_time_sync_status(
            ip=str(_pick(data, "ip", "host", "onvif_host", default="") or ""),
            port=_coerce_int(_pick(data, "port", "onvif_port", default=80), 80),
            scheme=str(_pick(data, "scheme", "control_scheme", default="http") or "http"),
            tolerance_seconds=_coerce_float(tolerance_v, time_sync_service.DEFAULT_TOLERANCE_SECONDS),
            timeout=_coerce_float(_pick(data, "timeout", default=time_sync_service.DEFAULT_TIMEOUT), time_sync_service.DEFAULT_TIMEOUT),
        )


_register_routes(router)
_register_routes(underscore_router)

# Side-effect registration into the main app (mirrors rtsp-health pattern).
# The auto-loader in app/main.py already picks up ``router``; this covers
# the underscore alias when imported after startup.
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
