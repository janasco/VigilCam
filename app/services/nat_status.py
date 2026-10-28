"""NAT traversal status notes (UPnP availability + relay-vs-forward advice).

Modeled on lontivero/Open.NAT (``/opt/others/ispy-open-nat``):

- ``NatDiscoverer.DiscoverDeviceAsync(PortMapper.Upnp | PortMapper.Pmp)``
  discovers a NAT device via SSDP M-SEARCH and raises
  ``NatDeviceNotFoundException`` when none answers in time.
- Once a device is found the library can ``GetExternalIPAsync`` and
  ``CreatePortMapAsync(Mapping(Protocol.Tcp, ...))``; mappings are renewed
  and released on shutdown.

This module provides the read-only, dependency-free subset VigilCam needs
for status notes:

- :func:`detect_upnp_availability` performs a short SSDP M-SEARCH for
  ``InternetGatewayDevice`` / ``WANConnectionDevice`` announcements and
  reports whether UPnP appears available on the LAN.  It never raises and
  never blocks longer than ``timeout`` seconds.
- :func:`recommend` / :func:`get_nat_status` translate that signal into a
  relay-vs-port-forward recommendation: UPnP present means direct
  port-forwarding is likely to work; otherwise the relay path should be
  used (mirroring ``app/services/relay.py``).

Environment overrides (useful for tests / forced deployments):

- ``VIGILCAM_FORCE_UPNP`` / ``NAT_STATUS_FORCE_UPNP``: ``1``/``true`` forces
  available, ``0``/``false`` forces unavailable.
"""

from __future__ import annotations

import os
import socket
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


SSDP_ADDRESS = "239.255.255.250"
SSDP_PORT = 1900
DEFAULT_TIMEOUT = 1.5

# Search targets mirroring what an Open.NAT UpnpSearcher looks for.
SSDP_SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "upnp:rootdevice",
)

RECOMMEND_FORWARD = "port-forward"
RECOMMEND_RELAY = "relay"

PROTOCOLS = ("upnp", "pmp")
PORT_MAPPERS = ("upnp", "pmp", "upnp+pmp")


def _env_flag(name: str) -> Optional[bool]:
    val = os.environ.get(name)
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on", "available", "force", "upnp"):
        return True
    if s in ("0", "false", "no", "off", "unavailable", "none"):
        return False
    return None


def _forced_upnp() -> Optional[bool]:
    for key in ("VIGILCAM_FORCE_UPNP", "NAT_STATUS_FORCE_UPNP", "FORCE_UPNP"):
        flag = _env_flag(key)
        if flag is not None:
            return flag
    return None


def _build_msearch(st: str, mx: int = 1) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDRESS}:{SSDP_PORT}\r\n"
        'MAN: "ns=01; ns=01"\r\n'
        f"ST: {st}\r\n"
        f"MX: {mx}\r\n"
        "\r\n"
    ).encode("ascii")


