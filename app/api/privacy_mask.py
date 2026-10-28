"""Privacy mask regions REST endpoints (output blackout).

Masks stored here are applied to *output* images (masked snapshot JPEG and
masked MJPEG stream). They are independent of motion zones, which only gate
detection and never modify output pixels.

No ``app/main.py`` changes are required: the application auto-loads any
``app.api`` module exposing ``router``.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

router = APIRouter(tags=["privacy_mask"])

_BASES = (
    "/api/privacy_mask",
    "/api/privacy_masks",
    "/api/privacy-mask",
    "/api/privacy-masks",
)


def _camera_key(value: Any) -> str:
    if value is None:
        return "default"
    return str(value).strip() or "default"


def _extract_regions(payload: Any) -> Optional[Any]:
    if payload is None:
        return None
    if isinstance(payload, (list, tuple)):
        return list(payload)
    if isinstance(payload, dict):
        for k in ("regions", "masks", "mask", "polygons", "polygon", "zone_list", "zones", "points_list"):
            if payload.get(k) is not None:
                return payload.get(k)
        for nested in ("payload", "options", "config", "params", "data", "body"):
            sub = payload.get(nested)
            if isinstance(sub, dict):
                found = _extract_regions(sub)
                if found is not None:
                    return found
            elif isinstance(sub, (list, tuple)):
                return list(sub)
        # Single region dict?
        if any(k in payload for k in ("points", "polygon", "coords", "coordinates", "x", "y", "bbox", "rect")):
            return [payload]
        return None
    return None


async def _read_json_body(request: Request) -> Any:
    ctype = (request.headers.get("content-type") or "").lower()
    if "multipart" in ctype:
        try:
            form = await request.form()
        except Exception:
            return {}
        out: Dict[str, Any] = {}
        for k, v in form.items():
            if hasattr(v, "read"):
                continue
            out[k] = str(v)
        return out
    try:
        raw = await request.body()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        import json as _json

        return _json.loads(raw.decode("utf-8", errors="strict"))
    except Exception:
        return {}


def _decode_image(value: Any) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    s = str(value).strip()
    if not s:
        return None
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    s = "".join(s.split())
    try:
        return base64.b64decode(s, validate=False)
    except Exception:
        return None


def _find_image(payload: Dict[str, Any]) -> Optional[bytes]:
    for k in ("image_base64", "image", "frame", "frame_base64", "snapshot", "picture", "photo", "data", "base64", "b64", "jpeg", "content"):
        if payload.get(k) is not None:
            raw = _decode_image(payload.get(k))
            if raw:
                return raw
    return None


def _placeholder_frame(camera_id: Any, width: int = 320, height: int = 240):
    import cv2
    import numpy as np

    w = max(16, min(1280, int(width)))
    h = max(16, min(1280, int(height)))
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (32, 32, 48)
    # Light center panel so blacked-out masks are visually obvious.
    cv2.rectangle(img, (w // 4, h // 4), (3 * w // 4, 3 * h // 4), (200, 200, 200), -1)
    cv2.putText(img, f"Cam {camera_id}", (10, max(20, h // 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return img


def _masked_placeholder_jpeg(camera_id: Any, width: int = 320, height: int = 240) -> bytes:
    from app.services import privacy_mask as svc

    frame = _placeholder_frame(camera_id, width, height)
    masked = svc.apply_privacy_masks(frame, camera_id=_camera_key(camera_id))
    return svc.encode_jpeg_bytes(masked)


def _register(r: APIRouter, base: str) -> None:
    async def _status() -> Dict[str, Any]:
        return {"status": "ok", "service": "privacy_mask", "ok": True}

    async def _usage() -> Dict[str, Any]:
        return {
            "status": "ok",
            "service": "privacy_mask",
            "ok": True,
            "usage": f"GET {base}/{{camera_id}} lists masks; POST {base}/{{camera_id}} sets {{regions: [...]}}; POST {base}/{{camera_id}}/preview masks an image; GET {base}/{{camera_id}}/snapshot returns masked JPEG; GET {base}/{{camera_id}}/mjpeg streams masked MJPEG.",
        }

    async def _get_masks(camera_id: Any) -> Dict[str, Any]:
        from app.services import privacy_mask as svc

        if str(camera_id).strip().lower() in ("status", "usage", "health"):
            return {"status": "ok", "service": "privacy_mask", "ok": True}
        regions = svc.get_masks(_camera_key(camera_id))
        return {
            "ok": True,
            "success": True,
            "camera_id": _camera_key(camera_id),
            "camera": _camera_key(camera_id),
            "regions": regions,
            "masks": regions,
            "count": len(regions),
            "total": len(regions),
        }

    async def _set_masks(camera_id: Any, request: Request) -> Dict[str, Any]:
        from app.services import privacy_mask as svc

        body = await _read_json_body(request)
        regions = _extract_regions(body if isinstance(body, dict) else None)
        if regions is None:
            # Query-string fallback: ?regions=<json> (best effort).
            regions = None
            try:
                q = request.query_params.get("regions") or request.query_params.get("masks")
                if q is not None:
                    import json as _json

                    regions = _json.loads(q)
            except Exception:
                regions = None
        if regions is None:
            # Direct list body?
            if isinstance(body, (list, tuple)):
                regions = list(body)
            else:
                raise HTTPException(status_code=400, detail="No mask regions provided (expected {regions: [...]})")
        try:
            stored = svc.set_masks(_camera_key(camera_id), regions)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc) or "Invalid mask regions")
        return {
            "ok": True,
            "success": True,
            "camera_id": _camera_key(camera_id),
            "regions": stored,
            "masks": stored,
            "count": len(stored),
        }

    async def _delete_masks(camera_id: Any) -> Dict[str, Any]:
        from app.services import privacy_mask as svc

        removed = svc.clear_masks(_camera_key(camera_id))
        return {"ok": True, "success": True, "camera_id": _camera_key(camera_id), "removed": removed, "count": 0, "regions": []}

    async def _preview(camera_id: Any, request: Request) -> Dict[str, Any]:
        from app.services import privacy_mask as svc

        body = await _read_json_body(request)
        payload = body if isinstance(body, dict) else {}
        # Merge query params as fallback.
        try:
            for k, v in request.query_params.items():
                payload.setdefault(k, v)
        except Exception:
            pass
        raw = _find_image(payload)
        if raw is None:
            raise HTTPException(status_code=400, detail="No image provided (expected image_base64)")
        regions = _extract_regions(payload)
        frame = svc.decode_jpeg_bytes(raw)
        if frame is None:
            raise HTTPException(status_code=400, detail="Could not decode image")
        h, w = frame.shape[:2]
        if regions is not None:
            try:
                masked = svc.apply_privacy_masks(frame, regions=regions)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc) or "Invalid mask regions")
        else:
            masked = svc.apply_privacy_masks(frame, camera_id=_camera_key(camera_id))
        out = svc.encode_jpeg_bytes(masked)
        return {
            "ok": True,
            "success": True,
            "camera_id": _camera_key(camera_id),
            "image_base64": base64.b64encode(out).decode("ascii"),
            "image": base64.b64encode(out).decode("ascii"),
            "width": w,
            "height": h,
            "masked": True,
        }

    async def _snapshot(camera_id: Any, width: int = Query(default=320), height: int = Query(default=240)) -> Response:
        jpeg = _masked_placeholder_jpeg(camera_id, width, height)
        return Response(content=jpeg, media_type="image/jpeg")

    async def _mjpeg(
        camera_id: Any,
        fps: float = Query(default=1.0),
        frames: Optional[int] = Query(default=None, alias="frames"),
        limit: Optional[int] = Query(default=None),
        max_frames: Optional[int] = Query(default=None),
        count: Optional[int] = Query(default=None),
    ) -> StreamingResponse:
        if frames is None:
            frames = limit if limit is not None else (max_frames if max_frames is not None else count)

        async def _gen():
            import asyncio as _asyncio

            delay = 1.0 / max(0.1, float(fps or 1.0))
            n = 0
            while True:
                if frames is not None and n >= frames:
                    break
                jpeg = _masked_placeholder_jpeg(camera_id)
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(jpeg)).encode()
                    + b"\r\n\r\n"
                    + jpeg
                    + b"\r\n"
                )
                n += 1
                await _asyncio.sleep(delay)

        return StreamingResponse(_gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    # Specific routes first so "{camera_id}" never shadows them.
    r.add_api_route(base + "/status", _status, methods=["GET"])
    r.add_api_route(base + "/{camera_id}/preview", _preview, methods=["POST", "PUT"])
    r.add_api_route(base + "/preview", _preview, methods=["POST", "PUT"])
    r.add_api_route(base + "/{camera_id}/snapshot", _snapshot, methods=["GET"])
    r.add_api_route(base + "/{camera_id}/mjpeg", _mjpeg, methods=["GET"])
    r.add_api_route(base + "/{camera_id}/snapshot.mjpeg", _mjpeg, methods=["GET"])
    r.add_api_route(base + "/{camera_id}", _get_masks, methods=["GET"])
    r.add_api_route(base + "/{camera_id}", _set_masks, methods=["POST", "PUT"])
    r.add_api_route(base + "/{camera_id}/", _set_masks, methods=["POST", "PUT"])
    r.add_api_route(base + "/{camera_id}", _delete_masks, methods=["DELETE"])
    r.add_api_route(base, _usage, methods=["GET"])
    r.add_api_route(base + "/", _usage, methods=["GET"])


for _base in _BASES:
    _register(router, _base)


# Prefixed alias routers (for callers expecting router.prefix to be set).
underscore_router = APIRouter(prefix="/api/privacy_mask", tags=["privacy_mask"])
hyphen_router = APIRouter(prefix="/api/privacy-masks", tags=["privacy_mask"])


def _make_prefixed(r: APIRouter) -> None:
    async def _status() -> Dict[str, Any]:
        return {"status": "ok", "service": "privacy_mask", "ok": True}

    async def _get_masks(camera_id: Any) -> Dict[str, Any]:
        from app.services import privacy_mask as svc

        regions = svc.get_masks(_camera_key(camera_id))
        return {"ok": True, "success": True, "camera_id": _camera_key(camera_id), "regions": regions, "masks": regions, "count": len(regions)}

    async def _set_masks(camera_id: Any, request: Request) -> Dict[str, Any]:
        from app.services import privacy_mask as svc

        body = await _read_json_body(request)
        regions = _extract_regions(body if isinstance(body, dict) else None)
        if regions is None:
            if isinstance(body, (list, tuple)):
                regions = list(body)
            else:
                raise HTTPException(status_code=400, detail="No mask regions provided")
        stored = svc.set_masks(_camera_key(camera_id), regions)
        return {"ok": True, "success": True, "camera_id": _camera_key(camera_id), "regions": stored, "masks": stored, "count": len(stored)}

    async def _delete_masks(camera_id: Any) -> Dict[str, Any]:
        from app.services import privacy_mask as svc

        removed = svc.clear_masks(_camera_key(camera_id))
        return {"ok": True, "success": True, "camera_id": _camera_key(camera_id), "removed": removed, "regions": []}

    r.add_api_route("/status", _status, methods=["GET"])
    r.add_api_route("/{camera_id}", _get_masks, methods=["GET"])
    r.add_api_route("/{camera_id}", _set_masks, methods=["POST", "PUT"])
    r.add_api_route("/{camera_id}", _delete_masks, methods=["DELETE"])
    r.add_api_route("", _status, methods=["GET"])


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)


# Compatibility aliases.
privacy_mask_router = router
privacyMaskRouter = router
privacy_masks_router = router

__all__ = [
    "router",
    "underscore_router",
    "hyphen_router",
    "privacy_mask_router",
    "privacyMaskRouter",
    "privacy_masks_router",
]
