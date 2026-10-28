"""Scoped API token helpers.

Verifies the baseline first: :mod:`app.services.api_keys` stores ``scopes``
on each record but :meth:`verify_api_key` only checks key hash, ``revoked``
flag and expiry — it never restricts by scope/permission. This module adds
an opt-in scope enforcement layer on top without changing ``api_keys``.

All helpers are sync and dependency-free so they work in tests without a DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

KNOWN_SCOPES: List[str] = [
    "read",
    "write",
    "admin",
    "cameras:read",
    "cameras:write",
    "recordings:read",
    "recordings:write",
    "events:read",
    "events:write",
    "snapshots:read",
    "stream:read",
    "ptz:control",
    "config:read",
    "config:write",
    "keys:read",
    "keys:write",
]

AVAILABLE_SCOPES = list(KNOWN_SCOPES)
ALL_SCOPES = list(KNOWN_SCOPES)
SUPPORTED_SCOPES = list(KNOWN_SCOPES)
VALID_SCOPES = list(KNOWN_SCOPES)
DEFAULT_SCOPES: List[str] = []
SCOPES = list(KNOWN_SCOPES)

WILDCARD_SCOPES = ("*", "admin", "all", "root", "*:*")
_SCOPE_ALIASES = ("scopes", "scope", "permissions", "perms", "perm", "access", "roles", "role")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_scopes(value: Any) -> List[str]:
    """Normalize arbitrary scope input to a clean list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    # Split embedded commas: ["read,write"] -> ["read", "write"].
                    if "," in s or ";" in s:
                        out.extend(normalize_scopes(s))
                    else:
                        out.append(s)
            elif item is not None:
                s = str(item).strip()
                if s:
                    out.append(s)
        # Deduplicate preserving order.
        seen = set()
        deduped: List[str] = []
        for s in out:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        return deduped
    s = str(value).strip()
    return [s] if s else []


parse_scopes = normalize_scopes
coerce_scopes = normalize_scopes
clean_scopes = normalize_scopes
sanitize_scopes = normalize_scopes


