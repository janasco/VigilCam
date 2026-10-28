"""Full system configuration export/import service.

Exports and restores the operator-facing system configuration as a single
JSON envelope covering every configuration table (cameras, groups, layouts,
standing rules, relay settings, storage areas, PTZ schedules, linked monitor
triggers, camera capabilities and camera permissions) plus a snapshot of the
application settings.

This complements :mod:`app.services.backup` (versioned ``.zip`` archives with
a SQLite snapshot plus ``.env`` files) and :mod:`app.services.preset_io`
(camera presets only): ``config_io`` is the JSON document you can diff, edit
by hand, check into version control and move between installs.

No ``app/main.py`` changes are required: the API module auto-registers.

Envelope shape::

    {
      "version": "1.0",
      "kind": "vigilcam-system-config",
      "exported_at": "2026-01-01T00:00:00+00:00",
      "app_version": "0.1.0",
      "app_name": "VigilCam",
      "sections": {
        "cameras": [...],
        "groups": [...],
        ...
      },
      "counts": {"cameras": 2, ...}
    }

Import ``mode``:

- ``"merge"`` (default): upsert each section using its natural key (``name``
  for cameras/groups/layouts/storage areas, ``camera_id`` for capabilities,
  ``(user_id, camera_id)`` for permissions, ``singleton_key`` for relay
  settings). Repeat imports are idempotent except for append-only sections
  (standing rules, PTZ schedules, linked monitor triggers) which create a
  new row per entry.
- ``"replace"``: delete existing rows in the imported sections first, then
  insert. Camera/group id references are remapped through the import so
  cross-section links survive.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

CONFIG_IO_VERSION = "1.0"
CONFIG_KIND = "vigilcam-system-config"

# Sections exported by default (stable, operator-facing configuration).
# Volatile history (events, recordings, export jobs, audit logs) and local
# user credentials are intentionally excluded; ``users``/``roles`` are
# supported on explicit request via ``sections=[...]``.
CONFIG_SECTIONS: Tuple[str, ...] = (
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

OPTIONAL_SECTIONS: Tuple[str, ...] = ("users", "roles")

_ALL_KNOWN_SECTIONS: Tuple[str, ...] = CONFIG_SECTIONS + OPTIONAL_SECTIONS

# Camera id bearing fields remapped on import.
_CAMERA_ID_FIELDS: Tuple[str, ...] = (
    "camera_id",
    "source_camera_id",
    "target_camera_id",
    "monitor_id",
    "linked_monitor_id",
    "trigger_camera_id",
    "linked_camera_id",
    "source_id",
    "target_id",
)

_RUNTIME_DROP_KEYS = ("_sa_instance_state",)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:  # noqa: BLE001
            pass
    try:
        if hasattr(value, "to_dict"):
            return value.to_dict()
    except Exception:  # noqa: BLE001
        pass
    return str(value)


def _as_dict(obj: Any) -> Dict[str, Any]:
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
    table = getattr(obj, "__table__", None)
    if table is not None and hasattr(table, "columns"):
        data: Dict[str, Any] = {}
        try:
            for col in table.columns:  # type: ignore[union-attr]
                try:
                    data[col.name] = getattr(obj, col.name, None)
                except Exception:  # noqa: BLE001
                    pass
            return data
        except Exception:  # noqa: BLE001
            pass
    raw = getattr(obj, "__dict__", None)
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if k not in _RUNTIME_DROP_KEYS and not k.startswith("_")}
    return {}


def _serialize_row(obj: Any, columns: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    data = _as_dict(obj)
    if columns is not None:
        names = list(columns)
        data = {k: data.get(k) for k in names if k in data or True}
        # ensure every column key exists (missing -> None)
        for k in names:
            data.setdefault(k, getattr(obj, k, None) if not isinstance(obj, dict) else data.get(k))
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if key in _RUNTIME_DROP_KEYS or key.startswith("_sa_"):
            continue
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        else:
            try:
                json.dumps(value, default=_json_default)
                out[key] = value
            except Exception:  # noqa: BLE001
                out[key] = _json_default(value)
    return out


def _parse_datetime(value: Any) -> Any:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
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


def _model_columns(model: Any) -> List[str]:
    try:
        return [c.name for c in model.__table__.columns]
    except Exception:  # noqa: BLE001
        return []


def _section_model(section: str) -> Any:
    """Resolve a section name to its SQLAlchemy model (or None for settings)."""
    if section == "settings":
        return None
    try:
        from app.models import entities as _e
    except Exception:  # noqa: BLE001
        return None
    mapping = {
        "groups": getattr(_e, "Group", None),
        "cameras": getattr(_e, "Camera", None),
        "camera_capabilities": getattr(_e, "CameraCapability", None),
        "camera_permissions": getattr(_e, "CameraPermission", None),
        "layouts": getattr(_e, "Layout", None),
        "standing_rules": getattr(_e, "StandingRule", None),
        "relay_settings": getattr(_e, "RelaySettings", None),
        "storage_areas": getattr(_e, "StorageArea", None),
        "ptz_schedules": getattr(_e, "PTZSchedule", None),
        "linked_monitor_triggers": getattr(_e, "LinkedMonitorTrigger", None),
    }
    if section in mapping:
        return mapping[section]
    if section == "users":
        try:
            from app.models import auth as _a

            return getattr(_a, "User", None)
        except Exception:  # noqa: BLE001
            return None
    if section == "roles":
        try:
            from app.models import auth as _a

            return getattr(_a, "Role", None)
        except Exception:  # noqa: BLE001
            return None
    return None


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


def _resolve_sections(requested: Any) -> List[str]:
    wanted = _normalize_sections_arg(requested)
    if not wanted:
        return list(CONFIG_SECTIONS)
    known = [s for s in wanted if s in _ALL_KNOWN_SECTIONS]
    # Silently drop unknown section names so hand-edited files with extra
    # keys (or typos) import the parts we understand instead of failing.
    return known


def _settings_snapshot() -> Dict[str, Any]:
    try:
        from app.core.config import settings as _settings
    except Exception:  # noqa: BLE001
        return {}
    data = _as_dict(_settings)
    # pydantic v2 settings objects expose model_dump; _as_dict handles it.
    if not data:
        try:
            data = dict(_settings.model_dump())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            data = {}
    out: Dict[str, Any] = {}
    for key, value in data.items():
        try:
            json.dumps(value, default=_json_default)
            out[key] = value
        except Exception:  # noqa: BLE001
            out[key] = _json_default(value)
    return out


# ---------------------------------------------------------------------------
# Envelope helpers (pure, no DB)
# ---------------------------------------------------------------------------

def build_envelope(
    sections_data: Optional[Dict[str, Any]] = None,
    version: str = CONFIG_IO_VERSION,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Wrap per-section row lists into a versioned config envelope."""
    sections: Dict[str, List[Dict[str, Any]]] = {}
    for key, value in (sections_data or {}).items():
        if isinstance(value, list):
            sections[str(key)] = [dict(v) if isinstance(v, dict) else v for v in value]
        elif isinstance(value, dict):
            sections[str(key)] = [dict(value)]
        elif value is None:
            sections[str(key)] = []
    counts = {name: len(rows) for name, rows in sections.items()}
    try:
        from app.core.config import settings as _settings

        app_version = getattr(_settings, "APP_VERSION", "0.1.0")
        app_name = getattr(_settings, "APP_NAME", "VigilCam")
    except Exception:  # noqa: BLE001
        app_version, app_name = "0.1.0", "VigilCam"
    envelope: Dict[str, Any] = {
        "version": kwargs.get("version", version),
        "kind": kwargs.get("kind", CONFIG_KIND),
        "exported_at": kwargs.get("exported_at", _utcnow_iso()),
        "app_version": kwargs.get("app_version", app_version),
        "app_name": kwargs.get("app_name", app_name),
        "sections": sections,
        "counts": counts,
        "count": sum(counts.values()),
    }
    return envelope


