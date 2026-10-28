"""Device-based camera name suggestions.

Camera creation requires a manual ``name`` (see ``app.schemas.CameraCreate``)
and onboarding only derives an IP/host via
``app.services.camera_onboarding.derive_ip_address`` — it never invents a
name from ONVIF ``GetDeviceInformation``. This module fills that gap with
pure, offline suggestion logic built from device info
(manufacturer/model/firmware/serial) plus the camera IP.

Auto-registered API lives in ``app.api.name_suggest``; no ``app/main.py``
edit is required.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

MAX_NAME_LENGTH = 120

_PLACEHOLDERS = {"", "unknown", "n/a", "na", "none", "null", "-", "--", "?"}


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Collapse internal whitespace.
    text = " ".join(text.split())
    if text.lower() in _PLACEHOLDERS:
        return None
    return text


def _is_placeholder(value: Any) -> bool:
    return _clean(value) is None


def _coalesce(*values: Any) -> Optional[str]:
    for value in values:
        cleaned = _clean(value)
        if cleaned:
            return cleaned
    return None


def _last_octet(ip: Any) -> Optional[str]:
    if ip is None:
        return None
    text = str(ip).strip()
    if not text:
        return None
    # Strip port/path if present.
    text = text.split("/", 1)[0].strip()
    if ":" in text and "." not in text:
        # IPv6: use last hextet.
        parts = [p for p in text.strip("[]").split(":") if p]
        return parts[-1] if parts else None
    if "." in text:
        tail = text.rsplit(".", 1)[-1].strip().strip("[]")
        # Drop trailing :port.
        if ":" in tail:
            tail = tail.split(":", 1)[0]
        tail = "".join(ch for ch in tail if ch.isalnum())
        return tail or None
    return text or None


def _sanitize_name(name: Any) -> Optional[str]:
    cleaned = _clean(name)
    if not cleaned:
        return None
    if len(cleaned) > MAX_NAME_LENGTH:
        cleaned = cleaned[:MAX_NAME_LENGTH].strip()
    return cleaned or None


def _dedupe(names: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for name in names:
        sanitized = _sanitize_name(name)
        if not sanitized:
            continue
        key = sanitized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(sanitized)
    return out


def normalize_device_info(raw: Any = None, **kwargs: Any) -> Dict[str, Optional[str]]:
    """Normalize ONVIF/discovery device info to canonical keys.

    Accepts the lowercase keys produced by
    ``app.services.onvif_digest_service.get_device_info``
    (``manufacturer``/``model``/``firmwareversion``/``serialnumber``/
    ``hardwareid``) as well as capitalized and alias spellings, plus flat
    kwargs. Returns canonical ``manufacturer``/``model``/``firmware_version``/
    ``serial_number``/``hardware_id``/``device_name``/``ip`` keys.
    """
    data: Dict[str, Any] = {}
    if isinstance(raw, dict):
        data.update(raw)
    elif raw is not None and not isinstance(raw, (str, bytes)):
        # Allow objects with attributes.
        for key in (
            "manufacturer", "Manufacturer", "model", "Model",
            "firmwareversion", "firmware_version", "FirmwareVersion",
            "serialnumber", "serial_number", "SerialNumber",
            "hardwareid", "hardware_id", "HardwareId",
            "device_name", "name", "hostname", "ip", "ip_address",
            "host", "onvif_host", "address",
        ):
            try:
                val = getattr(raw, key, None)
                if val is not None:
                    data[key] = val
            except Exception:  # noqa: BLE001
                continue
    # Flat kwargs override/extend.
    for key, value in kwargs.items():
        if value is not None:
            data[key] = value

    # Case-insensitive lookup.
    lowered: Dict[str, Any] = {}
    for key, value in data.items():
        lowered[str(key).strip().lower()] = value

    def _pick(*keys: str) -> Optional[str]:
        for key in keys:
            if key in lowered and lowered[key] is not None:
                cleaned = _clean(lowered[key])
                if cleaned:
                    return cleaned
        return None

    manufacturer = _pick(
        "manufacturer", "manufacturer_name", "vendor", "brand", "make"
    )
    model = _pick("model", "model_name", "product", "device_model")
    firmware_version = _pick(
        "firmware_version", "firmwareversion", "firmware", "fw",
        "firmware_ver", "version",
    )
    serial_number = _pick(
        "serial_number", "serialnumber", "serial", "sn", "serial_no"
    )
    hardware_id = _pick("hardware_id", "hardwareid", "hardware", "hwid")
    device_name = _pick(
        "device_name", "devicename", "device", "name",
        "hostname", "host_name", "friendly_name", "friendlyname",
    )
    ip = _pick(
        "ip", "ip_address", "ipaddress", "host", "onvif_host",
        "onvifhost", "address",
    )
    return {
        "manufacturer": manufacturer,
        "model": model,
        "firmware_version": firmware_version,
        "serial_number": serial_number,
        "hardware_id": hardware_id,
        "device_name": device_name,
        "ip": ip,
    }


def build_name_suggestions(
    device_info: Any = None,
    ip: Any = None,
    manufacturer: Any = None,
    model: Any = None,
    firmware_version: Any = None,
    serial_number: Any = None,
    hardware_id: Any = None,
    device_name: Any = None,
    name: Any = None,
    **kwargs: Any,
) -> List[str]:
    """Build ordered, device-based camera name suggestions.

    Pure function (no network). Prefers the device's own configured name,
    then ``Manufacturer Model`` combos, then model/IP fallbacks. Generic
    ``Camera {ip}`` is only a last resort so onboarding UIs can offer a
    meaningful default instead of forcing a fully manual name.
    """
    # Allow callers to pass the device dict positionally as any mapping,
    # or to pass flat fields / alias spellings.
    if device_info is not None and not isinstance(device_info, dict):
        # A bare string positional is treated as an IP shorthand.
        if isinstance(device_info, str) and ip is None and _clean(device_info):
            kwargs.setdefault("ip", device_info)
            device_info = None
        else:
            # Object with attributes; normalize handles it.
            pass
    norm = normalize_device_info(
        device_info,
        ip=ip,
        manufacturer=manufacturer,
        model=model,
        firmware_version=firmware_version,
        serial_number=serial_number,
        hardware_id=hardware_id,
        device_name=_coalesce(device_name, name, kwargs.get("deviceName")),
        **kwargs,
    )
    manu = norm.get("manufacturer")
    mod = norm.get("model")
    dev_name = norm.get("device_name")
    host_ip = _coalesce(norm.get("ip"), kwargs.get("ip_address"), kwargs.get("host"))
    octet = _last_octet(host_ip)

    candidates: List[str] = []
    if dev_name:
        candidates.append(dev_name)
        if host_ip:
            candidates.append(f"{dev_name} ({host_ip})")
    if manu and mod:
        candidates.append(f"{manu} {mod}")
        if host_ip:
            candidates.append(f"{manu} {mod} ({host_ip})")
        if octet and host_ip and octet != host_ip:
            candidates.append(f"{manu} {mod} {octet}")
    elif mod:
        candidates.append(mod)
        if host_ip:
            candidates.append(f"{mod} ({host_ip})")
    elif manu:
        candidates.append(f"{manu} Camera")
        if host_ip:
            candidates.append(f"{manu} Camera ({host_ip})")
        if octet and host_ip and octet != host_ip:
            candidates.append(f"{manu} Camera {octet}")
    if host_ip:
        candidates.append(f"Camera {host_ip}")
        if octet and octet != host_ip:
            candidates.append(f"Camera {octet}")
        # ONVIF-flavoured fallback mirrors bulk-import naming.
        candidates.append(f"ONVIF {host_ip}")
    if not candidates:
        candidates = ["Camera"]
    return _dedupe(candidates)


def suggest_camera_name(
    device_info: Any = None,
    ip: Any = None,
    existing_names: Any = None,
    taken_names: Any = None,
    **kwargs: Any,
) -> str:
    """Return the top suggestion, uniquified against existing names."""
    suggestions = build_name_suggestions(device_info, ip=ip, **kwargs)
    primary = suggestions[0] if suggestions else "Camera"
    taken: set[str] = set()
    for source in (existing_names, taken_names, kwargs.get("existing"), kwargs.get("taken")):
        if not source:
            continue
        if isinstance(source, str):
            taken.add(source.strip().lower())
        else:
            try:
                for item in source:
                    if item is not None and str(item).strip():
                        taken.add(str(item).strip().lower())
            except TypeError:
                continue
    if primary.lower() not in taken:
        return primary
    base = primary
    index = 2
    while f"{base} {index}".lower() in taken:
        index += 1
    return f"{base} {index}"


async def fetch_device_info(
    ip: Any = None,
    username: Any = None,
    password: Any = None,
    port: Any = None,
    scheme: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch ONVIF ``GetDeviceInformation`` for a camera (network call)."""
    host = _coalesce(
        ip,
        kwargs.get("ip_address"), kwargs.get("host"),
        kwargs.get("onvif_host"), kwargs.get("address"),
    )
    if not host:
        raise ValueError("Provide 'ip' (camera host) to fetch device info")
    from app.services import onvif_digest_service as ods

    user = "" if username is None else str(username)
    if username is None:
        username = kwargs.get("user", kwargs.get("username_or_empty"))
    pwd = "" if password is None else str(password)
    if password is None:
        pwd = kwargs.get("pass", kwargs.get("pwd", ""))
        user = str(username or "")
        pwd = str(pwd or "")
    else:
        user = str(username or "")
        pwd = str(pwd or "")
    try:
        port_i = int(port) if port is not None else int(kwargs.get("onvif_port") or kwargs.get("control_port") or 80)
    except (TypeError, ValueError):
        port_i = 80
    scheme_s = _clean(scheme or kwargs.get("control_scheme")) or "http"
    info = await ods.get_device_info(str(host).strip(), user, pwd, port_i, scheme_s)
    return dict(info) if isinstance(info, dict) else {}


