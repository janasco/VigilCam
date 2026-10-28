"""Device-based camera name suggestion API.

Auto-registered by ``app.main`` (any module in ``app.api`` exposing
``router`` is included), so no ``app/main.py`` edit is needed.
Both ``/api/name-suggest`` (canonical) and ``/api/name_suggest``
(underscore alias) are served.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, ConfigDict

import app.services.name_suggest as svc

router = APIRouter(prefix="/api/name-suggest", tags=["name-suggest"])
underscore_router = APIRouter(prefix="/api/name_suggest", tags=["name-suggest"])


class NameSuggestRequest(BaseModel):
    device_info: Optional[Dict[str, Any]] = None
    device: Optional[Dict[str, Any]] = None
    info: Optional[Dict[str, Any]] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    firmware_version: Optional[str] = None
    firmwareversion: Optional[str] = None
    serial_number: Optional[str] = None
    serialnumber: Optional[str] = None
    hardware_id: Optional[str] = None
    hardwareid: Optional[str] = None
    device_name: Optional[str] = None
    name: Optional[str] = None
    ip: Optional[str] = None
    ip_address: Optional[str] = None
    host: Optional[str] = None
    onvif_host: Optional[str] = None
    address: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class NameFetchRequest(BaseModel):
    ip: Optional[str] = None
    ip_address: Optional[str] = None
    host: Optional[str] = None
    onvif_host: Optional[str] = None
    address: Optional[str] = None
    username: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    port: Optional[Any] = None
    onvif_port: Optional[Any] = None
    scheme: Optional[str] = None
    control_scheme: Optional[str] = None

    model_config = ConfigDict(extra="allow")


def _payload_dict(payload: Any) -> Dict[str, Any]:
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


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coalesce_ip(data: Dict[str, Any]) -> Optional[str]:
    for key in ("ip", "ip_address", "host", "onvif_host", "address"):
        val = _clean(data.get(key))
        if val:
            return val
    return None


def _device_info_from_data(data: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("device_info", "device", "info"):
        val = data.get(key)
        if isinstance(val, dict) and val:
            return dict(val)
    # Flat fields form the device info.
    info: Dict[str, Any] = {}
    for key in (
        "manufacturer", "Manufacturer", "model", "Model",
        "firmware_version", "firmwareversion", "FirmwareVersion",
        "serial_number", "serialnumber", "SerialNumber",
        "hardware_id", "hardwareid", "HardwareId",
        "device_name", "name",
    ):
        if data.get(key) is not None:
            info[key] = data.get(key)
    # Also accept any extra keys callers pass through.
    return info


def _suggest_response(device_info: Dict[str, Any], ip: Optional[str]) -> Dict[str, Any]:
    suggestions: List[str] = svc.build_name_suggestions(device_info, ip=ip)
    primary = svc.suggest_camera_name(device_info, ip=ip)
    normalized = svc.normalize_device_info(device_info, ip=ip)
    return {
        "suggestions": suggestions,
        "names": suggestions,
        "primary": primary,
        "suggested_name": primary,
        "name": primary,
        "device_info": normalized,
        "ip": ip,
        "ip_address": ip,
        "ok": True,
        "success": True,
    }


def _do_suggest(data: Dict[str, Any]) -> Dict[str, Any]:
    # Drop None values so explicit nulls don't shadow device_info entries.
    data = {k: v for k, v in data.items() if v is not None}
    ip = _coalesce_ip(data)
    device_info = _device_info_from_data(data)
    # Merge flat manufacturer/model/ip fields into the info for the service.
    merged = dict(device_info)
    for key in (
        "manufacturer", "model", "firmware_version", "firmwareversion",
        "serial_number", "serialnumber", "hardware_id", "hardwareid",
        "device_name", "name",
    ):
        if data.get(key) is not None and key not in merged:
            merged[key] = data.get(key)
    return _suggest_response(merged, ip)


async def _do_fetch(data: Dict[str, Any]) -> Dict[str, Any]:
    data = {k: v for k, v in data.items() if v is not None}
    ip = _coalesce_ip(data)
    if not ip:
        raise HTTPException(status_code=422, detail="Provide 'ip' (camera host) to fetch device info")
    username = data.get("username", data.get("user", ""))
    password = data.get("password", data.get("pass", data.get("pwd", "")))
    port = data.get("port", data.get("onvif_port", data.get("control_port")))
    scheme = data.get("scheme", data.get("control_scheme"))
    try:
        result = await svc.suggest_names_for_device(
            ip, username or "", password or "", port, scheme,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Device info fetch failed: {exc}")
    return {**result, "ok": True, "success": True}


async def _suggest_get_impl(
    manufacturer: Optional[str] = None,
    model: Optional[str] = None,
    firmware_version: Optional[str] = None,
    serial_number: Optional[str] = None,
    hardware_id: Optional[str] = None,
    device_name: Optional[str] = None,
    ip: Optional[str] = None,
    ip_address: Optional[str] = None,
    host: Optional[str] = None,
) -> Dict[str, Any]:
    data = {
        "manufacturer": manufacturer,
        "model": model,
        "firmware_version": firmware_version,
        "serial_number": serial_number,
        "hardware_id": hardware_id,
        "device_name": device_name,
        "ip": ip or ip_address or host,
    }
    return _do_suggest(data)


async def _fetch_get_impl(
    ip: Optional[str] = None,
    host: Optional[str] = None,
    ip_address: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    port: Optional[Any] = None,
    scheme: Optional[str] = None,
) -> Dict[str, Any]:
    return await _do_fetch({
        "ip": ip or host or ip_address,
        "username": username,
        "password": password,
        "port": port,
        "scheme": scheme,
    })


# ---- Suggest (offline, no network) ----

@router.get("/suggest")
@underscore_router.get("/suggest")
async def suggest_get(
    manufacturer: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    firmware_version: Optional[str] = Query(default=None),
    serial_number: Optional[str] = Query(default=None),
    hardware_id: Optional[str] = Query(default=None),
    device_name: Optional[str] = Query(default=None),
    ip: Optional[str] = Query(default=None),
    ip_address: Optional[str] = Query(default=None),
    host: Optional[str] = Query(default=None),
):
    return await _suggest_get_impl(
        manufacturer, model, firmware_version, serial_number,
        hardware_id, device_name, ip, ip_address, host,
    )


@router.post("/suggest")
@underscore_router.post("/suggest")
async def suggest_post(payload: Any = Body(default=None)):
    return _do_suggest(_payload_dict(payload))


@router.get("/suggestions")
@underscore_router.get("/suggestions")
async def suggestions_get(
    manufacturer: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    firmware_version: Optional[str] = Query(default=None),
    serial_number: Optional[str] = Query(default=None),
    hardware_id: Optional[str] = Query(default=None),
    device_name: Optional[str] = Query(default=None),
    ip: Optional[str] = Query(default=None),
    ip_address: Optional[str] = Query(default=None),
    host: Optional[str] = Query(default=None),
):
    return await _suggest_get_impl(
        manufacturer, model, firmware_version, serial_number,
        hardware_id, device_name, ip, ip_address, host,
    )


@router.post("/suggestions")
@underscore_router.post("/suggestions")
async def suggestions_post(payload: Any = Body(default=None)):
    return _do_suggest(_payload_dict(payload))


@router.post("/preview")
@underscore_router.post("/preview")
async def preview_post(payload: Any = Body(default=None)):
    return _do_suggest(_payload_dict(payload))


@router.post("/names")
@underscore_router.post("/names")
async def names_post(payload: Any = Body(default=None)):
    return _do_suggest(_payload_dict(payload))


@router.get("/preview")
@underscore_router.get("/preview")
async def preview_get(
    manufacturer: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    ip: Optional[str] = Query(default=None),
    host: Optional[str] = Query(default=None),
):
    return await _suggest_get_impl(manufacturer, model, None, None, None, None, ip, None, host)


# ---- Fetch (live ONVIF GetDeviceInformation, then suggest) ----

@router.get("/fetch")
@underscore_router.get("/fetch")
async def fetch_get(
    ip: Optional[str] = Query(default=None),
    host: Optional[str] = Query(default=None),
    ip_address: Optional[str] = Query(default=None),
    username: Optional[str] = Query(default=None),
    password: Optional[str] = Query(default=None),
    port: Optional[int] = Query(default=None),
    scheme: Optional[str] = Query(default=None),
):
    return await _fetch_get_impl(ip, host, ip_address, username, password, port, scheme)


@router.post("/fetch")
@underscore_router.post("/fetch")
async def fetch_post(payload: Any = Body(default=None)):
    return await _do_fetch(_payload_dict(payload))


@router.post("/device")
@underscore_router.post("/device")
async def device_post(payload: Any = Body(default=None)):
    data = _payload_dict(payload)
    # If credentials + ip look like a live fetch, do it; otherwise offline.
    if _coalesce_ip(data) and ("username" in data or "password" in data or "user" in data):
        try:
            return await _do_fetch(data)
        except HTTPException as exc:
            # Fall back to offline suggestions only when the device did not
            # supply explicit device_info to suggest from.
            if not _device_info_from_data(data):
                raise exc
    return _do_suggest(data)


# Side-effect registration so the routers are live even when this module is
# imported after app startup (mirrors stream-probe pattern). The main app also
# auto-discovers app.api.* routers at import time. Guarded to avoid importing
# app.main (which loads every router) during standalone import/test.
try:
    import sys as _sys

    _main_app = _sys.modules.get("app.main")
    _main_app = getattr(_main_app, "app", None) if _main_app is not None else None
    if _main_app is not None:
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
