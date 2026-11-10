"""Barcode and QR Code Scanner API endpoints.

Provides endpoints to scan images/snapshots for Barcodes/QR codes and query scan history.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile, status

from app.services.barcode_scanner import (
    clear_history,
    get_history,
    scan_image,
)

router = APIRouter(prefix="/api/barcodes", tags=["barcodes"])


@router.post("/scan")
async def api_scan_barcode(
    request: Request,
    camera_id: Optional[str] = Query(default=None, description="Camera ID"),
    file: Optional[UploadFile] = File(default=None),
) -> Dict[str, Any]:
    """Scan an image/snapshot for barcodes and QR codes.

    Accepts image via multipart form upload, raw image bytes in request body,
    or JSON containing 'image_base64' or 'camera_id'.
    """
    image_bytes: Optional[bytes] = None

    # 1. Check multipart file upload
    if file is not None:
        image_bytes = await file.read()

    # 2. Check JSON / Raw body
    if not image_bytes:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                body = await request.json()
                if isinstance(body, dict):
                    if "image_base64" in body and body["image_base64"]:
                        image_bytes = base64.b64decode(body["image_base64"])
                    elif "image" in body and body["image"]:
                        image_bytes = base64.b64decode(body["image"])
                    if "camera_id" in body and not camera_id:
                        camera_id = str(body["camera_id"])
            except Exception:
                pass
        else:
            raw_body = await request.body()
            if raw_body:
                image_bytes = raw_body

    # 3. If no image bytes, try to fetch camera snapshot
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

    # 4. Perform scan
    try:
        detections = scan_image(image_bytes, camera_id=camera_id, record_history=True)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Failed to scan image: {exc}",
        )

    return {
        "status": "ok",
        "count": len(detections),
        "detections": detections,
        "camera_id": camera_id,
    }


@router.get("/history")
async def api_get_barcode_history(
    limit: int = Query(default=50, ge=1, le=500),
    camera_id: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Return recently scanned barcodes/QR codes."""
    records = get_history(limit=limit, camera_id=camera_id)
    return {
        "status": "ok",
        "count": len(records),
        "history": records,
    }


@router.delete("/history")
async def api_clear_barcode_history() -> Dict[str, Any]:
    """Clear barcode scan history."""
    clear_history()
    return {"status": "ok", "cleared": True}
