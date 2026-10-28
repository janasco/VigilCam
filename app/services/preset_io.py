"""Camera preset import/export helpers.

A *camera preset* is a reusable, JSON-serializable snapshot of a camera's
configuration (everything needed to recreate the camera via
``POST /api/cameras`` without copying runtime state such as ``id`` or
timestamps). This module provides pure helpers for converting between
``Camera`` rows / dicts and preset dicts, plus JSON envelope helpers and
async DB-backed export/import used by :mod:`app.api.preset_io`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PRESET_IO_VERSION = "1.0"
PRESET_KIND = "camera-presets"

# Camera columns that constitute a reusable preset. Runtime state
# (id, created_at, last_activity_at, last_motion_at) is intentionally excluded.
PRESET_FIELDS: Tuple[str, ...] = (
    "name",
    "kind",
    "source",
    "username",
    "password",
    "onvif_host",
    "ip_address",
    "control_scheme",
    "onvif_port",
    "enabled",
    "recording_enabled",
    "detection_enabled",
    "input_type",
    "recording_mode",
    "detector_enabled",
    "alert_enabled",
    "stream_quality",
    "rotation",
    "watermark",
    "audio_enabled",
    "storage_profile",
    "width",
    "height",
    "fps",
    "group",
    "group_id",
    "motion_zones",
    "manufacturer",
    "model",
    "firmware_version",
    "serial_number",
    "hardware_id",
)

_REQUIRED_FIELDS: Tuple[str, ...] = ("name", "source")

_VALID_KINDS = {"usb", "ip", "onvif"}


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        if hasattr(value, "model_dump"):
            return value.model_dump()
        if hasattr(value, "to_dict"):
            return value.to_dict()
    except Exception:  # noqa: BLE001
        pass
    return str(value)


def _as_dict(obj: Any) -> Dict[str, Any]:
    """Best-effort conversion of a Camera row / pydantic model / mapping."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "model_dump"):
        try:
            data = obj.model_dump()
            if isinstance(data, dict):
                return dict(data)
        except Exception:  # noqa: BLE001
            pass
    # SQLAlchemy rows expose __table__ columns; fall back to __dict__.
    data: Dict[str, Any] = {}
    try:
        table = getattr(obj, "__table__", None)
        if table is not None and hasattr(table, "columns"):
            for col in table.columns:  # type: ignore[union-attr]
                try:
                    data[col.name] = getattr(obj, col.name, None)
                except Exception:  # noqa: BLE001
                    pass
            return data
    except Exception:  # noqa: BLE001
        pass
    try:
        raw = getattr(obj, "__dict__", None)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if not k.startswith("_"):
                    data[k] = v
            return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def camera_to_preset(camera: Any) -> Dict[str, Any]:
    """Serialize a Camera row / dict / pydantic model to a preset dict."""
    data = _as_dict(camera)
    # Support alias motionZones.
    if "motion_zones" not in data and data.get("motionZones") is not None:
        data["motion_zones"] = data.get("motionZones")
    preset: Dict[str, Any] = {}
    for field in PRESET_FIELDS:
        preset[field] = data.get(field)
    # Sensible defaults matching CameraBase.
    if preset.get("kind") is None:
        preset["kind"] = "ip"
    if preset.get("enabled") is None:
        preset["enabled"] = True
    if preset.get("recording_enabled") is None:
        preset["recording_enabled"] = True
    if preset.get("detection_enabled") is None:
        preset["detection_enabled"] = True
    # Drop alias key if present.
    preset.pop("motionZones", None)
    return preset


