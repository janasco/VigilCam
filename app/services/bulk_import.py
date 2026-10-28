"""ONVIF bulk import service: import discovered devices as cameras."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


def extract_ip(device: Any) -> Optional[str]:
    """Extract a bare IP/host string from a discovered-device dict."""
    if device is None:
        return None
    if isinstance(device, str):
        text = device.strip()
        return text or None
    if not isinstance(device, dict):
        try:
            return extract_ip(dict(device))
        except Exception:
            return None
    for key in ("ip", "ip_address", "host", "address", "onvif_host"):
        val = device.get(key)
        if val is not None and str(val).strip():
            text = str(val).strip()
            # Strip scheme/path if present.
            if "://" in text:
                try:
                    from app.services.camera_onboarding import _host_from_value

                    host = _host_from_value(text)
                    if host:
                        return host
                except Exception:
                    pass
            # Strip trailing :port for bare host:port.
            bare = text.split("/", 1)[0].strip()
            if bare.startswith("[") and "]" in bare:
                end = bare.find("]")
                if end != -1:
                    return bare[1:end] or None
            if ":" in bare:
                host_part, _, port_part = bare.rpartition(":")
                if port_part.isdigit() and host_part:
                    bare = host_part.strip()
            if bare:
                return bare
    # Fall back to deriving from source URL.
    source = device.get("source") or device.get("stream_url") or device.get("url")
    if source:
        try:
            from app.services.camera_onboarding import derive_ip_address

            return derive_ip_address(source, None)
        except Exception:
            return None
    return None


def normalize_device(device: Any) -> Dict[str, Any]:
    """Return a normalized copy of a discovered-device dict."""
    if isinstance(device, str):
        ip = extract_ip(device)
        return {
            "ip": ip,
            "ip_address": ip,
            "host": ip,
            "address": ip,
            "name": f"Camera {ip}" if ip else "Camera",
            "source": f"rtsp://{ip}:554/" if ip else None,
            "onvif": False,
            "kind": "ip",
        }
    if not isinstance(device, dict):
        try:
            device = dict(device)
        except Exception:
            return {"ip": None, "name": "Camera", "source": None, "onvif": False, "kind": "ip"}
    out = dict(device)
    ip = extract_ip(out)
    if ip:
        out.setdefault("ip", ip)
        out.setdefault("ip_address", ip)
        out.setdefault("host", ip)
        out.setdefault("address", ip)
    else:
        out.setdefault("ip", None)
        out.setdefault("ip_address", out.get("ip"))
        out.setdefault("host", out.get("ip"))
        out.setdefault("address", out.get("ip"))
    if not out.get("name"):
        out["name"] = f"Camera {ip}" if ip else "Camera"
    if out.get("onvif") is None:
        out["onvif"] = False
    if not out.get("kind"):
        out["kind"] = "onvif" if out.get("onvif") else "ip"
    if not out.get("source"):
        if out.get("onvif"):
            http_port = None
            try:
                ports = out.get("http_ports") or []
                if ports:
                    http_port = int(ports[0])
            except Exception:
                http_port = None
            http_port = http_port or 80
            out["source"] = f"onvif://{ip}:{http_port}" if ip else None
        else:
            out["source"] = out.get("stream_url") or (f"rtsp://{ip}:554/" if ip else None)
    return out


def normalize_devices(devices: Any) -> List[Dict[str, Any]]:
    if devices is None:
        return []
    if isinstance(devices, dict):
        return [normalize_device(devices)]
    try:
        return [normalize_device(d) for d in list(devices)]
    except TypeError:
        return [normalize_device(devices)]


def device_to_camera_payload(
    device: Any,
    username: Optional[str] = None,
    password: Optional[str] = None,
    name_prefix: str = "ONVIF",
    onvif_port: Optional[int] = None,
    **defaults: Any,
) -> Any:
    """Build a CameraCreate payload from a discovered-device dict."""
    from app.schemas import CameraCreate

    norm = normalize_device(device)
    ip = extract_ip(norm)
    name = norm.get("name") or (f"{name_prefix} {ip}" if ip else f"{name_prefix} Camera")
    name = str(name).strip() or (f"{name_prefix} {ip}" if ip else f"{name_prefix} Camera")
    is_onvif = bool(norm.get("onvif"))
    kind = norm.get("kind") or ("onvif" if is_onvif else "ip")
    if kind not in ("usb", "ip", "onvif"):
        kind = "onvif" if is_onvif else "ip"
    source = norm.get("source") or norm.get("stream_url")
    if not source and ip:
        source = f"onvif://{ip}:80" if is_onvif else f"rtsp://{ip}:554/"
    if not source:
        source = f"onvif://{ip}:80" if ip else "onvif://unknown:80"
    user = username if username is not None else norm.get("username")
    pwd = password if password is not None else norm.get("password")
    port = onvif_port
    if port is None:
        port = norm.get("onvif_port")
    if port is None:
        try:
            ports = norm.get("http_ports") or []
            if ports:
                port = int(ports[0])
        except Exception:
            port = None
    payload: Dict[str, Any] = {
        "name": name,
        "kind": kind,
        "source": source,
        "username": user,
        "password": pwd,
        "onvif_host": ip,
        "ip_address": ip,
        "onvif_port": port,
    }
    for key, value in (defaults or {}).items():
        if value is not None:
            payload[key] = value
    # Drop None username/password so defaults apply cleanly.
    if payload.get("username") is None:
        payload.pop("username", None)
    if payload.get("password") is None:
        payload.pop("password", None)
    if payload.get("onvif_port") is None:
        payload.pop("onvif_port", None)
    return CameraCreate(**payload)


def get_existing_ips(cameras: Any) -> set:
    """Collect known camera IPs from camera objects/dicts."""
    ips: set = set()
    try:
        from app.services.camera_onboarding import derive_ip_address as _derive
    except Exception:
        _derive = None  # type: ignore[assignment]
    for cam in cameras or []:
        ip = None
        try:
            if isinstance(cam, dict):
                ip = cam.get("ip_address") or cam.get("onvif_host") or cam.get("ip") or cam.get("host")
                if not ip and _derive is not None:
                    ip = _derive(cam.get("source"), cam.get("onvif_host"))
            else:
                ip = getattr(cam, "ip_address", None) or getattr(cam, "onvif_host", None)
                if not ip and _derive is not None:
                    try:
                        ip = _derive(cam)
                    except Exception:
                        ip = None
        except Exception:
            ip = None
        if ip:
            ips.add(str(ip).strip())
    return ips


def filter_new_devices(devices: Any, existing_ips: Any) -> List[Dict[str, Any]]:
    """Return only devices whose IP is not already known."""
    known = set()
    if isinstance(existing_ips, (set, list, tuple)):
        known = {str(x).strip() for x in existing_ips if x is not None and str(x).strip()}
    elif existing_ips is not None:
        known = {str(existing_ips).strip()}
    out: List[Dict[str, Any]] = []
    for dev in normalize_devices(devices):
        ip = extract_ip(dev)
        if ip and str(ip).strip() in known:
            continue
        out.append(dev)
    return out


def _serialize_camera(camera: Any) -> Dict[str, Any]:
    if isinstance(camera, dict):
        return dict(camera)
    data: Dict[str, Any] = {}
    for key in (
        "id", "name", "kind", "source", "username", "onvif_host",
        "ip_address", "onvif_port", "enabled", "created_at",
    ):
        try:
            data[key] = getattr(camera, key, None)
        except Exception:
            data[key] = None
    return data


async def bulk_import_devices(
    db: Any,
    devices: Any,
    username: Optional[str] = None,
    password: Optional[str] = None,
    skip_existing: bool = True,
    name_prefix: str = "ONVIF",
    onvif_port: Optional[int] = None,
    **defaults: Any,
) -> Dict[str, Any]:
    """Import discovered device dicts as cameras.

    Returns a summary dict with created/skipped/errors lists plus totals.
    """
    from app.services import cameras as cameras_service

    normalized = normalize_devices(devices)
    try:
        existing = await cameras_service.list_cameras(db)
    except Exception:
        existing = []
    existing_ips = get_existing_ips(existing)

    created: List[Any] = []
    skipped: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for dev in normalized:
        ip = extract_ip(dev)
        if not ip:
            errors.append({"device": dev, "error": "missing ip/host"})
            continue
        if skip_existing and str(ip).strip() in existing_ips:
            skipped.append({**dev, "reason": "already_added"})
            continue
        try:
            payload = device_to_camera_payload(
                dev,
                username=username,
                password=password,
                name_prefix=name_prefix,
                onvif_port=onvif_port,
                **defaults,
            )
        except Exception as exc:
            errors.append({"device": dev, "error": str(exc)})
            continue
        try:
            camera = await cameras_service.create_camera(db, payload)
        except Exception as exc:
            errors.append({"device": dev, "error": str(exc)})
            continue
        created.append(camera)
        try:
            if ip:
                existing_ips.add(str(ip).strip())
        except Exception:
            pass

    return {
        "total_discovered": len(normalized),
        "total_created": len(created),
        "total_skipped": len(skipped),
        "total_errors": len(errors),
        "created": created,
        "skipped": skipped,
        "errors": errors,
    }


async def discover_devices(
    subnet: Optional[str] = None,
    subnets: Optional[Sequence[str]] = None,
    timeout: float = 0.5,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Discover ONVIF/IP devices via network discovery."""
    from app.services import network_discovery as nd

    targets: List[str] = []
    if subnets:
        targets = [str(s) for s in subnets]
    elif subnet:
        targets = [str(subnet)]
    if targets:
        found: List[Dict[str, Any]] = []
        for sn in targets:
            try:
                part = await nd.scan_subnet(sn, timeout=timeout)
            except Exception:
                part = []
            found.extend(part or [])
        return [normalize_device(d) for d in found]
    try:
        found = await nd.discover_network_cameras(timeout=timeout, **kwargs)
    except Exception:
        found = []
    return [normalize_device(d) for d in (found or [])]


