from __future__ import annotations

from fastapi import APIRouter

from app.schemas import CameraCreate  # noqa: F401 (kept for parity)
from app.services.onvif import OnvifProbeRequest, OnvifProbeResult, probe_onvif_endpoint

router = APIRouter(prefix="/api/onvif", tags=["onvif"])


@router.post("/probe", response_model=OnvifProbeResult)
async def probe(payload: OnvifProbeRequest):
    return probe_onvif_endpoint(payload.host, payload.port, payload.username, payload.password)
