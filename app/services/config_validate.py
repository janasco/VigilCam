"""Frigate-inspired config dry-run validator (no writes).

Mirrors the Frigate validation philosophy documented in
``docs/docs/configuration/advanced/system.md`` and implemented in
``frigate/config/config.py`` plus ``frigate/api/app.py``:

- Frigate parses the raw config (YAML or JSON, see
  ``FrigateConfig.parse``) into a dict and validates it with Pydantic,
  including cross-field ``model_validator`` checks (roles assigned to
  inputs, zones referencing tracked objects, required zones, autotrack
  zones, motion/detect dependency, LPR/face global gates, model device
  uniqueness, GenAI role uniqueness, live stream names, segment times).
- ``POST /api/config/save`` validates before writing and returns ``400``
  with per-location messages (line number plus ``loc`` path) when invalid.
- ``PUT /api/config`` bulk updates apply changes, re-parse, and roll back
  the file on validation failure.
- The CLI ``--validate-config`` flag returns exit code 1/0 without
  starting the process.

VigilCam equivalent for the JSON system-config envelope (see
:mod:`app.services.config_io`):

- :func:`validate_config_dry_run` parses flexible payloads (envelope,
  flat section map, JSON string/bytes, optional YAML text) into
  ``{section: [rows]}`` without touching the database or the filesystem.
- Every section is checked, all errors are collected (not fail-fast), and
  each error carries ``section``, ``index``, ``path`` and ``message`` so
  callers can render Frigate-style ``path -> message`` lines.
- Cross-section references (camera group links, PTZ/linked/standing-rule
  camera refs, capability/permission camera refs) are verified against the
  cameras/groups inside the same payload, mirroring Frigate's
  ``verify_required_zones_exist`` and ``verify_config_roles`` checks.
- Non-blocking concerns (disabled cameras, shared sources, high fps,
  expired rules, unknown sections, unreferenced groups) are returned as
  ``warnings`` while ``valid`` stays True, mirroring Frigate's
  ``logger.warning`` paths (for example high detect fps).

No ``app/main.py`` changes are required: the API module auto-registers.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CONFIG_VALIDATE_VERSION = "1.0"
CONFIG_VALIDATE_KIND = "vigilcam-config-validate"

try:
    from app.services.config_io import (
        CONFIG_SECTIONS as _IO_SECTIONS,
        OPTIONAL_SECTIONS as _IO_OPTIONAL,
    )

    KNOWN_SECTIONS: Tuple[str, ...] = tuple(_IO_SECTIONS)
    OPTIONAL_SECTIONS: Tuple[str, ...] = tuple(_IO_OPTIONAL)
    _ALL_KNOWN: Tuple[str, ...] = tuple(_IO_SECTIONS) + tuple(_IO_OPTIONAL)
except Exception:  # noqa: BLE001
    KNOWN_SECTIONS = (
        "groups",
        "cameras",
        "camera_capabilities",
        "camera_permissions",
        "layouts",
        "standing_rules",
        "relay_settings",
        "storage_areas",
        "ptz_schedules",
        "linked_monitor_triggers",
        "settings",
    )
    OPTIONAL_SECTIONS = ("users", "roles")
    _ALL_KNOWN = KNOWN_SECTIONS + OPTIONAL_SECTIONS
    logger.debug("config_io sections unavailable, using fallback list")

ALL_KNOWN_SECTIONS = _ALL_KNOWN

_VALID_KINDS = ("usb", "ip", "onvif")
_VALID_PERMISSIONS = {
    "view",
    "read",
    "list",
    "manage",
    "write",
    "control",
    "admin",
    "owner",
    "full",
    "full_access",
}
_VALID_QUALITIES = ("low", "medium", "high", "auto")
_VALID_ROTATIONS = (0, 90, 180, 270)
_VALID_MODES = (
    "motion",
    "continuous",
    "always",
    "never",
    "none",
    "events",
    "detections",
    "alerts",
    "all",
)
_CRON_PART = re.compile(r"^[0-9*,/\-]+$|^[a-zA-Z]+$")
_SOURCE_PREFIXES = (
    "rtsp://",
    "rtmp://",
    "rtmps://",
    "http://",
    "https://",
    "onvif://",
    "usb",
    "/dev/",
    "file://",
)


# ---------------------------------------------------------------------------
# Payload parsing (Frigate ``parse`` equivalent: sniff JSON vs YAML vs dict)
# ---------------------------------------------------------------------------


def _try_yaml_load(text: str) -> Any:
    try:
        import yaml  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        return yaml.safe_load(text)
    except Exception:  # noqa: BLE001
        return None


def _parse_raw_text(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        return json.loads(stripped)
    except Exception:  # noqa: BLE001
        pass
    parsed = _try_yaml_load(stripped)
    if parsed is not None:
        return parsed
    return {}


def _coerce_rows(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [dict(value)]
    if isinstance(value, (list, tuple)):
        out: List[Dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                out.append(dict(item))
            elif item is None:
                continue
            else:
                out.append({"value": item})
        return out
    return []


def extract_sections(payload: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Extract ``{section: [rows]}`` without validation (pure)."""
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return {}
    if isinstance(payload, str):
        payload = _parse_raw_text(payload)
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        try:
            payload = payload.model_dump()
        except Exception:  # noqa: BLE001
            return {}
    if not isinstance(payload, dict):
        return {}
    data = dict(payload)
    for _ in range(3):
        keys = set(data.keys())
        if len(keys) == 1:
            only = next(iter(keys))
            if only in (
                "config",
                "data",
                "body",
                "payload",
                "params",
                "options",
                "envelope",
                "import",
            ) and isinstance(data[only], dict):
                data = dict(data[only])
                continue
        break
    # Raw text nested under a text key (Frigate posts raw YAML as text/plain).
    for text_key in ("raw", "yaml", "yml", "text", "content"):
        raw_val = data.get(text_key)
        if isinstance(raw_val, str) and raw_val.strip() and len(data) <= 3:
            parsed = _parse_raw_text(raw_val)
            if isinstance(parsed, dict) and parsed:
                merged = dict(parsed)
                for k, v in data.items():
                    if k != text_key:
                        merged.setdefault(k, v)
                data = merged
                break
    sections: Dict[str, List[Dict[str, Any]]] = {}
    nested = data.get("sections")
    if isinstance(nested, dict):
        for name, rows in nested.items():
            sections[str(name)] = _coerce_rows(rows)
    for name in _ALL_KNOWN:
        if name in sections:
            continue
        if name in data and data[name] is not None:
            rows = _coerce_rows(data[name])
            if rows or name in data:
                sections[name] = rows
    # Preserve unknown top-level section-like keys so callers get
    # ``unknown_sections`` warnings instead of silent drops. Control and
    # metadata keys are excluded.
    _RESERVED = {
        "version",
        "kind",
        "exported_at",
        "app_version",
        "app_name",
        "counts",
        "count",
        "sections",
        "strict",
        "strict_valid",
        "check_references",
        "checkReferences",
        "verify_refs",
        "verifyRefs",
        "sections_filter",
        "sectionsFilter",
        "only",
        "include",
        "options",
        "params",
        "data",
        "body",
        "payload",
        "envelope",
        "config",
        "raw",
        "yaml",
        "yml",
        "text",
        "content",
        "config_text",
        "name",
        "source",
    }
    for key, value in data.items():
        if key in sections or key in _RESERVED:
            continue
        if isinstance(value, (list, tuple, dict)):
            rows = _coerce_rows(value)
            # Only treat as a section when it holds row-like entries.
            if rows:
                sections[str(key)] = rows
    if not sections and isinstance(data.get("presets"), list):
        sections["cameras"] = _coerce_rows(data.get("presets"))
    if not sections and isinstance(data.get("cameras"), list):
        sections["cameras"] = _coerce_rows(data.get("cameras"))
    # Single camera object posted directly (wizard style).
    if not sections and isinstance(data.get("name"), str) and isinstance(
        data.get("source"), str
    ):
        sections["cameras"] = [dict(data)]
    return sections


