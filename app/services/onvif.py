from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class OnvifProbeRequest(BaseModel):
    host: str
    port: int = 80
    username: Optional[str] = None
    password: Optional[str] = None


class OnvifProbeResult(BaseModel):
    host: str
    reachable: bool
    suggested_rtsp: Optional[str] = None
    profiles: list[str] = Field(default_factory=list)
    message: str


def probe_onvif_endpoint(host: str, port: int = 80, username: Optional[str] = None, password: Optional[str] = None) -> OnvifProbeResult:
    """Lightweight ONVIF metadata builder.

    This scaffold does not perform real WS-Discovery (which needs a SOAP/ONVIF
    stack). It returns a conventional RTSP URL so the rest of VigilCam can be
    exercised. Replace with python-onvif-zeep for production.
    """
    user = username or "admin"
    suggested = f"rtsp://{user}:***@{host}:554/Streaming/Channels/101"
    return OnvifProbeResult(
        host=host,
        reachable=True,
        suggested_rtsp=suggested,
        profiles=["main", "sub"],
        message="ONVIF probe is a stub. Install python-onvif-zeep and implement full discovery for production use.",
    )
