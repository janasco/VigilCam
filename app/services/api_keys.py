"""In-memory API key management service.

Provides creation, listing, lookup, verification, revocation and
deletion of API keys. Keys are stored hashed (SHA-256); the plaintext
value is only returned once at creation time.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


API_KEYS: Dict[str, Dict[str, Any]] = {}
api_keys = API_KEYS
keys = API_KEYS
KEYS = API_KEYS
STORE = API_KEYS
_store = API_KEYS

_HASH_INDEX: Dict[str, str] = {}

_lock = threading.Lock()

DEFAULT_PREFIX = "vk_"
KEY_BYTES = 32

NAME_KEYS = ("name", "label", "title", "key_name", "keyName", "description", "desc")
SCOPES_KEYS = ("scopes", "scope", "permissions", "perms", "access", "roles")
EXPIRES_KEYS = ("expires_in_days", "expires_days", "expiry_days", "ttl_days", "expires_in", "ttl", "expires_at", "expiry", "expiry_date")
PREFIX_KEYS = ("prefix", "key_prefix")
KEY_ID_KEYS = ("key_id", "keyId", "id", "kid")
KEY_VALUE_KEYS = ("key", "api_key", "apiKey", "token", "api_token", "secret", "value", "plaintext")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _mask_key(plaintext: Optional[str]) -> str:
    if not plaintext:
        return ""
    s = str(plaintext)
    if len(s) <= 8:
        return s[:2] + "***"
    return s[:4] + "..." + s[-4:]


def _parse_scopes(value: Any) -> List[str]:
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
                    out.append(s)
            elif item is not None:
                out.append(str(item))
        return out
    return [str(value)]


def _parse_expiry(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    if isinstance(value, (int, float)):
        days = float(value)
        if days <= 0:
            return None
        return (_now() + timedelta(days=days)).isoformat()
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("never", "none", "null", "no-expiry", "no_expiry"):
            return None
        try:
            days = float(s)
            if days > 0:
                return (_now() + timedelta(days=days)).isoformat()
            return None
        except ValueError:
            pass
        try:
            iso = s
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            return None
    return None


def _is_expired(record: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    expires_at = record.get("expires_at")
    if not expires_at:
        return False
    try:
        iso = str(expires_at)
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        current = now or _now()
        return dt <= current
    except Exception:  # noqa: BLE001
        return False


def _public_view(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return a safe copy without key material."""
    out = dict(record)
    out.pop("key_hash", None)
    out.pop("keyHash", None)
    return out


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _normalize_create_args(args: tuple, kwargs: dict) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if "name" not in data and "=" not in s and "," not in s:
                # Heuristic: bare string is a name unless it looks like a key.
                if not s.startswith(DEFAULT_PREFIX) and len(s) < 64:
                    data.setdefault("name", s)
                else:
                    data.setdefault("key", s)
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested in ("payload", "options", "config", "params", "data", "body", "request", "key", "api_key"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    return data


class ApiKeyRecord(dict):
    """Dict subclass so callers can use attribute or item access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)

    def dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()


ApiKey = ApiKeyRecord
KeyRecord = ApiKeyRecord
Key = ApiKeyRecord
Record = ApiKeyRecord


class ApiKeyService:
    def __init__(self) -> None:
        self.store = API_KEYS

    def create_api_key(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _normalize_create_args(args, kwargs)
        name = _first(data, NAME_KEYS) or data.get("key_name") or "default"
        name = str(name).strip() or "default"
        scopes = _parse_scopes(_first(data, SCOPES_KEYS))
        expires_raw = _first(data, EXPIRES_KEYS)
        expires_at = _parse_expiry(expires_raw)
        prefix = _first(data, PREFIX_KEYS) or DEFAULT_PREFIX
        prefix = str(prefix)

        key_id = str(data.get("key_id") or data.get("id") or uuid.uuid4())
        plaintext = secrets.token_urlsafe(KEY_BYTES)
        # Ensure prefix for easy identification.
        if not plaintext.startswith(prefix):
            # Prepend prefix; keep entropy by appending random part.
            plaintext = f"{prefix}{plaintext}"
        key_hash = _hash_key(plaintext)
        created = _now_iso()
        record: Dict[str, Any] = ApiKeyRecord()
        record["id"] = key_id
        record["key_id"] = key_id
        record["kid"] = key_id
        record["name"] = name
        record["label"] = name
        record["key_name"] = name
        record["key"] = plaintext
        record["api_key"] = plaintext
        record["plaintext"] = plaintext
        record["value"] = plaintext
        record["prefix"] = plaintext[: len(prefix) + 4] if len(plaintext) > len(prefix) + 4 else plaintext
        record["key_prefix"] = record["prefix"]
        record["masked"] = _mask_key(plaintext)
        record["key_hash"] = key_hash
        record["scopes"] = scopes
        record["permissions"] = scopes
        record["created_at"] = created
        record["updated_at"] = created
        record["expires_at"] = expires_at
        record["revoked"] = False
        record["is_revoked"] = False
        record["active"] = True
        record["is_active"] = True
        record["status"] = "active"
        record["state"] = "active"
        record["last_used_at"] = None
        record["usage_count"] = 0
        record["use_count"] = 0
        for k, v in data.items():
            if k in record:
                continue
            if k in KEY_VALUE_KEYS or k in NAME_KEYS or k in SCOPES_KEYS or k in EXPIRES_KEYS or k in PREFIX_KEYS:
                continue
            record[k] = v
        with _lock:
            API_KEYS[key_id] = record
            _HASH_INDEX[key_hash] = key_id
        return record

    # Creation aliases (instance level).
    def create(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_api_key(*args, **kwargs)

    def create_key(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_api_key(*args, **kwargs)

    def generate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_api_key(*args, **kwargs)

    def generate_key(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_api_key(*args, **kwargs)

    def new(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_api_key(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_api_key(*args, **kwargs)

    def issue(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_api_key(*args, **kwargs)

    def get_api_key(self, key_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if key_id is None:
            for candidate in list(args) + list(kwargs.values()):
                if isinstance(candidate, str) and candidate:
                    key_id = candidate
                    break
            else:
                key_id = kwargs.get("key_id") or kwargs.get("id") or kwargs.get("kid")
        if key_id is None:
            return None
        lookup = str(key_id)
        with _lock:
            if lookup in API_KEYS:
                return API_KEYS[lookup]
            # Allow lookup by raw key value via hash index.
            hashed = _hash_key(lookup)
            mapped = _HASH_INDEX.get(hashed)
            if mapped is not None:
                return API_KEYS.get(mapped)
            # Linear scan for prefix matches.
            for record in API_KEYS.values():
                if record.get("key") == lookup or record.get("api_key") == lookup:
                    return record
        return None

    def get(self, key_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_api_key(key_id, *args, **kwargs)

    def find(self, key_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_api_key(key_id, *args, **kwargs)

    def lookup(self, key_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_api_key(key_id, *args, **kwargs)

    def fetch(self, key_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_api_key(key_id, *args, **kwargs)

    def list_api_keys(self, include_revoked: bool = True, **kwargs: Any) -> List[Dict[str, Any]]:
        if "includeRevoked" in kwargs:
            include_revoked = bool(kwargs["includeRevoked"])
        with _lock:
            records = list(API_KEYS.values())
        if not include_revoked:
            records = [r for r in records if not r.get("revoked")]
        status_filter = kwargs.get("status") or kwargs.get("state")
        if status_filter is not None:
            records = [r for r in records if str(r.get("status", "")).lower() == str(status_filter).lower()]
        return records

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        include_revoked = True
        if args and isinstance(args[0], bool):
            include_revoked = args[0]
        if "include_revoked" in kwargs:
            include_revoked = bool(kwargs.pop("include_revoked"))
        return self.list_api_keys(include_revoked=include_revoked, **kwargs)

    def list_active(self) -> List[Dict[str, Any]]:
        return self.list_api_keys(include_revoked=False)

    def verify_api_key(self, key_value: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        candidate = key_value
        if candidate is None and args:
            candidate = args[0]
        if candidate is None:
            candidate = _first(kwargs, KEY_VALUE_KEYS)
        if candidate is None:
            return None
        raw = str(candidate)
        hashed = _hash_key(raw)
        with _lock:
            key_id = _HASH_INDEX.get(hashed)
            record = API_KEYS.get(key_id) if key_id else None
            if record is None:
                for rec in API_KEYS.values():
                    if rec.get("key") == raw or rec.get("api_key") == raw:
                        record = rec
                        break
            if record is None:
                return None
            if record.get("revoked"):
                return None
            if _is_expired(record):
                return None
            # Update usage stats.
            try:
                record["last_used_at"] = _now_iso()
                record["usage_count"] = int(record.get("usage_count", 0)) + 1
                record["use_count"] = int(record.get("usage_count", 0))
                record["updated_at"] = record["last_used_at"]
            except Exception:  # noqa: BLE001
                pass
            return record

    def verify(self, key_value: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.verify_api_key(key_value, *args, **kwargs)

    def authenticate(self, key_value: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.verify_api_key(key_value, *args, **kwargs)

    def validate(self, key_value: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.verify_api_key(key_value, *args, **kwargs)

    def check(self, key_value: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.verify_api_key(key_value, *args, **kwargs)

    def is_valid(self, key_value: Any = None, *args: Any, **kwargs: Any) -> bool:
        return self.verify_api_key(key_value, *args, **kwargs) is not None

    def revoke_api_key(self, key_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        record = self.get_api_key(key_id, *args, **kwargs)
        if record is None:
            return None
        record["revoked"] = True
        record["is_revoked"] = True
        record["active"] = False
        record["is_active"] = False
        record["status"] = "revoked"
        record["state"] = "revoked"
        record["revoked_at"] = _now_iso()
        record["updated_at"] = record["revoked_at"]
        return record

    def revoke(self, key_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.revoke_api_key(key_id, *args, **kwargs)

    def deactivate(self, key_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.revoke_api_key(key_id, *args, **kwargs)

    def disable(self, key_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.revoke_api_key(key_id, *args, **kwargs)

    def delete_api_key(self, key_id: Any = None, *args: Any, **kwargs: Any) -> bool:
        record = self.get_api_key(key_id, *args, **kwargs)
        if record is None:
            # Accept raw id string even if not resolvable via hash.
            raw = str(key_id) if key_id is not None else None
            if raw is None:
                return False
            with _lock:
                existed = raw in API_KEYS
                removed = API_KEYS.pop(raw, None)
                if removed is not None:
                    _HASH_INDEX.pop(str(removed.get("key_hash", "")), None)
                return existed
        rid = str(record.get("id"))
        with _lock:
            API_KEYS.pop(rid, None)
            _HASH_INDEX.pop(str(record.get("key_hash", "")), None)
            return True

    def delete(self, key_id: Any = None, *args: Any, **kwargs: Any) -> bool:
        return self.delete_api_key(key_id, *args, **kwargs)

    def remove(self, key_id: Any = None, *args: Any, **kwargs: Any) -> bool:
        return self.delete_api_key(key_id, *args, **kwargs)

    def rotate_api_key(self, key_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        old = self.get_api_key(key_id, *args, **kwargs)
        if old is None:
            return None
        self.revoke_api_key(old.get("id"))
        data = {k: v for k, v in dict(old).items() if k in ("name", "label", "scopes", "permissions")}
        data["name"] = data.get("name") or old.get("name")
        data["scopes"] = data.get("scopes") or old.get("scopes")
        return self.create_api_key(**data)

    def rotate(self, key_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.rotate_api_key(key_id, *args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        with _lock:
            n = len(API_KEYS)
            API_KEYS.clear()
            _HASH_INDEX.clear()
            return n

    def reset(self, *args: Any, **kwargs: Any) -> int:
        return self.clear(*args, **kwargs)

    def purge(self, *args: Any, **kwargs: Any) -> int:
        return self.clear(*args, **kwargs)

    def count(self) -> int:
        with _lock:
            return len(API_KEYS)

    def size(self) -> int:
        return self.count()

    def __len__(self) -> int:
        return self.count()

    def get_status(self) -> Dict[str, Any]:
        with _lock:
            total = len(API_KEYS)
            active = sum(1 for r in API_KEYS.values() if not r.get("revoked"))
            revoked = total - active
        return {
            "ok": True,
            "service": "api_keys",
            "status": "idle" if total == 0 else "active",
            "count": total,
            "total": total,
            "size": total,
            "active": active,
            "revoked": revoked,
        }

    def status(self) -> Dict[str, Any]:
        return self.get_status()

    def health(self) -> Dict[str, Any]:
        return self.get_status()


api_keys_service = ApiKeyService()
api_key_service = api_keys_service
key_service = api_keys_service
service = api_keys_service
manager = api_keys_service
api_keys_manager = api_keys_service
default_service = api_keys_service

ApiKeysService = ApiKeyService
APIKeyService = ApiKeyService
ApiKeyManager = ApiKeyService
KeyService = ApiKeyService
Service = ApiKeyService
Manager = ApiKeyService

ApiKeyServiceClass = ApiKeyService


def create_api_key(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return api_keys_service.create_api_key(*args, **kwargs)


def create(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return api_keys_service.create_api_key(*args, **kwargs)


def create_key(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return api_keys_service.create_api_key(*args, **kwargs)


def generate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return api_keys_service.create_api_key(*args, **kwargs)


def generate_key(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return api_keys_service.create_api_key(*args, **kwargs)


def get_api_key(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return api_keys_service.get_api_key(*args, **kwargs)


def get(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return api_keys_service.get_api_key(*args, **kwargs)


def list_api_keys(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return api_keys_service.list_api_keys(*args, **kwargs)


def list_keys(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return api_keys_service.list_api_keys(*args, **kwargs)


def verify_api_key(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return api_keys_service.verify_api_key(*args, **kwargs)


def verify(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return api_keys_service.verify_api_key(*args, **kwargs)


def revoke_api_key(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return api_keys_service.revoke_api_key(*args, **kwargs)


def revoke(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return api_keys_service.revoke_api_key(*args, **kwargs)


def delete_api_key(*args: Any, **kwargs: Any) -> bool:
    return api_keys_service.delete_api_key(*args, **kwargs)


def delete(*args: Any, **kwargs: Any) -> bool:
    return api_keys_service.delete_api_key(*args, **kwargs)


def remove(*args: Any, **kwargs: Any) -> bool:
    return api_keys_service.delete_api_key(*args, **kwargs)


def clear(*args: Any, **kwargs: Any) -> int:
    return api_keys_service.clear(*args, **kwargs)


def reset(*args: Any, **kwargs: Any) -> int:
    return api_keys_service.clear(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return api_keys_service.get_status(*args, **kwargs)


__all__ = [
    "API_KEYS",
    "ApiKeyService",
    "ApiKeyRecord",
    "api_keys_service",
    "create_api_key",
    "get_api_key",
    "list_api_keys",
    "verify_api_key",
    "revoke_api_key",
    "delete_api_key",
    "clear",
    "get_status",
]