def _normalize_sections_arg(sections: Any) -> Optional[List[str]]:
    if sections is None:
        return None
    if isinstance(sections, str):
        text = sections.strip()
        if not text:
            return None
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    sections = parsed
                else:
                    sections = [s.strip() for s in text.split(",")]
            except Exception:  # noqa: BLE001
                sections = [s.strip() for s in text.split(",")]
        else:
            sections = [s.strip() for s in text.split(",")]
    if isinstance(sections, (tuple, set)):
        sections = list(sections)
    if not isinstance(sections, list):
        return None
    out = [str(s).strip() for s in sections if str(s).strip()]
    return out or None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(
                tzinfo=None
            )
        except Exception:  # noqa: BLE001
            return None
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "enabled", "enable"):
        return True
    if s in ("0", "false", "no", "n", "off", "disabled", "disable"):
        return False
    return None


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None if value is None else int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _first_present(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) is not None and (
            not isinstance(row.get(key), str) or str(row.get(key)).strip()
        ):
            return row.get(key)
    for key in keys:
        if key in row:
            return row.get(key)
    return None


def _err(
    section: str, index: int, path: str, message: str, code: str = "invalid"
) -> Dict[str, Any]:
    return {
        "section": section,
        "index": index,
        "path": path,
        "loc": [section, index, path] if path else [section, index],
        "message": message,
        "code": code,
    }


def _warn(
    section: str, index: int, path: str, message: str, code: str = "warning"
) -> Dict[str, Any]:
    item = _err(section, index, path, message, code=code)
    return item


# ---------------------------------------------------------------------------
# Per-section validators
# ---------------------------------------------------------------------------


