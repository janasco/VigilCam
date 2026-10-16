from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

from app.core.config import settings

router = APIRouter(tags=["system"])


@router.get("/health")
async def health():
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION, "time": datetime.utcnow().isoformat()}


@router.get("/api/info")
async def info():
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "features": [
            "usb_cameras",
            "ip_cameras",
            "onvif_discovery_stub",
            "ai_detection_stub",
            "recording_metadata",
            "event_log",
        ],
    }
