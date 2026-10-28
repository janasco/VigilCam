from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any, Optional

import httpx

# In-memory tracking of IPs that have already been reported as discovered,
# so repeated scans do not re-notify the same devices as "new".
_discovered_ips: set[str] = set()
discovered_ips = _discovered_ips

_DEFAULT_HTTP_PORTS = (80, 8080, 8000, 8899, 5000)
_DEFAULT_RTSP_PORTS = (554, 8554, 5544)
_ONVIF_PATHS = ("/onvif/device_service", "/onvif/device_service/", "/device_service")


async def _tcp_open(ip: str, port: int, timeout: float) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


async def _http_probe(ip: str, port: int, timeout: float) -> dict[str, Any]:
    """Probe an HTTP port; return info dict if reachable."""
    info: dict[str, Any] = {"reachable": False, "status": None, "server": None, "onvif": False, "body": ""}
    url = f"http://{ip}:{port}/"
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False, follow_redirects=True) as client:
            r = await client.get(url)
            info["reachable"] = True
            info["status"] = r.status_code
            info["server"] = r.headers.get("server") or r.headers.get("Server")
            text = (r.text or "")[:2048].lower()
            info["body"] = text
            if "onvif" in text or "onvif" in (info["server"] or "").lower():
                info["onvif"] = True
    except Exception:  # noqa: BLE001
        return info
    # Try ONVIF device service endpoint with a real SOAP envelope.
    # An empty POST proves nothing (any web server answers 400/404), so only
    # treat SOAP-looking answers as ONVIF hints; real verification happens in
    # _onvif_verify() via GetSystemDateAndTime.
    soap_probe = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        "<s:Body/></s:Envelope>"
    )
    for path in _ONVIF_PATHS:
        try:
            async with httpx.AsyncClient(timeout=timeout, verify=False, follow_redirects=False) as client:
                r = await client.post(
                    f"http://{ip}:{port}{path}",
                    content=soap_probe.encode(),
                    headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                )
                body = (r.text or "").lower()
                if "envelope" in body or "getsystemdateandtime" in body or "notauthorized" in body or "onvif" in body:
                    info["onvif"] = True
                    if not info["server"]:
                        info["server"] = r.headers.get("server") or r.headers.get("Server")
                    break
        except Exception:  # noqa: BLE001
            continue
    return info