async def suggest_names_for_device(
    ip: Any = None,
    username: Any = None,
    password: Any = None,
    port: Any = None,
    scheme: Any = None,
    device_info: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch device info (unless supplied) and return name suggestions."""
    info: Dict[str, Any]
    if isinstance(device_info, dict) and device_info:
        info = dict(device_info)
        host = _coalesce(
            ip, kwargs.get("ip_address"), kwargs.get("host"),
            kwargs.get("onvif_host"), info.get("ip"),
        )
    else:
        info = await fetch_device_info(
            ip, username, password, port, scheme, **kwargs
        )
        host = _coalesce(
            ip, kwargs.get("ip_address"), kwargs.get("host"),
            kwargs.get("onvif_host"),
        )
    suggestions = build_name_suggestions(info, ip=host)
    primary = suggest_camera_name(info, ip=host)
    return {
        "device_info": info,
        "ip": host,
        "ip_address": host,
        "suggestions": suggestions,
        "names": suggestions,
        "primary": primary,
        "suggested_name": primary,
        "name": primary,
    }


# ---------------------------------------------------------------------------
# Aliases (repo convention: many names, same behaviour).
# ---------------------------------------------------------------------------
build_suggestions = build_name_suggestions
get_suggestions = build_name_suggestions
suggest_names = build_name_suggestions
suggest_camera_names = build_name_suggestions
get_name_suggestions = build_name_suggestions
build_camera_name_suggestions = build_name_suggestions
device_name_suggestions = build_name_suggestions
suggest_device_names = build_name_suggestions
build_name_suggestion = build_name_suggestions
get_name_suggestion = suggest_camera_name
suggest_name = suggest_camera_name
get_suggested_name = suggest_camera_name
primary_suggestion = suggest_camera_name
best_name = suggest_camera_name
pick_name = suggest_camera_name
normalize_info = normalize_device_info
normalize = normalize_device_info
clean_device_info = normalize_device_info
get_device_info = fetch_device_info
fetch_device_information = fetch_device_info
suggest_for_device = suggest_names_for_device
fetch_name_suggestions = suggest_names_for_device
suggest_names_for_camera = suggest_names_for_device
suggest_camera_name_for_device = suggest_names_for_device
suggest_device_name = suggest_names_for_device

__all__ = [
    "MAX_NAME_LENGTH",
    "normalize_device_info",
    "build_name_suggestions",
    "suggest_camera_name",
    "fetch_device_info",
    "suggest_names_for_device",
]
