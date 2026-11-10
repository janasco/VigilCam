from __future__ import annotations

import asyncio
import ipaddress
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services import cameras as cameras_service
from app.services import network_discovery as nd
from app.services import ws_discovery

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

    scan_kwargs = {"verify": verify}

    async def _scan_subnets():
        found_devices = []
        for sn in subnets:
            try:
                found = await nd.scan_subnet(sn, **scan_kwargs)
                if found:
                    found_devices.extend(found)
            except Exception:  # noqa: BLE001
                pass
        return found_devices

    async def _scan_ws():
        try:
            return await ws_discovery.discover_devices(timeout=2.5)
        except Exception:  # noqa: BLE001
            try:
                return await ws_discovery.discover(timeout=2.5)
            except Exception:  # noqa: BLE001
                return []

    subnet_found, ws_found = await asyncio.gather(
        _scan_subnets(),
        _scan_ws(),
        return_exceptions=True,
    )
    if isinstance(subnet_found, Exception):
        subnet_found = []
    if isinstance(ws_found, Exception):
        ws_found = []

    devices_by_ip: dict[str, dict] = {}
    for d in subnet_found or []:
        ip = d.get("ip") or d.get("ip_address") or d.get("host") or d.get("address")
        if not ip or ip in devices_by_ip:
            continue
        d.setdefault("ip", ip)
        d.setdefault("ip_address", ip)
        d.setdefault("host", ip)
        d.setdefault("address", ip)
        devices_by_ip[ip] = d

    for dev in ws_found or []:
        xaddresses = getattr(dev, "xaddresses", None) or (dev.get("xaddresses") if isinstance(dev, dict) else []) or []
        xaddr = xaddresses[0] if xaddresses else ""
        ip = getattr(dev, "address", None) or (dev.get("address") if isinstance(dev, dict) else None)
        port = None
        if xaddr:
            try:
                parsed = urlparse(xaddr)
                if not ip and parsed.hostname:
                    ip = parsed.hostname
                if parsed.port:
                    port = parsed.port
            except Exception:  # noqa: BLE001
                pass
        if not ip:
            continue

        if subnet:
            try:
                net = ipaddress.ip_network(subnet.strip(), strict=False)
                if ipaddress.ip_address(ip) not in net:
                    continue
            except Exception:  # noqa: BLE001
                pass

        mfr = getattr(dev, "mfr", None) or (dev.get("mfr") if isinstance(dev, dict) else None)
        model = getattr(dev, "model", None) or (dev.get("model") if isinstance(dev, dict) else None)
        scopes = getattr(dev, "scopes", None) or (dev.get("scopes") if isinstance(dev, dict) else []) or []

        if ip in devices_by_ip:
            d = devices_by_ip[ip]
            d["kind"] = "onvif"
            d["onvif"] = True
            d["verified"] = True
            d["onvif_verified"] = True
            d["verify_method"] = "onvif:ws-discovery"
            d["source"] = f"rtsp://{ip}:554/"
            d["onvif_url"] = xaddr
            d["stream_url"] = f"rtsp://{ip}:554/"
            if mfr:
                d["manufacturer"] = mfr
            if model:
                d["model"] = model
            if scopes:
                d["scopes"] = scopes
            d["name"] = f"{mfr or 'ONVIF'} {ip}"
            protocols = d.setdefault("protocols", [])
            if "onvif" not in protocols:
                protocols.append("onvif")
            if port:
                ports = d.setdefault("ports", [])
                if port not in ports:
                    ports.append(port)
                    ports.sort()
                hports = d.setdefault("http_ports", [])
                if port not in hports:
                    hports.append(port)
                    hports.sort()
        else:
            ports = [port] if port else [8899]
            if 554 not in ports:
                ports.append(554)
            ports.sort()
            devices_by_ip[ip] = {
                "ip": ip,
                "ip_address": ip,
                "host": ip,
                "address": ip,
                "ports": ports,
                "http_ports": [port] if port else [8899],
                "rtsp_ports": [554],
                "http": True,
                "rtsp": True,
                "onvif": True,
                "verified": True,
                "onvif_verified": True,
                "verify_method": "onvif:ws-discovery",
                "protocols": ["http", "onvif", "rtsp"],
                "name": f"{mfr or 'ONVIF'} {ip}",
                "manufacturer": mfr,
                "model": model,
                "scopes": scopes,
                "server": None,
                "kind": "onvif",
                "source": f"rtsp://{ip}:554/",
                "stream_url": f"rtsp://{ip}:554/",
                "onvif_url": xaddr,
                "new": ip not in nd._discovered_ips,
                "already_added": ip in existing_ips,
            }

    devices = []
    for ip, d in devices_by_ip.items():
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