async def _rtsp_options_probe(ip: str, port: int, timeout: float) -> bool:
    """Speak minimal RTSP: a real server answers with an RTSP/ status line."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
        try:
            writer.write(b"OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(256), timeout=timeout)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        text = data.decode("utf-8", errors="ignore").strip().upper()
        return text.startswith("RTSP/")
    except Exception:  # noqa: BLE001
        return False


async def _onvif_verify(ip: str, port: int, timeout: float) -> bool:
    """Confirm ONVIF with a real unauthenticated GetSystemDateAndTime call."""
    try:
        from app.services import onvif_digest_service as _ods

        await _ods.get_system_datetime(ip, port, scheme="http", timeout=min(timeout, 4.0))
        return True
    except Exception:  # noqa: BLE001
        return False


async def scan_host(ip: str, timeout: float = 0.5, **kwargs: Any) -> Optional[dict[str, Any]]:
    """Scan a single host for ONVIF/HTTP/RTSP camera candidates.

    Returns a device dict if any candidate service is found, else None.
    """
    timeout = float(kwargs.get("timeout", timeout) or timeout)
    # TCP port checks
    http_ports = list(kwargs.get("http_ports") or _DEFAULT_HTTP_PORTS)
    rtsp_ports = list(kwargs.get("rtsp_ports") or _DEFAULT_RTSP_PORTS)

    tcp_results: dict[int, bool] = {}
    all_ports = set(http_ports) | set(rtsp_ports)
    awaitables = {p: _tcp_open(ip, p, timeout) for p in all_ports}
    for p, coro in awaitables.items():
        tcp_results[p] = await coro

    open_http_ports = [p for p in http_ports if tcp_results.get(p)]
    open_rtsp_ports = [p for p in rtsp_ports if tcp_results.get(p)]

    if not open_http_ports and not open_rtsp_ports:
        return None

    device: dict[str, Any] = {
        "ip": ip,
        "ip_address": ip,
        "host": ip,
        "address": ip,
        "ports": sorted(list(all_ports and [p for p in all_ports if tcp_results.get(p)] or [])),
        "http_ports": open_http_ports,
        "rtsp_ports": open_rtsp_ports,
        "http": bool(open_http_ports),
        "rtsp": bool(open_rtsp_ports),
        "onvif": False,
        "verified": False,
        "verify_method": None,
        "protocols": [],
        "name": f"Camera {ip}",
        "manufacturer": None,
        "model": None,
        "server": None,
    }

    # HTTP / ONVIF probes
    http_info = None
    for p in open_http_ports:
        http_info = await _http_probe(ip, p, timeout)
        if http_info.get("reachable"):
            device["http"] = True
            if http_info.get("onvif"):
                device["onvif"] = True
            if http_info.get("server"):
                device["server"] = http_info["server"]
                if not device["name"] or device["name"] == f"Camera {ip}":
                    device["name"] = http_info["server"]
            break

    if device["http"]:
        device["protocols"].append("http")
    if device["onvif"]:
        device["protocols"].append("onvif")
    if device["rtsp"]:
        device["protocols"].append("rtsp")

    # Protocol verification: confirm the device actually speaks ONVIF or RTSP.
    # An open port alone proves nothing (printers/routers/NAS boxes all serve
    # HTTP). Skip verification only when explicitly requested.
    verify = kwargs.get("verify", True)
    if verify not in (False, 0, "0", "false", "False", "no", "off"):
        vtimeout = float(kwargs.get("verify_timeout", min(timeout * 4, 4.0)))
        verified_method = None
        if device["onvif"]:
            for p in open_http_ports:
                try:
                    if await _onvif_verify(ip, p, vtimeout):
                        device["onvif_verified"] = True
                        verified_method = f"onvif:{p}"
                        break
                except Exception:  # noqa: BLE001
                    continue
            if verified_method is None:
                # Hint said ONVIF but no SOAP answer: don't trust it.
                device["onvif"] = False
                device["protocols"] = [x for x in device["protocols"] if x != "onvif"]
        if verified_method is None and device["rtsp"]:
            for p in open_rtsp_ports:
                try:
                    if await _rtsp_options_probe(ip, p, vtimeout):
                        device["rtsp_verified"] = True
                        verified_method = f"rtsp:{p}"
                        break
                except Exception:  # noqa: BLE001
                    continue
            if verified_method is None:
                device["rtsp"] = False
                device["protocols"] = [x for x in device["protocols"] if x != "rtsp"]
        if verified_method is not None:
            device["verified"] = True
            device["verify_method"] = verified_method

    # Kind / source
    if device["onvif"]:
        device["kind"] = "onvif"
        http_port = open_http_ports[0] if open_http_ports else 80
        device["source"] = f"onvif://{ip}:{http_port}"
        device["stream_url"] = None
    elif device["rtsp"]:
        device["kind"] = "ip"
        rtsp_port = open_rtsp_ports[0] if open_rtsp_ports else 554
        device["source"] = f"rtsp://{ip}:{rtsp_port}/"
        device["stream_url"] = device["source"]
    else:
        device["kind"] = "ip"
        http_port = open_http_ports[0] if open_http_ports else 80
        device["source"] = f"http://{ip}:{http_port}/"
        device["stream_url"] = device["source"]

    # new flag (tracked in memory)
    device["new"] = ip not in _discovered_ips

    return device


async def scan_subnet(subnet: str, timeout: float = 0.5, **kwargs: Any) -> list[dict[str, Any]]:
    """Async scan of a subnet (CIDR) for camera candidates."""
    timeout = float(kwargs.get("timeout", timeout) or timeout)
    try:
        net = ipaddress.ip_network(str(subnet).strip(), strict=False)
    except ValueError:
        return []
    hosts = list(net.hosts())
    if not hosts:
        return []

    concurrency = int(kwargs.get("concurrency", 64) or 64)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(ip: str):
        async with sem:
            try:
                return await scan_host(ip, timeout=timeout, **{k: v for k, v in kwargs.items() if k not in ("timeout",)})
            except Exception:  # noqa: BLE001
                return None

    results = await asyncio.gather(*[_one(str(h)) for h in hosts], return_exceptions=True)
    devices = [r for r in results if isinstance(r, dict)]

    # Track discovered IPs in memory
    for d in devices:
        ip = d.get("ip")
        if ip:
            _discovered_ips.add(ip)

    return devices


def detect_local_subnets() -> list[str]:
    """Best-effort detection of local /24 subnets."""
    ips: list[str] = []
    # default route interface
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip not in ips:
                ips.append(ip)
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        pass
    # hostname resolution
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except Exception:  # noqa: BLE001
        pass

    subnets: list[str] = []
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.version != 4:
            continue
        if not addr.is_private or addr.is_loopback or addr.is_link_local:
            continue
        cidr = str(ipaddress.ip_network(f"{ip}/24", strict=False))
        if cidr not in subnets:
            subnets.append(cidr)
    if not subnets:
        # fallback to common private subnet derived from local ip
        try:
            local = socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa: BLE001
            local = "127.0.0.1"
        if local and local != "127.0.0.1":
            try:
                cidr = str(ipaddress.ip_network(f"{local}/24", strict=False))
                subnets.append(cidr)
            except Exception:  # noqa: BLE001
                pass
    return subnets


# Aliases for subnet detection
detect_subnets = detect_local_subnets
get_local_subnets = detect_local_subnets
local_subnets = detect_local_subnets


async def discover_network_cameras(*args, **kwargs) -> list[dict[str, Any]]:
    """High-level discovery entry point.

    Accepts optional ``subnet``/``subnets`` and ``existing_cameras``/``cameras``.
    """
    subnets = kwargs.get("subnets") or kwargs.get("subnet_list")
    subnet = kwargs.get("subnet")
    if subnets is None:
        if subnet is not None:
            subnets = [subnet]
        else:
            # positional
            if args:
                first = args[0]
                if isinstance(first, str):
                    subnets = [first]
                elif isinstance(first, (list, tuple)):
                    subnets = list(first)
                else:
                    subnets = None
            if subnets is None:
                subnets = kwargs.get("subnets_to_scan")
    if not subnets:
        subnets = detect_local_subnets() or []
    if not subnets:
        subnets = ["192.168.1.0/24"]

    # DB session support
    db = kwargs.get("db") or kwargs.get("session") or kwargs.get("database")
    cameras = kwargs.get("cameras") or kwargs.get("existing_cameras") or kwargs.get("existing") or []
    if db is not None and not cameras:
        try:
            from app.services import cameras as _cs

            cameras = await _cs.list_cameras(db)
        except Exception:  # noqa: BLE001
            try:
                cameras = await db.execute(__import__("sqlalchemy").select(__import__("app.models.entities", fromlist=["Camera"]).Camera))
                cameras = list(cameras.scalars().all())
            except Exception:  # noqa: BLE001
                cameras = []
    # positional: first could be db or subnet
    if len(args) > 1 and not cameras:
        second = args[1]
        if hasattr(second, "execute") or hasattr(second, "scalars"):
            try:
                from app.services import cameras as _cs

                cameras = await _cs.list_cameras(second)
            except Exception:  # noqa: BLE001
                cameras = []
        else:
            cameras = second
    # If first positional is a db session, fetch cameras and set subnet default
    if args and (hasattr(args[0], "execute") or hasattr(args[0], "scalars")):
        if db is None:
            db = args[0]
        if not cameras:
            try:
                from app.services import cameras as _cs

                cameras = await _cs.list_cameras(db)
            except Exception:  # noqa: BLE001
                cameras = []
        # no subnet provided
        if not subnets or subnets == []:
            subnets = None

    existing_ips = set()
    try:
        from app.services.camera_onboarding import derive_ip_address as _derive
    except Exception:  # noqa: BLE001
        _derive = None
    for cam in cameras or []:
        ip = None
        try:
            if hasattr(cam, "ip_address") and getattr(cam, "ip_address", None):
                ip = str(getattr(cam, "ip_address")).strip()
            elif isinstance(cam, dict) and cam.get("ip_address"):
                ip = str(cam.get("ip_address")).strip()
            if not ip and _derive is not None:
                ip = _derive(cam)
        except Exception:  # noqa: BLE001
            ip = None
        if ip:
            existing_ips.add(ip)

    timeout = float(kwargs.get("timeout", 0.5) or 0.5)
    all_devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sn in subnets:
        found = await scan_subnet(sn, timeout=timeout, **{k: v for k, v in kwargs.items() if k in ("concurrency",)})
        for d in found or []:
            ip = d.get("ip")
            if not ip or ip in seen:
                continue
            seen.add(ip)
            d["already_added"] = ip in existing_ips
            if "new" not in d:
                d["new"] = ip not in _discovered_ips
            all_devices.append(d)

    return all_devices


# Aliases
discover = discover_network_cameras
discover_network = discover_network_cameras
discover_cameras = discover_network_cameras
scan_network = discover_network_cameras
network_discover = discover_network_cameras
discover_devices = discover_network_cameras
scan = scan_subnet
discover_network_devices = discover_network_cameras


async def discover_subnet(*args, **kwargs):
    # If first arg looks like subnet, scan it
    if args and isinstance(args[0], str):
        return await scan_subnet(*args, **kwargs)
    return await discover_network_cameras(*args, **kwargs)


class NetworkDiscovery:
    def __init__(self, *a, **k):
        pass

    async def scan_subnet(self, *a, **k):
        return await scan_subnet(*a, **k)

    async def discover(self, *a, **k):
        return await discover_network_cameras(*a, **k)

    async def scan(self, *a, **k):
        return await scan_subnet(*a, **k)

    def detect_local_subnets(self):
        return detect_local_subnets()


network_discovery = NetworkDiscovery()
network_discovery_service = network_discovery

# Sync helpers
def scan_subnet_sync(*a, **k):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return loop.run_in_executor(pool, lambda: asyncio.run(scan_subnet(*a, **k)))
        return loop.run_until_complete(scan_subnet(*a, **k))
    except RuntimeError:
        return asyncio.run(scan_subnet(*a, **k))


def discover_sync(*a, **k):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return asyncio.run(discover_network_cameras(*a, **k))
        return loop.run_until_complete(discover_network_cameras(*a, **k))
    except RuntimeError:
        return asyncio.run(discover_network_cameras(*a, **k))


# Utility to reset in-memory tracking (tests)
def reset_discovered_ips():
    _discovered_ips.clear()


def clear_discovered_ips():
    _discovered_ips.clear()


def clear_discovered():
    _discovered_ips.clear()


__all__ = [
    "scan_host",
    "scan_subnet",
    "discover_network_cameras",
    "detect_local_subnets",
    "_discovered_ips",
    "discovered_ips",
]
