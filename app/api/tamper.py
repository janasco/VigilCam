"""Camera tamper detection API router.

Exposes :mod:`app.services.tamper` (reference frame store + histogram
drift compare) over HTTP. Auto-registered by ``app.main`` (it imports
every ``app.api`` module and includes ``router``), so no ``app/main.py``
edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query

import app.services.tamper as tamper_service_mod

router = APIRouter(prefix="/api/tamper", tags=["tamper"])


def _extract_frame(payload: Any) -> Any:
    if payload is None:
        return None
    if isinstance(payload, (bytes, bytearray, list, tuple, str)):
        return payload
    if isinstance(payload, dict):
        for key in ("frame", "pixels", "data", "image", "frame_data",
                    "histogram", "values", "samples", "bytes", "raw",
                    "content", "base64", "b64"):
            if payload.get(key) is not None:
                return payload.get(key)
        # A bare pixel list may be posted directly as {"0": ...}? No.
        # Otherwise treat numeric-looking dict values as the frame when the
        # dict itself looks like a payload (service handles this too).
        return payload
    if hasattr(payload, "model_dump"):
        try:
            return _extract_frame(payload.model_dump())
        except Exception:  # noqa: BLE001
            return None
    return payload


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            return int(value)
        return int(str(value).strip() if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return None


@router.get("/config")
async def get_tamper_config():
    return tamper_service_mod.get_config()


@router.post("/config")
async def update_tamper_config(payload: Any = Body(default=None)):
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    for nested_key in ("config", "settings", "params", "options", "data", "body"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return tamper_service_mod.configure(**data)


@router.post("/reference/{camera_id}")
async def set_tamper_reference(camera_id: int, payload: Any = Body(default=None)):
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        data = {"frame": payload}
    frame = _extract_frame(data if isinstance(data, dict) else payload)
    kwargs: dict = {}
    for key in ("bins", "num_bins", "histogram_bins", "threshold"):
        if isinstance(data, dict) and data.get(key) is not None:
            kwargs[key] = data.get(key)
    try:
        return tamper_service_mod.set_reference(camera_id, frame, **kwargs)
    except ValueError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/reference/{camera_id}")
async def get_tamper_reference(camera_id: int):
    ref = tamper_service_mod.get_reference(camera_id)
    if ref is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="No baseline reference stored")
    return ref


@router.delete("/reference/{camera_id}")
async def delete_tamper_reference(camera_id: int):
    return tamper_service_mod.clear_reference(camera_id)


@router.post("/compare/{camera_id}")
async def compare_tamper_frame(
    camera_id: int,
    payload: Any = Body(default=None),
    threshold: Optional[float] = Query(default=None),
):
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        data = {"frame": payload}
    frame = _extract_frame(data if isinstance(data, dict) else payload)
    kwargs: dict = {}
    if threshold is not None:
        kwargs["threshold"] = threshold
    if isinstance(data, dict):
        for key in ("threshold", "drift_threshold", "sensitivity", "limit",
                    "tolerance", "bins", "num_bins", "histogram_bins"):
            if data.get(key) is not None and key not in kwargs:
                kwargs[key] = data.get(key)
    try:
        return tamper_service_mod.compare_frame(camera_id, frame, **kwargs)
    except ValueError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/status/{camera_id}")
async def get_tamper_status(camera_id: int):
    return tamper_service_mod.get_status(camera_id)


@router.get("/status")
async def list_tamper_status():
    return {
        **tamper_service_mod.get_summary(),
        "references": tamper_service_mod.list_references(),
    }


@router.get("/references")
async def list_tamper_references():
    refs = tamper_service_mod.list_references()
    return {"count": len(refs), "references": refs, "ok": True}


# Compatibility aliases.
tamper_router = router
router_tamper = router

__all__ = ["router"]

# Side-effect registration into the main app so the router is live even when
# the module is imported after app startup (mirrors webhook_queue pattern).
# The main app also auto-discovers app.api.* routers at import time.
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    if id(router) not in _ids:
        _main_app.include_router(router)
        try:
            _main_app._registered_router_ids = _ids | {id(router)}
        except Exception:  # noqa: BLE001
            pass
except Exception:  # noqa: BLE001
    pass
