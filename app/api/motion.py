from __future__ import annotations

import base64
from typing import Any, Optional

import cv2
import numpy as np
from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from app.services.motion_zoning import MotionZoningEngine

router = APIRouter(prefix="/api/motion", tags=["motion"])


class MotionAnalyzeRequest(BaseModel):
    image_base64: Optional[str] = None
    previous_image_base64: Optional[str] = None
    image: Optional[str] = None
    previous: Optional[str] = None
    prev_image: Optional[str] = None
    previous_image: Optional[str] = None
    synthetic: Optional[bool] = None
    width: Optional[int] = None
    height: Optional[int] = None
    w: Optional[int] = None
    h: Optional[int] = None
    zones: Optional[Any] = None
    polygon_zones: Optional[Any] = None
    polygons: Optional[Any] = None
    threshold_percentage: Optional[float] = None
    threshold: Optional[float] = None
    grid_rows: Optional[int] = None
    grid_cols: Optional[int] = None
    grid_size: Optional[Any] = None
    grid: Optional[Any] = None
    rows: Optional[int] = None
    cols: Optional[int] = None
    cell_size: Optional[Any] = None
    min_blob_area: Optional[int] = None
    blob_min_area: Optional[int] = None
    min_blob_size: Optional[int] = None
    min_area: Optional[int] = None
    occupancy_threshold: Optional[float] = None
    grid_threshold: Optional[float] = None
    camera_id: Optional[int] = None

    model_config = {"populate_by_name": True, "extra": "allow"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"", "none", "null"}:
        return default
    if s in {"1", "true", "yes", "on", "y"}:
        return True
    if s in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}

    # merge extras
    try:
        for k, v in getattr(payload, "__dict__", {}).items():
            data.setdefault(k, v)
    except Exception:  # noqa: BLE001
        pass

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # image aliases
    if data.get("image_base64") is None:
        for k in ("image", "snapshot", "snapshot_base64", "frame", "frame_base64",
                  "img", "img_base64", "picture", "picture_base64", "photo", "photo_base64",
                  "current", "current_image", "current_image_base64", "current_frame",
                  "data", "content", "base64", "b64", "encoded"):
            if data.get(k) is not None:
                data["image_base64"] = data.get(k)
                break
    if data.get("previous_image_base64") is None:
        for k in ("previous", "previous_image", "prev", "prev_image", "previous_frame",
                  "prev_frame", "previous_image_base64", "prev_image_base64",
                  "previous_base64", "prev_base64", "before", "before_image"):
            if data.get(k) is not None:
                data["previous_image_base64"] = data.get(k)
                break

    # zones aliases
    if data.get("zones") is None:
        for k in ("zone", "polygon_zones", "polygons", "polygon", "zone_list", "zones_list"):
            if data.get(k) is not None:
                data["zones"] = data.get(k)
                break

    # grid aliases
    if data.get("grid_rows") is None:
        for k in ("rows", "gridRows", "grid_row", "n_rows"):
            if data.get(k) is not None:
                data["grid_rows"] = data.get(k)
                break
    if data.get("grid_cols") is None:
        for k in ("cols", "gridCols", "grid_col", "n_cols", "columns"):
            if data.get(k) is not None:
                data["grid_cols"] = data.get(k)
                break
    if data.get("grid_size") is None:
        for k in ("grid", "size", "cell", "gridSize"):
            if data.get(k) is not None:
                data["grid_size"] = data.get(k)
                break

    # threshold aliases
    if data.get("threshold_percentage") is None:
        for k in ("threshold", "threshold_percent", "threshold_pct", "percent"):
            if data.get(k) is not None:
                data["threshold_percentage"] = data.get(k)
                break

    # min blob area aliases
    if data.get("min_blob_area") is None:
        for k in ("blob_min_area", "min_blob_size", "min_area", "min_blob_pixels"):
            if data.get(k) is not None:
                data["min_blob_area"] = data.get(k)
                break

    # occupancy threshold aliases
    if data.get("occupancy_threshold") is None:
        for k in ("grid_threshold", "occupancy_cell_threshold", "cell_threshold"):
            if data.get(k) is not None:
                data["occupancy_threshold"] = data.get(k)
                break

    # dimensions aliases
    if data.get("width") is None and data.get("w") is not None:
        data["width"] = data.get("w")
    if data.get("height") is None and data.get("h") is not None:
        data["height"] = data.get("h")

    return data


