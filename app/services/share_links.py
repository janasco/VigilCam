"""Time-limited share links service.

Creates signed, expiring tokens that reference existing snapshot or event
resources. This service intentionally does NOT store images (see
``app.services.snapshots``) and does NOT copy files anywhere (see
``app.services.cloud_upload``) -- it only mints, verifies, lists, and
revokes lightweight signed references.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    from app.core.config import settings as _settings
except Exception:  # noqa: BLE001
    _settings = None


SHARE_LINKS: Dict[str, Dict[str, Any]] = {}
share_links = SHARE_LINKS
LINKS = SHARE_LINKS
STORE = SHARE_LINKS
_links = SHARE_LINKS
_store = SHARE_LINKS

_lock = threading.Lock()

DEFAULT_TTL_SECONDS = 24 * 3600
MAX_TTL_SECONDS = 30 * 24 * 3600

RESOURCE_ALIASES = {
    "snapshot": "snapshot",
    "snapshots": "snapshot",
    "snap": "snapshot",
    "image": "snapshot",
    "photo": "snapshot",
    "event": "event",
    "events": "event",
    "detection": "event",
}

TYPE_KEYS = ("resource_type", "resourceType", "type", "kind", "target_type", "ref_type")
ID_KEYS = ("resource_id", "resourceId", "event_id", "eventId", "id", "rid", "event", "snapshot_id")
PATH_KEYS = ("snapshot_path", "snapshotPath", "path", "file_path", "file", "ref", "reference")
TTL_KEYS = (
    "ttl_seconds", "ttlSeconds", "expires_in_seconds", "expires_in",
    "ttl", "ttl_minutes", "ttl_hours", "ttl_days",
    "expires_in_minutes", "expires_in_hours", "expires_in_days",
    "expires_at", "expiresAt", "expiry", "expiry_date",
)
TOKEN_KEYS = ("token", "share_token", "shareToken", "link_token", "code")
ID_LOOKUP_KEYS = ("link_id", "linkId", "share_id", "id", "jti", "kid")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_ts() -> int:
    return int(time.time())


def _now_iso() -> str:
    return _now().isoformat()


def _secret() -> bytes:
    candidates = []
    if _settings is not None:
        for attr in ("SECRET_KEY", "SECRET", "INTERNAL_API_KEY", "internal_api_key"):
            try:
                val = getattr(_settings, attr, None)
            except Exception:  # noqa: BLE001
                val = None
            if val:
                candidates.append(str(val))
    import os as _os

    for env_key in ("VIGILCAM_SECRET_KEY", "SECRET_KEY", "INTERNAL_API_KEY"):
        val = _os.environ.get(env_key)
        if val:
            candidates.append(val)
    for cand in candidates:
        if cand and cand not in ("", "change-me-in-production"):
            return cand.encode("utf-8")
    if candidates:
        return str(candidates[0]).encode("utf-8")
    return b"vigilcam-dev-share-secret"


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(data: str) -> bytes:
    s = str(data).strip()
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload_b64: str) -> str:
    sig = hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64e(sig)


def _normalize_resource_type(value: Any) -> str:
    s = str(value or "snapshot").strip().lower()
    if s in RESOURCE_ALIASES:
        return RESOURCE_ALIASES[s]
    raise ValueError(f"unsupported resource_type: {value!r} (expected 'snapshot' or 'event')")


def _parse_ttl_seconds(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - _now()).total_seconds()
        return max(1, int(delta))
    if isinstance(value, (int, float)):
        secs = int(value)
        if secs <= 0:
            raise ValueError("ttl must be positive")
        return secs
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Plain number string -> seconds.
        try:
            secs = int(float(s))
            if secs <= 0:
                raise ValueError("ttl must be positive")
            return secs
        except ValueError:
            pass
        # Try ISO datetime string -> expires_at.
        iso = s
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = (dt - _now()).total_seconds()
            if delta <= 0:
                raise ValueError("expires_at is in the past")
            return int(delta)
        except ValueError:
            pass
        raise ValueError(f"unrecognized ttl value: {value!r}")
    raise ValueError(f"unrecognized ttl value: {value!r}")


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
        elif isinstance(a, (int,)):
            data.setdefault("resource_id", a)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            # Bare strings that look like snapshot/event shorthand.
            if s.startswith("event:"):
                data.setdefault("resource_type", "event")
                rest = s.split(":", 1)[1]
                data.setdefault("resource_id", rest)
            elif s.startswith("snapshot:"):
                data.setdefault("resource_type", "snapshot")
                data.setdefault("snapshot_path", s.split(":", 1)[1])
            elif "/" in s or s.endswith((".jpg", ".jpeg", ".png")):
                data.setdefault("snapshot_path", s)
            else:
                data.setdefault("resource_id", s)
        else:
            try:
                attrs = {
                    k: getattr(a, k)
                    for k in dir(a)
                    if not k.startswith("_") and not callable(getattr(a, k, None))
                }
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested in ("payload", "options", "config", "params", "data", "body", "request", "link", "share"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    return data


def _resolve_ttl(data: Dict[str, Any]) -> int:
    # Explicit unit keys take precedence with proper scaling.
    for key, scale in (
        ("ttl_seconds", 1), ("ttlSeconds", 1), ("expires_in_seconds", 1),
        ("ttl", 1), ("expires_in", 1), ("ttl_minutes", 60),
        ("expires_in_minutes", 60), ("ttl_hours", 3600),
        ("expires_in_hours", 3600), ("ttl_days", 86400),
        ("expires_in_days", 86400),
    ):
        if data.get(key) is not None:
            try:
                secs = int(float(str(data.get(key)))) * scale
            except (TypeError, ValueError):
                raise ValueError(f"invalid ttl for {key}: {data.get(key)!r}")
            if secs <= 0:
                raise ValueError("ttl must be positive")
            return min(secs, MAX_TTL_SECONDS)
    raw = _first(data, TTL_KEYS)
    if raw is None:
        return DEFAULT_TTL_SECONDS
    secs = _parse_ttl_seconds(raw)
    if secs is None:
        return DEFAULT_TTL_SECONDS
    if secs <= 0:
        raise ValueError("ttl must be positive")
    return min(secs, MAX_TTL_SECONDS)


def _mint_token(link_id: str, resource_type: str, resource_ref: str, iat: int, exp: int) -> str:
    payload = {"v": 1, "jti": link_id, "typ": resource_type, "ref": resource_ref, "iat": iat, "exp": exp}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64e(raw)
    return payload_b64 + "." + _sign(payload_b64)


def _decode_token(token: str) -> Dict[str, Any]:
    parts = str(token or "").strip().split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("malformed share token")
    payload_b64, sig_b64 = parts
    try:
        payload_raw = _b64d(payload_b64)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"malformed share token: {exc}") from exc
    expected = _sign(payload_b64)
    if not hmac.compare_digest(expected, sig_b64.strip()):
        raise ValueError("invalid share token signature")
    try:
        payload = json.loads(payload_raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"malformed share token payload: {exc}") from exc
    if not isinstance(payload, dict) or "jti" not in payload or "exp" not in payload:
        raise ValueError("malformed share token payload")
    return payload


def build_share_url(token: str, base_url: Any = None) -> str:
    base = str(base_url or "/api/share").rstrip("/") or "/api/share"
    return f"{base}/{token}"


def create_share_link(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_create_args(args, kwargs)
    resource_type = _normalize_resource_type(_first(data, TYPE_KEYS) or "snapshot")

    resource_id = _first(data, ID_KEYS)
    snapshot_path = _first(data, PATH_KEYS)
    # Allow `ref`/`reference` to carry either an id or a path.
    ref = data.get("ref") or data.get("reference")
    if resource_id is None and snapshot_path is None and ref is not None:
        s = str(ref).strip()
        if resource_type == "event":
            resource_id = s
        else:
            snapshot_path = s

    if resource_type == "event":
        if resource_id is None or str(resource_id).strip() == "":
            raise ValueError("resource_id (event id) is required for event share links")
        try:
            resource_ref = str(int(str(resource_id).strip()))
        except (TypeError, ValueError):
            resource_ref = str(resource_id).strip()
    else:
        if snapshot_path is None and resource_id is not None:
            snapshot_path = str(resource_id)
        if snapshot_path is None or str(snapshot_path).strip() == "":
            raise ValueError("snapshot_path is required for snapshot share links")
        # Store the reference as given; never touch the filesystem here.
        # (Snapshots live in app.services.snapshots; uploads in app.services.cloud_upload.)
        resource_ref = str(snapshot_path).strip()
        if len(resource_ref) > 512:
            raise ValueError("snapshot_path is too long")

    ttl_seconds = _resolve_ttl(data)
    iat = _now_ts()
    exp = iat + ttl_seconds
    link_id = str(data.get("link_id") or data.get("id") or uuid.uuid4())
    token = _mint_token(link_id, resource_type, resource_ref, iat, exp)
    created = _now_iso()
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()

    record: Dict[str, Any] = {
        "id": link_id,
        "link_id": link_id,
        "jti": link_id,
        "token": token,
        "share_token": token,
        "url": build_share_url(token, data.get("base_url") or data.get("baseUrl")),
        "share_url": build_share_url(token, data.get("base_url") or data.get("baseUrl")),
        "resource_type": resource_type,
        "type": resource_type,
        "resource_id": resource_ref,
        "resource_ref": resource_ref,
        "snapshot_path": resource_ref if resource_type == "snapshot" else None,
        "path": resource_ref if resource_type == "snapshot" else None,
        "event_id": resource_ref if resource_type == "event" else None,
        "ttl_seconds": ttl_seconds,
        "expires_in_seconds": ttl_seconds,
        "iat": iat,
        "exp": exp,
        "created_at": created,
        "updated_at": created,
        "expires_at": expires_at,
        "revoked": False,
        "is_revoked": False,
        "active": True,
        "is_active": True,
        "status": "active",
        "state": "active",
    }
    with _lock:
        SHARE_LINKS[link_id] = record
    return record


def verify_share_token(token: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    candidate = token
    if candidate is None and args:
        for a in args:
            if isinstance(a, str) and a.strip():
                candidate = a
                break
    if candidate is None:
        candidate = _first(kwargs, TOKEN_KEYS)
    if candidate is None:
        for v in kwargs.values():
            if isinstance(v, str) and "." in v:
                candidate = v
                break
    if candidate is None:
        return None
    try:
        payload = _decode_token(str(candidate))
    except ValueError:
        return None
    now = _now_ts()
    try:
        if int(payload.get("exp", 0)) <= now:
            return None
    except (TypeError, ValueError):
        return None
    link_id = str(payload.get("jti", ""))
    with _lock:
        record = SHARE_LINKS.get(link_id)
        if record is not None:
            if record.get("revoked"):
                return None
            try:
                rec_exp = datetime.fromisoformat(str(record.get("expires_at")).replace("Z", "+00:00"))
                if rec_exp.tzinfo is None:
                    rec_exp = rec_exp.replace(tzinfo=timezone.utc)
                if rec_exp <= _now():
                    return None
            except Exception:  # noqa: BLE001
                pass
            out = dict(record)
            out["payload"] = payload
            out["valid"] = True
            return out
    # Stateless fallback: signature + expiry valid, link not tracked (e.g. restarted).
    resource_type = str(payload.get("typ", "snapshot"))
    ref = str(payload.get("ref", ""))
    return {
        "id": link_id,
        "link_id": link_id,
        "jti": link_id,
        "token": str(candidate),
        "share_token": str(candidate),
        "resource_type": resource_type,
        "type": resource_type,
        "resource_id": ref,
        "resource_ref": ref,
        "snapshot_path": ref if resource_type == "snapshot" else None,
        "event_id": ref if resource_type == "event" else None,
        "iat": payload.get("iat"),
        "exp": payload.get("exp"),
        "expires_at": datetime.fromtimestamp(int(payload.get("exp")), tz=timezone.utc).isoformat(),
        "revoked": False,
        "active": True,
        "status": "active",
        "valid": True,
        "stateless": True,
        "payload": payload,
    }


def get_share_link(link_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    lookup = link_id
    if lookup is None and args:
        for a in args:
            if isinstance(a, str) and a.strip():
                lookup = a
                break
    if lookup is None:
        lookup = _first(kwargs, ID_LOOKUP_KEYS)
    if lookup is None:
        lookup = _first(kwargs, TOKEN_KEYS)
        if lookup is not None:
            verified = verify_share_token(lookup)
            if verified is not None:
                with _lock:
                    return SHARE_LINKS.get(str(verified.get("id")))
            return None
    if lookup is None:
        return None
    key = str(lookup).strip()
    with _lock:
        if key in SHARE_LINKS:
            return SHARE_LINKS[key]
        # Allow lookup by full token value.
        for record in SHARE_LINKS.values():
            if record.get("token") == key:
                return record
    return None


def list_share_links(include_revoked: bool = True, **kwargs: Any) -> List[Dict[str, Any]]:
    if "includeRevoked" in kwargs:
        include_revoked = bool(kwargs["includeRevoked"])
    with _lock:
        records = list(SHARE_LINKS.values())
    if not include_revoked:
        records = [r for r in records if not r.get("revoked")]
    resource_type = kwargs.get("resource_type") or kwargs.get("type")
    if resource_type is not None:
        try:
            norm = _normalize_resource_type(resource_type)
        except ValueError:
            return []
        records = [r for r in records if r.get("resource_type") == norm]
    status_filter = kwargs.get("status") or kwargs.get("state")
    if status_filter is not None:
        records = [r for r in records if str(r.get("status", "")).lower() == str(status_filter).lower()]
    return records


def revoke_share_link(link_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    record = get_share_link(link_id, *args, **kwargs)
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


def delete_share_link(link_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    record = get_share_link(link_id, *args, **kwargs)
    if record is None:
        raw = str(link_id) if link_id is not None else None
        if raw is None:
            return False
        with _lock:
            return SHARE_LINKS.pop(raw, None) is not None
    with _lock:
        return SHARE_LINKS.pop(str(record.get("id")), None) is not None


def clear(*args: Any, **kwargs: Any) -> int:
    with _lock:
        n = len(SHARE_LINKS)
        SHARE_LINKS.clear()
        return n


def count() -> int:
    with _lock:
        return len(SHARE_LINKS)


def get_status() -> Dict[str, Any]:
    with _lock:
        total = len(SHARE_LINKS)
        active = sum(1 for r in SHARE_LINKS.values() if not r.get("revoked"))
    return {
        "ok": True,
        "service": "share_links",
        "status": "idle" if total == 0 else "active",
        "count": total,
        "total": total,
        "size": total,
        "active": active,
        "revoked": total - active,
    }


class ShareLinkRecord(dict):
    """Dict subclass allowing attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


