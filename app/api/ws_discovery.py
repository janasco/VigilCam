"""WS-Discovery (UDP multicast WS-Probe) API for VigilCam.

Auto-registered by ``app.main`` (any module in ``app.api`` exposing
``router`` is included), so no ``app/main.py`` edit is needed.
Both ``/api/ws-discovery`` (canonical) and ``/api/ws_discovery``
(underscore alias) are served.
"""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import app.services.ws_discovery as svc

router = APIRouter(prefix="/api/ws-discovery", tags=["ws-discovery"])
underscore_router = APIRouter(prefix="/api/ws_discovery", tags=["ws-discovery"])


class WsDiscoveryDevice(BaseModel):
    types: List[str] = Field(default_factory=list)
    xaddresses: List[str] = Field(default_factory=list)
    model: str = ""
    mfr: str = ""
    address: str = ""
    scopes: List[str] = Field(default_factory=list)
    endpoint: str = ""
    metadata_version: str = ""


class WsDiscoverRequest(BaseModel):
    timeout: float = Field(default=4.0, gt=0, le=60)
    types: Optional[Any] = None
    scopes: Optional[Any] = None

    class Config:
        extra = "allow"


def _device_payload(device: Any) -> dict:
    if hasattr(device, "to_dict"):
        try:
            data = device.to_dict()
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            pass
    if isinstance(device, dict):
        return dict(device)
    return {
        "types": list(getattr(device, "types", []) or []),
        "xaddresses": list(getattr(device, "xaddresses", []) or []),
        "model": str(getattr(device, "model", "") or ""),
        "mfr": str(getattr(device, "mfr", "") or ""),
        "address": str(getattr(device, "address", "") or ""),
        "scopes": list(getattr(device, "scopes", []) or []),
        "endpoint": str(getattr(device, "endpoint", "") or ""),
        "metadata_version": str(getattr(device, "metadata_version", "") or ""),
    }


def _split_param(value: Any) -> Optional[list[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(",", " ").split() if p.strip()]
        return parts or None
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return parts or None
    return None


async def _run_discover(
    timeout: float = 4.0, types: Any = None, scopes: Any = None
) -> list[dict]:
    try:
        devices = await svc.discover(timeout=timeout, types=types, scopes=scopes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except svc.DiscoveryException as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return [_device_payload(d) for d in devices or []]


@router.get("/discover", response_model=List[WsDiscoveryDevice])
@underscore_router.get("/discover", response_model=List[WsDiscoveryDevice])
async def ws_discover_get(
    timeout: float = Query(default=4.0, gt=0, le=60),
    types: Optional[str] = Query(default=None),
    scopes: Optional[str] = Query(default=None),
):
    return await _run_discover(
        timeout=timeout, types=_split_param(types), scopes=_split_param(scopes)
    )


@router.post("/discover", response_model=List[WsDiscoveryDevice])
@underscore_router.post("/discover", response_model=List[WsDiscoveryDevice])
async def ws_discover_post(payload: Optional[WsDiscoverRequest] = None):
    timeout = 4.0 if payload is None else payload.timeout
    types = None if payload is None else payload.types
    scopes = None if payload is None else payload.scopes
    if isinstance(types, str):
        types = _split_param(types)
    if isinstance(scopes, str):
        scopes = _split_param(scopes)
    return await _run_discover(timeout=timeout, types=types, scopes=scopes)


@router.get("/probe-message")
@underscore_router.get("/probe-message")
async def ws_probe_message(
    message_id: Optional[str] = Query(default=None),
    types: Optional[str] = Query(default=None),
    scopes: Optional[str] = Query(default=None),
):
    mid = (message_id or "").strip() or svc.new_message_id()
    try:
        datagram = svc.build_probe_message(
            mid, types=_split_param(types), scopes=_split_param(scopes)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "message_id": mid,
        "multicast": f"{svc.WS_MULTICAST_ADDRESS}:{svc.WS_MULTICAST_PORT}",
        "xml": datagram.decode("ascii"),
        "bytes": len(datagram),
    }


@router.get("/info")
@underscore_router.get("/info")
async def ws_discovery_info():
    try:
        interfaces = svc.get_local_ipv4_addresses()
    except Exception:  # noqa: BLE001
        interfaces = []
    return {
        "multicast_address": svc.WS_MULTICAST_ADDRESS,
        "multicast_port": svc.WS_MULTICAST_PORT,
        "default_types": list(svc.DEFAULT_PROBE_TYPES),
        "resend_interval_s": svc.PROBE_RESEND_INTERVAL,
        "local_ipv4_addresses": interfaces,
    }


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
                _main_app._registered_router_ids = getattr(
                    _main_app, "_registered_router_ids", set()
                ) | {id(_r)}
                _ids = _main_app._registered_router_ids
            except Exception:  # noqa: BLE001
                pass
except Exception:  # noqa: BLE001
    pass
