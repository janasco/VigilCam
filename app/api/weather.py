"""Weather Overlay API endpoints.

Provides current weather info, camera-specific weather, and weather overlay rendering onto snapshots.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services.cameras import get_camera
from app.services.weather_overlay import (
    get_weather,
    get_weather_by_ip,
    render_weather_overlay,
)

router = APIRouter(prefix="/api/weather", tags=["weather"])


@router.get("/current")
async def api_get_current_weather(
    lat: Optional[float] = Query(default=None, description="Latitude"),
    lon: Optional[float] = Query(default=None, description="Longitude"),
    ip: Optional[str] = Query(default=None, description="IP address for location lookup"),
) -> Dict[str, Any]:
    """Return current live weather data for given coordinates or IP."""
    if lat is not None and lon is not None:
        data = get_weather(lat, lon)
    else:
        data = get_weather_by_ip(ip)

    return {"status": "ok", **data}


@router.get("/camera/{camera_id}")
async def api_get_camera_weather(
    camera_id: str,
    db: AsyncSession = Depends(db_session),
) -> Dict[str, Any]:
    """Return live weather for camera's configured location or IP."""
    cam = None
    if camera_id.isdigit():
        cam = await get_camera(db, int(camera_id))

    # Also check solar_schedule config fallback
    solar_cfg = None
    try:
        from app.services.solar_schedule import _CONFIGS
        solar_cfg = _CONFIGS.get(str(camera_id))
    except Exception:
        pass

    if cam is None and solar_cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera {camera_id} not found",
        )

    lat = None
    lon = None

    if solar_cfg:
        lat = solar_cfg.get("lat", solar_cfg.get("latitude"))
        lon = solar_cfg.get("lon", solar_cfg.get("longitude"))

    if lat is None and cam is not None:
        lat = getattr(cam, "latitude", None)
        lon = getattr(cam, "longitude", None)

    if lat is not None and lon is not None:
        weather_data = get_weather(float(lat), float(lon))
    elif cam is not None and getattr(cam, "ip_address", None):
        weather_data = get_weather_by_ip(cam.ip_address)
    elif cam is not None and getattr(cam, "onvif_host", None):
        weather_data = get_weather_by_ip(cam.onvif_host)
    else:
        weather_data = get_weather_by_ip(None)

    return {
        "status": "ok",
        "camera_id": camera_id,
        "camera_name": getattr(cam, "name", f"Camera {camera_id}") if cam else f"Camera {camera_id}",
        **weather_data,
    }


@router.post("/overlay")
async def api_post_weather_overlay(
    request: Request,
    position: str = Query(default="top_right", description="Overlay position"),
    camera_id: Optional[str] = Query(default=None, description="Camera ID"),
    lat: Optional[float] = Query(default=None),
    lon: Optional[float] = Query(default=None),
    file: Optional[UploadFile] = File(default=None),
) -> Response:
    """Render weather information onto an image/snapshot.

    Accepts image via multipart form upload, raw bytes body, or JSON with base64 encoded image.
    """
    image_bytes: Optional[bytes] = None
    custom_weather: Optional[Dict[str, Any]] = None

    # 1. Check if multipart file was provided
    if file is not None:
        image_bytes = await file.read()

    # 2. Check body if not multipart file
    if not image_bytes:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                body = await request.json()
                if isinstance(body, dict):
                    if "image_base64" in body:
                        image_bytes = base64.b64decode(body["image_base64"])
                    elif "image" in body:
                        image_bytes = base64.b64decode(body["image"])
                    if "weather" in body and isinstance(body["weather"], dict):
                        custom_weather = body["weather"]
                    if "position" in body:
                        position = body["position"]
                    if "camera_id" in body and not camera_id:
                        camera_id = str(body["camera_id"])
                    if "lat" in body and lat is None:
                        lat = float(body["lat"])
                    if "lon" in body and lon is None:
                        lon = float(body["lon"])
            except Exception:
                pass
        else:
            raw_body = await request.body()
            if raw_body:
                # Raw image bytes directly sent
                image_bytes = raw_body

    # 3. If still no image bytes, try to capture/fetch snapshot for camera_id
    if not image_bytes and camera_id:
        try:
            from app.services import thumbnails as _th
            image_bytes = _th.get_thumbnail(camera_id)
        except Exception:
            pass

    if not image_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Image data or valid camera_id is required",
        )

    # 4. Resolve weather data if not custom provided
    if not custom_weather:
        if lat is not None and lon is not None:
            custom_weather = get_weather(lat, lon)
        else:
            custom_weather = get_weather_by_ip(None)

    # 5. Render overlay
    try:
        rendered_bytes = render_weather_overlay(image_bytes, custom_weather, position=position)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Failed to render weather overlay: {exc}",
        )

    return Response(
        content=rendered_bytes,
        media_type="image/jpeg",
        headers={"Content-Disposition": "inline; filename=weather_overlay.jpg"},
    )
