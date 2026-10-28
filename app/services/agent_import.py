"""Agent DVR config import service.

Parse Agent DVR-style device configs into VigilCam cameras.

Reference shapes inspected read-only (no vendoring):
``/opt/others/ispy-agent-api/swagger.yaml`` (Agent DVR 8.1.1.0,
``/command/getObject`` returns one device's config JSON including its
name, group, state and settings; ``/command/getObjects`` returns every
device) and ``/opt/others/ispy-agent-docker`` (official Agent DVR image,
no device-schema files — shapes below follow the API field names:
``oid``/``ot`` with ``ot`` 1 = microphone, 2 = camera, plus
``name``/``group``/``location``/``enabled``/``settings``).

Only cameras (``ot=2`` or no ``ot``) are imported; microphones are
skipped. Helpers are pure except the ``*_db`` functions.

No ``app/main.py`` changes are required: the API router is auto-loaded.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

AGENT_VERSION = "8.1.1.0"
AGENT_KIND = "agent-dvr"

VALID_KINDS = {"usb", "ip", "onvif"}

# ot values per Agent DVR local API: 1 = microphone, 2 = camera.
OT_MICROPHONE = 1
OT_CAMERA = 2

# Envelope keys that may hold a device list (case-insensitive lookup).
_ENVELOPE_LIST_KEYS = (
    "devices",
    "cameras",
    "objects",
    "mediaobjects",
    "media_objects",
    "agentdevices",
    "agent_devices",
    "channels",
    "sources",
    "items",
    "entries",
    "results",
    "data",
    "config",
    "configuration",
)

# Single-text keys that may hold an embedded JSON document.
_ENVELOPE_TEXT_KEYS = (
    "agent_json",
    "agentjson",
    "agent_config",
    "agentconfig",
    "config_json",
    "configjson",
    "json",
    "content",
    "text",
    "body",
    "data_text",
)

# Keys tried (in order) when hunting for a stream URL.
_URL_KEYS = (
    "videosource",
    "video_source",
    "mainurl",
    "main_url",
    "streamurl",
    "stream_url",
    "rtspurl",
    "rtsp_url",
    "onvifurl",
    "onvif_url",
    "url",
    "source",
    "stream",
    "connectionstring",
    "connection_string",
    "inputurl",
    "input_url",
    "input",
    "uri",
    "endpoint",
    "address",
    "path",
    "src",
)

# Nested dicts searched for URL/credential/host fields.
_NESTED_KEYS = (
    "settings",
    "video",
    "videosettings",
    "video_settings",
    "source",
    "connection",
    "stream",
    "streams",
    "device",
    "config",
    "camera",
    "media",
    "input",
    "recorder",
    "detector",
)

_CRED_USER_KEYS = ("username", "user", "login", "authuser", "auth_user")
_CRED_PASS_KEYS = ("password", "pass", "passwd", "pwd", "authpass", "auth_pass")
_HOST_KEYS = (
    "ip",
    "ipaddress",
    "ip_address",
    "host",
    "hostname",
    "onvifhost",
    "onvif_host",
    "onvifip",
    "onvif_ip",
    "server",
    "deviceip",
    "device_ip",
)
_PORT_KEYS = ("onvifport", "onvif_port", "port", "httpport", "http_port", "rtspport", "rtsp_port")
_NAME_KEYS = ("name", "devicename", "device_name", "cameraname", "camera_name", "title", "label")
_GROUP_KEYS = ("group", "groupname", "group_name", "category")
_LOCATION_KEYS = ("location", "area", "zone", "site")
_MANUFACTURER_KEYS = ("manufacturer", "vendor", "make", "brand")
_MODEL_KEYS = ("model", "modelname", "model_name", "product", "devicemodel", "device_model", "hardware")


def _norm_key(key: Any) -> str:
    return str(key or "").strip().lower().replace("-", "_").replace(" ", "_")


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if not s:
        return None
    if s in ("1", "true", "t", "yes", "y", "on", "enabled", "enable", "active", "online", "ok"):
        return True
    if s in ("0", "false", "f", "no", "n", "off", "disabled", "disable", "inactive", "stopped"):
        return False
    return None


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(float(s)) if "." in s else int(s)
    except Exception:
        return None


def _get_ci(mapping: Any, *names: str) -> Any:
    """Case-insensitive dict lookup; returns None when absent."""
    if not isinstance(mapping, dict):
        return None
    lowered = {_norm_key(k): v for k, v in mapping.items()}
    for name in names:
        key = _norm_key(name)
        if key in lowered:
            return lowered[key]
    return None


def _find_first(mapping: Any, keys: Tuple[str, ...]) -> Any:
    """Find first non-empty value for keys at top level, then one nested level."""
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        val = _get_ci(mapping, key)
        if val is not None and (not isinstance(val, str) or val.strip()):
            if isinstance(val, (dict, list)):
                continue
            return val
    for nested in _NESTED_KEYS:
        sub = _get_ci(mapping, nested)
        if isinstance(sub, dict):
            for key in keys:
                val = _get_ci(sub, key)
                if val is not None and (not isinstance(val, str) or val.strip()):
                    if isinstance(val, (dict, list)):
                        continue
                    return val
    return None


def _iter_candidate_urls(device: Dict[str, Any]) -> List[str]:
    """Collect candidate URL strings from a device dict (ordered, de-duplicated)."""
    found: List[str] = []
    seen: set = set()

    def _push(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                _push(item)
            return
        if isinstance(value, dict):
            for key in _URL_KEYS:
                _push(_get_ci(value, key))
            # Stream entries like {"url": ..., "main": ...}.
            for key in ("main", "sub", "low", "high", "primary"):
                _push(_get_ci(value, key))
            return
        if isinstance(value, str):
            s = value.strip()
            if s and s not in seen:
                seen.add(s)
                found.append(s)

    for key in _URL_KEYS:
        _push(_get_ci(device, key))
    for nested in _NESTED_KEYS:
        _push(_get_ci(device, nested))
    return found


def _looks_like_url(text: str) -> bool:
    s = text.strip().lower()
    return (
        "://" in s
        or s.startswith("/dev/")
        or s.startswith("usb")
        or s.startswith("rtsp")
        or s.startswith("rtmp")
        or s.startswith("http")
        or s.startswith("onvif")
        or s.startswith("ffmpeg")
    )


def extract_stream_url(device: Any) -> Optional[str]:
    """Return the best stream URL for an Agent DVR device dict."""
    if not isinstance(device, dict):
        return None
    for cand in _iter_candidate_urls(device):
        if _looks_like_url(cand):
            return cand.strip()
    # Accept any non-empty candidate as a last resort (e.g. plain host:port/path).
    cands = _iter_candidate_urls(device)
    return cands[0].strip() if cands else None


def extract_host(device: Any, url: Optional[str] = None) -> Optional[str]:
    """Extract a host/IP from the URL, else from known host fields."""
    if url:
        try:
            parsed = urlparse(url if "://" in url else f"rtsp://{url}")
            if parsed.hostname:
                return parsed.hostname.strip()
        except Exception:
            pass
    if isinstance(device, dict):
        val = _find_first(device, _HOST_KEYS)
        if val is not None and str(val).strip():
            text = str(val).strip()
            if "://" in text:
                try:
                    parsed = urlparse(text)
                    if parsed.hostname:
                        return parsed.hostname.strip()
                except Exception:
                    pass
            return text.split("/", 1)[0].split(":", 1)[0].strip() or None
    return None


def extract_port(device: Any, url: Optional[str] = None) -> Optional[int]:
    """Extract an ONVIF/connection port from explicit fields, else ONVIF URL port."""
    if isinstance(device, dict):
        # Prefer explicit ONVIF port fields over generic port fields.
        for key in ("onvifport", "onvif_port"):
            val = _find_first(device, (key,))
            coerced = _coerce_int(val)
            if coerced is not None and 0 < coerced < 65536:
                return coerced
        # Generic port only counts when the device is ONVIF-flavoured.
        hint = ""
        try:
            hint = f"{_find_first(device, ('kind', 'devicetype', 'device_type', 'type', 'sourcetype', 'source_type', 'device')) or ''} {url or ''}".lower()
        except Exception:
            hint = str(url or "").lower()
        if "onvif" in hint:
            for key in ("port", "httpport", "http_port", "rtspport", "rtsp_port"):
                val = _find_first(device, (key,))
                coerced = _coerce_int(val)
                if coerced is not None and 0 < coerced < 65536:
                    return coerced
    if url and str(url).strip().lower().startswith("onvif://"):
        try:
            parsed = urlparse(url)
            if parsed.port:
                return int(parsed.port)
        except Exception:
            return None
    return None


def extract_credentials(device: Any, url: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """Return (username, password) from fields, falling back to URL userinfo."""
    user = _clean_str(_find_first(device, _CRED_USER_KEYS)) if isinstance(device, dict) else None
    pwd = _clean_str(_find_first(device, _CRED_PASS_KEYS)) if isinstance(device, dict) else None
    if (not user or not pwd) and url and "://" in url:
        try:
            parsed = urlparse(url)
            if not user and parsed.username:
                user = str(parsed.username).strip() or None
            if not pwd and parsed.password:
                pwd = str(parsed.password).strip() or None
        except Exception:
            pass
    return user, pwd


def extract_ot(device: Any) -> Optional[int]:
    """Return the Agent object-type id (1 = mic, 2 = camera) when present."""
    if not isinstance(device, dict):
        return None
    for key in ("ot", "objecttype", "object_type", "objecttypeid", "object_type_id", "typeid", "type_id"):
        val = _get_ci(device, key)
        coerced = _coerce_int(val)
        if coerced is not None:
            return coerced
    # Some exports use a string type like "camera"/"microphone".
    for key in ("type", "devicetype", "device_type", "sourcetype", "source_type", "kind", "objectkind"):
        val = _get_ci(device, key)
        if isinstance(val, str):
            s = val.strip().lower()
            if s in ("1", "mic", "microphone", "audio"):
                return OT_MICROPHONE
            if s in ("2", "cam", "camera", "video"):
                return OT_CAMERA
    return None


def is_microphone(device: Any) -> bool:
    """Return True when the device is explicitly an Agent microphone."""
    return extract_ot(device) == OT_MICROPHONE


def extract_oid(device: Any) -> Optional[int]:
    if not isinstance(device, dict):
        return None
    for key in ("oid", "id", "deviceid", "device_id", "objectid", "object_id", "index"):
        val = _get_ci(device, key)
        coerced = _coerce_int(val)
        if coerced is not None:
            return coerced
    return None


def extract_name(device: Dict[str, Any], index: int = 0, oid: Optional[int] = None) -> str:
    for key in _NAME_KEYS:
        val = _clean_str(_get_ci(device, key))
        if val:
            return val
    if oid is not None:
        return f"Agent Camera {oid}"
    return f"Agent Camera {index + 1}"


def extract_group(device: Dict[str, Any]) -> Optional[str]:
    for key in _GROUP_KEYS:
        val = _get_ci(device, key)
        if isinstance(val, (list, tuple)):
            for item in val:
                s = _clean_str(item)
                if s:
                    return s
        else:
            s = _clean_str(val)
            if s:
                return s
    for key in _LOCATION_KEYS:
        s = _clean_str(_get_ci(device, key))
        if isinstance(s, str) and s:
            # Nested location dicts like {"name": "..."}.
            pass
        if s:
            return s
    nested = _get_ci(device, "location")
    if isinstance(nested, dict):
        for key in ("name", "title", "label"):
            s = _clean_str(_get_ci(nested, key))
            if s:
                return s
    return None


def extract_enabled(device: Dict[str, Any]) -> bool:
    for key in ("enabled", "active", "online"):
        val = _get_ci(device, key)
        if val is not None:
            coerced = _coerce_bool(val)
            if coerced is not None:
                return coerced
    state = _clean_str(_get_ci(device, "state") if isinstance(device, dict) else None)
    if state and state.lower() in ("off", "disabled", "inactive", "stopped", "disconnected"):
        return False
    nested = _get_ci(device, "settings") if isinstance(device, dict) else None
    if isinstance(nested, dict):
        for key in ("enabled", "active"):
            coerced = _coerce_bool(_get_ci(nested, key))
            if coerced is not None:
                return coerced
    return True


def _extract_flag(device: Dict[str, Any], *keys: str, default: bool = True) -> bool:
    if isinstance(device, dict):
        for key in keys:
            val = _find_first(device, (key,))
            if val is not None:
                coerced = _coerce_bool(val)
                if coerced is not None:
                    return coerced
        for section in ("recorder", "detector", "alerts", "settings"):
            sub = _get_ci(device, section)
            if isinstance(sub, dict):
                for key in keys:
                    coerced = _coerce_bool(_get_ci(sub, key))
                    if coerced is not None:
                        return coerced
    return default


def infer_kind(device: Dict[str, Any], url: Optional[str]) -> str:
    explicit = _clean_str(_find_first(device, ("kind", "devicetype", "device_type", "type", "sourcetype", "source_type", "device")))
    hay = f"{explicit or ''} {url or ''}".lower()
    if "onvif" in hay:
        return "onvif"
    if "usb" in hay or (url or "").strip().lower().startswith("/dev/"):
        return "usb"
    if explicit and explicit.strip().lower() in VALID_KINDS:
        return explicit.strip().lower()
    return "ip"


def normalize_agent_device(device: Any, index: int = 0) -> Dict[str, Any]:
    """Normalize one Agent DVR device dict to canonical camera fields."""
    if not isinstance(device, dict):
        return {}
    oid = extract_oid(device)
    url = extract_stream_url(device)
    host = extract_host(device, url)
    port = extract_port(device, url)
    user, pwd = extract_credentials(device, url)
    source = url
    if not source and host:
        # Synthesize a usable source from host/type when Agent only stored an IP.
        kind_hint = infer_kind(device, None)
        if kind_hint == "onvif":
            source = f"onvif://{host}:{port or 80}"
        elif kind_hint == "usb":
            source = host if host.startswith("/dev/") else f"usb://{host}"
        else:
            source = f"rtsp://{host}:554/live"
    kind = infer_kind(device, source)
    normalized: Dict[str, Any] = {
        "name": extract_name(device, index, oid),
        "kind": kind,
        "source": source,
        "username": user,
        "password": pwd,
        "onvif_host": host,
        "ip_address": host,
        "onvif_port": port,
        "enabled": extract_enabled(device),
        "recording_enabled": _extract_flag(
            device, "recording", "recording_enabled", "record", "recordondetect",
            "record_on_detect", "recordonalert", "record_on_alert", default=True,
        ),
        "detection_enabled": _extract_flag(
            device, "detection", "detection_enabled", "detect", "detector",
            "motiondetector", "motion_detector", "detect_enabled", default=True,
        ),
        "group": extract_group(device),
        "manufacturer": _clean_str(_find_first(device, _MANUFACTURER_KEYS)),
        "model": _clean_str(_find_first(device, _MODEL_KEYS)),
        "agent_oid": oid,
        "agent_ot": extract_ot(device),
        "agent_microphone": is_microphone(device),
    }
    # Resolution hints when present.
    for key, canon in (("width", "width"), ("height", "height"), ("fps", "fps"), ("framerate", "fps"), ("frame_rate", "fps")):
        val = _find_first(device, (key,))
        coerced = _coerce_int(val)
        if coerced is not None:
            normalized[canon] = coerced
    # Drop Nones so model defaults apply cleanly.
    return {k: v for k, v in normalized.items() if v is not None}


def _looks_like_device(mapping: Dict[str, Any]) -> bool:
    if not isinstance(mapping, dict):
        return False
    keys = {_norm_key(k) for k in mapping.keys()}
    if keys & {_norm_key(k) for k in _ENVELOPE_LIST_KEYS}:
        # Has an envelope list key with a list value -> not a single device.
        for key in _ENVELOPE_LIST_KEYS:
            val = _get_ci(mapping, key)
            if isinstance(val, list):
                return False
    deviceish = {_norm_key(k) for k in (
        "name", "devicename", "cameraname", "title", "oid", "id", "deviceid",
        "settings", "videosource", "video_source", "url", "source", "stream_url",
        "connectionstring", "ot", "objecttype",
    )}
    return bool(keys & deviceish)


def extract_devices(payload: Any) -> List[Dict[str, Any]]:
    """Extract raw Agent DVR device dicts from flexible import payloads.

    Accepts a device list, a single device dict, ``getObjects``-style
    ``{"devices": [...]}`` envelopes, ``{"MediaObjects": [...]}`` config
    exports, or a JSON document string/bytes.
    """
    if payload is None:
        return []
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8-sig", "ignore")
        except Exception:
            return []
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except Exception:
            return []
    if hasattr(payload, "read"):
        try:
            return extract_devices(payload.read())
        except Exception:
            return []
    if hasattr(payload, "model_dump"):
        try:
            payload = payload.model_dump()
        except Exception:
            pass
    if isinstance(payload, list):
        out: List[Dict[str, Any]] = []
        for item in payload:
            if isinstance(item, dict):
                out.append(dict(item))
            elif isinstance(item, str):
                out.extend(extract_devices(item))
        return out
    if isinstance(payload, tuple):
        return extract_devices(list(payload))
    if isinstance(payload, dict):
        # Envelope with a device list.
        for key in _ENVELOPE_LIST_KEYS:
            val = _get_ci(payload, key)
            if isinstance(val, list):
                out = []
                for item in val:
                    if isinstance(item, dict):
                        out.append(dict(item))
                    elif isinstance(item, str):
                        out.extend(extract_devices(item))
                if out:
                    return out
            elif isinstance(val, dict) and val:
                # {"config": {"devices": [...]}} or dict-of-devices.
                nested = extract_devices(val)
                if nested:
                    return nested
                if all(isinstance(v, dict) for v in val.values()):
                    return [dict(v) for v in val.values()]
        # Embedded JSON text.
        for key in _ENVELOPE_TEXT_KEYS:
            val = _get_ci(payload, key)
            if isinstance(val, (str, bytes, bytearray)) and str(val).strip():
                nested = extract_devices(val)
                if nested:
                    return nested
            elif isinstance(val, (list, dict)):
                nested = extract_devices(val)
                if nested:
                    return nested
        # Rows-style envelopes shared with other importers.
        for key in ("rows", "cameras", "records"):
            val = _get_ci(payload, key)
            if isinstance(val, list) and val and all(isinstance(v, dict) for v in val):
                return [dict(v) for v in val]
        if _looks_like_device(payload):
            return [dict(payload)]
        # Dict-of-devices fallback: {"1": {...}, "2": {...}}.
        values = list(payload.values())
        if values and all(isinstance(v, dict) for v in values):
            return [dict(v) for v in values]
        return []
    return []


def normalize_devices(payload: Any) -> List[Dict[str, Any]]:
    """Extract + normalize Agent DVR devices to canonical camera dicts."""
    raws = extract_devices(payload)
    return [normalize_agent_device(raw, i) for i, raw in enumerate(raws)]


def validate_device(device: Any) -> bool:
    """Return True when a (possibly raw) Agent device has name + source."""
    if device is None:
        return False
    data = normalize_agent_device(device) if not _is_normalized(device) else device
    if data.get("agent_microphone"):
        return False
    name = data.get("name")
    source = data.get("source")
    if not name or not str(name).strip():
        return False
    if not source or not str(source).strip():
        return False
    kind = str(data.get("kind") or "ip").strip().lower()
    return kind in VALID_KINDS


def _is_normalized(device: Any) -> bool:
    return isinstance(device, dict) and "agent_oid" in device and "source" in device


def validate_devices(payload: Any) -> List[bool]:
    return [validate_device(d) for d in extract_devices(payload)]


def is_valid_device(device: Any) -> bool:
    return validate_device(device)


def device_to_camera_payload(device: Any, **defaults: Any) -> Any:
    """Build a CameraCreate payload from an Agent DVR device dict."""
    from app.schemas import CameraCreate

    data = normalize_agent_device(device) if not _is_normalized(device) else dict(device)
    payload: Dict[str, Any] = {
        "name": data.get("name"),
        "kind": data.get("kind") or "ip",
        "source": data.get("source"),
    }
    for key in ("username", "password", "onvif_host", "ip_address", "onvif_port",
                "enabled", "recording_enabled", "detection_enabled", "group",
                "manufacturer", "model", "width", "height", "fps"):
        if data.get(key) is not None:
            payload[key] = data[key]
    for key, value in (defaults or {}).items():
        if value is not None:
            payload[key] = value
    if payload.get("username") is None:
        payload.pop("username", None)
    if payload.get("password") is None:
        payload.pop("password", None)
    if payload.get("onvif_port") is None:
        payload.pop("onvif_port", None)
    return CameraCreate(**payload)


def devices_to_camera_payloads(payload: Any, **defaults: Any) -> List[Any]:
    out = []
    for raw in extract_devices(payload):
        norm = normalize_agent_device(raw)
        if norm.get("agent_microphone"):
            continue
        if not validate_device(norm):
            continue
        out.append(device_to_camera_payload(norm, **defaults))
    return out


def import_agent_data(payload: Any, skip_invalid: bool = True, skip_microphones: bool = True,
                       **defaults: Any) -> List[Dict[str, Any]]:
    """Pure import: validate + normalize payload to camera payload dicts."""
    raws = extract_devices(payload)
    out: List[Dict[str, Any]] = []
    for index, raw in enumerate(raws):
        norm = normalize_agent_device(raw, index)
        if skip_microphones and norm.get("agent_microphone"):
            continue
        if validate_device(norm):
            data = {k: v for k, v in norm.items() if v is not None and not k.startswith("agent_")}
            for key, value in (defaults or {}).items():
                if value is not None:
                    data[key] = value
            out.append(data)
        elif not skip_invalid:
            data = {k: v for k, v in norm.items() if v is not None and not k.startswith("agent_")}
            out.append(data)
    return out


# ---------------------------------------------------------------------------
# DB-backed helpers
# ---------------------------------------------------------------------------

async def import_agent_db(
    db: Any,
    payload: Any,
    skip_invalid: bool = True,
    update_existing: bool = False,
    skip_microphones: bool = True,
    **defaults: Any,
) -> Dict[str, Any]:
    """Import Agent DVR devices into the DB, creating Camera rows.

    Returns ``{"imported": [...], "count": n, "errors": [...], "skipped": [...]}``.
    Microphones (``ot=1``) land in ``skipped`` with reason ``microphone``.
    """
    from app.models.entities import Camera as _Camera

    raws = extract_devices(payload)
    imported: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for index, raw in enumerate(raws):
        norm = normalize_agent_device(raw, index)
        if skip_microphones and norm.get("agent_microphone"):
            skipped.append({"index": index, "reason": "microphone", "device": raw})
            continue
        if not validate_device(norm):
            if skip_invalid:
                errors.append({"index": index, "error": "invalid device: 'name' and 'source' are required", "device": raw})
                continue
        try:
            data = {k: v for k, v in norm.items() if v is not None and not k.startswith("agent_")}
            for key, value in (defaults or {}).items():
                if value is not None:
                    data[key] = value
            data.pop("id", None)
            allowed = {"name", "kind", "source", "username", "password", "onvif_host",
                       "ip_address", "onvif_port", "enabled", "recording_enabled",
                       "detection_enabled", "group", "manufacturer", "model",
                       "width", "height", "fps"}
            existing = None
            if update_existing and data.get("name"):
                try:
                    from sqlalchemy import select as _select

                    res = await db.execute(_select(_Camera).where(_Camera.name == data["name"]))
                    existing = res.scalars().first()
                except Exception:
                    existing = None
            if existing is not None:
                for key, value in data.items():
                    if hasattr(existing, key) and value is not None:
                        setattr(existing, key, value)
                await db.commit()
                try:
                    await db.refresh(existing)
                except Exception:
                    pass
                row = {c: getattr(existing, c, None) for c in
                       ("name", "kind", "source", "username", "onvif_host", "ip_address",
                        "onvif_port", "enabled", "group")}
                try:
                    row["id"] = getattr(existing, "id", None)
                except Exception:
                    pass
                imported.append(row)
            else:
                camera = _Camera(**{k: v for k, v in data.items() if k in allowed})
                if not getattr(camera, "kind", None):
                    camera.kind = "ip"
                db.add(camera)
                await db.commit()
                try:
                    await db.refresh(camera)
                except Exception:
                    pass
                row = {c: getattr(camera, c, None) for c in
                       ("name", "kind", "source", "username", "onvif_host", "ip_address",
                        "onvif_port", "enabled", "group")}
                try:
                    row["id"] = getattr(camera, "id", None)
                except Exception:
                    pass
                imported.append(row)
        except Exception as exc:
            try:
                await db.rollback()
            except Exception:
                pass
            errors.append({"index": index, "error": str(exc)})
    return {"imported": imported, "count": len(imported), "created": imported,
            "errors": errors, "total": len(raws), "total_errors": len(errors),
            "skipped": skipped, "total_skipped": len(skipped)}


async def preview_agent_db(db: Any, payload: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Parse Agent devices and annotate validity (+ already_added when names match)."""
    raws = extract_devices(payload)
    existing_names: set = set()
    try:
        from sqlalchemy import select as _select

        from app.models.entities import Camera as _Camera

        result = await db.execute(_select(_Camera.name))
        for (name,) in result.all():
            if name:
                existing_names.add(str(name).strip().lower())
    except Exception:
        existing_names = set()
    out: List[Dict[str, Any]] = []
    for index, raw in enumerate(raws):
        norm = normalize_agent_device(raw, index)
        valid = validate_device(norm)
        name = str(norm.get("name") or "").strip().lower()
        already = bool(name and name in existing_names)
        entry = dict(norm)
        entry["valid"] = valid
        entry["already_added"] = already
        entry["new"] = valid and not already
        out.append(entry)
    return out


