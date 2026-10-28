"""ONVIF PTZ endpoints built on onvif_digest_service."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.schemas import (
    OnvifPtzContinuousMoveRequest,
    OnvifPtzGetPresetsRequest,
    OnvifPtzGotoPresetRequest,
    OnvifPtzSetPresetRequest,
    OnvifPtzStopRequest,
    OnvifPtzResponse,
)
from app.services import onvif_digest_service as ods

router = APIRouter(prefix="/api/onvif", tags=["onvif-ptz"])


async def _resolve(payload: Any):
    ip = payload.resolved_ip()
    if not ip:
        raise HTTPException(status_code=422, detail="ip/host is required")
    scheme, port = await ods.resolve_control_endpoint(ip, payload.resolved_port(), payload.resolved_scheme())
    return ip, payload.username or "", payload.password or "", port, scheme


def _profile(payload: Any) -> str:
    token = payload.resolved_profile()
    if not token:
        raise HTTPException(status_code=422, detail="profile_token is required")
    return token


async def continuous_move(payload: OnvifPtzContinuousMoveRequest) -> OnvifPtzResponse:
    ip, username, password, port, scheme = await _resolve(payload)
    profile_token = _profile(payload)
    x, y, z = payload.resolved_velocity()
    try:
        result = await ods.ptz_continuous_move_digest(
            ip, username, password, profile_token, x, y, z, port, scheme
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return OnvifPtzResponse(status="moving", data=result, preset=None)


async def stop_move(payload: OnvifPtzStopRequest) -> OnvifPtzResponse:
    ip, username, password, port, scheme = await _resolve(payload)
    profile_token = _profile(payload)
    try:
        result = await ods.ptz_stop_digest(ip, username, password, profile_token, port, scheme)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return OnvifPtzResponse(status="stopped", data=result)


async def goto_preset(payload: OnvifPtzGotoPresetRequest) -> OnvifPtzResponse:
    ip, username, password, port, scheme = await _resolve(payload)
    profile_token = _profile(payload)
    preset_token = payload.resolved_preset()
    if not preset_token:
        raise HTTPException(status_code=422, detail="preset_token is required")
    try:
        result = await ods.ptz_goto_preset_digest(
            ip, username, password, profile_token, preset_token, port, scheme
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return OnvifPtzResponse(status="moving", preset=preset_token, data=result)


async def get_presets(payload: OnvifPtzGetPresetsRequest) -> OnvifPtzResponse:
    ip, username, password, port, scheme = await _resolve(payload)
    profile_token = _profile(payload)
    try:
        result = await ods.ptz_get_presets_digest(
            ip, username, password, profile_token, port, scheme
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return OnvifPtzResponse(status="ok", presets=result, data=result)


async def set_preset(payload: OnvifPtzSetPresetRequest) -> OnvifPtzResponse:
    ip, username, password, port, scheme = await _resolve(payload)
    profile_token = _profile(payload)
    name = payload.resolved_name()
    token = payload.resolved_token()
    try:
        result = await ods.ptz_set_preset_digest(
            ip, username, password, profile_token, name, token, port, scheme
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return OnvifPtzResponse(
        status="ok",
        token=(result.get("token") if isinstance(result, dict) else None),
        name=name,
        data=result,
    )


# Continuous move variants
for _path in ("/ptz/continuous_move", "/ptz/continuous-move", "/ptz/move", "/ptz/continuous"):
    router.post(_path, response_model=OnvifPtzResponse)(continuous_move)

# Stop variants
for _path in ("/ptz/stop", "/ptz/stop_move", "/ptz/stop-move"):
    router.post(_path, response_model=OnvifPtzResponse)(stop_move)

# Goto preset variants
for _path in ("/ptz/goto_preset", "/ptz/goto-preset", "/ptz/go_to_preset", "/ptz/goto"):
    router.post(_path, response_model=OnvifPtzResponse)(goto_preset)

# Get presets variants (POST + GET)
for _path in ("/ptz/presets", "/ptz/get_presets", "/ptz/get-presets", "/ptz/list_presets"):
    router.post(_path, response_model=OnvifPtzResponse)(get_presets)
    router.get(_path, response_model=OnvifPtzResponse)(get_presets)

# Set preset variants
for _path in ("/ptz/set_preset", "/ptz/set-preset", "/ptz/preset", "/ptz/save_preset"):
    router.post(_path, response_model=OnvifPtzResponse)(set_preset)
# Ensure parameter names with profile for schema generation
# (FastAPI already introspects view function signatures)
