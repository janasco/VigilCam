"""UDP multicast WS-Discovery (WS-Probe) for ONVIF devices.

Python port of the discovery ideas in ``/opt/others/ispy-onvif-discovery``
(C# ``OnvifDiscovery`` library):

* SOAP WS-Probe datagram with ``dn:NetworkVideoTransmitter`` type and
  optional scope filtering (cf. ``Common/WSProbeMessageBuilder.cs`` and
  ``Common/Constants.cs``).
* RelatesTo/message-id matching and probe-match parsing
  (cf. ``Common/ProbeMessageProcessor.cs`` and ``Models/XmlProbeResponse.cs``).
* Model/manufacturer extraction from scope URIs
  (cf. ``Common/DeviceFactory.cs``).
* Per-interface UDP clients, periodic re-probing every 500 ms, overall
  timeout handling and duplicate suppression keyed by XAddrs
  (cf. ``Discovery.cs`` and ``Udp/UdpClientFactory.cs``).

Only the Python standard library is used (plus ``asyncio``), so this works
without extra dependencies.
"""

from __future__ import annotations

import asyncio
import re
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import unquote

WS_MULTICAST_ADDRESS = "239.255.255.250"
WS_MULTICAST_PORT = 3702

#: Default probed device type (mirrors the C# probe template).
DEFAULT_PROBE_TYPES = ("dn:NetworkVideoTransmitter",)

#: How often the probe datagram is re-sent while waiting (C# uses 500 ms).
PROBE_RESEND_INTERVAL = 0.5

#: Maximum UDP datagram we accept for a probe response.
MAX_DATAGRAM_SIZE = 65535

#: Scope URI pattern used to extract the trailing segment
#: (port of ``Constants.PATTERN`` from the C# library).
SCOPE_PATTERN = (
    r"^((onvif[s]?|ftp):\/)?\/?([^:\/\s]+)((\/\w+)*\/)"
    r"([\w\-\.]+[^#?\s]+)(.*)?(#[\w\-]+)?$"
)
_SCOPE_RE = re.compile(SCOPE_PATTERN)
_HARDWARE_RE = re.compile(r"hardware/(\S+)")


class DiscoveryException(Exception):
    """Raised when no usable UDP discovery client can be created."""


@dataclass
class DiscoveryDevice:
    """A discovered ONVIF device (port of C# ``DiscoveryDevice`` record)."""

    types: list[str] = field(default_factory=list)
    xaddresses: list[str] = field(default_factory=list)
    model: str = ""
    mfr: str = ""
    address: str = ""
    scopes: list[str] = field(default_factory=list)
    endpoint: str = ""
    metadata_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "types": list(self.types),
            "xaddresses": list(self.xaddresses),
            "model": self.model,
            "mfr": self.mfr,
            "address": self.address,
            "scopes": list(self.scopes),
            "endpoint": self.endpoint,
            "metadata_version": self.metadata_version,
        }


def new_message_id() -> str:
    """Return a fresh WS-Discovery message id (uuid4, no urn prefix)."""
    return str(uuid.uuid4())


def _normalize_message_id(message_id: Any) -> str:
    if message_id is None:
        raise ValueError("message_id must not be empty")
    text = str(message_id).strip()
    if not text or text == "00000000-0000-0000-0000-000000000000":
        raise ValueError("message_id must not be empty")
    return text


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v for v in re.split(r"[,\s]+", value.strip()) if v]
    return [str(v).strip() for v in value if str(v).strip()]


def build_probe_message(
    message_id: Any,
    types: Any = None,
    scopes: Any = None,
) -> bytes:
    """Build a SOAP WS-Probe datagram (port of ``WSProbeMessageBuilder``).

    ``types`` defaults to ``dn:NetworkVideoTransmitter``. ``scopes`` is an
    optional scope filter rendered as ``<d:Scopes>`` so callers can narrow
    the probe (e.g. ``"onvif://www.onvif.org/hardware/..."``).
    """
    mid = _normalize_message_id(message_id)
    type_list = _normalize_list(types) or list(DEFAULT_PROBE_TYPES)
    scope_list = _normalize_list(scopes)

    types_xml = " ".join(type_list)
    scopes_xml = ""
    if scope_list:
        # Space-separated scope list, matching the WS-Discovery schema.
        scopes_xml = "<d:Scopes>" + " ".join(scope_list) + "</d:Scopes>"
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
        ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
        ' xmlns:tds="https://www.onvif.org/ver10/device/wsdl/devicemgmt.wsdl"'
        ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        "<e:Header>"
        f"<w:MessageID>uuid:{mid}</w:MessageID>"
        "<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To> "
        "<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
        "</e:Header>"
        "<e:Body>"
        f"<d:Probe><d:Types>{types_xml}</d:Types>{scopes_xml}</d:Probe>"
        "</e:Body>"
        "</e:Envelope>"
    )
    return envelope.encode("ascii", errors="replace")


