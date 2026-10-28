from __future__ import annotations

from fastapi import APIRouter, Query

from app.drivers.registry import select_driver_class

router = APIRouter(prefix="/api/drivers", tags=["drivers"])


@router.get("/select")
async def select_driver(manufacturer: str = Query("ONVIF")):
    cls = select_driver_class(manufacturer)
    return {"manufacturer": manufacturer, "driver": cls.__name__, "module": cls.__module__}