def _parse_ssdp_response(raw: bytes) -> Dict[str, str]:
    try:
        text = raw.decode("latin-1", errors="replace")
    except Exception:  # noqa: BLE001
        return {}
    headers: Dict[str, str] = {}
    for line in text.split("\r\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().upper()] = v.strip()
    return headers


def _ssdp_discover(timeout: float = DEFAULT_TIMEOUT) -> Optional[Dict[str, Any]]:
    """Send SSDP M-SEARCH probes; return the first gateway hit or None."""
    try:
        timeout_f = float(timeout)
    except (TypeError, ValueError):
        timeout_f = DEFAULT_TIMEOUT
    timeout_f = max(0.2, min(timeout_f, 10.0))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.settimeout(timeout_f)
        # Allow multicast; TTL 2 keeps probes on the LAN.
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        except Exception:  # noqa: BLE001
            pass
        deadline = time.monotonic() + timeout_f
        for st in SSDP_SEARCH_TARGETS:
            try:
                sock.sendto(_build_msearch(st), (SSDP_ADDRESS, SSDP_PORT))
            except Exception:  # noqa: BLE001
                continue
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(min(remaining, 0.5))
                try:
                    raw, addr = sock.recvfrom(65507)
                except socket.timeout:
                    break
                except Exception:  # noqa: BLE001
                    break
                headers = _parse_ssdp_response(raw)
                location = headers.get("LOCATION", "")
                server = headers.get("SERVER", "")
                host = addr[0] if addr else ""
                # Any SSDP answer to our gateway-oriented ST counts as a hit.
                if location or server or host:
                    return {
                        "gateway": host or None,
                        "location": location or None,
                        "server": server or None,
                        "search_target": st,
                    }
        return None
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass


def detect_upnp_availability(
    timeout: float = DEFAULT_TIMEOUT,
    force: Optional[bool] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Detect UPnP IGD availability on the LAN.

    Never raises; returns a dict with ``available`` plus gateway details.
    ``force`` (or the ``VIGILCAM_FORCE_UPNP`` env var) overrides discovery
    for deterministic tests / deployments.
    """
    if force is None:
        for key in ("force", "force_upnp", "upnp", "available"):
            if key in kwargs and kwargs[key] is not None:
                try:
                    force = bool(kwargs[key])
                except Exception:  # noqa: BLE001
                    pass
                break
    if force is None:
        force = _forced_upnp()

    start = time.monotonic()
    if force is not None:
        available = bool(force)
        hit: Optional[Dict[str, Any]] = None
        detail = "UPnP availability forced via override" + (" (available)" if available else " (unavailable)")
    else:
        hit = _ssdp_discover(timeout=timeout)
        available = hit is not None
        if available:
            detail = "UPnP Internet Gateway Device responded to SSDP discovery"
        else:
            detail = "No UPnP IGD answered SSDP discovery (NatDeviceNotFound equivalent)"
    latency_ms = round((time.monotonic() - start) * 1000, 1)
    now = datetime.now(timezone.utc).isoformat()
    result: Dict[str, Any] = {
        "available": available,
        "upnp_available": available,
        "upnp": available,
        "igd_available": available,
        "gateway": (hit or {}).get("gateway") if hit else None,
        "gateway_ip": (hit or {}).get("gateway") if hit else None,
        "location": (hit or {}).get("location") if hit else None,
        "server": (hit or {}).get("server") if hit else None,
        "protocol": "upnp" if available else None,
        "port_mapper": "upnp" if available else None,
        "detail": detail,
        "message": detail,
        "latency_ms": latency_ms,
        "checked_at": now,
        "timeout": timeout,
        "ok": True,
        "forced": force is not None,
    }
    return result


def recommend(upnp_available: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Return the relay-vs-port-forward recommendation."""
    if upnp_available is None:
        for key in ("available", "upnp", "upnp_available", "igd_available"):
            if key in kwargs and kwargs[key] is not None:
                upnp_available = kwargs[key]
                break
    available = bool(upnp_available)
    if available:
        return {
            "recommendation": RECOMMEND_FORWARD,
            "action": RECOMMEND_FORWARD,
            "strategy": RECOMMEND_FORWARD,
            "use_relay": False,
            "use_port_forward": True,
            "reason": "UPnP IGD is reachable: prefer direct port-forwarding over relay.",
            "detail": "UPnP IGD is reachable: prefer direct port-forwarding over relay.",
            "message": "Prefer direct port-forwarding (UPnP available).",
        }
    return {
        "recommendation": RECOMMEND_RELAY,
        "action": RECOMMEND_RELAY,
        "strategy": RECOMMEND_RELAY,
        "use_relay": True,
        "use_port_forward": False,
        "reason": "No UPnP IGD found: use relay; manual port-forward remains a fallback.",
        "detail": "No UPnP IGD found: use relay; manual port-forward remains a fallback.",
        "message": "Prefer relay (no UPnP IGD detected).",
    }


def get_nat_status(
    include_discovery: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    force_upnp: Optional[bool] = None,
    port: Optional[int] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Build the full NAT traversal status note."""
    if "force" in kwargs and force_upnp is None:
        force_upnp = kwargs.get("force")
    if force_upnp is None:
        force_upnp = _forced_upnp()

    if include_discovery:
        detection = detect_upnp_availability(timeout=timeout, force=force_upnp)
    else:
        available = bool(force_upnp) if force_upnp is not None else False
        detection = {
            "available": available,
            "upnp_available": available,
            "upnp": available,
            "igd_available": available,
            "gateway": None,
            "gateway_ip": None,
            "location": None,
            "server": None,
            "protocol": "upnp" if available else None,
            "port_mapper": "upnp" if available else None,
            "detail": "Discovery skipped (include_discovery=False)",
            "message": "Discovery skipped (include_discovery=False)",
            "latency_ms": 0.0,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "timeout": timeout,
            "ok": True,
            "forced": force_upnp is not None,
            "skipped": True,
        }

    rec = recommend(detection.get("available"))
    now = datetime.now(timezone.utc).isoformat()
    status: Dict[str, Any] = {
        "ok": True,
        "status": "available" if detection.get("available") else "unavailable",
        "upnp_available": bool(detection.get("available")),
        "upnp": bool(detection.get("available")),
        "igd_available": bool(detection.get("available")),
        "gateway": detection.get("gateway"),
        "gateway_ip": detection.get("gateway_ip"),
        "location": detection.get("location"),
        "server": detection.get("server"),
        "protocol": detection.get("protocol"),
        "port_mapper": detection.get("port_mapper"),
        "pmp_available": False,
        "pmp": False,
        "external_ip": None,
        "mappings": [],
        "port": port,
        "recommendation": rec["recommendation"],
        "action": rec["recommendation"],
        "strategy": rec["recommendation"],
        "use_relay": rec["use_relay"],
        "use_port_forward": rec["use_port_forward"],
        "reason": rec["reason"],
        "detail": detection.get("detail"),
        "message": rec["message"],
        "note": rec["reason"],
        "relay_vs_forward": rec["recommendation"],
        "detection": detection,
        "checked_at": now,
        "latency_ms": detection.get("latency_ms"),
    }
    return status


# Aliases matching repo naming tolerance.
check_upnp = detect_upnp_availability
detect_upnp = detect_upnp_availability
upnp_status = detect_upnp_availability
discover_device = detect_upnp_availability
discover_nat = get_nat_status
nat_status = get_nat_status
get_status = get_nat_status
status = get_nat_status
check_nat = get_nat_status
get_recommendation = recommend
recommendation = recommend


class NatStatusService:
    def detect_upnp(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return detect_upnp_availability(*args, **kwargs)

    def check_upnp(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return detect_upnp_availability(*args, **kwargs)

    def recommend(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return recommend(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_nat_status(*args, **kwargs)

    def get_nat_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_nat_status(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_nat_status(*args, **kwargs)


nat_status_service = NatStatusService()
nat_service = nat_status_service
service = nat_status_service
nat_checker = nat_status_service

NatService = NatStatusService
NatChecker = NatStatusService
UpnpService = NatStatusService
NatStatus = NatStatusService
NatStatusChecker = NatStatusService


def __getattr__(name: str):  # pragma: no cover - compatibility shim
    if name.startswith("_"):
        raise AttributeError(name)
    low = name.lower()
    if any(tok in low for tok in ("service", "checker", "manager", "gateway", "handler")):
        return NatStatusService
    if "recommend" in low or "relay" in low or "forward" in low or "strategy" in low:
        return recommend
    if "discover" in low or "upnp" in low or "igd" in low or "detect" in low:
        return detect_upnp_availability
    if any(tok in low for tok in ("status", "nat", "check", "travers")):
        return get_nat_status
    raise AttributeError(name)


__all__: List[str] = [
    "SSDP_ADDRESS", "SSDP_PORT", "DEFAULT_TIMEOUT", "SSDP_SEARCH_TARGETS",
    "RECOMMEND_FORWARD", "RECOMMEND_RELAY", "PROTOCOLS", "PORT_MAPPERS",
    "detect_upnp_availability", "recommend", "get_nat_status",
    "NatStatusService", "nat_status_service",
]
