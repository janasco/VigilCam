"""CSV camera import service.

Pure helpers for parsing/validating CSV camera rows plus async DB-backed
import/export used by :mod:`app.api.csv_import`.

No ``app/main.py`` changes are required: the API router is auto-loaded.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

CSV_VERSION = "1.0"
CSV_KIND = "camera-csv"

CSV_COLUMNS: Tuple[str, ...] = (
    "name",
    "kind",
    "source",
    "username",
    "password",
    "onvif_host",
    "ip_address",
    "onvif_port",
    "enabled",
    "recording_enabled",
    "detection_enabled",
    "group",
    "manufacturer",
    "model",
)

DEFAULT_COLUMNS = list(CSV_COLUMNS)
CSV_HEADERS = list(CSV_COLUMNS)
CSV_FIELDS = list(CSV_COLUMNS)

REQUIRED_FIELDS: Tuple[str, ...] = ("name", "source")

VALID_KINDS = {"usb", "ip", "onvif"}

# Header aliases: normalized-lowercase header -> canonical field.
_HEADER_ALIASES: Dict[str, str] = {
    "camera": "name",
    "camera_name": "name",
    "cameraname": "name",
    "title": "name",
    "label": "name",
    "type": "kind",
    "camera_type": "kind",
    "cameratype": "kind",
    "url": "source",
    "stream": "source",
    "stream_url": "source",
    "streamurl": "source",
    "rtsp": "source",
    "rtsp_url": "source",
    "address": "source",
    "user": "username",
    "login": "username",
    "pass": "password",
    "passwd": "password",
    "pwd": "password",
    "host": "onvif_host",
    "ip": "ip_address",
    "ipaddress": "ip_address",
    "ip_addr": "ip_address",
    "onvifhost": "onvif_host",
    "onvif_ip": "onvif_host",
    "port": "onvif_port",
    "onvifport": "onvif_port",
    "onvif-port": "onvif_port",
    "group_name": "group",
    "camera_group": "group",
    "make": "manufacturer",
    "vendor": "manufacturer",
}


def _canonical_key(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    # Normalize: lower, strip BOM, spaces/dashes -> underscore.
    s = s.lstrip("\ufeff").strip().lower().replace("-", "_").replace(" ", "_")
    # Collapse repeated underscores.
    while "__" in s:
        s = s.replace("__", "_")
    if s in _HEADER_ALIASES:
        return _HEADER_ALIASES[s]
    return s


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s == "":
        return None
    if s in ("1", "true", "t", "yes", "y", "on", "enabled", "enable"):
        return True
    if s in ("0", "false", "f", "no", "n", "off", "disabled", "disable"):
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


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def parse_csv(text: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Parse CSV text/bytes into a list of raw row dicts.

    Accepts ``str``, ``bytes``/``bytearray``, or a file-like object.
    Header names are returned verbatim here; use :func:`normalize_row`
    for alias/canonical mapping.
    """
    if text is None:
        return []
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode(kwargs.get("encoding", "utf-8-sig"), "ignore")
    elif hasattr(text, "read"):
        try:
            text = text.read()
            if isinstance(text, (bytes, bytearray)):
                text = bytes(text).decode("utf-8-sig", "ignore")
        except Exception:
            return []
    elif not isinstance(text, str):
        # A list of dicts passed by mistake -> return as-is.
        if isinstance(text, list):
            return [dict(r) if isinstance(r, dict) else {"value": r} for r in text]
        if isinstance(text, dict):
            # {"csv_text": ...} style envelope.
            for key in ("csv_text", "csv", "content", "text", "data", "body"):
                if isinstance(text.get(key), (str, bytes, bytearray)):
                    return parse_csv(text.get(key), **kwargs)
            # {"rows": [...]} style envelope.
            for key in ("rows", "cameras", "items", "entries", "records"):
                nested = text.get(key)
                if isinstance(nested, list):
                    return [dict(r) if isinstance(r, dict) else {"value": r} for r in nested]
            return []
        text = str(text)
    content = str(text)
    # Strip BOM if present.
    if content.startswith("\ufeff"):
        content = content.lstrip("\ufeff")
    if not content.strip():
        return []
    delimiter = kwargs.get("delimiter")
    if delimiter is None:
        # Sniff delimiter; fall back to comma.
        try:
            sample = content[:4096]
            dialect = csv.Sniffer().sniff(sample, delimiters=[",", ";", "\t", "|"])
            delimiter = dialect.delimiter
        except Exception:
            delimiter = ","
    buf = io.StringIO(content)
    try:
        reader = csv.DictReader(buf, delimiter=delimiter)
        if reader.fieldnames is None:
            return []
        rows: List[Dict[str, Any]] = []
        for raw in reader:
            if raw is None:
                continue
            row: Dict[str, Any] = {}
            empty = True
            for key, value in raw.items():
                k = "" if key is None else str(key).strip()
                v = value.strip() if isinstance(value, str) else value
                if v is not None and str(v).strip() != "":
                    empty = False
                row[k] = v
            if empty:
                continue
            rows.append(row)
        return rows
    except Exception:
        return []