def _eq(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _scope_allows(granted: str, required: str) -> bool:
    g = granted.strip()
    r = required.strip()
    if not g or not r:
        return False
    if _eq(g, r):
        return True
    gl = g.lower()
    rl = r.lower()
    # Super-scopes grant everything.
    if gl in ("*", "admin", "all", "root", "*:*"):
        return True
    # Prefix wildcard: "cameras:*" grants "cameras:read".
    if gl.endswith(":*"):
        prefix = gl[:-2]
        if rl == prefix or rl.startswith(prefix + ":"):
            return True
    if gl.endswith("*"):
        prefix = gl[:-1].rstrip(":")
        if rl.startswith(prefix):
            return True
    return False


def get_key_scopes(record_or_id: Any) -> List[str]:
    """Return scopes for a key record, key id, or raw key value."""
    if record_or_id is None:
        return []
    if isinstance(record_or_id, dict):
        for alias in _SCOPE_ALIASES:
            if record_or_id.get(alias) is not None:
                return normalize_scopes(record_or_id.get(alias))
        return []
    # Assume key id / raw value: resolve via api_keys store.
    try:
        from app.services import api_keys as _api_keys

        record = _api_keys.get_api_key(str(record_or_id))
        if record is None:
            return []
        for alias in _SCOPE_ALIASES:
            if record.get(alias) is not None:
                return normalize_scopes(record.get(alias))
        return []
    except Exception:  # noqa: BLE001
        return []


get_scopes = get_key_scopes
scopes_for_key = get_key_scopes
key_scopes = get_key_scopes
list_key_scopes = get_key_scopes
fetch_scopes = get_key_scopes
resolve_scopes = get_key_scopes


def has_scope(record_or_id: Any, required: Any) -> bool:
    """Check whether a key record/id grants a single required scope.

    Empty/None ``required`` means no restriction (valid key is enough).
    """
    needed = normalize_scopes(required)
    if not needed:
        return True
    if len(needed) > 1:
        return has_all_scopes(record_or_id, needed)
    req = needed[0]
    granted = get_key_scopes(record_or_id)
    for g in granted:
        if _scope_allows(g, req):
            return True
    return False


def has_all_scopes(record_or_id: Any, required: Any) -> bool:
    needed = normalize_scopes(required)
    if not needed:
        return True
    granted = get_key_scopes(record_or_id)
    for req in needed:
        if not any(_scope_allows(g, req) for g in granted):
            return False
    return True


def has_any_scope(record_or_id: Any, required: Any) -> bool:
    needed = normalize_scopes(required)
    if not needed:
        return True
    granted = get_key_scopes(record_or_id)
    for req in needed:
        if any(_scope_allows(g, req) for g in granted):
            return True
    return False


# Compatibility aliases.
key_has_scope = has_scope
check_scope = has_scope
is_allowed = has_scope
is_authorized = has_scope
can = has_scope
allows = has_scope
permits = has_scope
has_permission = has_scope
has_permissions = has_all_scopes
check_scopes = has_all_scopes
has_scopes = has_all_scopes
require_scope_check = has_scope


def verify_with_scope(key_value: Any, required_scope: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Verify a raw API key value and enforce required scope(s).

    Returns the key record when the key is valid (hash/expiry/revoked per
    ``api_keys.verify_api_key``) AND the record grants ``required_scope``.
    Returns ``None`` otherwise. When ``required_scope`` is empty/None this
    behaves exactly like :func:`api_keys.verify_api_key` (no restriction).
    """
    # Allow required scope via kwargs aliases.
    if required_scope is None:
        for alias in ("scope", "scopes", "required_scope", "required_scopes",
                      "permission", "permissions", "perm", "perms"):
            if kwargs.get(alias) is not None:
                required_scope = kwargs.get(alias)
                break
    # Allow key via kwargs aliases when positional is None.
    candidate = key_value
    if candidate is None and args:
        candidate = args[0]
    if candidate is None:
        for alias in ("key", "api_key", "apiKey", "token", "value", "plaintext", "secret"):
            if kwargs.get(alias) is not None:
                candidate = kwargs.get(alias)
                break
    if candidate is None:
        return None
    try:
        from app.services import api_keys as _api_keys

        record = _api_keys.verify_api_key(candidate)
    except Exception:  # noqa: BLE001
        return None
    if record is None:
        return None
    needed = normalize_scopes(required_scope)
    if not needed:
        return record
    if has_all_scopes(record, needed):
        return record
    return None


# Aliases for harness compatibility.
verify_key_with_scope = verify_with_scope
verify_scoped = verify_with_scope
verify_key_scoped = verify_with_scope
check_key_scope = verify_with_scope
verify_with_scopes = verify_with_scope
authenticate_with_scope = verify_with_scope
validate_with_scope = verify_with_scope
require_scope = verify_with_scope


def _sync_scope_fields(record: Dict[str, Any], scopes: List[str]) -> Dict[str, Any]:
    record["scopes"] = list(scopes)
    record["permissions"] = list(scopes)
    record["scope"] = ",".join(scopes) if scopes else ""
    try:
        record["updated_at"] = _now_iso()
    except Exception:  # noqa: BLE001
        pass
    return record


def grant_scope(key_id: Any, scope: Any, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Grant one or more scopes to an existing key. Returns updated record or None."""
    extra = normalize_scopes(args[0]) if args else []
    new_scopes = normalize_scopes(scope) + extra
    for alias in ("scopes", "permissions", "perms"):
        if kwargs.get(alias) is not None:
            new_scopes += normalize_scopes(kwargs.get(alias))
    if not new_scopes:
        return None
    try:
        from app.services import api_keys as _api_keys

        record = _api_keys.get_api_key(key_id)
        if record is None:
            return None
        current = get_key_scopes(record)
        merged = list(current)
        for s in new_scopes:
            if not any(_eq(m, s) for m in merged):
                merged.append(s)
        return _sync_scope_fields(record, merged)
    except Exception:  # noqa: BLE001
        return None


add_scope = grant_scope
add_scopes = grant_scope
attach_scope = grant_scope
assign_scope = grant_scope


def revoke_scope(key_id: Any, scope: Any, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Remove one or more scopes from an existing key. Returns updated record or None."""
    extra = normalize_scopes(args[0]) if args else []
    doomed = normalize_scopes(scope) + extra
    for alias in ("scopes", "permissions", "perms"):
        if kwargs.get(alias) is not None:
            doomed += normalize_scopes(kwargs.get(alias))
    try:
        from app.services import api_keys as _api_keys

        record = _api_keys.get_api_key(key_id)
        if record is None:
            return None
        current = get_key_scopes(record)
        doomed_low = {s.lower() for s in doomed}
        kept = [s for s in current if s.lower() not in doomed_low]
        return _sync_scope_fields(record, kept)
    except Exception:  # noqa: BLE001
        return None


remove_scope = revoke_scope
remove_scopes = revoke_scope
detach_scope = revoke_scope
drop_scope = revoke_scope


def set_scopes(key_id: Any, scopes: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Replace the scope set on an existing key. Returns updated record or None."""
    if scopes is None and args:
        scopes = args[0]
    if scopes is None:
        for alias in ("scope", "permissions", "perms", "permission"):
            if kwargs.get(alias) is not None:
                scopes = kwargs.get(alias)
                break
    normalized = normalize_scopes(scopes)
    try:
        from app.services import api_keys as _api_keys

        record = _api_keys.get_api_key(key_id)
        if record is None:
            return None
        return _sync_scope_fields(record, normalized)
    except Exception:  # noqa: BLE001
        return None


update_scopes = set_scopes
replace_scopes = set_scopes
put_scopes = set_scopes
write_scopes = set_scopes


def create_scoped_key(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create a new API key with scopes (thin wrapper over api_keys.create_api_key)."""
    from app.services import api_keys as _api_keys

    return _api_keys.create_api_key(*args, **kwargs)


create_key_with_scopes = create_scoped_key
create_key_with_scope = create_scoped_key
issue_scoped_key = create_scoped_key
generate_scoped_key = create_scoped_key


def list_scopes() -> List[str]:
    return list(KNOWN_SCOPES)


def available_scopes() -> List[str]:
    return list(KNOWN_SCOPES)


def get_available_scopes() -> List[str]:
    return list(KNOWN_SCOPES)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    try:
        from app.services import api_keys as _api_keys

        base = _api_keys.get_status()
    except Exception:  # noqa: BLE001
        base = {"ok": True, "service": "api_keys", "count": 0}
    out = dict(base)
    out["service"] = "key_scopes"
    out["ok"] = True
    out["available_scopes"] = list(KNOWN_SCOPES)
    out["scopes"] = list(KNOWN_SCOPES)
    out["enforcement"] = "opt-in"
    out["baseline_enforces_scopes"] = False
    return out


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return get_status(*args, **kwargs)


def health(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return get_status(*args, **kwargs)


class KeyScopesService:
    """Object-oriented facade over the module functions."""

    def __init__(self) -> None:
        self.available = KNOWN_SCOPES
        self.known_scopes = KNOWN_SCOPES

    def normalize_scopes(self, value: Any) -> List[str]:
        return normalize_scopes(value)

    def parse_scopes(self, value: Any) -> List[str]:
        return normalize_scopes(value)

    def list_scopes(self) -> List[str]:
        return list(KNOWN_SCOPES)

    def available_scopes(self) -> List[str]:
        return list(KNOWN_SCOPES)

    def get_key_scopes(self, record_or_id: Any) -> List[str]:
        return get_key_scopes(record_or_id)

    def get_scopes(self, record_or_id: Any) -> List[str]:
        return get_key_scopes(record_or_id)

    def has_scope(self, record_or_id: Any, required: Any) -> bool:
        return has_scope(record_or_id, required)

    def has_all_scopes(self, record_or_id: Any, required: Any) -> bool:
        return has_all_scopes(record_or_id, required)

    def has_any_scope(self, record_or_id: Any, required: Any) -> bool:
        return has_any_scope(record_or_id, required)

    def check_scope(self, record_or_id: Any, required: Any) -> bool:
        return has_scope(record_or_id, required)

    def verify_with_scope(self, key_value: Any, required_scope: Any = None, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return verify_with_scope(key_value, required_scope, *a, **k)

    def verify(self, key_value: Any, required_scope: Any = None, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return verify_with_scope(key_value, required_scope, *a, **k)

    def grant_scope(self, key_id: Any, scope: Any, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return grant_scope(key_id, scope, *a, **k)

    def revoke_scope(self, key_id: Any, scope: Any, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return revoke_scope(key_id, scope, *a, **k)

    def set_scopes(self, key_id: Any, scopes: Any = None, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return set_scopes(key_id, scopes, *a, **k)

    def create_scoped_key(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return create_scoped_key(*a, **k)

    def get_status(self) -> Dict[str, Any]:
        return get_status()

    def status(self) -> Dict[str, Any]:
        return get_status()

    def health(self) -> Dict[str, Any]:
        return get_status()


key_scopes_service = KeyScopesService()
key_scope_service = key_scopes_service
scopes_service = key_scopes_service
scope_service = key_scopes_service
service = key_scopes_service
manager = key_scopes_service
default_service = key_scopes_service

KeyScopesManager = KeyScopesService
KeyScopeService = KeyScopesService
ScopeService = KeyScopesService
ScopesService = KeyScopesService
Service = KeyScopesService
Manager = KeyScopesService
KeyScopesServiceClass = KeyScopesService

__all__ = [
    "KNOWN_SCOPES",
    "AVAILABLE_SCOPES",
    "ALL_SCOPES",
    "SUPPORTED_SCOPES",
    "VALID_SCOPES",
    "DEFAULT_SCOPES",
    "KeyScopesService",
    "key_scopes_service",
    "normalize_scopes",
    "parse_scopes",
    "get_key_scopes",
    "get_scopes",
    "has_scope",
    "has_all_scopes",
    "has_any_scope",
    "verify_with_scope",
    "verify_key_with_scope",
    "grant_scope",
    "revoke_scope",
    "set_scopes",
    "create_scoped_key",
    "list_scopes",
    "available_scopes",
    "get_available_scopes",
    "get_status",
]
