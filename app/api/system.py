from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from app.core.config import settings
from app.core.state import is_initialized, mark_initialized
from app.api.deps import db_session
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["system"])


@router.get("/health")
async def health():
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION, "time": datetime.utcnow().isoformat()}


@router.get("/api/v1/status")
async def setup_status():
    return {"initialized": is_initialized()}


@router.post("/api/v1/setup/complete")
async def complete_setup():
    mark_initialized()
    return {"initialized": True}
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


@router.get("/api/system/metrics")
async def system_metrics(db: AsyncSession = Depends(db_session)):
    from app.services.system_metrics import collect_system_metrics

    return await collect_system_metrics(db)


@router.get("/metrics")
async def system_metrics_alias(db: AsyncSession = Depends(db_session)):
    from app.services.system_metrics import collect_system_metrics

    return await collect_system_metrics(db)
