from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Query

from app.services import devices as devices_service_module


router = APIRouter(prefix="/api/devices", tags=["devices"])


def _list_usb(*args: Any, **kwargs: Any) -> List[Any]:
    svc = devices_service_module.devices_service
    for name in ("list_usb_devices", "list_devices", "enumerate_usb_devices", "get_usb_devices", "usb_devices"):
        fn = getattr(svc, name, None)
        if callable(fn):
            try:
                return list(fn(*args, **kwargs))
            except Exception:
                pass
    for name in ("list_usb_devices", "list_devices", "enumerate_usb_devices", "get_usb_devices", "usb_devices", "devices"):
        fn = getattr(devices_service_module, name, None)
        if callable(fn):
            try:
                return list(fn(*args, **kwargs))
            except Exception:
                pass
    return []


@router.get("/usb")
def list_usb_devices(include_metadata: Optional[bool] = Query(True), refresh: Optional[bool] = Query(True)):
    return _list_usb(include_metadata=include_metadata)


@router.get("/usb/")
def list_usb_devices_slash(include_metadata: Optional[bool] = Query(True), refresh: Optional[bool] = Query(True)):
    return _list_usb(include_metadata=include_metadata)


@router.get("")
def list_devices_root():
    return _list_usb()


@router.get("/")
def list_devices_root_slash():
    return _list_usb()


device_router = APIRouter(prefix="/api/device", tags=["devices"])


@device_router.get("/usb")
def list_usb_devices_singular():
    return _list_usb()


@device_router.get("")
def list_devices_singular():
    return _list_usb()


devices_router = router
usb_router = router
router_usb = router