def _decode_image(b64: Any, width: Optional[int], height: Optional[int]) -> Optional[np.ndarray]:
    if b64 is None:
        return None
    if isinstance(b64, np.ndarray):
        img = b64
    elif isinstance(b64, (bytes, bytearray)):
        buf = np.frombuffer(bytes(b64), dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    else:
        s = str(b64).strip()
        if s.startswith("data:"):
            if "," in s:
                s = s.split(",", 1)[1]
        s = "".join(s.split())
        try:
            raw = base64.b64decode(s, validate=False)
        except Exception:  # noqa: BLE001
            return None
        buf = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        return None
    if width and height:
        if img.shape[1] != int(width) or img.shape[0] != int(height):
            try:
                img = cv2.resize(img, (int(width), int(height)))
            except Exception:  # noqa: BLE001
                pass
    return img


def _synthetic_frames(width: int, height: int):
    prev = np.zeros((height, width, 3), dtype=np.uint8)
    curr = np.zeros((height, width, 3), dtype=np.uint8)
    # a couple of moving blobs
    curr[height // 6:height // 2, width // 6:width // 2] = 255
    curr[height // 2:5 * height // 6, 2 * width // 3:5 * width // 6] = 200
    return prev, curr


@router.post("/analyze")
async def motion_analyze(
    payload: Any = Body(default=None),
    request: Request = None,
):
    data = _normalize(payload, request)

    # Parse dimensions.
    width = data.get("width")
    height = data.get("height")
    if width is None:
        width = data.get("w")
    if height is None:
        height = data.get("h")
    if width is None:
        width = 640
    if height is None:
        height = 480
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        width = 640
        height = 480
    if width < 1 or height < 1:
        width = 640
        height = 480

    image_b64 = data.get("image_base64")
    prev_b64 = data.get("previous_image_base64")

    synthetic = data.get("synthetic")
    if synthetic is None:
        synthetic = (image_b64 is None)

    curr = None
    prev = None
    if image_b64 is not None:
        curr = _decode_image(image_b64, width, height)
        if curr is None:
            raise HTTPException(status_code=400, detail="Could not decode image_base64")
        height, width = curr.shape[:2]
    if prev_b64 is not None:
        prev = _decode_image(prev_b64, width, height)

    if curr is None or _as_bool(synthetic, default=False):
        # synthetic mode
        if curr is None:
            synthetic = True
        if synthetic and curr is None:
            prev, curr = _synthetic_frames(width, height)
            height, width = curr.shape[:2]

    if curr is None:
        raise HTTPException(status_code=400, detail="No image available")

    if prev is None:
        prev = np.zeros_like(curr)

    zones = data.get("zones")
    if zones is None:
        zones = data.get("polygon_zones")
    if zones is None:
        zones = data.get("polygons")

    threshold = data.get("threshold_percentage")
    if threshold is None:
        threshold = data.get("threshold")

    grid_rows = data.get("grid_rows")
    grid_cols = data.get("grid_cols")
    grid_size = data.get("grid_size")
    grid = data.get("grid")
    rows = data.get("rows")
    cols = data.get("cols")
    cell_size = data.get("cell_size")
    min_blob_area = data.get("min_blob_area")
    occupancy_threshold = data.get("occupancy_threshold")

    try:
        engine = MotionZoningEngine(
            width,
            height,
            threshold_percentage=threshold if threshold is not None else 5.0,
            zones=zones,
            grid_rows=grid_rows,
            grid_cols=grid_cols,
            grid_size=grid_size,
            grid=grid,
            rows=rows,
            cols=cols,
            cell_size=cell_size,
            min_blob_area=min_blob_area,
            occupancy_threshold=occupancy_threshold,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Engine init failed: {exc}")

    try:
        engine.process_and_evaluate(prev)
        result = engine.process_and_evaluate(curr)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Analysis failed: {exc}")

    out = result.to_dict()
    out.setdefault("ok", True)
    out.setdefault("width", width)
    out.setdefault("height", height)
    return out


@router.get("/analyze")
async def motion_analyze_get():
    return {"status": "ok", "endpoint": "POST /api/motion/analyze"}


# Additional path aliases for compatibility.
router_motion_zoning = APIRouter(prefix="/api/motion_zoning", tags=["motion"])
router_motion_zoning_analyze = router_motion_zoning

@router_motion_zoning.post("/analyze")
async def motion_zoning_analyze(payload: Any = Body(default=None), request: Request = None):
    return await motion_analyze(payload=payload, request=request)

router_motion_dash = APIRouter(prefix="/api/motion-zoning", tags=["motion"])

@router_motion_dash.post("/analyze")
async def motion_zoning_dash_analyze(payload: Any = Body(default=None), request: Request = None):
    return await motion_analyze(payload=payload, request=request)