def split_list(value: Any) -> list[str]:
    """Split a whitespace-separated WS-Discovery list (port of ConvertToList)."""
    if not value:
        return []
    return [part.strip() for part in str(value).split() if part.strip()]


def parse_model_from_scopes(scopes: str) -> str:
    """Extract the hardware model from a scopes string (port of DeviceFactory)."""
    if not scopes:
        return ""
    match = _HARDWARE_RE.search(str(scopes))
    if not match:
        return ""
    return unquote(match.group(1))


def parse_mfr_from_scopes(scopes: str) -> str:
    """Extract the manufacturer from a scopes string (port of DeviceFactory)."""
    if not scopes:
        return ""
    tokens = str(scopes).split()
    mfr_tokens = [
        t for t in tokens if "mfr/" in t or "manufacturer/" in t
    ]
    if mfr_tokens:
        match = _SCOPE_RE.match(mfr_tokens[0])
        if match:
            return unquote(match.group(6))
        return ""
    name_tokens = [t for t in tokens if "name/" in t]
    if not name_tokens:
        return ""
    match = _SCOPE_RE.match(name_tokens[0])
    if not match:
        return ""
    mfr = unquote(match.group(6))
    if " " in mfr:
        mfr = mfr.split()[0]
    return mfr


def _local_name(tag: str) -> str:
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    if ":" in tag:
        return tag.rsplit(":", 1)[-1]
    return tag


def _find_child(parent: ET.Element, name: str) -> Optional[ET.Element]:
    for child in parent:
        if _local_name(child.tag) == name:
            return child
    return None


def _find_children(parent: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in parent if _local_name(c.tag) == name]


def _text(element: Optional[ET.Element]) -> str:
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def _parse_envelope(data: Any) -> Optional[tuple[str, list[dict[str, str]]]]:
    """Parse a probe-response envelope into (relates_to, probe_matches)."""
    if data is None:
        return None
    if isinstance(data, (bytes, bytearray)):
        try:
            text = bytes(data).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None
    else:
        text = str(data)
    start = text.find("<")
    if start > 0:
        text = text[start:]
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    header = _find_child(root, "Header")
    body = _find_child(root, "Body")
    if header is None or body is None:
        return None
    relates_to = _text(_find_child(header, "RelatesTo"))

    probe_matches_el: Optional[ET.Element] = None
    for child in body:
        if _local_name(child.tag) == "ProbeMatches":
            probe_matches_el = child
            break
        # Some devices nest ProbeMatch directly under Body.
        if _local_name(child.tag) == "ProbeMatch":
            probe_matches_el = body
            break
    if probe_matches_el is None:
        return None

    matches: list[dict[str, str]] = []
    for pm in _find_children(probe_matches_el, "ProbeMatch"):
        endpoint_ref = _find_child(pm, "EndpointReference")
        endpoint = ""
        if endpoint_ref is not None:
            endpoint = _text(_find_child(endpoint_ref, "Address"))
        matches.append(
            {
                "types": _text(_find_child(pm, "Types")),
                "scopes": _text(_find_child(pm, "Scopes")),
                "xaddrs": _text(_find_child(pm, "XAddrs")),
                "metadata_version": _text(_find_child(pm, "MetadataVersion")),
                "endpoint": endpoint,
            }
        )
    return relates_to, matches


