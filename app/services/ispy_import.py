"""Classic iSpy config import service.

Parse classic iSpy ``objects.xml`` camera definitions into VigilCam cameras.

Reference shapes inspected read-only (no vendoring):

* ``/opt/others/ispy/XML/objects.xsd`` — root ``<objects>`` with
  ``<cameras><camera .../>`` entries. Each ``<camera>`` carries attributes
  (``id``, ``name``, ``description``, ``directory``, ``flipx``/``flipy``,
  ``rotate90``, ``ptz``, ``resolution``, ``deleted``, ...) and children
  ``<settings>`` (``videosourcestring``, ``sourceindex``, ``login``,
  ``password``, ``active``, ``framerate``/``maxframerate``, ``ffmpeg``,
  ``vlcargs``, ``onvifident``, ``accessgroups``, ...), ``<detector>``
  (``type``, ``recordondetect``/``recordonalert``, ``motionzones``),
  ``<recorder>``, ``<alerts>``, ``<schedule>``, ...
* ``/opt/others/ispy/VideoSource.cs`` — ``sourceindex`` meanings:
  0 = JPEG URL, 1 = MJPEG URL, 2 = FFMPEG/file/RTSP, 3 = local USB device
  (``videosourcestring`` is a device moniker), 4 = desktop/screen,
  5 = VLC URL, 6 = Ximea, 7 = Kinect, 8 = custom, 9 = ONVIF
  (``videosourcestring`` is the ONVIF device-service URL, also stored as
  ``settings/onvifident``; ``ptz`` is ``-5``), 10 = clone of another
  camera (``videosourcestring`` is the source camera id).
* ``/opt/others/ispy/XML/objects.cs`` — xsd-generated bindings confirming
  the same element/attribute names.

Only ``<camera>`` entries are imported; ``<microphone>`` entries are
ignored (microphones are out of scope for camera import). Clone entries
(``sourceindex=10``) reference another camera instead of a stream and are
skipped. Entries with ``deleted="true"`` are skipped.

This is intentionally separate from ``app.services.agent_import``: that
module targets the Agent DVR JSON API (``ot``/``oid`` device dicts,
``getObjects``/``getObject``), a different format from classic iSpy XML.

No ``app/main.py`` changes are required: the API router is auto-loaded.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


ISPY_KIND = "ispy-classic"

VALID_KINDS = {"usb", "ip", "onvif"}

# sourceindex values per VideoSource.cs (see module docstring).
SOURCE_JPEG = 0
SOURCE_MJPEG = 1
SOURCE_FFMPEG = 2
SOURCE_USB = 3
SOURCE_DESKTOP = 4
SOURCE_VLC = 5
SOURCE_XIMEA = 6
SOURCE_KINECT = 7
SOURCE_CUSTOM = 8
SOURCE_ONVIF = 9
SOURCE_CLONE = 10

# Sources with no independently usable stream for VigilCam.
_NON_STREAM_SOURCES = {SOURCE_XIMEA, SOURCE_KINECT, SOURCE_CLONE}

# Envelope keys that may hold an XML document or a camera list
# (case-insensitive lookup).
_ENVELOPE_TEXT_KEYS = (
    "xml",
    "ispy_xml",
    "ispyxml",
    "objects_xml",
    "objectsxml",
    "config_xml",
    "configxml",
    "content",
    "text",
    "body",
    "data_text",
)

_ENVELOPE_LIST_KEYS = (
    "cameras",
    "objects",
    "devices",
    "channels",
    "sources",
    "items",
    "entries",
    "results",
    "data",
    "rows",
    "records",
)


def _norm_key(key: Any) -> str:
    return str(key or "").strip().lower().replace("-", "_").replace(" ", "_")


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
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
    if s in ("1", "true", "t", "yes", "y", "on", "enabled", "enable",
             "active", "online", "ok"):
        return True
    if s in ("0", "false", "f", "no", "n", "off", "disabled", "disable",
             "inactive", "stopped"):
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


def _child_text(elem: Optional[ET.Element], *names: str) -> Optional[str]:
    """Return stripped text of the first matching child element."""
    if elem is None:
        return None
    for child in elem:
        tag = _norm_key(child.tag.split("}")[-1])
        for name in names:
            if tag == _norm_key(name) and child.text is not None:
                text = child.text.strip()
                if text:
                    return text
    return None


def _find_child(elem: Optional[ET.Element], *names: str) -> Optional[ET.Element]:
    if elem is None:
        return None
    wanted = {_norm_key(n) for n in names}
    for child in elem:
        if _norm_key(child.tag.split("}")[-1]) in wanted:
            return child
    return None


def _camera_elem_to_dict(elem: ET.Element) -> Dict[str, Any]:
    """Convert one <camera> element to a raw dict of attributes/sections."""
    raw: Dict[str, Any] = {"_tag": "camera"}
    for key, value in elem.attrib.items():
        raw[_norm_key(key)] = value
    settings = _find_child(elem, "settings")
    if settings is not None:
        section: Dict[str, Any] = {}
        for key, value in settings.attrib.items():
            section[_norm_key(key)] = value
        for child in settings:
            tag = _norm_key(child.tag.split("}")[-1])
            if len(child):
                # Nested blocks such as <onvif><rtspport>.
                nested: Dict[str, Any] = {}
                for sub in child:
                    if sub.text is not None and sub.text.strip():
                        nested[_norm_key(sub.tag.split("}")[-1])] = sub.text.strip()
                if child.attrib and not nested:
                    nested = { _norm_key(k): v for k, v in child.attrib.items()}
                section[tag] = nested or None
            elif child.text is not None and child.text.strip():
                section[tag] = child.text.strip()
            elif child.attrib:
                section[tag] = {_norm_key(k): v for k, v in child.attrib.items()}
        raw["settings"] = section
    detector = _find_child(elem, "detector")
    if detector is not None:
        section = {_norm_key(k): v for k, v in detector.attrib.items()}
        zones = _find_child(detector, "motionzones")
        if zones is not None:
            section["motionzones"] = [
                {_norm_key(k): v for k, v in z.attrib.items()}
                for z in zones
                if _norm_key(z.tag.split("}")[-1]) == "zone"
            ]
        raw["detector"] = section
    recorder = _find_child(elem, "recorder")
    if recorder is not None:
        raw["recorder"] = {_norm_key(k): v for k, v in recorder.attrib.items()}
        for child in recorder:
            tag = _norm_key(child.tag.split("}")[-1])
            if child.text is not None and child.text.strip():
                raw["recorder"][tag] = child.text.strip()
    alerts = _find_child(elem, "alerts")
    if alerts is not None:
        section = {_norm_key(k): v for k, v in alerts.attrib.items()}
        for child in alerts:
            tag = _norm_key(child.tag.split("}")[-1])
            if child.text is not None and child.text.strip():
                section[tag] = child.text.strip()
        raw["alerts"] = section
    schedule = _find_child(elem, "schedule")
    if schedule is not None:
        raw["schedule"] = {_norm_key(k): v for k, v in schedule.attrib.items()}
    return raw


def parse_ispy_xml(text: Any) -> List[Dict[str, Any]]:
    """Parse classic iSpy ``objects.xml`` text into raw camera dicts."""
    if text is None:
        return []
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8-sig", "ignore")
        except Exception:
            return []
    if not isinstance(text, str):
        return []
    s = text.strip()
    if not s:
        return []
    # Strip a UTF-8 BOM if present.
    s = s.lstrip("\ufeff")
    try:
        root = ET.fromstring(s)
    except ET.ParseError:
        return []
    tag = _norm_key(root.tag.split("}")[-1])
    cams: List[ET.Element] = []
    if tag == "camera":
        cams = [root]
    elif tag == "cameras":
        cams = [e for e in root if _norm_key(e.tag.split("}")[-1]) == "camera"]
    elif tag == "objects":
        node = _find_child(root, "cameras")
        if node is not None:
            cams = [e for e in node if _norm_key(e.tag.split("}")[-1]) == "camera"]
    else:
        return []
    return [_camera_elem_to_dict(e) for e in cams]


def _looks_like_camera_dict(mapping: Dict[str, Any]) -> bool:
    if not isinstance(mapping, dict):
        return False
    keys = {_norm_key(k) for k in mapping.keys()}
    cameraish = {_norm_key(k) for k in (
        "name", "videosourcestring", "video_source", "source", "url",
        "sourceindex", "source_index", "login", "settings", "onvifident",
        "onvif_ident",
    )}
    if not (keys & cameraish):
        return False
    for key in _ENVELOPE_LIST_KEYS:
        val = _get_ci(mapping, key)
        if isinstance(val, list):
            return False
    return True


def extract_cameras(payload: Any) -> List[Dict[str, Any]]:
    """Extract raw classic-iSpy camera dicts from flexible import payloads.

    Accepts ``objects.xml`` text (``str``/``bytes``), ``{"xml": ...}``
    envelopes, a camera list, a single camera dict, or a JSON document
    string/bytes.
    """
    if payload is None:
        return []
    if isinstance(payload, (bytes, bytearray)):
        try:
            text = bytes(payload).decode("utf-8-sig", "ignore")
        except Exception:
            return []
        if "<" in text and (("<camera" in text) or ("<objects" in text) or ("<cameras" in text)):
            return parse_ispy_xml(text)
        return extract_cameras(text)
    if isinstance(payload, str):
        s = payload.strip()
        if not s:
            return []
        if "<" in s and (("<camera" in s) or ("<objects" in s) or ("<cameras" in s)):
            return parse_ispy_xml(s)
        import json as _json

        try:
            payload = _json.loads(s)
        except Exception:
            return []
    if hasattr(payload, "read"):
        try:
            return extract_cameras(payload.read())
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
            elif isinstance(item, (str, bytes, bytearray)):
                out.extend(extract_cameras(item))
        return out
    if isinstance(payload, tuple):
        return extract_cameras(list(payload))
    if isinstance(payload, dict):
        for key in _ENVELOPE_TEXT_KEYS:
            val = _get_ci(payload, key)
            if isinstance(val, (str, bytes, bytearray)) and str(val).strip():
                nested = extract_cameras(val)
                if nested:
                    return nested
            elif isinstance(val, (list, dict)):
                nested = extract_cameras(val)
                if nested:
                    return nested
        for key in _ENVELOPE_LIST_KEYS:
            val = _get_ci(payload, key)
            if isinstance(val, list):
                out = []
                for item in val:
                    if isinstance(item, dict):
                        out.append(dict(item))
                    elif isinstance(item, (str, bytes, bytearray)):
                        out.extend(extract_cameras(item))
                if out:
                    return out
            elif isinstance(val, dict) and val:
                nested = extract_cameras(val)
                if nested:
                    return nested
                if all(isinstance(v, dict) for v in val.values()):
                    return [dict(v) for v in val.values()]
        # {"objects": "<objects>...</objects>"} style single-text envelopes.
        if _looks_like_camera_dict(payload):
            return [dict(payload)]
        # {"objects": {"cameras": [...]}} nesting.
        for key in ("objects", "config", "configuration"):
            val = _get_ci(payload, key)
            if isinstance(val, dict):
                nested = extract_cameras(val)
                if nested:
                    return nested
        values = list(payload.values())
        if values and all(isinstance(v, dict) for v in values):
            return [dict(v) for v in values]
        return []
    return []


def extract_sourceindex(camera: Any) -> Optional[int]:
    """Return the iSpy ``sourceindex`` for a raw camera dict."""
    if not isinstance(camera, dict):
        return None
    settings = _get_ci(camera, "settings")
    for container in (settings, camera):
        for key in ("sourceindex", "source_index", "source_type_index"):
            val = _get_ci(container, key) if isinstance(container, dict) else None
            coerced = _coerce_int(val)
            if coerced is not None:
                return coerced
    return None


def extract_ispy_id(camera: Any) -> Optional[int]:
    if not isinstance(camera, dict):
        return None
    for key in ("id", "cameraid", "camera_id", "ispy_id"):
        coerced = _coerce_int(_get_ci(camera, key))
        if coerced is not None:
            return coerced
    return None


def _settings_get(camera: Dict[str, Any], *names: str) -> Any:
    settings = _get_ci(camera, "settings")
    if isinstance(settings, dict):
        for name in names:
            val = _get_ci(settings, name)
            if val is not None and (not isinstance(val, str) or val.strip()):
                return val
    for name in names:
        val = _get_ci(camera, name)
        if val is not None and (not isinstance(val, str) or val.strip()):
            return val
    return None


def extract_stream_url(camera: Any) -> Optional[str]:
    """Return the best stream/source string for a raw iSpy camera dict."""
    if not isinstance(camera, dict):
        return None
    url = _clean_str(_settings_get(
        camera, "videosourcestring", "video_source", "url", "source",
        "stream_url", "onvifident", "onvif_ident",
    ))
    if url:
        return url
    # ONVIF fallback: usable endpoint may live under settings/onvif.
    settings = _get_ci(camera, "settings")
    if isinstance(settings, dict):
        onvif = _get_ci(settings, "onvif")
        if isinstance(onvif, dict):
            for key in ("url", "uri", "endpoint", "service", "host"):
                val = _clean_str(_get_ci(onvif, key))
                if val:
                    return val
    return None


def extract_host(camera: Any, url: Optional[str] = None) -> Optional[str]:
    """Extract a host/IP from the URL, else from known host fields."""
    if url:
        try:
            parsed = urlparse(url if "://" in url else f"rtsp://{url}")
            if parsed.hostname:
                return parsed.hostname.strip()
        except Exception:
            pass
    if isinstance(camera, dict):
        for key in ("audioip", "audio_ip", "ip", "ipaddress", "ip_address",
                    "host", "hostname", "onvif_host"):
            val = _clean_str(_settings_get(camera, key))
            if val:
                text = val.strip()
                if "://" in text:
                    try:
                        parsed = urlparse(text)
                        if parsed.hostname:
                            return parsed.hostname.strip()
                    except Exception:
                        pass
                return text.split("/", 1)[0].split(":", 1)[0].strip() or None
    return None


def extract_port(camera: Any, url: Optional[str] = None) -> Optional[int]:
    """Extract an ONVIF/connection port from explicit fields or the URL."""
    if isinstance(camera, dict):
        settings = _get_ci(camera, "settings")
        candidates: List[Any] = []
        if isinstance(settings, dict):
            onvif = _get_ci(settings, "onvif")
            if isinstance(onvif, dict):
                candidates.append(_get_ci(onvif, "rtspport"))
            candidates.append(_get_ci(settings, "ptzport"))
        candidates.append(_settings_get(camera, "onvifport", "onvif_port",
                                        "port", "httpport", "http_port",
                                        "rtspport", "rtsp_port"))
        for val in candidates:
            coerced = _coerce_int(val)
            if coerced is not None and 0 < coerced < 65536:
                return coerced
    if url and "://" in (url or ""):
        try:
            parsed = urlparse(url)
            if parsed.port:
                return int(parsed.port)
        except Exception:
            return None
    return None


def extract_credentials(camera: Any,
                        url: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """Return (username, password) from settings/login+password or URL."""
    user = _clean_str(_settings_get(camera, "login", "username", "user",
                                    "authuser", "auth_user")) if isinstance(camera, dict) else None
    pwd = _clean_str(_settings_get(camera, "password", "pass", "passwd",
                                   "authpass", "auth_pass")) if isinstance(camera, dict) else None
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


def extract_name(camera: Dict[str, Any], index: int = 0,
                 ispy_id: Optional[int] = None) -> str:
    for key in ("name", "cameraname", "camera_name", "title", "label", "devicename"):
        val = _clean_str(_get_ci(camera, key))
        if val:
            return val
    if ispy_id is not None:
        return f"iSpy Camera {ispy_id}"
    return f"iSpy Camera {index + 1}"


def extract_group(camera: Dict[str, Any]) -> Optional[str]:
    for key in ("accessgroups", "access_groups", "group", "groupname", "group_name"):
        val = _settings_get(camera, key)
        if isinstance(val, (list, tuple)):
            for item in val:
                s = _clean_str(item)
                if s:
                    return s.split(",")[0].strip() or None
        else:
            s = _clean_str(val)
            if s:
                # accessgroups is comma-separated ("Admin, Guest").
                return s.split(",")[0].strip() or None
    return None


def extract_enabled(camera: Dict[str, Any]) -> bool:
    for key in ("active", "enabled", "online"):
        val = _settings_get(camera, key)
        if val is not None:
            coerced = _coerce_bool(val)
            if coerced is not None:
                return coerced
    return True


def is_deleted(camera: Any) -> bool:
    if not isinstance(camera, dict):
        return False
    for key in ("deleted", "is_deleted"):
        coerced = _coerce_bool(_get_ci(camera, key))
        if coerced:
            return True
    return False


def is_clone(camera: Any) -> bool:
    return extract_sourceindex(camera) == SOURCE_CLONE


def infer_kind(camera: Dict[str, Any], url: Optional[str]) -> str:
    sourceindex = extract_sourceindex(camera)
    if sourceindex == SOURCE_ONVIF:
        return "onvif"
    if sourceindex == SOURCE_USB:
        return "usb"
    ptz = _coerce_int(_get_ci(camera, "ptz"))
    if ptz == -5:
        return "onvif"
    hay = f"{_clean_str(_settings_get(camera, 'onvifident', 'ffmpeg', 'vlcargs')) or ''} {url or ''}".lower()
    if "onvif" in hay:
        return "onvif"
    if (url or "").strip().lower().startswith("/dev/"):
        return "usb"
    if isinstance(camera, dict):
        settings = _get_ci(camera, "settings")
        moniker = str(_settings_get(camera, "videosourcestring") or "")
        if sourceindex == SOURCE_USB or (
            moniker.startswith("@device:") or "usb#vid" in moniker.lower()
        ):
            return "usb"
        _ = settings
    explicit = _clean_str(_settings_get(camera, "kind", "devicetype", "device_type"))
    if explicit and explicit.strip().lower() in VALID_KINDS:
        return explicit.strip().lower()
    return "ip"


def extract_resolution(camera: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    text = _clean_str(_get_ci(camera, "resolution"))
    if text:
        m = re.match(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", text)
        if m:
            try:
                return int(m.group(1)), int(m.group(2))
            except Exception:
                pass
    return None, None


def extract_fps(camera: Dict[str, Any]) -> Optional[int]:
    for key in ("maxframerate", "max_frame_rate", "framerate", "frame_rate", "fps"):
        coerced = _coerce_int(_settings_get(camera, key))
        if coerced is not None and coerced > 0:
            return coerced
    return None


def extract_motion_zones(camera: Dict[str, Any]) -> Optional[List[Dict[str, int]]]:
    if not isinstance(camera, dict):
        return None
    detector = _get_ci(camera, "detector")
    if not isinstance(detector, dict):
        return None
    zones = _get_ci(detector, "motionzones")
    if not isinstance(zones, list) or not zones:
        return None
    out: List[Dict[str, int]] = []
    for z in zones:
        if not isinstance(z, dict):
            continue
        try:
            out.append({
                "left": int(float(str(_get_ci(z, "left") or 0))),
                "top": int(float(str(_get_ci(z, "top") or 0))),
                "width": int(float(str(_get_ci(z, "width") or 0))),
                "height": int(float(str(_get_ci(z, "height") or 0))),
            })
        except Exception:
            continue
    return out or None


def normalize_ispy_camera(camera: Any, index: int = 0) -> Dict[str, Any]:
    """Normalize one raw classic-iSpy camera dict to canonical fields."""
    if not isinstance(camera, dict):
        return {}
    ispy_id = extract_ispy_id(camera)
    sourceindex = extract_sourceindex(camera)
    url = extract_stream_url(camera)
    host = extract_host(camera, url)
    port = extract_port(camera, url)
    user, pwd = extract_credentials(camera, url)
    ptz = _coerce_int(_get_ci(camera, "ptz"))

    source = url
    clone_of: Optional[int] = None
    if sourceindex == SOURCE_CLONE and url:
        clone_of = _coerce_int(url)
        source = None
    elif sourceindex == SOURCE_DESKTOP and url and _coerce_int(url) is not None:
        source = f"screen://{_coerce_int(url)}"
    if not source and host:
        kind_hint = infer_kind(camera, None)
        if kind_hint == "onvif":
            source = f"onvif://{host}:{port or 80}"
        elif kind_hint == "usb":
            source = host if host.startswith("/dev/") else f"usb://{host}"
        else:
            source = f"rtsp://{host}:554/live"

    kind = infer_kind(camera, source)
    width, height = extract_resolution(camera)
    fps = extract_fps(camera)

    detector = _get_ci(camera, "detector")
    record_on_detect = _coerce_bool(_get_ci(detector, "recordondetect")) \
        if isinstance(detector, dict) else None
    record_on_alert = _coerce_bool(_get_ci(detector, "recordonalert")) \
        if isinstance(detector, dict) else None
    detector_type = _clean_str(_get_ci(detector, "type")) if isinstance(detector, dict) else None
    recording_enabled = True
    if record_on_detect is False and record_on_alert is False:
        recording_enabled = False
    detection_enabled = True
    if detector_type is not None and detector_type.strip().lower() == "none":
        detection_enabled = False

    normalized: Dict[str, Any] = {
        "name": extract_name(camera, index, ispy_id),
        "kind": kind,
        "source": source,
        "username": user,
        "password": pwd,
        "onvif_host": host,
        "ip_address": host,
        "onvif_port": port,
        "enabled": extract_enabled(camera),
        "recording_enabled": recording_enabled,
        "detection_enabled": detection_enabled,
        "group": extract_group(camera),
        "width": width,
        "height": height,
        "fps": fps,
        "motion_zones": extract_motion_zones(camera),
        "ispy_id": ispy_id,
        "ispy_sourceindex": sourceindex,
        "ispy_ptz": ptz,
        "ispy_clone_of": clone_of,
        "ispy_deleted": is_deleted(camera),
    }
    return {k: v for k, v in normalized.items() if v is not None}


def normalize_cameras(payload: Any) -> List[Dict[str, Any]]:
    """Extract + normalize classic-iSpy cameras to canonical camera dicts."""
    raws = extract_cameras(payload)
    return [normalize_ispy_camera(raw, i) for i, raw in enumerate(raws)]


def validate_camera(camera: Any) -> bool:
    """Return True when a (possibly raw) iSpy camera has name + source."""
    if camera is None:
        return False
    data = normalize_ispy_camera(camera) if not _is_normalized(camera) else camera
    if not isinstance(data, dict):
        return False
    if data.get("ispy_deleted"):
        return False
    if data.get("ispy_clone_of") is not None:
        return False
    name = data.get("name")
    source = data.get("source")
    if not name or not str(name).strip():
        return False
    if not source or not str(source).strip():
        return False
    kind = str(data.get("kind") or "ip").strip().lower()
    return kind in VALID_KINDS


def _is_normalized(camera: Any) -> bool:
    return isinstance(camera, dict) and ("ispy_id" in camera or "ispy_sourceindex" in camera) \
        and "source" in camera


def validate_cameras(payload: Any) -> List[bool]:
    return [validate_camera(d) for d in extract_cameras(payload)]


def is_valid_camera(camera: Any) -> bool:
    return validate_camera(camera)


def camera_to_camera_payload(camera: Any, **defaults: Any) -> Any:
    """Build a CameraCreate payload from a classic-iSpy camera dict."""
    from app.schemas import CameraCreate

    data = normalize_ispy_camera(camera) if not _is_normalized(camera) else dict(camera)
    payload: Dict[str, Any] = {
        "name": data.get("name"),
        "kind": data.get("kind") or "ip",
        "source": data.get("source"),
    }
    for key in ("username", "password", "onvif_host", "ip_address", "onvif_port",
                "enabled", "recording_enabled", "detection_enabled", "group",
                "width", "height", "fps", "motion_zones"):
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


def cameras_to_camera_payloads(payload: Any, **defaults: Any) -> List[Any]:
    out = []
    for raw in extract_cameras(payload):
        norm = normalize_ispy_camera(raw)
        if not validate_camera(norm):
            continue
        out.append(camera_to_camera_payload(norm, **defaults))
    return out


def import_ispy_data(payload: Any, skip_invalid: bool = True,
                     skip_clones: bool = True, skip_deleted: bool = True,
                     **defaults: Any) -> List[Dict[str, Any]]:
    """Pure import: validate + normalize payload to camera payload dicts."""
    raws = extract_cameras(payload)
    out: List[Dict[str, Any]] = []
    for index, raw in enumerate(raws):
        norm = normalize_ispy_camera(raw, index)
        if skip_deleted and norm.get("ispy_deleted"):
            continue
        if skip_clones and norm.get("ispy_clone_of") is not None:
            continue
        if validate_camera(norm):
            data = {k: v for k, v in norm.items() if v is not None and not k.startswith("ispy_")}
            for key, value in (defaults or {}).items():
                if value is not None:
                    data[key] = value
            out.append(data)
        elif not skip_invalid:
            data = {k: v for k, v in norm.items() if v is not None and not k.startswith("ispy_")}
            out.append(data)
    return out


# ---------------------------------------------------------------------------
# DB-backed helpers
# ---------------------------------------------------------------------------

async def import_ispy_db(
    db: Any,
    payload: Any,
    skip_invalid: bool = True,
    update_existing: bool = False,
    skip_clones: bool = True,
    skip_deleted: bool = True,
    **defaults: Any,
) -> Dict[str, Any]:
    """Import classic-iSpy cameras into the DB, creating Camera rows.

    Returns ``{"imported": [...], "count": n, "errors": [...], "skipped": [...]}``.
    Clones land in ``skipped`` with reason ``clone``; deleted entries land
    in ``skipped`` with reason ``deleted``.
    """
    from app.models.entities import Camera as _Camera

    raws = extract_cameras(payload)
    imported: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for index, raw in enumerate(raws):
        norm = normalize_ispy_camera(raw, index)
        if skip_deleted and norm.get("ispy_deleted"):
            skipped.append({"index": index, "reason": "deleted", "camera": raw})
            continue
        if skip_clones and norm.get("ispy_clone_of") is not None:
            skipped.append({"index": index, "reason": "clone", "camera": raw})
            continue
        if not validate_camera(norm):
            if skip_invalid:
                errors.append({"index": index,
                               "error": "invalid camera: 'name' and 'source' are required",
                               "camera": raw})
                continue
        try:
            data = {k: v for k, v in norm.items() if v is not None and not k.startswith("ispy_")}
            for key, value in (defaults or {}).items():
                if value is not None:
                    data[key] = value
            data.pop("id", None)
            allowed = {"name", "kind", "source", "username", "password", "onvif_host",
                       "ip_address", "onvif_port", "enabled", "recording_enabled",
                       "detection_enabled", "group", "width", "height", "fps",
                       "motion_zones"}
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


async def preview_ispy_db(db: Any, payload: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Parse iSpy cameras and annotate validity (+ already_added on name match)."""
    raws = extract_cameras(payload)
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
        norm = normalize_ispy_camera(raw, index)
        valid = validate_camera(norm)
        name = str(norm.get("name") or "").strip().lower()
        already = bool(name and name in existing_names)
        entry = dict(norm)
        entry["valid"] = valid
        entry["already_added"] = already
        entry["new"] = valid and not already
        out.append(entry)
    return out


