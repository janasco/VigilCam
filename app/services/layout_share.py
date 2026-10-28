"""Layout export/import (sharing) helpers.

A *layout share* is a JSON-serializable snapshot of a ``Layout`` row
(everything needed to recreate it via ``POST /api/layouts`` without
copying runtime state such as ``id`` or timestamps). This module provides
pure helpers for converting between ``Layout`` rows / dicts and share
dicts, plus JSON envelope helpers and async DB-backed export/import used
by :mod:`app.api.layout_share`.

No ``app/main.py`` changes are required: the API module auto-registers.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

LAYOUT_SHARE_VERSION = "1.0"
LAYOUT_SHARE_KIND = "layouts"

# Layout columns that constitute a reusable share. Runtime state
# (id, created_at, updated_at) is intentionally excluded.
LAYOUT_FIELDS: Tuple[str, ...] = (
    "name",
    "slug",
    "json_layout",
)

_NAME_KEYS: Tuple[str, ...] = ("name", "title", "layout_name", "layoutName")

_LAYOUT_KEYS: Tuple[str, ...] = (
    "json_layout",
    "layout",
    "json",
    "data",
    "config",
    "content",
    "definition",
    "structure",
    "value",
    "jsonLayout",
)

_CAMERA_POSITION_KEYS: Tuple[str, ...] = (
    "camera_positions",
    "cameraPositions",
    "cameras",
    "positions",
    "markers",
)

_ENVELOPE_LIST_KEYS: Tuple[str, ...] = (
    "layouts",
    "layout_shares",
    "layoutShares",
    "shares",
    "items",
    "data",
    "entries",
    "results",
    "rows",
)


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
    """Best-effort conversion of a Layout row / pydantic model / mapping."""
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
    try:
        raw = getattr(obj, "__dict__", None)
        if isinstance(raw, dict):
            return {k: v for k, v in raw.items() if not k.startswith("_")}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _resolve_name(data: Dict[str, Any]) -> Optional[str]:
    for key in _NAME_KEYS:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _resolve_layout_json(data: Dict[str, Any]) -> Any:
    for key in _LAYOUT_KEYS:
        if key in data and data.get(key) is not None:
            return data.get(key)
    for key in _CAMERA_POSITION_KEYS:
        if key in data and data.get(key) is not None:
            return {"camera_positions": data.get(key)}
    return None


def _fold_camera_positions(base: Any, data: Dict[str, Any]) -> Any:
    """Fold top-level camera-position keys into *base* when present."""
    cp = None
    for key in _CAMERA_POSITION_KEYS:
        if key in data and data.get(key) is not None:
            cp = data.get(key)
            break
    if cp is None:
        return base
    try:
        from app.services import floorplan_editor as fe

        return fe.apply_camera_positions(
            base if base is not None else {},
            cp,
            width=data.get("width"),
            height=data.get("height"),
            merge=True,
        )
    except Exception:  # noqa: BLE001
        if isinstance(base, dict):
            merged = dict(base)
            merged.setdefault("camera_positions", cp)
            return merged
        if base is None:
            return {"camera_positions": cp}
        return base


def layout_to_share(layout: Any) -> Dict[str, Any]:
    """Serialize a Layout row / dict / pydantic model to a share dict."""
    data = _as_dict(layout)
    name = _resolve_name(data)
    json_layout = _resolve_layout_json(data)
    json_layout = _fold_camera_positions(json_layout, data)
    share: Dict[str, Any] = {
        "name": name,
        "json_layout": json_layout,
    }
    slug = data.get("slug")
    if slug is not None and str(slug).strip():
        share["slug"] = str(slug).strip()
    return share


def share_to_layout_payload(share: Any) -> Dict[str, Any]:
    """Normalize a share dict to a payload suitable for Layout creation."""
    data = _as_dict(share)
    # Unwrap single-key envelopes accidentally passed here.
    if set(data.keys()) <= {"layout", "share", "item", "data", "preset"} and len(data) == 1:
        for key in ("layout", "share", "item", "data", "preset"):
            if isinstance(data.get(key), dict):
                data = dict(data[key])
                break
    payload: Dict[str, Any] = {}
    name = _resolve_name(data)
    if name is not None:
        payload["name"] = name
    json_layout = _resolve_layout_json(data)
    json_layout = _fold_camera_positions(json_layout, data)
    if json_layout is not None:
        payload["json_layout"] = json_layout
    slug = data.get("slug")
    if slug is not None and str(slug).strip():
        payload["slug"] = str(slug).strip()
    # Drop runtime keys if callers passed full row dicts.
    for drop in ("id", "created_at", "updated_at"):
        payload.pop(drop, None)
    return payload


def validate_share(share: Any) -> bool:
    """Return True when *share* has the minimum required layout fields."""
    if share is None:
        return False
    data = _as_dict(share)
    if set(data.keys()) <= {"layout", "share", "item", "data", "preset"} and len(data) == 1:
        for key in ("layout", "share", "item", "data", "preset"):
            if isinstance(data.get(key), dict):
                data = dict(data[key])
                break
    return _resolve_name(data) is not None


def validate_shares(shares: Any) -> List[bool]:
    return [validate_share(s) for s in _coerce_to_list(shares)]


def is_valid_share(share: Any) -> bool:
    return validate_share(share)


def normalize_share(share: Any) -> Dict[str, Any]:
    return share_to_layout_payload(share)


def normalize_shares(shares: Any) -> List[Dict[str, Any]]:
    return [share_to_layout_payload(s) for s in _coerce_to_list(shares)]


def _coerce_to_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return [value]


def extract_shares(payload: Any) -> List[Dict[str, Any]]:
    """Extract a list of share dicts from flexible import payloads.

    Accepts:
    - a list of share dicts
    - an envelope dict like ``{"layouts": [...]}`` (also ``items``,
      ``data``, ``shares``, ``entries`` keys)
    - a single share dict
    - a JSON string encoding any of the above
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8", "ignore")
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
    if hasattr(payload, "model_dump"):
        try:
            payload = payload.model_dump()
        except Exception:  # noqa: BLE001
            return []
    if isinstance(payload, list):
        return [dict(p) if isinstance(p, dict) else p for p in payload]
    if isinstance(payload, tuple):
        return list(payload)
    if isinstance(payload, dict):
        for key in _ENVELOPE_LIST_KEYS:
            nested = payload.get(key)
            if isinstance(nested, list):
                return [dict(p) if isinstance(p, dict) else p for p in nested]
            if isinstance(nested, dict):
                values = list(nested.values())
                if values and all(isinstance(v, dict) for v in values):
                    return [dict(v) for v in values]
        # Single share dict (has a name-like key) -> wrap.
        if any(k in payload for k in (*_NAME_KEYS, *_LAYOUT_KEYS, "share", "slug")):
            inner = None
            if set(payload.keys()) <= {"layout", "share", "item", "data", "preset"} and len(payload) == 1:
                for key in ("layout", "share", "item", "data", "preset"):
                    if isinstance(payload.get(key), dict):
                        inner = payload[key]
                        break
            if inner is not None:
                return [dict(inner)]
            return [dict(payload)]
        # Mapping of name -> share dict.
        values = list(payload.values())
        if values and all(isinstance(v, dict) for v in values):
            return [dict(v) for v in values]
        return []
    return []


