"""Bulk camera operations router (auto-loaded by ``app.main``).

No edits to ``app/main.py`` are needed: this module exposes ``router``
and the pkgutil auto-loader in ``app.main`` picks it up.

Endpoints (POST, JSON bodies are tolerant of alias keys):

- ``/api/cameras/bulk-enable`` -- enable cameras by id list
- ``/api/cameras/bulk-disable`` -- disable cameras by id list
- ``/api/cameras/bulk-delete`` -- delete cameras by id list

Hyphen/underscore and ``/api/cameras/bulk/...`` aliases are registered
for every endpoint.

Verification note: ``app/api/group_bulk.py`` only toggles member cameras
of *groups* (``group_ids``); ``DELETE /api/cameras`` without ``ids`` wipes
everything. There was no bulk enable/disable/delete by arbitrary camera
id list -- these endpoints fill that gap.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services import bulk_cameras as svc

router = APIRouter(tags=["bulk_cameras"])


class BulkCamerasRequest(BaseModel):
    camera_ids: Optional[Any] = None
    camera_id: Optional[Any] = None
    cameras: Optional[Any] = None
    ids: Optional[Any] = None
    id: Optional[Any] = None
    enabled: Optional[bool] = None
    active: Optional[bool] = None
    fields: Optional[Any] = None

    model_config = {"extra": "allow"}


def _payload_dict(payload: Any, request: Request | None = None) -> Dict[str, Any]:
    if payload is None:
        data: Dict[str, Any] = {}
    elif isinstance(payload, (list, tuple, set)):
        data = {"camera_ids": list(payload)}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump(exclude_unset=False))
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}
    data = {k: v for k, v in data.items() if v is not None}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    for nested_key in ("payload", "params", "data", "body"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _extract_camera_ids(data: Dict[str, Any]) -> Any:
    for key in ("camera_ids", "cameraIds", "camera_id", "cameras", "ids", "id", "items"):
        if data.get(key) is not None:
            return data.get(key)
    return []


def _extract_enabled(data: Dict[str, Any], default: bool) -> bool:
    for key in ("enabled", "active", "is_enabled", "enable"):
        if data.get(key) is not None:
            val = data.get(key)
            if isinstance(val, str):
                return val.strip().lower() in ("1", "true", "yes", "y", "on")
            return bool(val)
    return default


def _extract_fields(data: Dict[str, Any]) -> Optional[List[str]]:
    for key in ("fields", "field", "flags", "toggles"):
        if data.get(key) is not None:
            val = data.get(key)
            if isinstance(val, str):
                return [p.strip() for p in val.split(",") if p.strip()]
            if isinstance(val, (list, tuple)):
                return [str(x).strip() for x in val if str(x).strip()]
            return [str(val)]
    out = []
    for flag, fields in (
        ("recording", ["recording_enabled"]),
        ("recording_enabled", ["recording_enabled"]),
        ("detection", ["detection_enabled", "detector_enabled"]),
        ("detection_enabled", ["detection_enabled", "detector_enabled"]),
        ("alerts", ["alert_enabled"]),
        ("alert_enabled", ["alert_enabled"]),
    ):
        if data.get(flag) is not None:
            out.extend(fields)
    if out:
        out = ["enabled"] + [f for f in out if f != "enabled"]
        seen: List[str] = []
        for f in out:
            if f not in seen:
                seen.append(f)
        return seen
    return None


async def _enable_impl(db: AsyncSession, data: Dict[str, Any], default: bool) -> Dict[str, Any]:
    camera_ids = _extract_camera_ids(data)
    cids = svc._coerce_int_list(camera_ids)  # noqa: SLF001
    if not cids:
        raise HTTPException(status_code=400, detail="camera_ids is required")
    enabled = _extract_enabled(data, default)
    fields = _extract_fields(data)
    return await svc.bulk_set_cameras(db, cids, enabled=enabled, fields=fields)


async def _delete_impl(db: AsyncSession, data: Dict[str, Any]) -> Dict[str, Any]:
    camera_ids = _extract_camera_ids(data)
    cids = svc._coerce_int_list(camera_ids)  # noqa: SLF001
    if not cids:
        raise HTTPException(status_code=400, detail="camera_ids is required")
    return await svc.bulk_delete_cameras(db, cids)


def _make_enable_endpoint(default: bool):
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        data = _payload_dict(payload, request)
        return await _enable_impl(db, data, default)

    return _endpoint


def _make_delete_endpoint():
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        data = _payload_dict(payload, request)
        return await _delete_impl(db, data)

    return _endpoint


_ENABLE_PATHS = (
    "/api/cameras/bulk-enable",
    "/api/cameras/bulk_enable",
    "/api/cameras/bulk/enable",
)

_DISABLE_PATHS = (
    "/api/cameras/bulk-disable",
    "/api/cameras/bulk_disable",
    "/api/cameras/bulk/disable",
)

_DELETE_PATHS = (
    "/api/cameras/bulk-delete",
    "/api/cameras/bulk_delete",
    "/api/cameras/bulk/delete",
)


def _slug(p: str) -> str:
    return p.strip("/").replace("/", "_").replace("-", "_")


for _p in _ENABLE_PATHS:
    router.add_api_route(
        _p, _make_enable_endpoint(default=True), methods=["POST"],
        operation_id=f"bulk_cameras_enable_{_slug(_p)}",
    )
for _p in _DISABLE_PATHS:
    router.add_api_route(
        _p, _make_enable_endpoint(default=False), methods=["POST"],
        operation_id=f"bulk_cameras_disable_{_slug(_p)}",
    )
for _p in _DELETE_PATHS:
    router.add_api_route(
        _p, _make_delete_endpoint(), methods=["POST"],
        operation_id=f"bulk_cameras_delete_{_slug(_p)}",
    )


# Compatibility aliases.
bulk_cameras_router = router
bulk_camera_router = router
cameras_bulk_router = router
api_router = router

__all__ = ["router", "BulkCamerasRequest"]