class IspyImportService:
    """Object-oriented facade around the module-level helpers."""

    def extract(self, payload: Any) -> List[Dict[str, Any]]:
        return extract_cameras(payload)

    def normalize(self, camera: Any, index: int = 0) -> Dict[str, Any]:
        return normalize_ispy_camera(camera, index)

    def validate(self, camera: Any) -> bool:
        return validate_camera(camera)

    def to_payload(self, camera: Any, **defaults: Any) -> Any:
        return camera_to_camera_payload(camera, **defaults)

    def prepare_import(self, payload: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return import_ispy_data(payload, **kwargs)

    async def import_db(self, db: Any, payload: Any, **kwargs: Any) -> Dict[str, Any]:
        return await import_ispy_db(db, payload, **kwargs)

    async def preview_db(self, db: Any, payload: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return await preview_ispy_db(db, payload, **kwargs)


ispy_import_service = IspyImportService()
service = ispy_import_service
manager = ispy_import_service


# Compatibility aliases for varied caller naming.
parse = extract_cameras
loads = extract_cameras
from_xml = extract_cameras
from_text = extract_cameras
load_text = extract_cameras
parse_text = extract_cameras
parse_string = extract_cameras
parse_xml = parse_ispy_xml
extract = extract_cameras
get_cameras = extract_cameras
fetch_cameras = extract_cameras
load_cameras = extract_cameras
parse_cameras = extract_cameras
parse_import_payload = extract_cameras
to_list = extract_cameras
to_dict_list = extract_cameras
normalize = normalize_ispy_camera
normalize_ispy_row = normalize_ispy_camera
normalize_ispy_rows = normalize_cameras
validate = validate_camera
is_valid = validate_camera
validate_ispy_camera = validate_camera
validate_ispy_cameras = validate_cameras
is_valid_device = validate_camera
camera_to_payload = camera_to_camera_payload
ispy_camera_to_camera_payload = camera_to_camera_payload
to_camera_payload = camera_to_camera_payload
to_camera_payloads = cameras_to_camera_payloads
to_payloads = cameras_to_camera_payloads
prepare_import = import_ispy_data
import_data = import_ispy_data
import_rows = import_ispy_data
import_cameras_data = import_ispy_data
import_devices = import_ispy_db
bulk_import_ispy = import_ispy_db
import_to_db = import_ispy_db
import_into_db = import_ispy_db
import_db = import_ispy_db
create_from_ispy = import_ispy_db
import_cameras = import_ispy_db
import_cameras_ispy = import_ispy_db
export_db = None
preview = preview_ispy_db
preview_import = preview_ispy_db
preview_ispy = preview_ispy_db


__all__ = [
    "ISPY_KIND",
    "VALID_KINDS",
    "SOURCE_JPEG",
    "SOURCE_MJPEG",
    "SOURCE_FFMPEG",
    "SOURCE_USB",
    "SOURCE_DESKTOP",
    "SOURCE_VLC",
    "SOURCE_XIMEA",
    "SOURCE_KINECT",
    "SOURCE_CUSTOM",
    "SOURCE_ONVIF",
    "SOURCE_CLONE",
    "parse_ispy_xml",
    "extract_cameras",
    "extract_sourceindex",
    "extract_ispy_id",
    "extract_stream_url",
    "extract_host",
    "extract_port",
    "extract_credentials",
    "extract_name",
    "extract_group",
    "extract_enabled",
    "is_deleted",
    "is_clone",
    "infer_kind",
    "extract_resolution",
    "extract_fps",
    "extract_motion_zones",
    "normalize_ispy_camera",
    "normalize_cameras",
    "validate_camera",
    "validate_cameras",
    "is_valid_camera",
    "camera_to_camera_payload",
    "cameras_to_camera_payloads",
    "import_ispy_data",
    "import_ispy_db",
    "preview_ispy_db",
    "IspyImportService",
    "ispy_import_service",
    "service",
]