def preset_to_camera_payload(preset: Any) -> Dict[str, Any]:
    """Normalize a preset dict to a payload suitable for CameraCreate."""
    data = _as_dict(preset)
    # Unwrap single-key envelopes accidentally passed here.
    if set(data.keys()) <= {"preset", "camera", "data", "item"} and len(data) == 1:
        for key in ("preset", "camera", "data", "item"):
            if isinstance(data.get(key), dict):
                data = dict(data[key])
                break
    payload: Dict[str, Any] = {}
    for field in PRESET_FIELDS:
        if field in data:
            payload[field] = data[field]
        elif field == "motion_zones" and data.get("motionZones") is not None:
            payload[field] = data.get("motionZones")
    # Ignore runtime keys if callers passed full camera dicts.
    for drop in ("id", "created_at", "last_activity_at", "last_motion_at", "active"):
        payload.pop(drop, None)
    # Normalize kind.
    kind = payload.get("kind")
    if kind is not None:
        ks = str(kind).strip().lower()
        payload["kind"] = ks if ks in _VALID_KINDS else "ip"
    else:
        payload["kind"] = "ip"
    return payload


def validate_preset(preset: Any) -> bool:
    """Return True when *preset* has the minimum required camera fields."""
    if preset is None:
        return False
    data = _as_dict(preset)
    # Unwrap envelope-style single preset.
    if set(data.keys()) <= {"preset", "camera", "data", "item"} and len(data) == 1:
        for key in ("preset", "camera", "data", "item"):
            if isinstance(data.get(key), dict):
                data = dict(data[key])
                break
    for field in _REQUIRED_FIELDS:
        value = data.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
    kind = data.get("kind")
    if kind is not None and str(kind).strip():
        if str(kind).strip().lower() not in _VALID_KINDS:
            return False
    return True


def validate_presets(presets: Any) -> List[bool]:
    items = _coerce_to_list(presets)
    return [validate_preset(p) for p in items]


def is_valid_preset(preset: Any) -> bool:
    return validate_preset(preset)


def normalize_preset(preset: Any) -> Dict[str, Any]:
    return preset_to_camera_payload(preset)


def normalize_presets(presets: Any) -> List[Dict[str, Any]]:
    return [preset_to_camera_payload(p) for p in _coerce_to_list(presets)]


def _coerce_to_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return [value]


