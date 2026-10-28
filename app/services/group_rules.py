"""Camera grouping auto-rules service.

Automatically assigns cameras to groups based on declarative match rules.

A rule maps a camera attribute to a target group::

    {"name": "lobby cams", "group": "Lobby", "field": "name",
     "operator": "contains", "value": "lobby", "priority": 10}

Supported ``field`` values (with aliases):

- ``name`` (``camera_name``, ``title``)
- ``kind`` (``type``, ``driver``) -- e.g. ``usb`` / ``ip`` / ``onvif``
- ``source`` (``url``, ``stream``, ``rtsp``)
- ``manufacturer`` (``vendor``, ``make``)
- ``model``
- ``ip_address`` (``ip``, ``host``, ``onvif_host``)

Supported ``operator`` values (with aliases):

- ``contains`` / ``icontains``
- ``equals`` / ``eq`` / ``iexact``
- ``startswith`` / ``prefix``
- ``endswith`` / ``suffix``
- ``regex`` / ``match`` / ``pattern``

Matching is case-insensitive except for ``regex`` when an explicit inline
flag is used (regexes are compiled with ``re.IGNORECASE`` by default).

Rules are evaluated in ``(priority, id)`` order; the first matching enabled
rule wins. Rules persist to ``DATA_DIR/group_rules.json`` so they survive
restarts without requiring a schema migration.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from copy import deepcopy
from dataclasses import dataclass, field as _dc_field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - import-safe fallback
    from app.core.config import DATA_DIR as _DATA_DIR
except Exception:  # noqa: BLE001
    _DATA_DIR = Path("data")

_RULES_FILE = Path(str(_DATA_DIR)) / "group_rules.json" if not isinstance(_DATA_DIR, Path) else _DATA_DIR / "group_rules.json"

_LOCK = threading.RLock()
_RULES: Dict[str, Dict[str, Any]] = {}
_LOADED = False

# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

_FIELD_ALIASES: Dict[str, str] = {
    "name": "name",
    "camera_name": "name",
    "cameraname": "name",
    "title": "name",
    "camera": "name",
    "kind": "kind",
    "type": "kind",
    "camera_type": "kind",
    "driver": "kind",
    "source": "source",
    "url": "source",
    "stream": "source",
    "stream_url": "source",
    "rtsp": "source",
    "manufacturer": "manufacturer",
    "vendor": "manufacturer",
    "make": "manufacturer",
    "brand": "manufacturer",
    "model": "model",
    "ip_address": "ip_address",
    "ip": "ip_address",
    "host": "ip_address",
    "onvif_host": "ip_address",
    "hostname": "ip_address",
}

_OPERATOR_ALIASES: Dict[str, str] = {
    "contains": "contains",
    "icontains": "contains",
    "includes": "contains",
    "like": "contains",
    "substring": "contains",
    "equals": "equals",
    "equal": "equals",
    "eq": "equals",
    "exact": "equals",
    "iexact": "equals",
    "is": "equals",
    "startswith": "startswith",
    "starts_with": "startswith",
    "prefix": "startswith",
    "begins": "startswith",
    "endswith": "endswith",
    "ends_with": "endswith",
    "suffix": "endswith",
    "ends": "endswith",
    "regex": "regex",
    "regexp": "regex",
    "match": "regex",
    "matches": "regex",
    "pattern": "regex",
}

VALID_FIELDS = sorted(set(_FIELD_ALIASES.values()))
VALID_OPERATORS = sorted(set(_OPERATOR_ALIASES.values()))


def normalize_field(value: Any) -> Optional[str]:
    """Normalize a user-supplied field name to its canonical key."""
    if value is None:
        return None
    if isinstance(value, dict):
        for k in ("field", "attribute", "attr", "key", "column", "match_field"):
            if value.get(k) is not None:
                return normalize_field(value.get(k))
        return None
    s = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    s = re.sub(r"__+", "_", s)
    if s in _FIELD_ALIASES:
        return _FIELD_ALIASES[s]
    return None


def normalize_operator(value: Any) -> Optional[str]:
    """Normalize a user-supplied operator to its canonical key."""
    if value is None:
        return None
    if isinstance(value, dict):
        for k in ("operator", "op", "match", "matcher", "condition", "mode"):
            if value.get(k) is not None:
                return normalize_operator(value.get(k))
        return None
    s = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    s = re.sub(r"__+", "_", s)
    if s in _OPERATOR_ALIASES:
        return _OPERATOR_ALIASES[s]
    return None


def is_valid_field(value: Any) -> bool:
    return normalize_field(value) is not None


def is_valid_operator(value: Any) -> bool:
    return normalize_operator(value) is not None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _rules_file() -> Path:
    try:
        from app.core.config import DATA_DIR as _dd

        base = Path(str(_dd))
        if not base.is_absolute():
            from pathlib import Path as _P

            base = _P(__file__).resolve().parents[2] / base
        return base / "group_rules.json"
    except Exception:  # noqa: BLE001
        return _RULES_FILE


def _load_from_disk() -> None:
    global _LOADED
    path = _rules_file()
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
            items = raw.get("rules", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
            _RULES.clear()
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    _RULES[str(item["id"])] = dict(item)
    except Exception:  # noqa: BLE001
        pass
    _LOADED = True


def _ensure_loaded() -> None:
    if not _LOADED:
        with _LOCK:
            if not _LOADED:
                _load_from_disk()


def _save_to_disk() -> None:
    path = _rules_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"rules": sorted(_RULES.values(), key=lambda r: (int(r.get("priority", 100)), str(r.get("id"))))}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _coerce_priority(value: Any, default: int = 100) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _validate_payload(data: Dict[str, Any], *, partial: bool = False) -> Dict[str, Any]:
    """Validate/normalize a rule payload. Raises ValueError on problems."""
    out: Dict[str, Any] = {}
    # Target group (required unless partial and not provided).
    group = None
    for k in ("group", "group_name", "target_group", "target", "name_of_group"):
        if data.get(k) is not None and str(data.get(k)).strip():
            group = str(data.get(k)).strip()
            break
    # Rule display name is optional; fall back to group-derived name.
    name = None
    for k in ("name", "title", "label", "rule_name"):
        if data.get(k) is not None and str(data.get(k)).strip():
            name = str(data.get(k)).strip()
            break
    if not partial or "group" in data or "group_name" in data or "target_group" in data or "target" in data:
        if not group:
            raise ValueError("Missing target group (provide 'group' or 'group_name')")
        out["group"] = group
    if name is not None:
        out["name"] = name

    # Field.
    field_value = None
    for k in ("field", "attribute", "attr", "key", "column", "match_field"):
        if data.get(k) is not None:
            field_value = data.get(k)
            break
    if not partial or field_value is not None:
        canonical = normalize_field(field_value) if field_value is not None else None
        if canonical is None:
            raise ValueError(f"Invalid match field: {field_value!r}. Valid: {VALID_FIELDS}")
        out["field"] = canonical

    # Operator (default: contains).
    op_value = None
    for k in ("operator", "op", "match", "matcher", "condition", "mode"):
        if data.get(k) is not None:
            op_value = data.get(k)
            break
    if op_value is None and not partial:
        out["operator"] = "contains"
    elif op_value is not None:
        canonical_op = normalize_operator(op_value)
        if canonical_op is None:
            raise ValueError(f"Invalid operator: {op_value!r}. Valid: {VALID_OPERATORS}")
        out["operator"] = canonical_op

    # Value.
    if not partial or "value" in data or "pattern" in data or "text" in data:
        value = data.get("value")
        if value is None:
            for k in ("pattern", "text", "match_value", "expected", "substring"):
                if data.get(k) is not None:
                    value = data.get(k)
                    break
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError("Missing match value (provide 'value')")
        out["value"] = str(value)
        if out.get("operator") == "regex":
            try:
                re.compile(str(value), re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"Invalid regex pattern: {exc}") from exc

    # Priority.
    for k in ("priority", "order", "weight", "rank"):
        if data.get(k) is not None:
            out["priority"] = _coerce_priority(data.get(k))
            break

    # Enabled.
    for k in ("enabled", "active", "is_enabled", "is_active"):
        if data.get(k) is not None:
            v = data.get(k)
            if isinstance(v, str):
                out["enabled"] = v.strip().lower() not in ("0", "false", "no", "off", "disabled")
            else:
                out["enabled"] = bool(v)
            break
    return out


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def list_rules() -> List[Dict[str, Any]]:
    """Return all rules ordered by (priority, id)."""
    _ensure_loaded()
    with _LOCK:
        rules = [deepcopy(r) for r in _RULES.values()]
    rules.sort(key=lambda r: (int(r.get("priority", 100)), str(r.get("id"))))
    return rules


def list_group_rules() -> List[Dict[str, Any]]:
    return list_rules()


def get_rule(rule_id: Any) -> Optional[Dict[str, Any]]:
    _ensure_loaded()
    with _LOCK:
        rule = _RULES.get(str(rule_id))
        return deepcopy(rule) if rule is not None else None


def get_group_rule(rule_id: Any) -> Optional[Dict[str, Any]]:
    return get_rule(rule_id)


def create_rule(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Create a rule from a payload dict. Raises ValueError on bad input."""
    if payload is None:
        raise ValueError("Missing rule payload")
    data = dict(payload) if isinstance(payload, dict) else {"value": payload}
    # Flatten nested payload wrappers for tolerance.
    for nested in ("payload", "rule", "config", "params", "data", "body"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    validated = _validate_payload(data, partial=False)
    _ensure_loaded()
    with _LOCK:
        rid = _new_id()
        rule: Dict[str, Any] = {
            "id": rid,
            "name": validated.get("name") or f"rule_{rid[:6]}",
            "group": validated["group"],
            "group_name": validated["group"],
            "field": validated["field"],
            "operator": validated.get("operator", "contains"),
            "value": validated["value"],
            "priority": int(validated.get("priority", 100)),
            "enabled": bool(validated.get("enabled", True)),
        }
        # Keep target aliases in sync.
        rule["target_group"] = rule["group"]
        _RULES[rid] = rule
        _save_to_disk()
        return deepcopy(rule)


def create_group_rule(payload: Dict[str, Any]) -> Dict[str, Any]:
    return create_rule(payload)


def update_rule(rule_id: Any, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    _ensure_loaded()
    with _LOCK:
        existing = _RULES.get(str(rule_id))
        if existing is None:
            return None
        data = dict(payload or {})
        for nested in ("payload", "rule", "config", "params", "data", "body"):
            nested_val = data.get(nested)
            if isinstance(nested_val, dict):
                for k, v in nested_val.items():
                    data.setdefault(k, v)
        validated = _validate_payload(data, partial=True)
        merged = dict(existing)
        for k, v in validated.items():
            merged[k] = v
        if "group" in merged:
            merged["group_name"] = merged["group"]
            merged["target_group"] = merged["group"]
        if "group_name" in validated and "group" not in validated:
            merged["group"] = validated["group_name"]
            merged["target_group"] = validated["group_name"]
        _RULES[str(rule_id)] = merged
        _save_to_disk()
        return deepcopy(merged)


def update_group_rule(rule_id: Any, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return update_rule(rule_id, payload)


def delete_rule(rule_id: Any) -> bool:
    _ensure_loaded()
    with _LOCK:
        if str(rule_id) in _RULES:
            del _RULES[str(rule_id)]
            _save_to_disk()
            return True
        return False


def delete_group_rule(rule_id: Any) -> bool:
    return delete_rule(rule_id)


def clear_rules() -> int:
    """Remove all rules. Returns number removed (test helper)."""
    _ensure_loaded()
    with _LOCK:
        count = len(_RULES)
        _RULES.clear()
        _save_to_disk()
        return count


def clear_group_rules() -> int:
    return clear_rules()


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _camera_attr(camera: Any, field_name: str) -> str:
    if isinstance(camera, dict):
        # Try canonical key then common aliases.
        candidates = [field_name]
        for alias, canonical in _FIELD_ALIASES.items():
            if canonical == field_name and alias not in candidates:
                candidates.append(alias)
        for key in candidates:
            if camera.get(key) is not None:
                return str(camera.get(key))
        # Case-insensitive key scan.
        lowered = {str(k).lower(): v for k, v in camera.items()}
        for key in candidates:
            if str(key).lower() in lowered and lowered[str(key).lower()] is not None:
                return str(lowered[str(key).lower()])
        return ""
    for attr in (field_name,):
        try:
            value = getattr(camera, attr, None)
            if value is not None:
                return str(value)
        except Exception:  # noqa: BLE001
            pass
    # Alias attribute scan for ORM objects / dataclasses.
    for alias, canonical in _FIELD_ALIASES.items():
        if canonical != field_name:
            continue
        try:
            value = getattr(camera, alias, None)
            if value is not None:
                return str(value)
        except Exception:  # noqa: BLE001
            pass
    # Mapping-style objects.
    try:
        getter = getattr(camera, "get", None)
        if callable(getter):
            for alias, canonical in _FIELD_ALIASES.items():
                if canonical == field_name:
                    try:
                        value = getter(alias)
                    except Exception:  # noqa: BLE001
                        value = None
                    if value is not None:
                        return str(value)
    except Exception:  # noqa: BLE001
        pass
    return ""


def rule_matches_camera(rule: Dict[str, Any], camera: Any) -> bool:
    """Return True when an enabled rule matches the given camera."""
    if not rule.get("enabled", True):
        return False
    field_name = normalize_field(rule.get("field")) or "name"
    operator = normalize_operator(rule.get("operator")) or "contains"
    expected = str(rule.get("value", ""))
    actual = _camera_attr(camera, field_name)
    if operator == "equals":
        return actual.strip().lower() == expected.strip().lower()
    if operator == "startswith":
        return actual.strip().lower().startswith(expected.strip().lower())
    if operator == "endswith":
        return actual.strip().lower().endswith(expected.strip().lower())
    if operator == "regex":
        try:
            return re.search(expected, actual, re.IGNORECASE) is not None
        except re.error:
            return False
    # Default: contains (case-insensitive).
    return expected.strip().lower() in actual.strip().lower()


def match_camera(camera: Any, rules: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """Return the first matching rule for a camera, or None."""
    candidates = rules if rules is not None else list_rules()
    ordered = sorted(candidates, key=lambda r: (int(r.get("priority", 100)), str(r.get("id"))))
    for rule in ordered:
        try:
            if rule_matches_camera(rule, camera):
                return rule
        except Exception:  # noqa: BLE001
            continue
    return None


def evaluate_camera(camera: Any, rules: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """Return the target group name for a camera, or None if no rule matches."""
    matched = match_camera(camera, rules)
    if matched is None:
        return None
    return str(matched.get("group") or matched.get("group_name") or matched.get("target_group") or "")


def preview_cameras(cameras: List[Any], rules: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Preview grouping without writing anything. Returns per-camera details."""
    candidates = rules if rules is not None else list_rules()
    out: List[Dict[str, Any]] = []
    for cam in cameras:
        matched = match_camera(cam, candidates)
        cam_id = cam.get("id") if isinstance(cam, dict) else getattr(cam, "id", None)
        cam_name = cam.get("name") if isinstance(cam, dict) else getattr(cam, "name", None)
        out.append(
            {
                "camera_id": cam_id,
                "camera_name": cam_name,
                "matched_rule_id": matched.get("id") if matched else None,
                "matched_rule_name": matched.get("name") if matched else None,
                "target_group": str(matched.get("group")) if matched else None,
            }
        )
    return out


# Aliases for tolerant imports.
evaluate = evaluate_camera
evaluate_cameras = preview_cameras
preview = preview_cameras
match = match_camera
matches = rule_matches_camera
rule_matches = rule_matches_camera
camera_matches_rule = rule_matches_camera
list_all = list_rules


@dataclass
class GroupRule:
    """Typed view of a group auto-rule."""

    id: str = ""
    name: str = ""
    group: str = ""
    field: str = "name"
    operator: str = "contains"
    value: str = ""
    priority: int = 100
    enabled: bool = True
    extra: Dict[str, Any] = _dc_field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "group": self.group,
            "group_name": self.group,
            "target_group": self.group,
            "field": self.field,
            "operator": self.operator,
            "value": self.value,
            "priority": self.priority,
            "enabled": self.enabled,
        }
        d.update(self.extra or {})
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GroupRule":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            group=str(data.get("group") or data.get("group_name") or data.get("target_group") or ""),
            field=str(data.get("field", "name")),
            operator=str(data.get("operator", "contains")),
            value=str(data.get("value", "")),
            priority=_coerce_priority(data.get("priority")),
            enabled=bool(data.get("enabled", True)),
        )


# ---------------------------------------------------------------------------
# DB apply
# ---------------------------------------------------------------------------


async def apply_rules(db=None, *, commit: bool = True) -> Dict[str, Any]:
    """Apply enabled rules to every camera in the DB.

    Creates target groups on demand and assigns ``camera.group_id`` /
    ``camera.group``. Returns a summary dict.
    """
    from sqlalchemy import select as _select

    close_after = False
    if db is None:
        from app.core.database import SessionLocal as _SessionLocal

        db = _SessionLocal()
        close_after = True
    try:
        from app.models.entities import Camera as _Camera, Group as _Group

        rules = list_rules()
        result = await db.execute(_select(_Camera).order_by(_Camera.id))
        cameras = list(result.scalars().all())
        existing_groups: Dict[str, Any] = {}
        gres = await db.execute(_select(_Group))
        for g in gres.scalars().all():
            existing_groups[str(g.name).lower()] = g

        assigned = 0
        details: List[Dict[str, Any]] = []
        for cam in cameras:
            matched = match_camera(cam, rules)
            if matched is None:
                continue
            target = str(matched.get("group") or "").strip()
            if not target:
                continue
            group_obj = existing_groups.get(target.lower())
            if group_obj is None:
                group_obj = _Group(name=target)
                db.add(group_obj)
                try:
                    await db.flush()
                except Exception:  # noqa: BLE001
                    await db.rollback()
                    # Re-fetch in case of race/unique conflict.
                    r2 = await db.execute(_select(_Group).where(_Group.name == target))
                    group_obj = r2.scalars().first()
                    if group_obj is None:
                        continue
                existing_groups[target.lower()] = group_obj
            cam.group_id = group_obj.id
            cam.group = group_obj.name
            assigned += 1
            details.append(
                {
                    "camera_id": cam.id,
                    "camera_name": cam.name,
                    "group_id": group_obj.id,
                    "group": group_obj.name,
                    "rule_id": matched.get("id"),
                }
            )
        if commit:
            await db.commit()
        return {
            "assigned": assigned,
            "total_cameras": len(cameras),
            "total_rules": len([r for r in rules if r.get("enabled", True)]),
            "count": assigned,
            "details": details,
        }
    finally:
        try:
            if close_after:
                await db.close()
        except Exception:  # noqa: BLE001
            pass


async def apply_group_rules(db=None, *, commit: bool = True) -> Dict[str, Any]:
    return await apply_rules(db, commit=commit)


async def auto_assign_cameras(db=None, *, commit: bool = True) -> Dict[str, Any]:
    return await apply_rules(db, commit=commit)


async def run_auto_grouping(db=None, *, commit: bool = True) -> Dict[str, Any]:
    return await apply_rules(db, commit=commit)


__all__ = [
    "GroupRule",
    "VALID_FIELDS",
    "VALID_OPERATORS",
    "normalize_field",
    "normalize_operator",
    "is_valid_field",
    "is_valid_operator",
    "list_rules",
    "list_group_rules",
    "get_rule",
    "get_group_rule",
    "create_rule",
    "create_group_rule",
    "update_rule",
    "update_group_rule",
    "delete_rule",
    "delete_group_rule",
    "clear_rules",
    "clear_group_rules",
    "rule_matches_camera",
    "rule_matches",
    "camera_matches_rule",
    "matches",
    "match_camera",
    "match",
    "evaluate_camera",
    "evaluate",
    "preview_cameras",
    "preview",
    "evaluate_cameras",
    "apply_rules",
    "apply_group_rules",
    "auto_assign_cameras",
    "run_auto_grouping",
]