async def preview_bulk_import(
    db: Any,
    subnet: Optional[str] = None,
    devices: Any = None,
    timeout: float = 0.5,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Discover (or accept) devices and annotate already_added/new flags."""
    from app.services import cameras as cameras_service

    if devices is None:
        if subnet:
            devices = await discover_devices(subnet=subnet, timeout=timeout, **kwargs)
        else:
            try:
                devices = await discover_devices(timeout=timeout, **kwargs)
            except Exception:
                devices = []
    normalized = normalize_devices(devices)
    try:
        existing = await cameras_service.list_cameras(db)
    except Exception:
        existing = []
    existing_ips = get_existing_ips(existing)
    out: List[Dict[str, Any]] = []
    for dev in normalized:
        ip = extract_ip(dev)
        already = bool(ip and str(ip).strip() in existing_ips)
        row = dict(dev)
        row["already_added"] = already
        if "new" not in row:
            row["new"] = not already
        row.setdefault("ip", ip)
        row.setdefault("ip_address", ip)
        row.setdefault("host", ip)
        row.setdefault("address", ip)
        out.append(row)
    return out


async def bulk_import_subnet(
    db: Any,
    subnet: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    skip_existing: bool = True,
    timeout: float = 0.5,
    name_prefix: str = "ONVIF",
    onvif_port: Optional[int] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Discover a subnet then import all found devices as cameras."""
    devices = await discover_devices(subnet=subnet, timeout=timeout)
    return await bulk_import_devices(
        db,
        devices,
        username=username,
        password=password,
        skip_existing=skip_existing,
        name_prefix=name_prefix,
        onvif_port=onvif_port,
        **kwargs,
    )


# Aliases for caller convenience.
import_discovered_devices = bulk_import_devices
import_devices = bulk_import_devices
bulk_import = bulk_import_devices
import_onvif_devices = bulk_import_devices
bulk_import_onvif_devices = bulk_import_devices
import_discovered_cameras = bulk_import_devices
bulk_import_cameras = bulk_import_devices
discover = discover_devices
discover_onvif_devices = discover_devices
preview = preview_bulk_import
preview_import = preview_bulk_import


__all__ = [
    "extract_ip",
    "normalize_device",
    "normalize_devices",
    "device_to_camera_payload",
    "get_existing_ips",
    "filter_new_devices",
    "bulk_import_devices",
    "discover_devices",
    "preview_bulk_import",
    "bulk_import_subnet",
]