def extract_presets(payload: Any) -> List[Dict[str, Any]]:
    """Extract a list of preset dicts from flexible import payloads.

    Accepts:
    - a list of preset dicts
    - an envelope dict like ``{"presets": [...]}`` (also ``cameras``,
      ``items``, ``data``, ``entries`` keys)
    - a single preset dict
    - a JSON string encoding any of the above
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return []
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except Exception:  # noqa: BLE001
            return []
    if payload is None:
        return []
    if isinstance(payload, list):
        return [dict(p) if isinstance(p, dict) else p for p in payload]
    if isinstance(payload, tuple):
        return list(payload)
    if isinstance(payload, dict):
        for key in ("presets", "cameras", "items", "data", "entries", "results", "rows"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [dict(p) if isinstance(p, dict) else p for p in nested]
            if isinstance(nested, dict):
                # e.g. {"data": {"presets": [...]}} already handled; mapping of name->preset
                values = list(nested.values())
                if values and all(isinstance(v, dict) for v in values):
                    return [dict(v) for v in values]
        # Single preset dict (has required fields) -> wrap.
        if any(k in payload for k in ("name", "source", "preset", "camera")):
            # If it looks like {"preset": {...}} unwrap handled by callers; keep as single.
            inner = None
            if set(payload.keys()) <= {"preset", "camera", "data", "item"} and len(payload) == 1:
                for key in ("preset", "camera", "data", "item"):
                    if isinstance(payload.get(key), dict):
                        inner = payload[key]
                        break
            if inner is not None:
                return [dict(inner)]
            return [dict(payload)]
        # Mapping of name -> preset dict.
        values = list(payload.values())
        if values and all(isinstance(v, dict) for v in values):
            return [dict(v) for v in values]
        return []
    return []


def parse_import_payload(payload: Any) -> List[Dict[str, Any]]:
    return extract_presets(payload)


def build_envelope(presets: Iterable[Any], version: str = PRESET_IO_VERSION) -> Dict[str, Any]:
    items = [camera_to_preset(p) if not _looks_like_preset(p) else dict(_as_dict(p)) for p in (presets or [])]
    # Normalize through camera_to_preset when items look like cameras.
    normalized: List[Dict[str, Any]] = []
    for item in items:
        d = dict(item)
        # If item already has preset shape keep it, else convert.
        if _looks_like_preset(d):
            # Ensure all preset fields present.
            full = camera_to_preset(d)
            # Preserve any extra keys? keep minimal preset shape.
            normalized.append(full)
        else:
            normalized.append(camera_to_preset(d))
    return {
        "version": version,
        "kind": PRESET_KIND,
        "count": len(normalized),
        "presets": normalized,
    }


def _looks_like_preset(data: Any) -> bool:
    if not isinstance(data, dict):
        d = _as_dict(data)
    else:
        d = data
    return isinstance(d.get("name"), str) and isinstance(d.get("source"), str)


def unwrap_envelope(payload: Any) -> List[Dict[str, Any]]:
    return extract_presets(payload)


def export_presets(cameras: Iterable[Any]) -> List[Dict[str, Any]]:
    """Pure export: serialize an iterable of cameras to preset dicts."""
    if cameras is None:
        return []
    if isinstance(cameras, dict):
        # Envelope passed by mistake -> extract then re-export.
        extracted = extract_presets(cameras)
        if extracted:
            return [camera_to_preset(p) for p in extracted]
        return [camera_to_preset(cameras)]
    items = list(cameras) if not isinstance(cameras, list) else cameras
    return [camera_to_preset(c) for c in items]


def export_presets_json(cameras: Iterable[Any], indent: int = 2) -> str:
    envelope = build_envelope(cameras or [])
    return json.dumps(envelope, indent=indent, default=_json_default)


def dumps(cameras: Iterable[Any], indent: int = 2) -> str:
    return export_presets_json(cameras, indent=indent)


def dump_json(cameras: Iterable[Any], indent: int = 2) -> str:
    return export_presets_json(cameras, indent=indent)


def to_json(cameras: Iterable[Any], indent: int = 2) -> str:
    return export_presets_json(cameras, indent=indent)


def to_json_str(cameras: Iterable[Any], indent: int = 2) -> str:
    return export_presets_json(cameras, indent=indent)


def serialize(cameras: Iterable[Any], indent: int = 2) -> str:
    return export_presets_json(cameras, indent=indent)


def to_list(cameras: Iterable[Any]) -> List[Dict[str, Any]]:
    return export_presets(cameras)


def to_dict_list(cameras: Iterable[Any]) -> List[Dict[str, Any]]:
    return export_presets(cameras)


def parse_json(text: Any) -> List[Dict[str, Any]]:
    return extract_presets(text)


def loads(text: Any) -> List[Dict[str, Any]]:
    return extract_presets(text)


def from_json(text: Any) -> List[Dict[str, Any]]:
    return extract_presets(text)


def deserialize(text: Any) -> List[Dict[str, Any]]:
    return extract_presets(text)


def load_json(text: Any) -> List[Dict[str, Any]]:
    return extract_presets(text)


def import_presets_data(payload: Any, skip_invalid: bool = True) -> List[Dict[str, Any]]:
    """Pure import: validate + normalize payload to camera payload dicts."""
    items = extract_presets(payload)
    out: List[Dict[str, Any]] = []
    for item in items:
        if validate_preset(item):
            out.append(preset_to_camera_payload(item))
        elif not skip_invalid:
            out.append(preset_to_camera_payload(item))
    return out


def save_presets(path: Any, cameras: Iterable[Any], indent: int = 2) -> str:
    """Write export envelope JSON to *path*. Returns the path as string."""
    p = Path(str(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    envelope = build_envelope(cameras or [])
    p.write_text(json.dumps(envelope, indent=indent, default=_json_default), encoding="utf-8")
    return str(p)


def load_presets_file(path: Any) -> List[Dict[str, Any]]:
    """Read presets from a JSON file (envelope or bare list)."""
    p = Path(str(path))
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return extract_presets(raw)


# File-helper aliases.
def save_json(path: Any, cameras: Iterable[Any], indent: int = 2) -> str:
    return save_presets(path, cameras, indent=indent)


def dump_file(path: Any, cameras: Iterable[Any], indent: int = 2) -> str:
    return save_presets(path, cameras, indent=indent)


def write_file(path: Any, cameras: Iterable[Any], indent: int = 2) -> str:
    return save_presets(path, cameras, indent=indent)


def dump(path: Any, cameras: Iterable[Any] = None, **kwargs: Any) -> str:  # type: ignore[override]
    # Support dump(cameras) -> json string and dump(path, cameras) -> file.
    if cameras is None and isinstance(path, (list, tuple)):
        return export_presets_json(path, indent=int(kwargs.get("indent", 2)))
    if isinstance(path, (str, Path)) and cameras is not None:
        return save_presets(path, cameras, indent=int(kwargs.get("indent", 2)))
    if isinstance(path, (str, Path)):
        # dump(path) with no cameras -> not meaningful; try reading? return empty file write.
        return save_presets(path, [], indent=int(kwargs.get("indent", 2)))
    return export_presets_json(path, indent=int(kwargs.get("indent", 2)))


def load_file(path: Any) -> List[Dict[str, Any]]:
    return load_presets_file(path)


def read_file(path: Any) -> List[Dict[str, Any]]:
    return load_presets_file(path)


def load(path: Any) -> List[Dict[str, Any]]:
    p = Path(str(path)) if isinstance(path, (str, Path)) else None
    if p is not None and p.exists():
        return load_presets_file(p)
    return extract_presets(path)


# ---------------------------------------------------------------------------
# DB-backed helpers
# ---------------------------------------------------------------------------

async def export_presets_db(db: Any) -> List[Dict[str, Any]]:
    """Export all cameras from the DB as preset dicts."""
    from sqlalchemy import select as _select

    from app.models.entities import Camera as _Camera

    result = await db.execute(_select(_Camera).order_by(_Camera.id))
    cameras = list(result.scalars().all())
    return export_presets(cameras)


async def import_presets_db(
    db: Any,
    payload: Any,
    skip_invalid: bool = True,
    update_existing: bool = False,
) -> Dict[str, Any]:
    """Import presets into the DB, creating Camera rows.

    Returns ``{"imported": [...], "count": n, "errors": [...]}`` where
    ``imported`` holds the serialized created cameras.
    """
    from app.models.entities import Camera as _Camera

    items = extract_presets(payload)
    imported: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for index, item in enumerate(items):
        if not validate_preset(item):
            if skip_invalid:
                errors.append({"index": index, "error": "invalid preset: 'name' and 'source' are required"})
                continue
        try:
            data = preset_to_camera_payload(item)
            # Never reuse incoming ids.
            data.pop("id", None)
            # Optionally update a camera with the same name instead of creating.
            existing = None
            if update_existing and data.get("name"):
                try:
                    from sqlalchemy import select as _select

                    res = await db.execute(_select(_Camera).where(_Camera.name == data["name"]))
                    existing = res.scalars().first()
                except Exception:  # noqa: BLE001
                    existing = None
            if existing is not None:
                for key, value in data.items():
                    if hasattr(existing, key) and value is not None:
                        setattr(existing, key, value)
                await db.commit()
                try:
                    await db.refresh(existing)
                except Exception:  # noqa: BLE001
                    pass
                imported.append(camera_to_preset(existing))
            else:
                camera = _Camera(**{k: v for k, v in data.items() if k in PRESET_FIELDS})
                db.add(camera)
                await db.commit()
                try:
                    await db.refresh(camera)
                except Exception:  # noqa: BLE001
                    pass
                imported.append(camera_to_preset(camera))
                # Keep id for API responses.
                try:
                    imported[-1]["id"] = getattr(camera, "id", None)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            errors.append({"index": index, "error": str(exc)})
    return {"imported": imported, "count": len(imported), "errors": errors}


class PresetIOService:
    """Object-oriented facade around the module-level helpers."""

    def export(self, cameras: Iterable[Any]) -> List[Dict[str, Any]]:
        return export_presets(cameras)

    def export_json(self, cameras: Iterable[Any], indent: int = 2) -> str:
        return export_presets_json(cameras, indent=indent)

    def parse(self, payload: Any) -> List[Dict[str, Any]]:
        return extract_presets(payload)

    def validate(self, preset: Any) -> bool:
        return validate_preset(preset)

    def normalize(self, preset: Any) -> Dict[str, Any]:
        return preset_to_camera_payload(preset)

    def prepare_import(self, payload: Any, skip_invalid: bool = True) -> List[Dict[str, Any]]:
        return import_presets_data(payload, skip_invalid=skip_invalid)

    def save(self, path: Any, cameras: Iterable[Any], indent: int = 2) -> str:
        return save_presets(path, cameras, indent=indent)

    def load(self, path: Any) -> List[Dict[str, Any]]:
        return load_presets_file(path)

    async def export_db(self, db: Any) -> List[Dict[str, Any]]:
        return await export_presets_db(db)

    async def import_db(self, db: Any, payload: Any, **kwargs: Any) -> Dict[str, Any]:
        return await import_presets_db(db, payload, **kwargs)


preset_io_service = PresetIOService()
service = preset_io_service
manager = preset_io_service


# Compatibility aliases for varied caller naming.
export = export_presets
export_list = export_presets
list_presets = export_presets
get_presets = export_presets
dump_presets = export_presets
export_camera_presets = export_presets
export_presets_list = export_presets
export_to_dict = export_presets
export_to_list = export_presets

import_presets = import_presets_data
parse_presets = extract_presets
parse = extract_presets
get_all = extract_presets
fetch_presets = extract_presets
load_presets = extract_presets
read_presets = extract_presets
normalize = normalize_preset
validate = validate_preset

export_from_db = export_presets_db
list_preset_dicts = export_presets_db
get_presets_db = export_presets_db
export_db = export_presets_db

import_to_db = import_presets_db
import_into_db = import_presets_db
import_db = import_presets_db
create_from_presets = import_presets_db

save = save_presets
write = save_presets
load_state = load_presets_file

camera_to_dict = camera_to_preset
camera_preset_to_dict = camera_to_preset
dict_to_camera_preset = preset_to_camera_payload
dict_to_preset = preset_to_camera_payload
camera_dict_to_preset = camera_to_preset
preset_to_dict = preset_to_camera_payload

wrap_envelope = build_envelope
build_export = build_envelope
make_envelope = build_envelope

__all__ = [
    "PRESET_IO_VERSION",
    "PRESET_KIND",
    "PRESET_FIELDS",
    "camera_to_preset",
    "preset_to_camera_payload",
    "validate_preset",
    "validate_presets",
    "is_valid_preset",
    "normalize_preset",
    "normalize_presets",
    "extract_presets",
    "parse_import_payload",
    "build_envelope",
    "unwrap_envelope",
    "export_presets",
    "export_presets_json",
    "dumps",
    "dump_json",
    "to_json",
    "to_json_str",
    "serialize",
    "to_list",
    "to_dict_list",
    "parse_json",
    "loads",
    "from_json",
    "deserialize",
    "load_json",
    "import_presets_data",
    "save_presets",
    "load_presets_file",
    "save_json",
    "dump_file",
    "write_file",
    "dump",
    "load_file",
    "read_file",
    "load",
    "export_presets_db",
    "import_presets_db",
    "PresetIOService",
    "preset_io_service",
    "service",
]