def parse_import_payload(payload: Any) -> List[Dict[str, Any]]:
    return extract_shares(payload)


def build_envelope(layouts: Iterable[Any], version: str = LAYOUT_SHARE_VERSION) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for item in layouts or []:
        d = _as_dict(item)
        # If the item already looks like a share keep its shape, else convert.
        if _resolve_name(d) is not None:
            items.append(layout_to_share(d))
        else:
            items.append(layout_to_share(d))
    return {
        "version": version,
        "kind": LAYOUT_SHARE_KIND,
        "count": len(items),
        "layouts": items,
    }


def unwrap_envelope(payload: Any) -> List[Dict[str, Any]]:
    return extract_shares(payload)


def export_layouts(layouts: Iterable[Any]) -> List[Dict[str, Any]]:
    """Pure export: serialize an iterable of layouts to share dicts."""
    if layouts is None:
        return []
    if isinstance(layouts, dict):
        extracted = extract_shares(layouts)
        if extracted:
            return [layout_to_share(p) for p in extracted]
        return [layout_to_share(layouts)]
    items = list(layouts) if not isinstance(layouts, list) else layouts
    return [layout_to_share(c) for c in items]


def export_layouts_json(layouts: Iterable[Any], indent: int = 2) -> str:
    envelope = build_envelope(layouts or [])
    return json.dumps(envelope, indent=indent, default=_json_default)