def _validate_group(
    row: Any, index: int, seen_names: Dict[str, int]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(row, dict):
        return ([_err("groups", index, "", "row is not an object")], warnings)
    name = str(row.get("name") or "").strip()
    if not name:
        errors.append(_err("groups", index, "name", "group 'name' is required"))
    elif len(name) > 120:
        errors.append(
            _err("groups", index, "name", "group 'name' must be 120 chars or fewer")
        )
    else:
        if name in seen_names:
            errors.append(
                _err(
                    "groups",
                    index,
                    "name",
                    "duplicate group name '%s' (first seen at index %d)"
                    % (name, seen_names[name]),
                    code="duplicate",
                )
            )
        else:
            seen_names[name] = index
    return errors, warnings


def _validate_camera(
    row: Any,
    index: int,
    seen_names: Dict[str, int],
    seen_ids: Dict[Any, int],
    group_names: Dict[str, int],
    group_ids: Dict[Any, int],
    has_groups_section: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(row, dict):
        return ([_err("cameras", index, "", "row is not an object")], warnings)
    name = str(row.get("name") or "").strip()
    if not name:
        errors.append(_err("cameras", index, "name", "camera 'name' is required"))
    elif len(name) > 120:
        errors.append(
            _err("cameras", index, "name", "camera 'name' must be 120 chars or fewer")
        )
    else:
        if name in seen_names:
            errors.append(
                _err(
                    "cameras",
                    index,
                    "name",
                    "duplicate camera name '%s' (first seen at index %d)"
                    % (name, seen_names[name]),
                    code="duplicate",
                )
            )
        else:
            seen_names[name] = index
        if name != str(row.get("name") or ""):
            warnings.append(
                _warn("cameras", index, "name", "camera name has leading/trailing spaces")
            )
    raw_id = row.get("id")
    if raw_id is not None:
        if raw_id in seen_ids or str(raw_id) in seen_ids:
            errors.append(
                _err(
                    "cameras",
                    index,
                    "id",
                    "duplicate camera id '%s'" % (raw_id,),
                    code="duplicate",
                )
            )
        else:
            seen_ids[raw_id] = index
            seen_ids[str(raw_id)] = index
    source = _first_present(
        row, "source", "source_url", "url", "stream_url", "stream", "rtsp_url", "address"
    )
    if source is None or (isinstance(source, str) and not source.strip()):
        errors.append(_err("cameras", index, "source", "camera 'source' is required"))
    else:
        s = str(source).strip()
        lowered = s.lower()
        has_scheme = "://" in s
        valid_prefix = lowered.startswith(_SOURCE_PREFIXES) or has_scheme or s.startswith(
            "/"
        )
        if not valid_prefix:
            errors.append(
                _err(
                    "cameras",
                    index,
                    "source",
                    "camera 'source' must be a URL (rtsp://, http://, ...) "
                    "or device path (/dev/video0)",
                )
            )
        if re.search(r"\s", s):
            warnings.append(
                _warn("cameras", index, "source", "camera source contains whitespace")
            )
    kind = _first_present(row, "kind", "type", "camera_kind")
    if kind is not None and str(kind).strip():
        if str(kind).strip().lower() not in _VALID_KINDS:
            errors.append(
                _err(
                    "cameras",
                    index,
                    "kind",
                    "camera 'kind' must be one of: %s" % ", ".join(_VALID_KINDS),
                )
            )
    fps = _first_present(row, "fps", "frame_rate", "framerate")
    if fps is not None:
        n = _as_int(fps)
        if n is None:
            errors.append(_err("cameras", index, "fps", "camera 'fps' must be a number"))
        elif n <= 0:
            errors.append(
                _err("cameras", index, "fps", "camera 'fps' must be greater than 0")
            )
        elif n > 60:
            errors.append(
                _err("cameras", index, "fps", "camera 'fps' must be 60 or less")
            )
        elif n > 30:
            warnings.append(
                _warn(
                    "cameras",
                    index,
                    "fps",
                    "camera fps is set to %d which is unusually high, "
                    "high values may increase resource usage" % n,
                )
            )
    for field in ("width", "height"):
        val = row.get(field)
        if val is not None:
            n = _as_int(val)
            if n is None:
                errors.append(
                    _err("cameras", index, field, "camera '%s' must be a number" % field)
                )
            elif n <= 0:
                errors.append(
                    _err(
                        "cameras",
                        index,
                        field,
                        "camera '%s' must be greater than 0" % field,
                    )
                )
            elif n < 160 or n > 8192:
                warnings.append(
                    _warn(
                        "cameras",
                        index,
                        field,
                        "camera '%s' of %d looks unusual" % (field, n),
                    )
                )
    rotation = row.get("rotation")
    if rotation is not None:
        n = _as_int(rotation)
        if n is None or n not in _VALID_ROTATIONS:
            errors.append(
                _err(
                    "cameras",
                    index,
                    "rotation",
                    "camera 'rotation' must be one of: 0, 90, 180, 270",
                )
            )
    quality = _first_present(row, "stream_quality", "quality")
    if quality is not None and str(quality).strip():
        if str(quality).strip().lower() not in _VALID_QUALITIES:
            warnings.append(
                _warn(
                    "cameras",
                    index,
                    "stream_quality",
                    "unknown stream_quality '%s', expected one of: %s"
                    % (quality, ", ".join(_VALID_QUALITIES)),
                )
            )
    mode = _first_present(row, "recording_mode", "record_mode", "mode")
    if mode is not None and str(mode).strip():
        if str(mode).strip().lower() not in _VALID_MODES:
            warnings.append(
                _warn(
                    "cameras",
                    index,
                    "recording_mode",
                    "unknown recording_mode '%s'" % (mode,),
                )
            )
    for flag in ("enabled", "recording_enabled", "detection_enabled"):
        if flag in row and row.get(flag) is not None:
            if _as_bool(row.get(flag)) is None:
                warnings.append(
                    _warn(
                        "cameras", index, flag, "camera '%s' should be true/false" % flag
                    )
                )
    if _as_bool(row.get("enabled")) is False:
        warnings.append(
            _warn("cameras", index, "enabled", "camera '%s' is disabled" % (name or index))
        )
    group = _first_present(row, "group", "group_name")
    group_id = _first_present(row, "group_id", "groupId")
    if group is not None and str(group).strip():
        gname = str(group).strip()
        if has_groups_section and gname not in group_names:
            errors.append(
                _err(
                    "cameras",
                    index,
                    "group",
                    "camera references unknown group '%s'" % gname,
                    code="unknown_reference",
                )
            )
        elif not has_groups_section:
            warnings.append(
                _warn(
                    "cameras",
                    index,
                    "group",
                    "camera references group '%s' which cannot be verified "
                    "(no groups in payload)" % gname,
                )
            )
    if group_id is not None:
        if has_groups_section and (
            group_id not in group_ids and str(group_id) not in group_ids
        ):
            errors.append(
                _err(
                    "cameras",
                    index,
                    "group_id",
                    "camera references unknown group_id '%s'" % (group_id,),
                    code="unknown_reference",
                )
            )
    zones = row.get("motion_zones", row.get("motionZones"))
    if zones is not None:
        if isinstance(zones, dict):
            zone_items = list(zones.values())
        elif isinstance(zones, list):
            zone_items = zones
        else:
            errors.append(
                _err(
                    "cameras",
                    index,
                    "motion_zones",
                    "camera 'motion_zones' must be a list or mapping",
                )
            )
            zone_items = []
        for zpos, zone in enumerate(zone_items):
            if not isinstance(zone, dict):
                errors.append(
                    _err(
                        "cameras",
                        index,
                        "motion_zones[%d]" % zpos,
                        "motion zone must be an object with points",
                    )
                )
                continue
            pts = zone.get("points", zone.get("polygon", zone.get("coordinates")))
            if pts is None:
                warnings.append(
                    _warn(
                        "cameras",
                        index,
                        "motion_zones[%d]" % zpos,
                        "motion zone has no points/polygon",
                    )
                )
    for host_field in ("ip_address", "onvif_host"):
        if row.get(host_field) is not None and str(row.get(host_field)).strip():
            if re.search(r"\s", str(row.get(host_field))):
                errors.append(
                    _err(
                        "cameras",
                        index,
                        host_field,
                        "camera '%s' must not contain spaces" % host_field,
                    )
                )
    profile = _first_present(row, "storage_profile", "storageProfile")
    if profile is not None and isinstance(profile, str) and not profile.strip():
        warnings.append(
            _warn("cameras", index, "storage_profile", "storage profile is blank")
        )
    return errors, warnings


def _validate_layout(
    row: Any, index: int, seen_names: Dict[str, int]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(row, dict):
        return ([_err("layouts", index, "", "row is not an object")], warnings)
    name = str(row.get("name") or row.get("title") or row.get("layout_name") or "").strip()
    if not name:
        errors.append(_err("layouts", index, "name", "layout 'name' is required"))
    elif name in seen_names:
        errors.append(
            _err(
                "layouts",
                index,
                "name",
                "duplicate layout name '%s'" % name,
                code="duplicate",
            )
        )
    else:
        seen_names[name] = index
    content = _first_present(
        row,
        "json_layout",
        "layout",
        "json",
        "data",
        "config",
        "content",
        "definition",
        "structure",
        "value",
        "camera_positions",
        "cameras",
        "positions",
        "markers",
    )
    if content is None:
        warnings.append(
            _warn("layouts", index, "json_layout", "layout has no content")
        )
    return errors, warnings


def _validate_storage(
    row: Any, index: int, seen_names: Dict[str, int], seen_paths: Dict[str, int]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(row, dict):
        return ([_err("storage_areas", index, "", "row is not an object")], warnings)
    name = str(row.get("name") or "").strip()
    path = _first_present(
        row, "path", "storage_path", "root_path", "storage_root", "root"
    )
    if not name:
        errors.append(_err("storage_areas", index, "name", "storage area 'name' is required"))
    elif name in seen_names:
        errors.append(
            _err(
                "storage_areas",
                index,
                "name",
                "duplicate storage area name '%s'" % name,
                code="duplicate",
            )
        )
    else:
        seen_names[name] = index
    if path is None or (isinstance(path, str) and not path.strip()):
        errors.append(_err("storage_areas", index, "path", "storage area 'path' is required"))
    else:
        p = str(path).strip()
        if not p.startswith("/"):
            errors.append(
                _err(
                    "storage_areas",
                    index,
                    "path",
                    "storage area 'path' must be absolute (start with /)",
                )
            )
        if p in seen_paths:
            warnings.append(
                _warn(
                    "storage_areas",
                    index,
                    "path",
                    "storage path '%s' is used by another area" % p,
                )
            )
        else:
            seen_paths[p] = index
    for field in ("max_bytes", "used_bytes"):
        val = _first_present(
            row,
            field,
            "max_size" if field == "max_bytes" else "used_size",
            "max_total_bytes" if field == "max_bytes" else "used",
        )
        if val is not None:
            n = _as_int(val)
            if n is None:
                errors.append(
                    _err(
                        "storage_areas", index, field, "storage '%s' must be a number" % field
                    )
                )
            elif n < 0:
                errors.append(
                    _err(
                        "storage_areas",
                        index,
                        field,
                        "storage '%s' must be 0 or greater" % field,
                    )
                )
    if _as_bool(row.get("enabled")) is False:
        warnings.append(
            _warn("storage_areas", index, "enabled", "storage area '%s' is disabled" % (name or index))
        )
    return errors, warnings


def _validate_relay(
    row: Any, index: int
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(row, dict):
        return ([_err("relay_settings", index, "", "row is not an object")], warnings)
    url = _first_present(
        row, "relay_url", "url", "remote_url", "server_url", "relay"
    )
    enabled = _as_bool(row.get("enabled", row.get("is_enabled", row.get("relay_enabled"))))
    if enabled is None and url is None and row.get("token") is None:
        warnings.append(
            _warn("relay_settings", index, "", "relay settings row is empty")
        )
        return errors, warnings
    if enabled is True:
        if url is None or (isinstance(url, str) and not url.strip()):
            errors.append(
                _err(
                    "relay_settings",
                    index,
                    "relay_url",
                    "relay 'relay_url' is required when enabled",
                )
            )
        elif not str(url).strip().lower().startswith(("http://", "https://")):
            errors.append(
                _err(
                    "relay_settings",
                    index,
                    "relay_url",
                    "relay 'relay_url' must start with http:// or https://",
                )
            )
    elif url is not None and str(url).strip():
        if not str(url).strip().lower().startswith(("http://", "https://")):
            errors.append(
                _err(
                    "relay_settings",
                    index,
                    "relay_url",
                    "relay 'relay_url' must start with http:// or https://",
                )
            )
    return errors, warnings


def _validate_standing_rule(
    row: Any,
    index: int,
    camera_keys: Dict[str, int],
    has_cameras_section: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(row, dict):
        return ([_err("standing_rules", index, "", "row is not an object")], warnings)
    sentence = str(row.get("sentence") or "").strip()
    target = str(row.get("target_object") or row.get("targetObject") or "").strip()
    expires = _first_present(row, "expires_at", "expiresAt", "expires")
    if not sentence:
        errors.append(
            _err("standing_rules", index, "sentence", "standing rule 'sentence' is required")
        )
    if not target:
        errors.append(
            _err(
                "standing_rules",
                index,
                "target_object",
                "standing rule 'target_object' is required",
            )
        )
    if expires is None or (isinstance(expires, str) and not expires.strip()):
        errors.append(
            _err(
                "standing_rules", index, "expires_at", "standing rule 'expires_at' is required"
            )
        )
    else:
        parsed = _parse_datetime(expires)
        if parsed is None:
            errors.append(
                _err(
                    "standing_rules",
                    index,
                    "expires_at",
                    "standing rule 'expires_at' is not a valid datetime",
                )
            )
        elif parsed < datetime.now().replace(tzinfo=None):
            warnings.append(
                _warn(
                    "standing_rules",
                    index,
                    "expires_at",
                    "standing rule is already expired",
                )
            )
    cam_ref = _first_present(
        row, "camera_id", "camera", "cam", "source_camera_id", "monitor_id"
    )
    if cam_ref is not None:
        key = str(cam_ref).strip() if not isinstance(cam_ref, int) else cam_ref
        if has_cameras_section and key not in camera_keys and str(key) not in camera_keys:
            errors.append(
                _err(
                    "standing_rules",
                    index,
                    "camera_id",
                    "standing rule references unknown camera '%s'" % (cam_ref,),
                    code="unknown_reference",
                )
            )
    return errors, warnings


def _validate_cron(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "schedule must not be empty"
    parts = str(value).strip().split()
    if len(parts) not in (5, 6):
        return "schedule must have 5 or 6 cron fields (minute hour day month weekday [year])"
    for part in parts:
        if not _CRON_PART.match(part):
            return "schedule field '%s' is not valid cron syntax" % part
    return None


def _validate_ptz(
    row: Any,
    index: int,
    camera_keys: Dict[str, int],
    has_cameras_section: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(row, dict):
        return ([_err("ptz_schedules", index, "", "row is not an object")], warnings)
    schedule = _first_present(
        row,
        "schedule",
        "cron",
        "cron_expression",
        "schedule_string",
        "cron_string",
        "cron_str",
        "expr",
        "expression",
    )
    if schedule is None or (isinstance(schedule, str) and not schedule.strip()):
        errors.append(
            _err("ptz_schedules", index, "schedule", "PTZ schedule 'schedule' is required")
        )
    else:
        problem = _validate_cron(schedule)
        if problem is not None:
            errors.append(_err("ptz_schedules", index, "schedule", problem))
    preset = _first_present(
        row, "preset_token", "preset", "preset_id", "token"
    )
    if preset is None or (isinstance(preset, str) and not preset.strip()):
        warnings.append(
            _warn("ptz_schedules", index, "preset_token", "PTZ schedule has no preset token")
        )
    cam_ref = _first_present(row, "camera_id", "camera", "cam", "cameraId")
    if cam_ref is None:
        warnings.append(
            _warn("ptz_schedules", index, "camera_id", "PTZ schedule has no camera reference")
        )
    elif has_cameras_section and cam_ref not in camera_keys and str(cam_ref) not in camera_keys:
        errors.append(
            _err(
                "ptz_schedules",
                index,
                "camera_id",
                "PTZ schedule references unknown camera '%s'" % (cam_ref,),
                code="unknown_reference",
            )
        )
    return errors, warnings


def _linked_endpoints(row: Dict[str, Any]) -> Tuple[Any, Any, str, str]:
    src_keys = (
        "source_camera_id",
        "source_id",
        "source",
        "monitor_id",
        "camera_id",
        "trigger_camera_id",
    )
    dst_keys = (
        "target_camera_id",
        "target_id",
        "target",
        "linked_monitor_id",
        "linked_camera_id",
        "monitor_id",
    )
    src = _first_present(row, *src_keys)
    dst = _first_present(row, *dst_keys)
    # Disambiguate monitor_id shared alias: if only one of src/dst resolved
    # through monitor_id, keep it; callers handle equality check.
    return src, dst, "source_camera_id", "target_camera_id"


def _validate_linked(
    row: Any,
    index: int,
    camera_keys: Dict[str, int],
    has_cameras_section: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(row, dict):
        return ([_err("linked_monitor_triggers", index, "", "row is not an object")], warnings)
    src, dst, src_path, dst_path = _linked_endpoints(row)
    if src is None and dst is None:
        errors.append(
            _err(
                "linked_monitor_triggers",
                index,
                "source_camera_id",
                "linked monitor trigger must reference at least one camera",
            )
        )
    if src is not None and dst is not None and str(src) == str(dst):
        errors.append(
            _err(
                "linked_monitor_triggers",
                index,
                dst_path,
                "linked monitor source and target must differ",
            )
        )
    for ref, path in ((src, src_path), (dst, dst_path)):
        if ref is not None and has_cameras_section:
            if ref not in camera_keys and str(ref) not in camera_keys:
                errors.append(
                    _err(
                        "linked_monitor_triggers",
                        index,
                        path,
                        "linked monitor references unknown camera '%s'" % (ref,),
                        code="unknown_reference",
                    )
                )
    if _as_bool(row.get("enabled")) is False:
        warnings.append(
            _warn("linked_monitor_triggers", index, "enabled", "linked trigger is disabled")
        )
    return errors, warnings


def _validate_capability(
    row: Any,
    index: int,
    camera_keys: Dict[str, int],
    has_cameras_section: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(row, dict):
        return ([_err("camera_capabilities", index, "", "row is not an object")], warnings)
    cam_ref = _first_present(row, "camera_id", "camera", "cam")
    if cam_ref is None or (isinstance(cam_ref, str) and not cam_ref.strip()):
        errors.append(
            _err(
                "camera_capabilities",
                index,
                "camera_id",
                "camera capability 'camera_id' is required",
            )
        )
    elif has_cameras_section and cam_ref not in camera_keys and str(cam_ref) not in camera_keys:
        errors.append(
            _err(
                "camera_capabilities",
                index,
                "camera_id",
                "capability references unknown camera '%s'" % (cam_ref,),
                code="unknown_reference",
            )
        )
    return errors, warnings


def _validate_permission(
    row: Any,
    index: int,
    camera_keys: Dict[str, int],
    has_cameras_section: bool,
    seen_pairs: Dict[Tuple[str, str], int],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(row, dict):
        return ([_err("camera_permissions", index, "", "row is not an object")], warnings)
    user_id = _first_present(row, "user_id", "user", "uid", "username")
    cam_ref = _first_present(row, "camera_id", "camera", "cam")
    perm = _first_present(row, "permission", "perm", "access", "level", "role")
    if user_id is None or (isinstance(user_id, str) and not user_id.strip()):
        errors.append(
            _err(
                "camera_permissions",
                index,
                "user_id",
                "camera permission 'user_id' is required",
            )
        )
    if cam_ref is None or (isinstance(cam_ref, str) and not str(cam_ref).strip()):
        errors.append(
            _err(
                "camera_permissions",
                index,
                "camera_id",
                "camera permission 'camera_id' is required",
            )
        )
    elif has_cameras_section and cam_ref not in camera_keys and str(cam_ref) not in camera_keys:
        errors.append(
            _err(
                "camera_permissions",
                index,
                "camera_id",
                "permission references unknown camera '%s'" % (cam_ref,),
                code="unknown_reference",
            )
        )
    if perm is None or (isinstance(perm, str) and not perm.strip()):
        warnings.append(
            _warn(
                "camera_permissions",
                index,
                "permission",
                "permission defaults to 'view'",
            )
        )
    elif str(perm).strip().lower() not in _VALID_PERMISSIONS:
        errors.append(
            _err(
                "camera_permissions",
                index,
                "permission",
                "unknown permission '%s', expected one of: %s"
                % (perm, ", ".join(sorted(_VALID_PERMISSIONS))),
            )
        )
    if user_id is not None and cam_ref is not None:
        pair = (str(user_id), str(cam_ref))
        if pair in seen_pairs:
            errors.append(
                _err(
                    "camera_permissions",
                    index,
                    "camera_id",
                    "duplicate permission for user '%s' camera '%s'" % pair,
                    code="duplicate",
                )
            )
        else:
            seen_pairs[pair] = index
    return errors, warnings


def _validate_settings_row(
    row: Any, index: int
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not isinstance(row, dict):
        return ([_err("settings", index, "", "settings entry must be an object")], [])
    return [], []


def _validate_user_row(
    row: Any, index: int, seen: Dict[str, int]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(row, dict):
        return ([_err("users", index, "", "row is not an object")], warnings)
    username = str(row.get("username") or "").strip()
    if not username:
        errors.append(_err("users", index, "username", "user 'username' is required"))
    elif username in seen:
        errors.append(
            _err("users", index, "username", "duplicate username '%s'" % username, code="duplicate")
        )
    else:
        seen[username] = index
    if not _first_present(row, "hashed_password", "password_hash", "password"):
        warnings.append(
            _warn("users", index, "hashed_password", "user has no password hash")
        )
    return errors, warnings


def _validate_role_row(
    row: Any, index: int, seen: Dict[str, int]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(row, dict):
        return ([_err("roles", index, "", "row is not an object")], warnings)
    name = str(row.get("name") or "").strip()
    if not name:
        errors.append(_err("roles", index, "name", "role 'name' is required"))
    elif name in seen:
        errors.append(
            _err("roles", index, "name", "duplicate role name '%s'" % name, code="duplicate")
        )
    else:
        seen[name] = index
    return errors, warnings


# ---------------------------------------------------------------------------
# Dry-run entry point (Frigate ``parse`` plus cross-checks, without writes)
# ---------------------------------------------------------------------------


def validate_config_dry_run(
    payload: Any = None,
    sections: Any = None,
    strict: bool = False,
    check_references: bool = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Validate a system-config payload without persisting anything.

    Args:
        payload: Envelope (``{"sections": {...}}``), flat section map,
            JSON/YAML string or bytes. Single-key wrappers (``config``,
            ``data``, ...) are unwrapped like :mod:`app.services.config_io`.
        sections: Optional allow-list of sections to validate. Unknown
            names are ignored.
        strict: When True, ``strict_valid`` requires zero warnings in
            addition to zero errors.
        check_references: When False, cross-section camera/group reference
            checks are skipped (payload-local existence only).

    Returns:
        Report dict with ``valid``, ``errors``, ``warnings``, ``counts``,
        ``sections``, ``unknown_sections`` and a ``summary`` line. Never
        raises for invalid payloads; only raises ``TypeError``-style for
        programming errors. No database or filesystem writes are made.
    """
    wanted = _normalize_sections_arg(sections if sections is not None else kwargs.get("include"))
    if wanted is None and kwargs.get("sections") is not None and sections is None:
        wanted = _normalize_sections_arg(kwargs.get("sections"))
    strict = bool(kwargs.get("strict", strict) if "strict" in kwargs else strict)
    if isinstance(strict, str):
        strict = strict.strip().lower() in ("1", "true", "yes", "y", "on", "strict")
    check_references = kwargs.get("check_references", check_references)
    if isinstance(check_references, str):
        check_references = check_references.strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
    else:
        check_references = bool(check_references)

    extracted = extract_sections(payload)
    if wanted is not None:
        extracted = {k: v for k, v in extracted.items() if k in wanted}

    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    unknown_sections = sorted(n for n in extracted if n not in _ALL_KNOWN)
    for name in unknown_sections:
        warnings.append(
            _warn(
                str(name),
                -1,
                "",
                "unknown section '%s' will be ignored on import" % name,
                code="unknown_section",
            )
        )

    known = {k: v for k, v in extracted.items() if k in _ALL_KNOWN}
    if payload is None or (isinstance(payload, (str, bytes, bytearray)) and not str(payload).strip()):
        errors.append(_err("_payload", -1, "", "empty config payload"))
    elif not known or not any(isinstance(v, list) and v for v in known.values()):
        has_any = bool(extracted)
        if has_any and not known:
            errors.append(
                _err("_payload", -1, "", "no known config sections found in payload")
            )
        elif not any(isinstance(v, list) and v for v in known.values()):
            errors.append(
                _err("_payload", -1, "", "no config entries found in payload")
            )

    # Build reference sets from the payload itself (Frigate checks in-config
    # references such as zones and models the same way).
    camera_keys: Dict[str, int] = {}
    camera_name_set: Dict[str, int] = {}
    group_names: Dict[str, int] = {}
    group_ids: Dict[Any, int] = {}
    has_cameras_section = bool(known.get("cameras"))
    has_groups_section = bool(known.get("groups"))
    if not check_references:
        has_cameras_section = False
        has_groups_section = False
    else:
        for idx, row in enumerate(known.get("cameras", [])):
            if isinstance(row, dict):
                nm = str(row.get("name") or "").strip()
                if nm and nm not in camera_name_set:
                    camera_name_set[nm] = idx
                for key in (row.get("id"), row.get("name")):
                    if key is not None and str(key).strip():
                        camera_keys.setdefault(key, idx)
                        camera_keys.setdefault(str(key), idx)
        for idx, row in enumerate(known.get("groups", [])):
            if isinstance(row, dict):
                nm = str(row.get("name") or "").strip()
                if nm and nm not in group_names:
                    group_names[nm] = idx
                gid = row.get("id")
                if gid is not None:
                    group_ids.setdefault(gid, idx)
                    group_ids.setdefault(str(gid), idx)

    counts: Dict[str, int] = {}
    per_section: Dict[str, Dict[str, Any]] = {}
    # Trackers for duplicates.
    cam_names: Dict[str, int] = {}
    cam_ids: Dict[Any, int] = {}
    grp_names: Dict[str, int] = {}
    layout_names: Dict[str, int] = {}
    storage_names: Dict[str, int] = {}
    storage_paths: Dict[str, int] = {}
    perm_pairs: Dict[Tuple[str, str], int] = {}
    user_names: Dict[str, int] = {}
    role_names: Dict[str, int] = {}

    # Duplicate source detection (warning, like Frigate duplicate-device hints).
    source_users: Dict[str, List[str]] = {}

    order = list(_ALL_KNOWN) + unknown_sections
    for section in order:
        rows = known.get(section, [])
        if section not in known:
            continue
        if not isinstance(rows, list):
            rows = _coerce_rows(rows)
        counts[section] = len(rows)
        serr: List[Dict[str, Any]] = []
        swarn: List[Dict[str, Any]] = []
        if section == "groups":
            for idx, row in enumerate(rows):
                e, w = _validate_group(row, idx, grp_names)
                serr.extend(e)
                swarn.extend(w)
        elif section == "cameras":
            for idx, row in enumerate(rows):
                e, w = _validate_camera(
                    row, idx, cam_names, cam_ids, group_names, group_ids, has_groups_section
                )
                serr.extend(e)
                swarn.extend(w)
                if isinstance(row, dict):
                    src = _first_present(row, "source", "source_url", "url", "stream_url")
                    nm = str(row.get("name") or "index %d" % idx)
                    if isinstance(src, str) and src.strip():
                        source_users.setdefault(src.strip(), []).append(nm)
        elif section == "layouts":
            for idx, row in enumerate(rows):
                e, w = _validate_layout(row, idx, layout_names)
                serr.extend(e)
                swarn.extend(w)
        elif section == "storage_areas":
            for idx, row in enumerate(rows):
                e, w = _validate_storage(row, idx, storage_names, storage_paths)
                serr.extend(e)
                swarn.extend(w)
        elif section == "relay_settings":
            for idx, row in enumerate(rows):
                e, w = _validate_relay(row, idx)
                serr.extend(e)
                swarn.extend(w)
        elif section == "standing_rules":
            for idx, row in enumerate(rows):
                e, w = _validate_standing_rule(row, idx, camera_keys, has_cameras_section)
                serr.extend(e)
                swarn.extend(w)
        elif section == "ptz_schedules":
            for idx, row in enumerate(rows):
                e, w = _validate_ptz(row, idx, camera_keys, has_cameras_section)
                serr.extend(e)
                swarn.extend(w)
        elif section == "linked_monitor_triggers":
            for idx, row in enumerate(rows):
                e, w = _validate_linked(row, idx, camera_keys, has_cameras_section)
                serr.extend(e)
                swarn.extend(w)
        elif section == "camera_capabilities":
            for idx, row in enumerate(rows):
                e, w = _validate_capability(row, idx, camera_keys, has_cameras_section)
                serr.extend(e)
                swarn.extend(w)
        elif section == "camera_permissions":
            for idx, row in enumerate(rows):
                e, w = _validate_permission(
                    row, idx, camera_keys, has_cameras_section, perm_pairs
                )
                serr.extend(e)
                swarn.extend(w)
        elif section == "settings":
            for idx, row in enumerate(rows):
                e, w = _validate_settings_row(row, idx)
                serr.extend(e)
                swarn.extend(w)
        elif section == "users":
            for idx, row in enumerate(rows):
                e, w = _validate_user_row(row, idx, user_names)
                serr.extend(e)
                swarn.extend(w)
        elif section == "roles":
            for idx, row in enumerate(rows):
                e, w = _validate_role_row(row, idx, role_names)
                serr.extend(e)
                swarn.extend(w)
        errors.extend(serr)
        warnings.extend(swarn)
        per_section[section] = {
            "count": len(rows),
            "valid": len(serr) == 0,
            "errors": serr,
            "warnings": swarn,
            "error_count": len(serr),
            "warning_count": len(swarn),
        }

    for src, users in source_users.items():
        if len(users) > 1:
            warnings.append(
                _warn(
                    "cameras",
                    -1,
                    "source",
                    "source '%s' is shared by cameras: %s" % (src, ", ".join(users)),
                    code="shared_source",
                )
            )

    if has_groups_section and known.get("cameras"):
        referenced: set = set()
        for row in known.get("cameras", []):
            if isinstance(row, dict):
                g = _first_present(row, "group", "group_name")
                if isinstance(g, str) and g.strip():
                    referenced.add(g.strip())
        for gname in group_names:
            if gname not in referenced:
                warnings.append(
                    _warn(
                        "groups",
                        group_names[gname],
                        "name",
                        "group '%s' is not referenced by any camera" % gname,
                        code="unreferenced",
                    )
                )

    # Attach unknown-section rows to counts for transparency.
    for name in unknown_sections:
        rows = extracted.get(name, [])
        counts[name] = len(rows) if isinstance(rows, list) else 0

    total = sum(n for s, n in counts.items() if s in _ALL_KNOWN)
    valid = len(errors) == 0
    strict_valid = valid and len(warnings) == 0 if strict else valid
    summary = "%d error(s), %d warning(s) across %d row(s) in %d section(s)" % (
        len(errors),
        len(warnings),
        total,
        len([s for s in counts if s in _ALL_KNOWN]),
    )
    logger.debug("Config dry-run validation: %s", summary)
    report: Dict[str, Any] = {
        "valid": valid,
        "ok": valid,
        "success": valid,
        "strict_valid": strict_valid,
        "strict": bool(strict),
        "dry_run": True,
        "applied": False,
        "persisted": False,
        "writes": False,
        "count": total,
        "total": total,
        "counts": dict(counts),
        "sections": dict(counts),
        "per_section": per_section,
        "results": [
            {
                "section": name,
                "count": info["count"],
                "valid": info["valid"],
                "error_count": info["error_count"],
                "warning_count": info["warning_count"],
            }
            for name, info in per_section.items()
        ],
        "errors": errors,
        "warnings": warnings,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "unknown_sections": list(unknown_sections),
        "known_sections": sum(1 for s in counts if s in _ALL_KNOWN),
        "version": CONFIG_VALIDATE_VERSION,
        "kind": CONFIG_VALIDATE_KIND,
        "summary": summary,
        "message": summary,
    }
    return report


def validate_envelope(payload: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return validate_config_dry_run(payload, **kwargs)


def validate_payload(payload: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return validate_config_dry_run(payload, **kwargs)


def dry_run(payload: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return validate_config_dry_run(payload, **kwargs)


def check(payload: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return validate_config_dry_run(payload, **kwargs)


def validate(payload: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return validate_config_dry_run(payload, **kwargs)


def validate_config(payload: Any = None, **kwargs: Any) -> bool:
    """Return True when the payload has no validation errors."""
    try:
        return bool(validate_config_dry_run(payload, **kwargs).get("valid"))
    except Exception:  # noqa: BLE001
        return False


def is_valid(payload: Any = None, **kwargs: Any) -> bool:
    return validate_config(payload, **kwargs)


def errors_for(payload: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    try:
        report = validate_config_dry_run(payload, **kwargs)
        errs = report.get("errors", [])
        return list(errs) if isinstance(errs, list) else []
    except Exception:  # noqa: BLE001
        return []


def warnings_for(payload: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    try:
        report = validate_config_dry_run(payload, **kwargs)
        warns = report.get("warnings", [])
        return list(warns) if isinstance(warns, list) else []
    except Exception:  # noqa: BLE001
        return []


def error_messages(payload: Any = None, **kwargs: Any) -> List[str]:
    msgs: List[str] = []
    for item in errors_for(payload, **kwargs):
        try:
            loc = ".".join(str(p) for p in item.get("loc", []) if p != "")
            msgs.append("%s: %s" % (loc or item.get("section", "?"), item.get("message", "")))
        except Exception:  # noqa: BLE001
            continue
    return msgs


def format_errors(payload: Any = None, **kwargs: Any) -> str:
    lines = error_messages(payload, **kwargs)
    if not lines:
        return "Configuration is valid."
    header = "Your configuration is invalid."
    return header + "\n" + "\n".join("Line %s" % line if False else line for line in lines)


# ---------------------------------------------------------------------------
# Object-oriented facade plus compatibility aliases
# ---------------------------------------------------------------------------


class ConfigValidateService:
    """Stateless dry-run validator (no DB or filesystem writes)."""

    def validate(
        self, payload: Any = None, **kwargs: Any
    ) -> Dict[str, Any]:
        return validate_config_dry_run(payload, **kwargs)

    def dry_run(self, payload: Any = None, **kwargs: Any) -> Dict[str, Any]:
        return validate_config_dry_run(payload, **kwargs)

    def check(self, payload: Any = None, **kwargs: Any) -> Dict[str, Any]:
        return validate_config_dry_run(payload, **kwargs)

    def validate_envelope(self, payload: Any = None, **kwargs: Any) -> Dict[str, Any]:
        return validate_config_dry_run(payload, **kwargs)

    def validate_payload(self, payload: Any = None, **kwargs: Any) -> Dict[str, Any]:
        return validate_config_dry_run(payload, **kwargs)

    def is_valid(self, payload: Any = None, **kwargs: Any) -> bool:
        return validate_config(payload, **kwargs)

    def errors(self, payload: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
        return errors_for(payload, **kwargs)

    def warnings(self, payload: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
        return warnings_for(payload, **kwargs)

    def extract(self, payload: Any = None) -> Dict[str, List[Dict[str, Any]]]:
        return extract_sections(payload)


config_validate_service = ConfigValidateService()
service = config_validate_service
manager = config_validate_service
validator = config_validate_service
dry_run_service = config_validate_service
config_service = config_validate_service
validate_service = config_validate_service
default_service = config_validate_service

ConfigValidateManager = ConfigValidateService
ConfigValidator = ConfigValidateService
DryRunService = ConfigValidateService
ConfigDryRunService = ConfigValidateService
SystemConfigValidateService = ConfigValidateService

# Lowercase/camel aliases used across the codebase conventions.
validate_dry_run = validate_config_dry_run
validate_dryrun = validate_config_dry_run
dryrun = validate_config_dry_run
dryRun = validate_config_dry_run
config_dry_run = validate_config_dry_run
config_validate = validate_config_dry_run
validate_sections = validate_config_dry_run
extract_config = extract_sections
parse_payload = extract_sections
parse = extract_sections
get_errors = errors_for
get_warnings = warnings_for

__all__ = [
    "CONFIG_VALIDATE_VERSION",
    "CONFIG_VALIDATE_KIND",
    "KNOWN_SECTIONS",
    "OPTIONAL_SECTIONS",
    "ALL_KNOWN_SECTIONS",
    "extract_sections",
    "validate_config_dry_run",
    "validate_envelope",
    "validate_payload",
    "dry_run",
    "check",
    "validate",
    "validate_config",
    "is_valid",
    "errors_for",
    "warnings_for",
    "error_messages",
    "format_errors",
    "ConfigValidateService",
    "config_validate_service",
    "service",
    "manager",
    "validator",
]
