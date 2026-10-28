from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import onvif_digest_service as ods

router = APIRouter(prefix="/api/onvif", tags=["onvif-driver"])


class ProbeRequest(BaseModel):
    ip: str
    username: str = ""
    password: str = ""
    port: Optional[int] = None


@router.post("/probe_device")
async def probe_device(payload: ProbeRequest):
    try:
        scheme, port = await ods.resolve_control_endpoint(payload.ip, payload.port)
        info = await ods.get_device_info(payload.ip, payload.username, payload.password, port, scheme)
        caps = await ods.get_capabilities(payload.ip, payload.username, payload.password, port, scheme)
        return {"scheme": scheme, "port": port, "device": info, "capabilities": caps}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
