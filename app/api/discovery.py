from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services import cameras as cameras_service
from app.services import network_discovery as nd

router = APIRouter(prefix="/api/discovery", tags=["discovery"])


def _existing_ips(cameras) -> set:
    ips = set()
    try:
        from app.services.camera_onboarding import derive_ip_address as _derive
    except Exception:  # noqa: BLE001
        _derive = None
    for cam in cameras or []:
        ip = None
        try:
            if hasattr(cam, "ip_address") and getattr(cam, "ip_address", None):
                ip = str(getattr(cam, "ip_address")).strip()
            if not ip and _derive is not None:
                ip = _derive(cam)
        except Exception:  # noqa: BLE001
            ip = None
        if ip:
            ips.add(ip)
    return ips


@router.get("/network")
async def discover_network(
    subnet: Optional[str] = Query(default=None),
    verified_only: bool = Query(default=False),
    verify: bool = Query(default=True),
    db: AsyncSession = Depends(db_session),
):
    # Determine subnets
    subnets = []
    if subnet:
        subnets = [subnet]
    else:
        try:
            subnets = nd.detect_local_subnets() or []
        except Exception:  # noqa: BLE001
            subnets = []
    if not subnets:
        try:
            import socket

            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            finally:
                s.close()
            subnets = [ip.rsplit(".", 1)[0] + ".0/24"]
        except Exception:  # noqa: BLE001
            subnets = ["192.168.1.0/24"]

    # Existing cameras
    try:
        cams = await cameras_service.list_cameras(db)
    except Exception:  # noqa: BLE001
        cams = []
    existing_ips = _existing_ips(cams)

    devices = []
    seen = set()
    scan_kwargs = {"verify": verify}
    for sn in subnets:
        try:
            found = await nd.scan_subnet(sn, **scan_kwargs)
        except Exception:  # noqa: BLE001
            found = []
        for d in found or []:
            ip = d.get("ip") or d.get("ip_address") or d.get("host") or d.get("address")
            if not ip:
                continue
            if ip in seen:
                continue
            seen.add(ip)
            if "new" not in d:
                d["new"] = ip not in nd._discovered_ips
            d["already_added"] = ip in existing_ips
            if verified_only and not d.get("verified"):
                continue
            # normalize keys
            d.setdefault("ip", ip)
            d.setdefault("ip_address", ip)
            d.setdefault("host", ip)
            d.setdefault("address", ip)
            devices.append(d)

    # track discovered IPs in memory
    try:
        for d in devices:
            ip = d.get("ip")
            if ip:
                nd._discovered_ips.add(ip)
    except Exception:  # noqa: BLE001
        pass

    return devices