def create_device(probe_match: dict[str, str], remote_addr: str = "") -> DiscoveryDevice:
    """Build a DiscoveryDevice from a parsed ProbeMatch (port of DeviceFactory)."""
    scopes_raw = probe_match.get("scopes", "")
    return DiscoveryDevice(
        types=split_list(probe_match.get("types", "")),
        xaddresses=split_list(probe_match.get("xaddrs", "")),
        model=parse_model_from_scopes(scopes_raw),
        mfr=parse_mfr_from_scopes(scopes_raw),
        address=str(remote_addr or ""),
        scopes=split_list(scopes_raw),
        endpoint=probe_match.get("endpoint", ""),
        metadata_version=probe_match.get("metadata_version", ""),
    )


def _relates_to_probe(relates_to: str, expected_message_id: Any) -> bool:
    """Mirror C# ``IsFromProbeMessage``: RelatesTo must contain our id."""
    if expected_message_id is None:
        return True
    expected = str(expected_message_id).strip().lower()
    if not expected:
        return True
    return expected in (relates_to or "").lower()


def parse_probe_responses(
    data: Any,
    expected_message_id: Any,
    remote_addr: str = "",
) -> list[DiscoveryDevice]:
    """Parse *all* ProbeMatch entries in a datagram.

    Entries with empty scopes are skipped, mirroring the C# guard that a
    useful ProbeMatch must carry scopes.
    """
    parsed = _parse_envelope(data)
    if parsed is None:
        return []
    relates_to, matches = parsed
    if not _relates_to_probe(relates_to, expected_message_id):
        return []
    devices: list[DiscoveryDevice] = []
    for match in matches:
        if not (match.get("scopes") or "").strip():
            continue
        devices.append(create_device(match, remote_addr))
    return devices


def parse_probe_response(
    data: Any,
    expected_message_id: Any,
    remote_addr: str = "",
) -> Optional[DiscoveryDevice]:
    """Parse the first ProbeMatch in a datagram (mirrors C# behaviour)."""
    devices = parse_probe_responses(data, expected_message_id, remote_addr)
    return devices[0] if devices else None


def merge_device(seen_xaddrs: set[str], device: DiscoveryDevice) -> bool:
    """Duplicate suppression keyed by XAddrs (port of the C# dictionary).

    Returns True when the device is new (and records its XAddrs); a device
    is a duplicate only when *all* of its XAddrs were already seen.
    """
    keys = [x.strip().rstrip("/") for x in device.xaddresses if x.strip()]
    if not keys:
        # Fall back to the sender address so datagrams without XAddrs
        # still deduplicate per sender.
        keys = [device.address.strip()] if device.address.strip() else []
    if keys and all(k.lower() in {s.lower() for s in seen_xaddrs} for k in keys):
        return False
    for key in keys:
        seen_xaddrs.add(key)
    return True


def deduplicate_devices(devices: list[DiscoveryDevice]) -> list[DiscoveryDevice]:
    """Return devices with XAddr duplicates removed, preserving order."""
    seen: set[str] = set()
    unique: list[DiscoveryDevice] = []
    for device in devices:
        if merge_device(seen, device):
            unique.append(device)
    return unique


def get_local_ipv4_addresses() -> list[str]:
    """Best-effort enumeration of local IPv4 addresses for probing.

    Mirrors ``UdpClientFactory`` (Ethernet/WiFi, up, IPv4) but degrades
    gracefully: ``psutil`` is used when available, otherwise hostname
    resolution plus the default-route trick.
    """
    found: list[str] = []

    try:
        import psutil  # type: ignore

        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                if getattr(addr, "family", None) != socket.AF_INET:
                    continue
                ip = getattr(addr, "address", "")
                if not ip or ip.startswith("127.") or ip.startswith("169.254."):
                    continue
                if ip not in found:
                    found.append(ip)
    except Exception:  # noqa: BLE001
        pass
    if found:
        return found

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except Exception:  # noqa: BLE001
        pass

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            ip = probe.getsockname()[0]
            if ip and not ip.startswith("127.") and ip not in found:
                found.append(ip)
        finally:
            probe.close()
    except Exception:  # noqa: BLE001
        pass

    return found