def dumps(layouts: Iterable[Any], indent: int = 2) -> str:
    return export_layouts_json(layouts, indent=indent)


def dump_json(layouts: Iterable[Any], indent: int = 2) -> str:
    return export_layouts_json(layouts, indent=indent)


def to_json(layouts: Iterable[Any], indent: int = 2) -> str:
    return export_layouts_json(layouts, indent=indent)


def to_json_str(layouts: Iterable[Any], indent: int = 2) -> str:
    return export_layouts_json(layouts, indent=indent)


def serialize(layouts: Iterable[Any], indent: int = 2) -> str:
    return export_layouts_json(layouts, indent=indent)


def to_list(layouts: Iterable[Any]) -> List[Dict[str, Any]]:
    return export_layouts(layouts)


def to_dict_list(layouts: Iterable[Any]) -> List[Dict[str, Any]]:
    return export_layouts(layouts)


def parse_json(text: Any) -> List[Dict[str, Any]]:
    return extract_shares(text)


def loads(text: Any) -> List[Dict[str, Any]]:
    return extract_shares(text)


def from_json(text: Any) -> List[Dict[str, Any]]:
    return extract_shares(text)


def deserialize(text: Any) -> List[Dict[str, Any]]:
    return extract_shares(text)


def load_json(text: Any) -> List[Dict[str, Any]]:
    return extract_shares(text)


def import_layouts_data(payload: Any, skip_invalid: bool = True) -> List[Dict[str, Any]]:
    """Pure import: validate + normalize payload to layout payload dicts."""
    items = extract_shares(payload)
    out: List[Dict[str, Any]] = []
    for item in items:
        if validate_share(item):
            out.append(share_to_layout_payload(item))
        elif not skip_invalid:
            out.append(share_to_layout_payload(item))
    return out


