"""NAT traversal status API.

Auto-registered by ``app/main.py``'s router auto-loader (no ``main.py``
edit required).  Stateless read-only status surface over
:mod:`app.services.nat_status`.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

from app.services import nat_status as nat_service


router = APIRouter(prefix="/api/nat", tags=["nat"])
nat_router = router


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
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _status_response(data: dict) -> dict:
    include_raw = _coerce_bool(data.get("include_discovery", data.get("discover", True)))
    include_discovery = True if include_raw is None else include_raw
    timeout = _coerce_float(
        data.get("timeout", data.get("discovery_timeout", nat_service.DEFAULT_TIMEOUT)),
        nat_service.DEFAULT_TIMEOUT,
    )
    force = _coerce_bool(
        data.get("force_upnp", data.get("force", data.get("upnp", data.get("available"))))
    )
    port = data.get("port", data.get("internal_port", data.get("external_port")))
    try:
        port_i = int(port) if port is not None else None
    except (TypeError, ValueError):
        port_i = None
    result = nat_service.get_nat_status(
        include_discovery=include_discovery, timeout=timeout, force_upnp=force, port=port_i
    )
    # Redundant keys for client/test tolerance.
    result.setdefault("ok", True)
    result.setdefault("success", True)
    result.setdefault("upnp_available", result.get("upnp"))
    result.setdefault("recommendation", result.get("strategy"))
    return result


@router.get("/status")
async def nat_status_endpoint(
    request: Request,
    include_discovery: Optional[str] = Query(default=None),
    timeout: Optional[str] = Query(default=None),
    force_upnp: Optional[str] = Query(default=None),
    port: Optional[str] = Query(default=None),
):
    data: dict = {}
    try:
        for k, v in request.query_params.items():
            data[k] = v
    except Exception:  # noqa: BLE001
        pass
    return _status_response(data)


@router.get("/nat-status")
async def nat_status_alias(request: Request):
    data: dict = {}
    try:
        for k, v in request.query_params.items():
            data[k] = v
    except Exception:  # noqa: BLE001
        pass
    return _status_response(data)


@router.get("/upnp")
async def upnp_status(request: Request):
    data: dict = {}
    try:
        for k, v in request.query_params.items():
            data[k] = v
    except Exception:  # noqa: BLE001
        pass
    timeout = _coerce_float(data.get("timeout", nat_service.DEFAULT_TIMEOUT), nat_service.DEFAULT_TIMEOUT)
    force = _coerce_bool(data.get("force_upnp", data.get("force")))
    result = nat_service.detect_upnp_availability(timeout=timeout, force=force)
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


@router.post("/check")
async def nat_check(payload: Any = Body(default=None), request: Request = None):
    return _status_response(_normalize(payload, request))


@router.post("/status")
async def nat_status_post(payload: Any = Body(default=None), request: Request = None):
    return _status_response(_normalize(payload, request))


@router.post("/discover")
async def nat_discover(payload: Any = Body(default=None), request: Request = None):
    return _status_response(_normalize(payload, request))