def _create_udp_socket(local_ip: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            # Enable broadcast use, matching UdpClientWrapper behaviour.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        bind_ip = "" if local_ip in ("", "0.0.0.0") else local_ip
        sock.bind((bind_ip, 0))
        try:
            # Keep multicast traffic on the local network segment.
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except OSError:
            pass
        if bind_ip:
            try:
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_MULTICAST_IF,
                    socket.inet_aton(bind_ip),
                )
            except OSError:
                pass
        return sock
    except Exception:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass
        raise


def _validate_timeout(timeout: Any) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"timeout must be a number of seconds: {timeout!r}") from exc
    if not (value > 0) or value > 60:
        raise ValueError("timeout must be within (0, 60] seconds")
    return value


def _discover_sync(
    timeout: float = 4.0,
    types: Any = None,
    scopes: Any = None,
    message_id: Any = None,
    resend_interval: float = PROBE_RESEND_INTERVAL,
) -> list[DiscoveryDevice]:
    """Blocking WS-Discovery scan (one UDP client per local IPv4 address)."""
    timeout = _validate_timeout(timeout)
    try:
        resend_interval = float(resend_interval)
    except (TypeError, ValueError):
        resend_interval = PROBE_RESEND_INTERVAL
    if resend_interval <= 0:
        resend_interval = PROBE_RESEND_INTERVAL

    mid = str(message_id).strip() if message_id else new_message_id()
    _normalize_message_id(mid)
    datagram = build_probe_message(mid, types=types, scopes=scopes)
    multicast = (WS_MULTICAST_ADDRESS, WS_MULTICAST_PORT)

    local_ips = get_local_ipv4_addresses() or ["0.0.0.0"]
    sockets: list[socket.socket] = []
    for ip in local_ips:
        try:
            sockets.append(_create_udp_socket(ip))
        except OSError:
            # Mirror C#: discard interfaces whose client cannot be created.
            continue
    if not sockets:
        raise DiscoveryException(
            "Missing valid NetworkInterfaces, UdpClients could not be created"
        )

    seen: set[str] = set()
    devices: list[DiscoveryDevice] = []
    deadline = time.monotonic() + timeout
    last_send = 0.0
    try:
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                break
            if now - last_send >= resend_interval:
                for sock in sockets:
                    try:
                        sock.sendto(datagram, multicast)
                    except OSError:
                        continue
                last_send = now
            per_recv = min(resend_interval, remaining)
            for sock in sockets:
                try:
                    sock.settimeout(per_recv)
                    payload, remote = sock.recvfrom(MAX_DATAGRAM_SIZE)
                except socket.timeout:
                    continue
                except OSError:
                    # Mirror C#: tolerate malformed datagrams/receive errors.
                    continue
                remote_addr = ""
                try:
                    remote_addr = str(remote[0])
                except Exception:  # noqa: BLE001
                    remote_addr = ""
                for device in parse_probe_responses(payload, mid, remote_addr):
                    if merge_device(seen, device):
                        devices.append(device)
                if time.monotonic() >= deadline:
                    break
    finally:
        for sock in sockets:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass
    return devices


async def discover(
    timeout: float = 4.0,
    types: Any = None,
    scopes: Any = None,
    message_id: Any = None,
) -> list[DiscoveryDevice]:
    """Async WS-Discovery scan; returns deduplicated devices after ``timeout``."""
    return await asyncio.to_thread(
        _discover_sync, timeout, types, scopes, message_id
    )


def discover_sync(
    timeout: float = 4.0,
    types: Any = None,
    scopes: Any = None,
    message_id: Any = None,
) -> list[DiscoveryDevice]:
    """Synchronous WS-Discovery scan (convenience wrapper)."""
    return _discover_sync(timeout, types=types, scopes=scopes, message_id=message_id)


__all__ = [
    "WS_MULTICAST_ADDRESS",
    "WS_MULTICAST_PORT",
    "DEFAULT_PROBE_TYPES",
    "PROBE_RESEND_INTERVAL",
    "DiscoveryDevice",
    "DiscoveryException",
    "new_message_id",
    "build_probe_message",
    "split_list",
    "parse_model_from_scopes",
    "parse_mfr_from_scopes",
    "create_device",
    "parse_probe_response",
    "parse_probe_responses",
    "merge_device",
    "deduplicate_devices",
    "get_local_ipv4_addresses",
    "discover",
    "discover_sync",
]