def save_layouts(path: Any, layouts: Iterable[Any], indent: int = 2) -> str:
    """Write export envelope JSON to *path*. Returns the path as string."""
    p = Path(str(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    envelope = build_envelope(layouts or [])
    p.write_text(json.dumps(envelope, indent=indent, default=_json_default), encoding="utf-8")
    return str(p)


def load_layouts_file(path: Any) -> List[Dict[str, Any]]:
    """Read shares from a JSON file (envelope or bare list)."""
    p = Path(str(path))
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return extract_shares(raw)


# File-helper aliases.
def save_json(path: Any, layouts: Iterable[Any], indent: int = 2) -> str:
    return save_layouts(path, layouts, indent=indent)


def dump_file(path: Any, layouts: Iterable[Any], indent: int = 2) -> str:
    return save_layouts(path, layouts, indent=indent)


def write_file(path: Any, layouts: Iterable[Any], indent: int = 2) -> str:
    return save_layouts(path, layouts, indent=indent)


def dump(path: Any, layouts: Iterable[Any] = None, **kwargs: Any) -> str:  # type: ignore[override]
    # Support dump(layouts) -> json string and dump(path, layouts) -> file.
    if layouts is None and isinstance(path, (list, tuple)):
        return export_layouts_json(path, indent=int(kwargs.get("indent", 2)))
    if isinstance(path, (str, Path)) and layouts is not None:
        return save_layouts(path, layouts, indent=int(kwargs.get("indent", 2)))
    if isinstance(path, (str, Path)):
        return save_layouts(path, [], indent=int(kwargs.get("indent", 2)))
    return export_layouts_json(path, indent=int(kwargs.get("indent", 2)))


def load_file(path: Any) -> List[Dict[str, Any]]:
    return load_layouts_file(path)


def read_file(path: Any) -> List[Dict[str, Any]]:
    return load_layouts_file(path)


def load(path: Any) -> List[Dict[str, Any]]:
    p = Path(str(path)) if isinstance(path, (str, Path)) else None
    if p is not None and p.exists():
        return load_layouts_file(p)
    return extract_shares(path)


# ---------------------------------------------------------------------------
# DB-backed helpers
# ---------------------------------------------------------------------------

async def export_layouts_db(db: Any) -> List[Dict[str, Any]]:
    """Export all layouts from the DB as share dicts."""
    from sqlalchemy import select as _select

    from app.models.entities import Layout as _Layout

    result = await db.execute(_select(_Layout).order_by(_Layout.id))
    layouts = list(result.scalars().all())
    return export_layouts(layouts)


async def import_layouts_db(
    db: Any,
    payload: Any,
    skip_invalid: bool = True,
    update_existing: bool = False,
) -> Dict[str, Any]:
    """Import shares into the DB, creating Layout rows.

    Returns ``{"imported": [...], "count": n, "errors": [...]}`` where
    ``imported`` holds the serialized created layouts.
    """
    from app.models.entities import Layout as _Layout

    items = extract_shares(payload)
    imported: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for index, item in enumerate(items):
        if not validate_share(item):
            if skip_invalid:
                errors.append({"index": index, "error": "invalid share: 'name' is required"})
                continue
        try:
            data = share_to_layout_payload(item)
            # Never reuse incoming ids.
            data.pop("id", None)
            existing = None
            if data.get("name"):
                try:
                    from sqlalchemy import select as _select

                    res = await db.execute(_select(_Layout).where(_Layout.name == data["name"]))
                    existing = res.scalars().first()
                except Exception:  # noqa: BLE001
                    existing = None
            if existing is not None:
                if update_existing:
                    if data.get("json_layout") is not None:
                        existing.json_layout = data.get("json_layout")
                    if data.get("slug"):
                        try:
                            from app.services.slugs import ensure_unique_slug, slugify_name

                            existing.slug = await ensure_unique_slug(
                                db, _Layout, slugify_name(data["slug"]), exclude_id=existing.id
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    from datetime import datetime as _dt

                    try:
                        existing.updated_at = _dt.utcnow()
                    except Exception:  # noqa: BLE001
                        pass
                    await db.commit()
                    try:
                        await db.refresh(existing)
                    except Exception:  # noqa: BLE001
                        pass
                    imported.append(layout_to_share(existing))
                    try:
                        imported[-1]["id"] = getattr(existing, "id", None)
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    errors.append(
                        {"index": index, "error": f"layout {data.get('name')!r} already exists"}
                    )
                continue
            # New row: ensure a unique slug.
            try:
                from app.services.slugs import ensure_unique_slug, slugify_name

                base = data.get("slug") or data.get("name") or "layout"
                data["slug"] = await ensure_unique_slug(db, _Layout, slugify_name(base))
            except Exception:  # noqa: BLE001
                pass
            row = _Layout(
                name=str(data.get("name")),
                json_layout=data.get("json_layout"),
                slug=data.get("slug"),
            )
            db.add(row)
            await db.commit()
            try:
                await db.refresh(row)
            except Exception:  # noqa: BLE001
                pass
            share = layout_to_share(row)
            try:
                share["id"] = getattr(row, "id", None)
            except Exception:  # noqa: BLE001
                pass
            imported.append(share)
        except Exception as exc:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            errors.append({"index": index, "error": str(exc)})
    return {"imported": imported, "count": len(imported), "errors": errors}


class LayoutShareService:
    """Object-oriented facade around the module-level helpers."""

    def export(self, layouts: Iterable[Any]) -> List[Dict[str, Any]]:
        return export_layouts(layouts)

    def export_json(self, layouts: Iterable[Any], indent: int = 2) -> str:
        return export_layouts_json(layouts, indent=indent)

    def parse(self, payload: Any) -> List[Dict[str, Any]]:
        return extract_shares(payload)

    def validate(self, share: Any) -> bool:
        return validate_share(share)

    def normalize(self, share: Any) -> Dict[str, Any]:
        return share_to_layout_payload(share)

    def prepare_import(self, payload: Any, skip_invalid: bool = True) -> List[Dict[str, Any]]:
        return import_layouts_data(payload, skip_invalid=skip_invalid)

    def save(self, path: Any, layouts: Iterable[Any], indent: int = 2) -> str:
        return save_layouts(path, layouts, indent=indent)

    def load(self, path: Any) -> List[Dict[str, Any]]:
        return load_layouts_file(path)

    async def export_db(self, db: Any) -> List[Dict[str, Any]]:
        return await export_layouts_db(db)

    async def import_db(self, db: Any, payload: Any, **kwargs: Any) -> Dict[str, Any]:
        return await import_layouts_db(db, payload, **kwargs)


layout_share_service = LayoutShareService()
service = layout_share_service
manager = layout_share_service


# Compatibility aliases for varied caller naming.
export = export_layouts
export_list = export_layouts
list_layouts = export_layouts
get_layouts = export_layouts
dump_layouts = export_layouts
export_layout_shares = export_layouts
export_shares = export_layouts
export_layouts_list = export_layouts
export_to_dict = export_layouts
export_to_list = export_layouts

import_layouts = import_layouts_data
parse_layouts = extract_shares
parse_shares = extract_shares
parse = extract_shares
get_all = extract_shares
fetch_layouts = extract_shares
fetch_shares = extract_shares
load_layouts = extract_shares
read_layouts = extract_shares
normalize = normalize_share
validate = validate_share

export_from_db = export_layouts_db
list_layout_dicts = export_layouts_db
get_layouts_db = export_layouts_db
export_db = export_layouts_db

import_to_db = import_layouts_db
import_into_db = import_layouts_db
import_db = import_layouts_db
create_from_layouts = import_layouts_db

save = save_layouts
write = save_layouts
load_state = load_layouts_file

layout_to_dict = layout_to_share
layout_share_to_dict = layout_to_share
dict_to_layout_share = share_to_layout_payload
dict_to_share = share_to_layout_payload
layout_dict_to_share = layout_to_share
share_to_dict = share_to_layout_payload

wrap_envelope = build_envelope
build_export = build_envelope
make_envelope = build_envelope

LAYOUT_SHARE_KIND_ALIAS = LAYOUT_SHARE_KIND
LAYOUT_KIND = LAYOUT_SHARE_KIND
SHARE_KIND = LAYOUT_SHARE_KIND
SHARE_VERSION = LAYOUT_SHARE_VERSION
VERSION = LAYOUT_SHARE_VERSION

__all__ = [
    "LAYOUT_SHARE_VERSION",
    "LAYOUT_SHARE_KIND",
    "LAYOUT_FIELDS",
    "layout_to_share",
    "share_to_layout_payload",
    "validate_share",
    "validate_shares",
    "is_valid_share",
    "normalize_share",
    "normalize_shares",
    "extract_shares",
    "parse_import_payload",
    "build_envelope",
    "unwrap_envelope",
    "export_layouts",
    "export_layouts_json",
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
    "import_layouts_data",
    "save_layouts",
    "load_layouts_file",
    "save_json",
    "dump_file",
    "write_file",
    "dump",
    "load_file",
    "read_file",
    "load",
    "export_layouts_db",
    "import_layouts_db",
    "LayoutShareService",
    "layout_share_service",
    "service",
]
