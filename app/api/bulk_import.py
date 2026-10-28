"""ONVIF bulk import router: import discovered devices as cameras."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session

router = APIRouter(tags=["bulk-import"])


class BulkImportDevice(BaseModel):
    ip: Optional[str] = None
    ip_address: Optional[str] = None
    host: Optional[str] = None
    address: Optional[str] = None
    name: Optional[str] = None
    source: Optional[str] = None
    stream_url: Optional[str] = None
    onvif: Optional[bool] = None
    kind: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    onvif_host: Optional[str] = None
    onvif_port: Optional[int] = None
    http_ports: Optional[List[int]] = None
    rtsp_ports: Optional[List[int]] = None

    model_config = {"extra": "allow"}


class BulkImportRequest(BaseModel):
    devices: Optional[List[Any]] = Field(default=None)
    subnet: Optional[str] = None
    subnets: Optional[List[str]] = None
    username: Optional[str] = None
    password: Optional[str] = None
    skip_existing: Optional[bool] = True
    name_prefix: Optional[str] = "ONVIF"
    onvif_port: Optional[int] = None
    timeout: Optional[float] = 0.5

    model_config = {"extra": "allow"}


def _to_camera_dict(camera: Any) -> Dict[str, Any]:
    if isinstance(camera, dict):
        return dict(camera)
    try:
        from app.schemas import CameraRead

        return CameraRead.model_validate(camera, from_attributes=True).model_dump(mode="json")
    except Exception:
        pass
    data: Dict[str, Any] = {}
    for key in ("id", "name", "kind", "source", "onvif_host", "ip_address", "onvif_port"):
        try:
            data[key] = getattr(camera, key, None)
        except Exception:
            data[key] = None
    return data


async def _preview_impl(
    db: AsyncSession,
    subnet: Optional[str] = None,
    timeout: float = 0.5,
) -> List[Dict[str, Any]]:
    from app.services import bulk_import as svc

    return await svc.preview_bulk_import(db, subnet=subnet, timeout=timeout)


async def _import_impl(db: AsyncSession, payload: BulkImportRequest) -> Dict[str, Any]:
    from app.services import bulk_import as svc

    devices = payload.devices
    subnet = payload.subnet
    if (not devices) and (subnet or payload.subnets):
        targets = list(payload.subnets) if payload.subnets else ([subnet] if subnet else [])
        discovered: List[Dict[str, Any]] = []
        for sn in targets:
            try:
                part = await svc.discover_devices(subnet=sn, timeout=payload.timeout or 0.5)
            except Exception:
                part = []
            discovered.extend(part or [])
        devices = discovered
    devices = devices or []
    result = await svc.bulk_import_devices(
        db,
        devices,
        username=payload.username,
        password=payload.password,
        skip_existing=bool(payload.skip_existing) if payload.skip_existing is not None else True,
        name_prefix=payload.name_prefix or "ONVIF",
        onvif_port=payload.onvif_port,
    )
    return {
        "total_discovered": result.get("total_discovered", 0),
        "total_created": result.get("total_created", 0),
        "total_skipped": len(result.get("skipped", [])),
        "total_errors": len(result.get("errors", [])),
        "created": [_to_camera_dict(c) for c in result.get("created", [])],
        "skipped": result.get("skipped", []),
        "errors": result.get("errors", []),
    }


def _make_preview_endpoint():
    async def _endpoint(
        db: AsyncSession = Depends(db_session),
        subnet: Optional[str] = Query(default=None),
        timeout: float = Query(default=0.5),
    ):
        return await _preview_impl(db, subnet=subnet, timeout=timeout)

    return _endpoint


def _make_import_endpoint():
    async def _endpoint(payload: BulkImportRequest, db: AsyncSession = Depends(db_session)):
        return await _import_impl(db, payload)

    return _endpoint


for _prefix in ("/api/bulk-import", "/api/bulk_import"):
    for _path in ("preview", "discover", "devices"):
        router.add_api_route(
            f"{_prefix}/{_path}",
            _make_preview_endpoint(),
            methods=["GET"],
        )
    router.add_api_route(
        f"{_prefix}/import",
        _make_import_endpoint(),
        methods=["POST"],
    )


__all__ = ["router", "BulkImportDevice", "BulkImportRequest"]