class ShareLinksService:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.store = SHARE_LINKS

    def create_share_link(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_share_link(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_share_link(*args, **kwargs)

    def issue(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_share_link(*args, **kwargs)

    def generate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_share_link(*args, **kwargs)

    def mint(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_share_link(*args, **kwargs)

    def verify(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return verify_share_token(*args, **kwargs)

    def verify_share_token(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return verify_share_token(*args, **kwargs)

    def validate(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return verify_share_token(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_share_link(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        include_revoked = True
        if args and isinstance(args[0], bool):
            include_revoked = args[0]
        if "include_revoked" in kwargs:
            include_revoked = bool(kwargs.pop("include_revoked"))
        return list_share_links(include_revoked=include_revoked, **kwargs)

    def revoke(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return revoke_share_link(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return delete_share_link(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    def count(self) -> int:
        return count()

    def get_status(self) -> Dict[str, Any]:
        return get_status()


share_links_service = ShareLinksService()
share_link_service = share_links_service
service = share_links_service
manager = share_links_service

ShareLinkService = ShareLinksService
ShareLinksManager = ShareLinksService
Service = ShareLinksService
Manager = ShareLinksService


def create(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_share_link(*args, **kwargs)


def issue(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_share_link(*args, **kwargs)


def generate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_share_link(*args, **kwargs)


def mint(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_share_link(*args, **kwargs)


def verify(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return verify_share_token(*args, **kwargs)


def validate(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return verify_share_token(*args, **kwargs)


def check(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return verify_share_token(*args, **kwargs)


def get(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_share_link(*args, **kwargs)


def fetch(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_share_link(*args, **kwargs)


def lookup(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_share_link(*args, **kwargs)


def list_links(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return share_links_service.list(*args, **kwargs)


def revoke(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return revoke_share_link(*args, **kwargs)


def remove(*args: Any, **kwargs: Any) -> bool:
    return delete_share_link(*args, **kwargs)


def delete(*args: Any, **kwargs: Any) -> bool:
    return delete_share_link(*args, **kwargs)


def reset(*args: Any, **kwargs: Any) -> int:
    return clear(*args, **kwargs)


__all__ = [
    "SHARE_LINKS",
    "ShareLinksService",
    "ShareLinkRecord",
    "share_links_service",
    "create_share_link",
    "verify_share_token",
    "get_share_link",
    "list_share_links",
    "revoke_share_link",
    "delete_share_link",
    "build_share_url",
    "clear",
    "count",
    "get_status",
]