def extract_config(payload: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Extract ``{section: [rows]}`` from flexible import payloads.

    Accepts an envelope (``{"sections": {...}}``), a flat section map
    (``{"cameras": [...]}``), a JSON string or UTF-8 bytes of either.
    Unknown sections are preserved so callers can warn about them.
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return {}
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except Exception:  # noqa: BLE001
            return {}
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
    # Unwrap single-key wrappers ({"config": {...}}, {"data": {...}}, ...).
    for _ in range(3):
        keys = set(data.keys())
        if len(keys) == 1:
            only = next(iter(keys))
            if only in ("config", "data", "body", "payload", "params", "options", "envelope", "import") and isinstance(
                data[only], dict
            ):
                data = dict(data[only])
                continue
        break
    sections: Dict[str, List[Dict[str, Any]]] = {}
    nested = data.get("sections")
    if isinstance(nested, dict):
        for name, rows in nested.items():
            sections[str(name)] = _coerce_rows(rows)
    # Flat section map at top level.
    for name in _ALL_KNOWN_SECTIONS:
        if name in sections:
            continue
        if name in data and data[name] is not None:
            rows = _coerce_rows(data[name])
            if rows or name in data:
                sections[name] = rows
    # Tolerate a bare list of cameras (preset-style) as {"cameras": [...]}.
    if not sections and isinstance(data.get("presets"), list):
        sections["cameras"] = _coerce_rows(data.get("presets"))
    if not sections and isinstance(data.get("cameras"), list):
        sections["cameras"] = _coerce_rows(data.get("cameras"))
    return sections


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


def parse_import_payload(payload: Any) -> Dict[str, List[Dict[str, Any]]]:
    return extract_config(payload)


def validate_config(payload: Any) -> bool:
    """Return True when *payload* holds at least one known config section."""
    sections = extract_config(payload)
    if not sections:
        return False
    for name, rows in sections.items():
        if name in _ALL_KNOWN_SECTIONS and isinstance(rows, list) and rows:
            return True
    return False


def validate_sections(payload: Any) -> Dict[str, bool]:
    sections = extract_config(payload)
    return {name: bool(rows) for name, rows in sections.items()}


def export_config_data(sections_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Pure export: wrap an in-memory section map in an envelope."""
    return build_envelope(dict(sections_data or {}))


def export_config_json(sections_data: Optional[Dict[str, Any]] = None, indent: int = 2) -> str:
    return json.dumps(build_envelope(dict(sections_data or {})), indent=indent, default=_json_default)


def dumps(sections_data: Optional[Dict[str, Any]] = None, indent: int = 2) -> str:
    return export_config_json(sections_data, indent=indent)


def dump_json(sections_data: Optional[Dict[str, Any]] = None, indent: int = 2) -> str:
    return export_config_json(sections_data, indent=indent)


def to_json(sections_data: Optional[Dict[str, Any]] = None, indent: int = 2) -> str:
    return export_config_json(sections_data, indent=indent)


def to_json_str(sections_data: Optional[Dict[str, Any]] = None, indent: int = 2) -> str:
    return export_config_json(sections_data, indent=indent)


def serialize(sections_data: Optional[Dict[str, Any]] = None, indent: int = 2) -> str:
    return export_config_json(sections_data, indent=indent)


def parse_json(text: Any) -> Dict[str, List[Dict[str, Any]]]:
    return extract_config(text)


def loads(text: Any) -> Dict[str, List[Dict[str, Any]]]:
    return extract_config(text)


def from_json(text: Any) -> Dict[str, List[Dict[str, Any]]]:
    return extract_config(text)


def deserialize(text: Any) -> Dict[str, List[Dict[str, Any]]]:
    return extract_config(text)


def load_json(text: Any) -> Dict[str, List[Dict[str, Any]]]:
    return extract_config(text)


def save_config(path: Any, sections_data: Any = None, indent: int = 2) -> str:
    """Write an envelope (or raw section map) to *path* as JSON."""
    p = Path(str(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(sections_data, dict) and "sections" in sections_data:
        envelope = sections_data
    else:
        envelope = build_envelope(dict(sections_data or {}))
    p.write_text(json.dumps(envelope, indent=indent, default=_json_default), encoding="utf-8")
    return str(p)


def load_config_file(path: Any) -> Dict[str, List[Dict[str, Any]]]:
    p = Path(str(path))
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return extract_config(raw)


def save_json(path: Any, sections_data: Any = None, indent: int = 2) -> str:
    return save_config(path, sections_data, indent=indent)


def dump_file(path: Any, sections_data: Any = None, indent: int = 2) -> str:
    return save_config(path, sections_data, indent=indent)


def write_file(path: Any, sections_data: Any = None, indent: int = 2) -> str:
    return save_config(path, sections_data, indent=indent)


def dump(path: Any, sections_data: Any = None, **kwargs: Any) -> Any:
    indent = int(kwargs.get("indent", 2))
    if isinstance(path, (str, Path)) and sections_data is not None:
        return save_config(path, sections_data, indent=indent)
    if isinstance(path, (str, Path)) and sections_data is None:
        return save_config(path, {}, indent=indent)
    return export_config_json(path, indent=indent)


def load_file(path: Any) -> Dict[str, List[Dict[str, Any]]]:
    return load_config_file(path)


def read_file(path: Any) -> Dict[str, List[Dict[str, Any]]]:
    return load_config_file(path)


def load(path: Any) -> Dict[str, List[Dict[str, Any]]]:
    p = Path(str(path)) if isinstance(path, (str, Path)) else None
    if p is not None and p.exists():
        return load_config_file(p)
    return extract_config(path)


class ConfigIOError(Exception):
    """Raised when a config export/import operation fails."""


# ---------------------------------------------------------------------------
# DB-backed export
# ---------------------------------------------------------------------------

async def export_section_db(db: Any, section: str) -> List[Dict[str, Any]]:
    """Export a single section from the DB as a list of row dicts."""
    if section == "settings":
        snapshot = _settings_snapshot()
        return [snapshot] if snapshot else []
    model = _section_model(section)
    if model is None:
        return []
    try:
        from sqlalchemy import select as _select
    except Exception:  # noqa: BLE001
        return []
    try:
        columns = _model_columns(model)
        order = getattr(model, "id", None)
        query = _select(model)
        if order is not None:
            query = query.order_by(order)
        result = await db.execute(query)
        rows = list(result.scalars().all())
        return [_serialize_row(r, columns) for r in rows]
    except Exception:  # noqa: BLE001
        return []


async def export_config_db(
    db: Any,
    sections: Any = None,
    include_settings: bool = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Export the full system configuration from the DB as an envelope."""
    if "include" in kwargs and sections is None:
        sections = kwargs.get("include")
    names = _resolve_sections(sections)
    if not include_settings:
        names = [n for n in names if n != "settings"]
    data: Dict[str, List[Dict[str, Any]]] = {}
    for name in names:
        try:
            data[name] = await export_section_db(db, name)
        except Exception:  # noqa: BLE001
            data[name] = []
    return build_envelope(data)


# ---------------------------------------------------------------------------
# DB-backed import
# ---------------------------------------------------------------------------

def _strip_runtime(payload: Dict[str, Any], columns: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in payload.items():
        if key in _RUNTIME_DROP_KEYS or key.startswith("_sa_"):
            continue
        if columns and key not in columns:
            continue
        out[key] = value
    return out


async def _delete_section_rows(db: Any, section: str) -> int:
    model = _section_model(section)
    if model is None or section == "settings":
        return 0
    try:
        from sqlalchemy import delete as _delete

        result = await db.execute(_delete(model))
        await db.commit()
        try:
            return int(result.rowcount or 0)
        except Exception:  # noqa: BLE001
            return 0
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


async def import_config_db(
    db: Any,
    payload: Any,
    sections: Any = None,
    mode: str = "merge",
    update_existing: bool = True,
    skip_invalid: bool = True,
    apply_settings: bool = False,  # noqa: ARG001 - informational only
    **kwargs: Any,
) -> Dict[str, Any]:
    """Import a config envelope (or section map) into the DB.

    Returns ``{"imported": {...}, "updated": {...}, "count": n,
    "errors": [...], "mode": mode, "sections": [...]}``.
    """
    if isinstance(sections, str) and sections in ("merge", "replace"):
        mode = sections
        sections = None
    mode = str(kwargs.get("mode", mode) or "merge").strip().lower()
    if mode not in ("merge", "replace"):
        mode = "merge"
    if "update" in kwargs and "update_existing" not in kwargs:
        update_existing = bool(kwargs.get("update"))
    wanted = _resolve_sections(sections)
    extracted = extract_config(payload)
    # ``sections=`` filters which parts of the payload are applied.
    scope = wanted if _normalize_sections_arg(sections) else sorted(set(wanted) | set(extracted.keys()))
    scope = [s for s in scope if s in _ALL_KNOWN_SECTIONS]

    imported: Dict[str, int] = {}
    updated: Dict[str, int] = {}
    errors: List[Dict[str, Any]] = []

    if mode == "replace":
        # Delete dependents first so FK-bearing rows vanish cleanly.
        for name in reversed(scope):
            if name == "settings":
                continue
            try:
                await _delete_section_rows(db, name)
            except Exception as exc:  # noqa: BLE001
                errors.append({"section": name, "error": f"replace clear failed: {exc}"})

    group_id_map: Dict[Any, Any] = {}
    group_name_map: Dict[str, Any] = {}
    camera_id_map: Dict[Any, Any] = {}
    camera_name_map: Dict[str, Any] = {}

    def _remap_camera(old: Any) -> Any:
        if old is None:
            return None
        return camera_id_map.get(old, camera_id_map.get(str(old), old))

    # Import groups first so cameras can resolve group_id references.
    ordered = sorted(scope, key=lambda s: _import_order(s))
    for section in ordered:
        rows = extracted.get(section, [])
        if section == "settings":
            # Informational snapshot; never written to the DB by default.
            imported[section] = 0
            updated[section] = 0
            continue
        if not isinstance(rows, list):
            rows = _coerce_rows(rows)
        try:
            created, changed = await _import_section(
                db,
                section,
                rows,
                update_existing=update_existing,
                skip_invalid=skip_invalid,
                group_id_map=group_id_map,
                group_name_map=group_name_map,
                camera_id_map=camera_id_map,
                camera_name_map=camera_name_map,
                remap_camera=_remap_camera,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"section": section, "error": str(exc)})
            continue
        imported[section] = created
        updated[section] = changed
        for err in changed_errors(_import_section):
            pass
        # _import_section appends row errors directly via the shared list.
        errors.extend(getattr(_import_section, "_pending_errors", []))
        try:
            _import_section._pending_errors = []  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    total = sum(imported.values()) + sum(updated.values())
    return {
        "imported": imported,
        "updated": updated,
        "count": total,
        "total": total,
        "errors": errors,
        "mode": mode,
        "sections": ordered,
        "ok": True,
        "status": "completed",
    }


def changed_errors(_fn: Any) -> List[Any]:
    return []


def _import_order(section: str) -> int:
    try:
        return _ALL_KNOWN_SECTIONS.index(section)
    except ValueError:
        return len(_ALL_KNOWN_SECTIONS)


async def _import_section(
    db: Any,
    section: str,
    rows: List[Dict[str, Any]],
    update_existing: bool = True,
    skip_invalid: bool = True,
    group_id_map: Optional[Dict[Any, Any]] = None,
    group_name_map: Optional[Dict[str, Any]] = None,
    camera_id_map: Optional[Dict[Any, Any]] = None,
    camera_name_map: Optional[Dict[str, Any]] = None,
    remap_camera: Any = None,
) -> Tuple[int, int]:
    model = _section_model(section)
    pending: List[Dict[str, Any]] = []
    setattr(_import_section, "_pending_errors", pending)
    if model is None:
        return 0, 0
    columns = _model_columns(model)
    created = 0
    changed = 0
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            if skip_invalid:
                pending.append({"section": section, "index": index, "error": "row is not an object"})
                continue
            raw = {"value": raw}
        try:
            result = await _import_row(
                db,
                section,
                model,
                columns,
                dict(raw),
                update_existing=update_existing,
                group_id_map=group_id_map or {},
                group_name_map=group_name_map or {},
                camera_id_map=camera_id_map or {},
                camera_name_map=camera_name_map or {},
                remap_camera=remap_camera,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            if skip_invalid:
                pending.append({"section": section, "index": index, "error": str(exc)})
                continue
            raise
        if result == "created":
            created += 1
        elif result == "updated":
            changed += 1
    return created, changed


async def _import_row(
    db: Any,
    section: str,
    model: Any,
    columns: List[str],
    raw: Dict[str, Any],
    update_existing: bool,
    group_id_map: Dict[Any, Any],
    group_name_map: Dict[str, Any],
    camera_id_map: Dict[Any, Any],
    camera_name_map: Dict[str, Any],
    remap_camera: Any,
) -> str:
    from sqlalchemy import select as _select

    old_id = raw.get("id")
    data = _strip_runtime(raw, columns)
    # Normalize datetime-looking fields back to datetimes.
    for key in ("created_at", "updated_at", "expires_at", "probed_at", "last_probe_at",
                "timestamp", "started_at", "ended_at"):
        if key in data and isinstance(data[key], str):
            parsed = _parse_datetime(data[key])
            if parsed is not None:
                data[key] = parsed

    if section == "groups":
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("group 'name' is required")
        existing = (await db.execute(_select(model).where(model.name == name))).scalars().first()
        if existing is not None and update_existing:
            if data.get("description") is not None:
                existing.description = data.get("description")
            await db.commit()
            try:
                await db.refresh(existing)
            except Exception:  # noqa: BLE001
                pass
            _record_group_map(existing, old_id, group_id_map, group_name_map)
            return "updated"
        if existing is not None:
            _record_group_map(existing, old_id, group_id_map, group_name_map)
            return "updated"
        row = model(name=name, description=data.get("description"))
        db.add(row)
        await db.commit()
        try:
            await db.refresh(row)
        except Exception:  # noqa: BLE001
            pass
        _record_group_map(row, old_id, group_id_map, group_name_map)
        return "created"

    if section == "cameras":
        name = str(data.get("name") or "").strip()
        source = str(data.get("source") or "").strip()
        if not name or not source:
            raise ValueError("camera 'name' and 'source' are required")
        data.pop("id", None)
        # Remap group_id through the imported groups when possible.
        if data.get("group_id") is not None and group_id_map:
            gid = data.get("group_id")
            data["group_id"] = group_id_map.get(gid, group_id_map.get(str(gid), gid))
        existing = (await db.execute(_select(model).where(model.name == name))).scalars().first()
        if existing is not None and update_existing:
            for key, value in data.items():
                if key == "name":
                    continue
                if hasattr(existing, key) and value is not None:
                    try:
                        setattr(existing, key, value)
                    except Exception:  # noqa: BLE001
                        pass
            await db.commit()
            try:
                await db.refresh(existing)
            except Exception:  # noqa: BLE001
                pass
            _record_camera_map(existing, old_id, camera_id_map, camera_name_map)
            return "updated"
        if existing is not None:
            _record_camera_map(existing, old_id, camera_id_map, camera_name_map)
            return "updated"
        payload = {k: v for k, v in data.items() if k in columns and k != "id"}
        row = model(**payload)
        db.add(row)
        await db.commit()
        try:
            await db.refresh(row)
        except Exception:  # noqa: BLE001
            pass
        _record_camera_map(row, old_id, camera_id_map, camera_name_map)
        return "created"

    if section == "camera_capabilities":
        new_cid = remap_camera(data.get("camera_id")) if callable(remap_camera) else data.get("camera_id")
        data["camera_id"] = new_cid
        if new_cid is None:
            raise ValueError("camera_capabilities 'camera_id' is required")
        data.pop("id", None)
        existing = (await db.execute(_select(model).where(model.camera_id == new_cid))).scalars().first()
        if existing is not None and update_existing:
            for key, value in data.items():
                if key in ("camera_id",):
                    continue
                if hasattr(existing, key) and value is not None:
                    try:
                        setattr(existing, key, value)
                    except Exception:  # noqa: BLE001
                        pass
            await db.commit()
            return "updated"
        if existing is not None:
            return "updated"
        row = model(**{k: v for k, v in data.items() if k in columns and k != "id"})
        db.add(row)
        await db.commit()
        return "created"

    if section == "camera_permissions":
        new_cid = remap_camera(data.get("camera_id")) if callable(remap_camera) else data.get("camera_id")
        data["camera_id"] = new_cid
        if data.get("user_id") is None or new_cid is None:
            raise ValueError("camera_permissions 'user_id' and 'camera_id' are required")
        data.pop("id", None)
        existing = (
            await db.execute(
                _select(model).where(model.user_id == data["user_id"], model.camera_id == new_cid)
            )
        ).scalars().first()
        if existing is not None:
            if update_existing and data.get("permission") is not None:
                existing.permission = data["permission"]
                await db.commit()
            return "updated"
        row = model(**{k: v for k, v in data.items() if k in columns and k != "id"})
        db.add(row)
        await db.commit()
        return "created"

    if section == "layouts":
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("layout 'name' is required")
        data.pop("id", None)
        existing = (await db.execute(_select(model).where(model.name == name))).scalars().first()
        if existing is not None and update_existing:
            for key, value in data.items():
                if key == "name":
                    continue
                if hasattr(existing, key) and value is not None:
                    try:
                        setattr(existing, key, value)
                    except Exception:  # noqa: BLE001
                        pass
            await db.commit()
            return "updated"
        if existing is not None:
            return "updated"
        row = model(**{k: v for k, v in data.items() if k in columns and k != "id"})
        db.add(row)
        await db.commit()
        return "created"

    if section == "relay_settings":
        data.pop("id", None)
        key = data.get("singleton_key") or "default"
        data["singleton_key"] = key
        existing = None
        if "singleton_key" in columns:
            existing = (await db.execute(_select(model).where(model.singleton_key == key))).scalars().first()
        if existing is None:
            existing = (await db.execute(_select(model))).scalars().first()
        if existing is not None and update_existing:
            for fkey, value in data.items():
                if hasattr(existing, fkey) and value is not None:
                    try:
                        setattr(existing, fkey, value)
                    except Exception:  # noqa: BLE001
                        pass
            await db.commit()
            return "updated"
        if existing is not None:
            return "updated"
        row = model(**{k: v for k, v in data.items() if k in columns and k != "id"})
        db.add(row)
        await db.commit()
        return "created"

    if section == "storage_areas":
        name = str(data.get("name") or "").strip()
        if not name and not data.get("path"):
            raise ValueError("storage_areas 'name' is required")
        data.pop("id", None)
        existing = None
        if name:
            existing = (await db.execute(_select(model).where(model.name == name))).scalars().first()
        if existing is not None and update_existing:
            for key, value in data.items():
                if key == "name":
                    continue
                if hasattr(existing, key) and value is not None:
                    try:
                        setattr(existing, key, value)
                    except Exception:  # noqa: BLE001
                        pass
            await db.commit()
            return "updated"
        if existing is not None:
            return "updated"
        row = model(**{k: v for k, v in data.items() if k in columns and k != "id"})
        db.add(row)
        await db.commit()
        return "created"

    if section in ("standing_rules", "ptz_schedules", "linked_monitor_triggers"):
        data.pop("id", None)
        # Remap every camera-id-like field through the camera map.
        for field in _CAMERA_ID_FIELDS:
            if data.get(field) is not None and camera_id_map:
                old = data[field]
                data[field] = camera_id_map.get(old, camera_id_map.get(str(old), old))
        # Remap group references on schedules if present.
        if data.get("group_id") is not None and group_id_map:
            gid = data.get("group_id")
            data["group_id"] = group_id_map.get(gid, group_id_map.get(str(gid), gid))
        if section == "standing_rules":
            if not data.get("sentence") or not data.get("target_object") or not data.get("expires_at"):
                raise ValueError("standing rule requires 'sentence', 'target_object' and 'expires_at'")
        row = model(**{k: v for k, v in data.items() if k in columns and k != "id"})
        db.add(row)
        await db.commit()
        return "created"

    if section in ("users", "roles"):
        # Opt-in identity sections: upsert by natural key without ever
        # overwriting an existing password hash unless one is provided.
        data.pop("id", None)
        if section == "roles":
            name = str(data.get("name") or "").strip()
            if not name:
                raise ValueError("role 'name' is required")
            existing = (await db.execute(_select(model).where(model.name == name))).scalars().first()
            if existing is not None:
                if update_existing and data.get("description") is not None:
                    existing.description = data.get("description")
                    await db.commit()
                return "updated"
            db.add(model(**{k: v for k, v in data.items() if k in columns and k != "id"}))
            await db.commit()
            return "created"
        username = str(data.get("username") or "").strip()
        if not username:
            raise ValueError("user 'username' is required")
        existing = (await db.execute(_select(model).where(model.username == username))).scalars().first()
        if existing is not None:
            if update_existing:
                for key, value in data.items():
                    if key in ("username", "hashed_password") and value is None:
                        continue
                    if key == "hashed_password" and not value:
                        continue
                    if hasattr(existing, key) and value is not None:
                        try:
                            setattr(existing, key, value)
                        except Exception:  # noqa: BLE001
                            pass
                await db.commit()
            return "updated"
        if not data.get("hashed_password"):
            raise ValueError("user 'hashed_password' is required for new users")
        db.add(model(**{k: v for k, v in data.items() if k in columns and k != "id"}))
        await db.commit()
        return "created"

    raise ValueError(f"Unknown section: {section}")


def _record_group_map(row: Any, old_id: Any, id_map: Dict[Any, Any], name_map: Dict[str, Any]) -> None:
    try:
        new_id = getattr(row, "id", None)
        name = getattr(row, "name", None)
        if new_id is not None:
            if old_id is not None:
                id_map[old_id] = new_id
                id_map[str(old_id)] = new_id
            if name:
                name_map[str(name)] = new_id
    except Exception:  # noqa: BLE001
        pass


def _record_camera_map(row: Any, old_id: Any, id_map: Dict[Any, Any], name_map: Dict[str, Any]) -> None:
    try:
        new_id = getattr(row, "id", None)
        name = getattr(row, "name", None)
        if new_id is not None:
            if old_id is not None:
                id_map[old_id] = new_id
                id_map[str(old_id)] = new_id
            if name:
                name_map[str(name)] = new_id
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Object-oriented facade + compatibility aliases
# ---------------------------------------------------------------------------

class ConfigIOService:
    """Object-oriented facade around the module-level helpers."""

    def export(self, sections_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return export_config_data(sections_data)

    def export_json(self, sections_data: Optional[Dict[str, Any]] = None, indent: int = 2) -> str:
        return export_config_json(sections_data, indent=indent)

    def parse(self, payload: Any) -> Dict[str, List[Dict[str, Any]]]:
        return extract_config(payload)

    def validate(self, payload: Any) -> bool:
        return validate_config(payload)

    def normalize(self, payload: Any) -> Dict[str, List[Dict[str, Any]]]:
        return extract_config(payload)

    def prepare_import(self, payload: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:  # noqa: ARG002
        return extract_config(payload)

    def save(self, path: Any, sections_data: Any = None, indent: int = 2) -> str:
        return save_config(path, sections_data, indent=indent)

    def load(self, path: Any) -> Dict[str, List[Dict[str, Any]]]:
        return load_config_file(path)

    async def export_db(self, db: Any, sections: Any = None, **kwargs: Any) -> Dict[str, Any]:
        return await export_config_db(db, sections=sections, **kwargs)

    async def import_db(self, db: Any, payload: Any, **kwargs: Any) -> Dict[str, Any]:
        return await import_config_db(db, payload, **kwargs)


config_io_service = ConfigIOService()
service = config_io_service
manager = config_io_service
config_service = config_io_service
system_config_service = config_io_service


async def export_config_section(db: Any, section: str) -> List[Dict[str, Any]]:
    return await export_section_db(db, section)


async def export_system_config(db: Any, sections: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return await export_config_db(db, sections=sections, **kwargs)


async def import_system_config(db: Any, payload: Any, **kwargs: Any) -> Dict[str, Any]:
    return await import_config_db(db, payload, **kwargs)


async def export_full_config(db: Any, sections: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return await export_config_db(db, sections=sections, **kwargs)


async def import_full_config(db: Any, payload: Any, **kwargs: Any) -> Dict[str, Any]:
    return await import_config_db(db, payload, **kwargs)


def export_config(payload: Any = None, **kwargs: Any) -> Dict[str, Any]:
    if isinstance(payload, dict) and ("sections" in payload or any(k in _ALL_KNOWN_SECTIONS for k in payload)):
        return build_envelope(payload if "sections" not in payload else payload.get("sections", {}), **kwargs)
    return build_envelope(dict(payload or {}), **kwargs)


def import_config(payload: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return extract_config(payload)


# Additional alias names used across the codebase conventions.
export = export_config_data
export_list = export_config_data
get_config = export_config_data
fetch_config = export_config_data
dump_config = export_config_data
serialize_config = export_config_json
export_to_dict = export_config_data
export_to_json = export_config_json

parse_config = extract_config
parse = extract_config
get_all = extract_config
fetch = extract_config
load_presets = extract_config  # noqa: F811 - intentional cross-module naming shim
read_config = extract_config
normalize = extract_config  # noqa: F811
validate = validate_config

export_from_db = export_config_db
list_config_dicts = export_config_db
get_config_db = export_config_db
export_db = export_config_db

import_to_db = import_config_db
import_into_db = import_config_db
import_db = import_config_db
create_from_config = import_config_db

save = save_config
write = save_config
load_state = load_config_file

ConfigIOManager = ConfigIOService
SystemConfigService = ConfigIOService
SystemConfigManager = ConfigIOService
FullConfigService = ConfigIOService

__all__ = [
    "CONFIG_IO_VERSION",
    "CONFIG_KIND",
    "CONFIG_SECTIONS",
    "OPTIONAL_SECTIONS",
    "ConfigIOError",
    "build_envelope",
    "extract_config",
    "parse_import_payload",
    "validate_config",
    "validate_sections",
    "export_config_data",
    "export_config_json",
    "dumps",
    "dump_json",
    "to_json",
    "to_json_str",
    "serialize",
    "parse_json",
    "loads",
    "from_json",
    "deserialize",
    "load_json",
    "save_config",
    "load_config_file",
    "save_json",
    "dump_file",
    "write_file",
    "dump",
    "load_file",
    "read_file",
    "load",
    "export_section_db",
    "export_config_db",
    "import_config_db",
    "export_config_section",
    "export_system_config",
    "import_system_config",
    "export_full_config",
    "import_full_config",
    "export_config",
    "import_config",
    "ConfigIOService",
    "config_io_service",
    "service",
]
