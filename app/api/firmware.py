from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query

from app.services import firmware as firmware_service_mod
from app.services.firmware import check_firmware, get_latest_version

router = APIRouter(tags=["firmware"])


def _normalize(payload: Any, query: Optional[dict] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}
    if query:
        for k, v in query.items():
            if v is not None and (k not in data or data.get(k) is None):
                data[k] = v
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "firmware", "camera"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _do_check(data: dict) -> dict:
    return check_firmware(
        current_version=data.get("current_version"),
        latest_version=data.get("latest_version"),
        model=data.get("model"),
        camera_id=data.get("camera_id"),
        **{k: v for k, v in data.items() if k not in ("current_version", "latest_version", "model", "camera_id")},
    )


@router.get("/api/firmware/latest")
async def firmware_latest(model: Optional[str] = Query(default=None)):
    latest = get_latest_version(model)
    return {
        "model": (model or firmware_service_mod.DEFAULT_MODEL),
        "latest_version": latest,
        "latest": latest,
        "version": latest,
        "ok": True,
        "success": True,
    }


@router.get("/api/firmware/models")
async def firmware_models():
    models = firmware_service_mod.list_models()
    return {"models": models, "ok": True, "success": True}


@router.get("/api/firmware/check")
async def firmware_check_get(
    current_version: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    latest_version: Optional[str] = Query(default=None),
    camera_id: Optional[int] = Query(default=None),
):
    return _do_check(
        {
            "current_version": current_version,
            "model": model,
            "latest_version": latest_version,
            "camera_id": camera_id,
        }
    )


@router.get("/api/firmware/status")
async def firmware_status_get(
    current_version: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    latest_version: Optional[str] = Query(default=None),
    camera_id: Optional[int] = Query(default=None),
):
    return await firmware_check_get(current_version, model, latest_version, camera_id)


@router.post("/api/firmware/check")
async def firmware_check_post(
    payload: Any = Body(default=None),
    current_version: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    latest_version: Optional[str] = Query(default=None),
    camera_id: Optional[int] = Query(default=None),
):
    data = _normalize(payload, {"current_version": current_version, "model": model, "latest_version": latest_version, "camera_id": camera_id})
    return _do_check(data)


@router.post("/api/firmware/status")
async def firmware_status_post(
    payload: Any = Body(default=None),
    current_version: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    latest_version: Optional[str] = Query(default=None),
    camera_id: Optional[int] = Query(default=None),
):
    return await firmware_check_post(payload, current_version, model, latest_version, camera_id)


@router.get("/api/cameras/{camera_id}/firmware")
async def camera_firmware(
    camera_id: int,
    current_version: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    latest_version: Optional[str] = Query(default=None),
):
    return _do_check(
        {
            "current_version": current_version,
            "model": model,
            "latest_version": latest_version,
            "camera_id": camera_id,
        }
    )