def normalize_row(row: Any) -> Dict[str, Any]:
    """Map header aliases to canonical fields and coerce scalar types."""
    if row is None:
        return {}
    if not isinstance(row, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in row.items():
        canon = _canonical_key(key)
        if not canon:
            continue
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                value = None
        out[canon] = value
    # Mirror ip/onvif_host when only one is given.
    ip = out.get("ip_address")
    host = out.get("onvif_host")
    if not ip and host:
        out["ip_address"] = host
    if not host and ip:
        # Only mirror for onvif-ish sources; otherwise leave host empty
        # but keep ip_address. Mirror anyway for convenience.
        out["onvif_host"] = ip
    # Normalize kind.
    if out.get("kind") is not None:
        ks = str(out["kind"]).strip().lower()
        out["kind"] = ks if ks in VALID_KINDS else "ip"
    else:
        out["kind"] = "ip"
    # Coerce booleans.
    for field in ("enabled", "recording_enabled", "detection_enabled"):
        if field in out and out[field] is not None and not isinstance(out[field], bool):
            coerced = _coerce_bool(out[field])
            out[field] = True if coerced is None and str(out[field]).strip() else coerced
            if out[field] is None:
                out.pop(field, None)
    # Coerce ints.
    if "onvif_port" in out and out["onvif_port"] is not None:
        coerced = _coerce_int(out["onvif_port"])
        if coerced is None:
            out.pop("onvif_port", None)
        else:
            out["onvif_port"] = coerced
    # Drop None username/password so model defaults apply cleanly.
    if out.get("username") is None:
        out.pop("username", None)
    if out.get("password") is None:
        out.pop("password", None)
    # Clean strings.
    for field in ("name", "source", "username", "password", "onvif_host",
                  "ip_address", "group", "manufacturer", "model"):
        if field in out and out[field] is not None:
            cleaned = _clean_str(out[field])
            if cleaned is None:
                out.pop(field, None)
            else:
                out[field] = cleaned
    return out


def normalize_rows(rows: Any) -> List[Dict[str, Any]]:
    if rows is None:
        return []
    if isinstance(rows, dict):
        rows = extract_rows(rows)
    if isinstance(rows, (str, bytes, bytearray)):
        rows = parse_csv(rows)
    try:
        items = list(rows)
    except TypeError:
        return []
    return [normalize_row(r) for r in items]


def validate_row(row: Any) -> bool:
    """Return True when a (possibly raw) row has minimum camera fields."""
    if row is None:
        return False
    data = normalize_row(row) if isinstance(row, dict) else {}
    for field in REQUIRED_FIELDS:
        value = data.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
    kind = data.get("kind")
    if kind is not None and str(kind).strip():
        if str(kind).strip().lower() not in VALID_KINDS:
            return False
    return True


def validate_rows(rows: Any) -> List[bool]:
    return [validate_row(r) for r in normalize_rows(rows)]


def is_valid_row(row: Any) -> bool:
    return validate_row(row)


def row_to_camera_payload(row: Any, **defaults: Any) -> Any:
    """Build a CameraCreate payload from a CSV row dict."""
    from app.schemas import CameraCreate

    data = normalize_row(row)
    payload: Dict[str, Any] = {
        "name": data.get("name"),
        "kind": data.get("kind") or "ip",
        "source": data.get("source"),
    }
    for key in ("username", "password", "onvif_host", "ip_address",
                "onvif_port", "enabled", "recording_enabled",
                "detection_enabled", "group", "manufacturer", "model"):
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


def rows_to_camera_payloads(rows: Any, **defaults: Any) -> List[Any]:
    out = []
    for row in normalize_rows(rows):
        if not validate_row(row):
            continue
        out.append(row_to_camera_payload(row, **defaults))
    return out


def extract_rows(payload: Any) -> List[Dict[str, Any]]:
    """Extract raw row dicts from flexible import payloads.

    Accepts CSV text, bytes, ``{"csv_text": ...}`` envelopes,
    ``{"rows": [...]}`` envelopes, or a plain list of row dicts.
    """
    if payload is None:
        return []
    if isinstance(payload, (str, bytes, bytearray)):
        return parse_csv(payload)
    if hasattr(payload, "read"):
        return parse_csv(payload)
    if isinstance(payload, list):
        return [dict(r) if isinstance(r, dict) else {"value": r} for r in payload]
    if isinstance(payload, tuple):
        return list(payload)
    if isinstance(payload, dict):
        for key in ("csv_text", "csv", "content", "text", "body", "data_text"):
            if isinstance(payload.get(key), (str, bytes, bytearray)):
                return parse_csv(payload[key])
        for key in ("rows", "cameras", "items", "entries", "records", "data", "results"):
            nested = payload.get(key)
            if isinstance(nested, list):
                out = []
                for r in nested:
                    if isinstance(r, dict):
                        out.append(dict(r))
                    elif isinstance(r, str) and ("," in r or "\n" in r):
                        out.extend(parse_csv(r))
                    else:
                        out.append({"value": r})
                # If rows look like CSV-in-a-string, parse them.
                return out
            if isinstance(nested, (str, bytes, bytearray)):
                return parse_csv(nested)
        # Single row dict?
        if any(_canonical_key(k) in set(CSV_COLUMNS) | {"camera", "url", "stream_url"} for k in payload.keys()):
            return [dict(payload)]
        values = list(payload.values())
        if values and all(isinstance(v, dict) for v in values):
            return [dict(v) for v in values]
        return []
    return []


def import_csv_data(payload: Any, skip_invalid: bool = True, **defaults: Any) -> List[Dict[str, Any]]:
    """Pure import: validate + normalize payload to camera payload dicts."""
    rows = extract_rows(payload)
    normalized = [normalize_row(r) for r in rows]
    out: List[Dict[str, Any]] = []
    for row in normalized:
        if validate_row(row):
            data = {k: v for k, v in row.items() if v is not None}
            for key, value in (defaults or {}).items():
                if value is not None:
                    data[key] = value
            out.append(data)
        elif not skip_invalid:
            out.append({k: v for k, v in row.items() if v is not None})
    return out


def cameras_to_csv(cameras: Any, columns: Any = None) -> str:
    """Serialize cameras/rows to a CSV string."""
    cols = list(columns or DEFAULT_COLUMNS)
    items: List[Dict[str, Any]] = []
    if cameras is None:
        items = []
    elif isinstance(cameras, dict):
        extracted = extract_rows(cameras)
        items = [normalize_row(r) for r in extracted] if extracted else [cameras]
    elif isinstance(cameras, (str, bytes, bytearray)):
        # Already CSV -> normalize round-trip.
        items = normalize_rows(cameras)
    else:
        try:
            raw_items = list(cameras)
        except TypeError:
            raw_items = [cameras]
        for item in raw_items:
            if isinstance(item, dict):
                items.append(normalize_row(item))
            elif hasattr(item, "model_dump"):
                try:
                    items.append(normalize_row(item.model_dump()))
                except Exception:
                    continue
            else:
                data: Dict[str, Any] = {}
                try:
                    table = getattr(item, "__table__", None)
                    if table is not None and hasattr(table, "columns"):
                        for col in table.columns:  # type: ignore[union-attr]
                            try:
                                data[col.name] = getattr(item, col.name, None)
                            except Exception:
                                pass
                        items.append(normalize_row(data))
                        continue
                except Exception:
                    pass
                try:
                    raw = getattr(item, "__dict__", None)
                    if isinstance(raw, dict):
                        items.append(normalize_row({k: v for k, v in raw.items() if not k.startswith("_")}))
                except Exception:
                    continue
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in items:
        writer.writerow({c: ("" if row.get(c) is None else row.get(c)) for c in cols})
    return buf.getvalue()


def build_template_csv(columns: Any = None) -> str:
    """Return a CSV template with header plus one example row."""
    cols = list(columns or DEFAULT_COLUMNS)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerow({
        "name": "Front Door",
        "kind": "onvif",
        "source": "onvif://192.168.1.10:80",
        "username": "admin",
        "password": "password",
        "onvif_host": "192.168.1.10",
        "ip_address": "192.168.1.10",
        "onvif_port": 80,
        "enabled": True,
        "recording_enabled": True,
        "detection_enabled": True,
        "group": "entrance",
        "manufacturer": "",
        "model": "",
    })
    return buf.getvalue()


def build_header_csv(columns: Any = None) -> str:
    cols = list(columns or DEFAULT_COLUMNS)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    return buf.getvalue()


def parse_csv_file(path: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    p = Path(str(path))
    if not p.exists():
        return []
    try:
        return parse_csv(p.read_text(encoding=kwargs.get("encoding", "utf-8-sig")), **kwargs)
    except Exception:
        return []


def save_csv(path: Any, cameras: Any, columns: Any = None) -> str:
    p = Path(str(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(cameras_to_csv(cameras, columns=columns), encoding="utf-8")
    return str(p)


def load_csv(path: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return parse_csv_file(path, **kwargs)


# ---------------------------------------------------------------------------
# DB-backed helpers
# ---------------------------------------------------------------------------

async def export_cameras_csv_db(db: Any, columns: Any = None) -> str:
    from sqlalchemy import select as _select

    from app.models.entities import Camera as _Camera

    result = await db.execute(_select(_Camera).order_by(_Camera.id))
    cameras = list(result.scalars().all())
    return cameras_to_csv(cameras, columns=columns)


async def import_csv_db(
    db: Any,
    payload: Any,
    skip_invalid: bool = True,
    update_existing: bool = False,
    **defaults: Any,
) -> Dict[str, Any]:
    """Import CSV rows into the DB, creating Camera rows.

    Returns ``{"imported": [...], "count": n, "errors": [...]}``.
    """
    from app.models.entities import Camera as _Camera

    rows = extract_rows(payload)
    normalized = [normalize_row(r) for r in rows]
    imported: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    created_count = 0
    for index, row in enumerate(normalized):
        if not validate_row(row):
            if skip_invalid:
                errors.append({"index": index, "error": "invalid row: 'name' and 'source' are required", "row": rows[index] if index < len(rows) else row})
                continue
        try:
            data = {k: v for k, v in row.items() if v is not None}
            for key, value in (defaults or {}).items():
                if value is not None:
                    data[key] = value
            data.pop("id", None)
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
                imported.append({c: getattr(existing, c, None) for c in CSV_COLUMNS if hasattr(existing, c)})
                try:
                    imported[-1]["id"] = getattr(existing, "id", None)
                except Exception:
                    pass
                created_count += 1
            else:
                allowed = {c for c in CSV_COLUMNS}
                camera = _Camera(**{k: v for k, v in data.items() if k in allowed})
                # Fall back: ensure kind default.
                if not getattr(camera, "kind", None):
                    camera.kind = "ip"
                db.add(camera)
                await db.commit()
                try:
                    await db.refresh(camera)
                except Exception:
                    pass
                imported.append({c: getattr(camera, c, None) for c in CSV_COLUMNS if hasattr(camera, c)})
                try:
                    imported[-1]["id"] = getattr(camera, "id", None)
                except Exception:
                    pass
                created_count += 1
        except Exception as exc:
            try:
                await db.rollback()
            except Exception:
                pass
            errors.append({"index": index, "error": str(exc)})
    return {"imported": imported, "count": len(imported), "created": imported, "errors": errors, "total": len(rows), "total_errors": len(errors)}


async def preview_csv_db(db: Any, payload: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Parse rows and annotate validity (+ already_added when names match)."""
    rows = extract_rows(payload)
    normalized = [normalize_row(r) for r in rows]
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
    for row in normalized:
        valid = validate_row(row)
        name = str(row.get("name") or "").strip().lower()
        already = bool(name and name in existing_names)
        entry = dict(row)
        entry["valid"] = valid
        entry["already_added"] = already
        entry["new"] = valid and not already
        out.append(entry)
    return out


class CsvImportService:
    """Object-oriented facade around the module-level helpers."""

    def parse(self, payload: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return extract_rows(payload) if isinstance(payload, dict) else parse_csv(payload, **kwargs)

    def normalize(self, row: Any) -> Dict[str, Any]:
        return normalize_row(row)

    def validate(self, row: Any) -> bool:
        return validate_row(row)

    def to_payload(self, row: Any, **defaults: Any) -> Any:
        return row_to_camera_payload(row, **defaults)

    def prepare_import(self, payload: Any, skip_invalid: bool = True, **defaults: Any) -> List[Dict[str, Any]]:
        return import_csv_data(payload, skip_invalid=skip_invalid, **defaults)

    def to_csv(self, cameras: Any, columns: Any = None) -> str:
        return cameras_to_csv(cameras, columns=columns)

    def template(self, columns: Any = None) -> str:
        return build_template_csv(columns=columns)

    def save(self, path: Any, cameras: Any, columns: Any = None) -> str:
        return save_csv(path, cameras, columns=columns)

    def load(self, path: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return load_csv(path, **kwargs)

    async def export_db(self, db: Any, columns: Any = None) -> str:
        return await export_cameras_csv_db(db, columns=columns)

    async def import_db(self, db: Any, payload: Any, **kwargs: Any) -> Dict[str, Any]:
        return await import_csv_db(db, payload, **kwargs)

    async def preview_db(self, db: Any, payload: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return await preview_csv_db(db, payload, **kwargs)


csv_import_service = CsvImportService()
service = csv_import_service
manager = csv_import_service


# Compatibility aliases for varied caller naming.
parse = parse_csv
loads = parse_csv
from_csv = parse_csv
from_text = parse_csv
load_text = parse_csv
parse_text = parse_csv
parse_string = parse_csv
parse_bytes = parse_csv
read_csv = parse_csv
deserialize = parse_csv
to_list = extract_rows
to_dict_list = extract_rows
extract = extract_rows
get_rows = extract_rows
fetch_rows = extract_rows
load_rows = extract_rows
parse_rows = extract_rows
parse_import_payload = extract_rows
normalize = normalize_row
normalize_csv_row = normalize_row
normalize_csv_rows = normalize_rows
validate = validate_row
is_valid = validate_row
validate_csv_row = validate_row
validate_csv_rows = validate_rows
row_to_payload = row_to_camera_payload
csv_row_to_camera_payload = row_to_camera_payload
to_camera_payload = row_to_camera_payload
to_camera_payloads = rows_to_camera_payloads
prepare_import = import_csv_data
import_data = import_csv_data
import_rows = import_csv_data
to_payloads = rows_to_camera_payloads
to_csv = cameras_to_csv
dumps = cameras_to_csv
dumps_csv = cameras_to_csv
dump_csv = cameras_to_csv
export_csv = cameras_to_csv
export_cameras = cameras_to_csv
build_csv = cameras_to_csv
serialize = cameras_to_csv
template_csv = build_template_csv
sample_csv = build_template_csv
example_csv = build_template_csv
template = build_template_csv
sample = build_template_csv
header_csv = build_header_csv
load_file = load_csv
read_file = load_csv
load = load_csv
save = save_csv
write = save_csv
dump = save_csv
write_file = save_csv
dump_file = save_csv
save_file = save_csv
export_db = export_cameras_csv_db
export_to_db = export_cameras_csv_db
import_to_db = import_csv_db
import_into_db = import_csv_db
import_db = import_csv_db
create_from_csv = import_csv_db
bulk_import_csv = import_csv_db
import_cameras = import_csv_db
import_cameras_csv = import_csv_db
preview = preview_csv_db
preview_import = preview_csv_db
preview_csv = preview_csv_db


__all__ = [
    "CSV_VERSION",
    "CSV_KIND",
    "CSV_COLUMNS",
    "DEFAULT_COLUMNS",
    "CSV_HEADERS",
    "CSV_FIELDS",
    "REQUIRED_FIELDS",
    "VALID_KINDS",
    "parse_csv",
    "normalize_row",
    "normalize_rows",
    "validate_row",
    "validate_rows",
    "is_valid_row",
    "row_to_camera_payload",
    "rows_to_camera_payloads",
    "extract_rows",
    "import_csv_data",
    "cameras_to_csv",
    "build_template_csv",
    "build_header_csv",
    "parse_csv_file",
    "save_csv",
    "load_csv",
    "export_cameras_csv_db",
    "import_csv_db",
    "preview_csv_db",
    "CsvImportService",
    "csv_import_service",
    "service",
]