class AgentImportService:
    """Object-oriented facade around the module-level helpers."""

    def extract(self, payload: Any) -> List[Dict[str, Any]]:
        return extract_devices(payload)

    def normalize(self, device: Any, index: int = 0) -> Dict[str, Any]:
        return normalize_agent_device(device, index)

    def validate(self, device: Any) -> bool:
        return validate_device(device)

    def to_payload(self, device: Any, **defaults: Any) -> Any:
        return device_to_camera_payload(device, **defaults)

    def prepare_import(self, payload: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return import_agent_data(payload, **kwargs)

    async def import_db(self, db: Any, payload: Any, **kwargs: Any) -> Dict[str, Any]:
        return await import_agent_db(db, payload, **kwargs)

    async def preview_db(self, db: Any, payload: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return await preview_agent_db(db, payload, **kwargs)


agent_import_service = AgentImportService()
service = agent_import_service
manager = agent_import_service


# Compatibility aliases for varied caller naming.
parse = extract_devices
loads = extract_devices
from_json = extract_devices
from_text = extract_devices
load_text = extract_devices
parse_text = extract_devices
parse_string = extract_devices
extract = extract_devices
get_devices = extract_devices
fetch_devices = extract_devices
load_devices = extract_devices
parse_devices = extract_devices
parse_import_payload = extract_devices
to_list = extract_devices
to_dict_list = extract_devices
normalize = normalize_agent_device
normalize_agent_row = normalize_agent_device
normalize_agent_rows = normalize_devices
validate = validate_device
is_valid = validate_device
validate_agent_device = validate_device
validate_agent_devices = validate_devices
is_valid_device = validate_device
device_to_payload = device_to_camera_payload
agent_device_to_camera_payload = device_to_camera_payload
to_camera_payload = device_to_camera_payload
to_camera_payloads = devices_to_camera_payloads
to_payloads = devices_to_camera_payloads
prepare_import = import_agent_data
import_data = import_agent_data
import_rows = import_agent_data
import_devices = import_agent_db
bulk_import_agent = import_agent_db
import_to_db = import_agent_db
import_into_db = import_agent_db
import_db = import_agent_db
create_from_agent = import_agent_db
import_cameras = import_agent_db
import_cameras_agent = import_agent_db
export_db = None
preview = preview_agent_db
preview_import = preview_agent_db
preview_agent = preview_agent_db


__all__ = [
    "AGENT_VERSION",
    "AGENT_KIND",
    "VALID_KINDS",
    "OT_MICROPHONE",
    "OT_CAMERA",
    "extract_stream_url",
    "extract_host",
    "extract_port",
    "extract_credentials",
    "extract_ot",
    "extract_oid",
    "is_microphone",
    "extract_name",
    "extract_group",
    "extract_enabled",
    "infer_kind",
    "normalize_agent_device",
    "extract_devices",
    "normalize_devices",
    "validate_device",
    "validate_devices",
    "is_valid_device",
    "device_to_camera_payload",
    "devices_to_camera_payloads",
    "import_agent_data",
    "import_agent_db",
    "preview_agent_db",
    "AgentImportService",
    "agent_import_service",
    "service",
]
