"""Temporary stream access token service.

Issues short-lived, HMAC-signed tokens bound to a single camera that gate
MJPEG live-stream access. This is intentionally separate from
``app.services.share_links`` which only covers ``snapshot`` and ``event``
resources (see ``RESOURCE_ALIASES`` / ``_normalize_resource_type`` there)
and never mints stream credentials.

Tokens are stored in-memory keyed by token id; verification checks
signature, expiry, revocation, and camera binding. A stateless fallback
(signature + expiry) is returned when the id is unknown, e.g. after a
restart, mirroring ``share_links.verify_share_token``.
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


STREAM_TOKENS: Dict[str, Dict[str, Any]] = {}
stream_tokens = STREAM_TOKENS
TOKENS = STREAM_TOKENS
STORE = STREAM_TOKENS
_tokens = STREAM_TOKENS
_store = STREAM_TOKENS
STREAM_TOKEN_STORE = STREAM_TOKENS

_lock = threading.Lock()

# Temp tokens: short-lived by design. Default 5 minutes, cap 1 hour.
DEFAULT_TTL_SECONDS = 300
MAX_TTL_SECONDS = 3600

SCOPE_ALIASES = {
    "mjpeg": "mjpeg",
    "stream": "mjpeg",
    "live": "mjpeg",
    "video": "mjpeg",
    "all": "mjpeg",
}

CAMERA_KEYS = (
    "camera_id",
    "cameraId",
    "camera",
    "cam",
    "cam_id",
    "camId",
    "id",
    "cameraID",
)
SCOPE_KEYS = ("scope", "stream_scope", "kind", "type", "access", "permission")
TTL_KEYS = (
    "ttl_seconds",
    "ttlSeconds",
    "expires_in_seconds",
    "expires_in",
    "ttl",
    "ttl_minutes",
    "ttl_hours",
    "expires_at",
    "expiresAt",
    "expiry",
)
TOKEN_KEYS = ("token", "stream_token", "streamToken", "access_token", "accessToken", "value", "secret", "code")
ID_LOOKUP_KEYS = ("token_id", "tokenId", "stream_token_id", "id", "jti", "kid")


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
    return b"vigilcam-dev-stream-secret"


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(data: str) -> bytes:
    s = str(data).strip()
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _hmac_sign(payload_b64: str) -> str:
    sig = hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64e(sig)


def _sign(payload_b64: str) -> str:
    return _hmac_sign(payload_b64)


def _normalize_scope(value: Any) -> str:
    s = str(value or "mjpeg").strip().lower()
    return SCOPE_ALIASES.get(s, "mjpeg")


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
        try:
            secs = int(float(s))
            if secs <= 0:
                raise ValueError("ttl must be positive")
            return secs
        except ValueError:
            pass
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
            data.setdefault("camera_id", a)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if "/" in s or s.startswith("vk_") or ("." in s and len(s) > 32):
                # Looks like a token value, not a camera id.
                data.setdefault("token", s)
            elif "camera_id" not in data and "camera" not in data:
                data.setdefault("camera_id", s)
            else:
                data.setdefault("scope", s)
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
    for nested in ("payload", "options", "config", "params", "data", "body", "request", "token_request", "stream"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    return data


def _resolve_ttl(data: Dict[str, Any]) -> int:
    for key, scale in (
        ("ttl_seconds", 1),
        ("ttlSeconds", 1),
        ("expires_in_seconds", 1),
        ("ttl", 1),
        ("expires_in", 1),
        ("ttl_minutes", 60),
        ("expires_in_minutes", 60),
        ("ttl_hours", 3600),
        ("expires_in_hours", 3600),
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


def _mint_token(token_id: str, camera_id: str, scope: str, iat: int, exp: int) -> str:
    payload = {"v": 1, "jti": token_id, "cam": camera_id, "scope": scope, "iat": iat, "exp": exp}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64e(raw)
    return payload_b64 + "." + _hmac_sign(payload_b64)


def _decode_token(token: str) -> Dict[str, Any]:
    parts = str(token or "").strip().split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("malformed stream token")
    payload_b64, sig_b64 = parts
    try:
        payload_raw = _b64d(payload_b64)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"malformed stream token: {exc}") from exc
    expected = _hmac_sign(payload_b64)
    if not hmac.compare_digest(expected, sig_b64.strip()):
        raise ValueError("invalid stream token signature")
    try:
        payload = json.loads(payload_raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"malformed stream token payload: {exc}") from exc
    if not isinstance(payload, dict) or "jti" not in payload or "exp" not in payload:
        raise ValueError("malformed stream token payload")
    return payload


def build_stream_url(camera_id: Any, token: str, base_url: Any = None) -> str:
    base = str(base_url or "/api/stream_tokens").rstrip("/") or "/api/stream_tokens"
    return f"{base}/mjpeg/{camera_id}?token={token}"


def _camera_matches(record_cam: str, requested: Optional[Any]) -> bool:
    if requested is None:
        return True
    if str(record_cam) == "*":
        return True
    return str(record_cam) == str(requested)


def create_stream_token(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_create_args(args, kwargs)
    camera_id = _first(data, CAMERA_KEYS)
    if camera_id is None or str(camera_id).strip() == "":
        raise ValueError("camera_id is required for stream tokens")
    camera_id = str(camera_id).strip()
    scope = _normalize_scope(_first(data, SCOPE_KEYS) or data.get("scope") or "mjpeg")
    ttl_seconds = _resolve_ttl(data)
    iat = _now_ts()
    exp = iat + ttl_seconds
    token_id = str(data.get("token_id") or data.get("id") or uuid.uuid4())
    token = _mint_token(token_id, camera_id, scope, iat, exp)
    created = _now_iso()
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
    label = data.get("label") or data.get("name") or data.get("description")

    record: Dict[str, Any] = {
        "id": token_id,
        "token_id": token_id,
        "jti": token_id,
        "token": token,
        "stream_token": token,
        "access_token": token,
        "value": token,
        "camera_id": camera_id,
        "cameraId": camera_id,
        "cam": camera_id,
        "scope": scope,
        "label": label,
        "name": label,
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
        "usage_count": 0,
        "use_count": 0,
        "stream_url": build_stream_url(camera_id, token, data.get("base_url") or data.get("baseUrl")),
        "mjpeg_url": build_stream_url(camera_id, token, data.get("base_url") or data.get("baseUrl")),
    }
    with _lock:
        STREAM_TOKENS[token_id] = record
    return record


def verify_stream_token(token: Any = None, camera_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    candidate = token
    if candidate is None and args:
        for a in args:
            if isinstance(a, str) and a.strip():
                # If a second positional looks like a camera id and candidate set, treat as camera.
                if candidate is not None and camera_id is None and "." not in a:
                    camera_id = a
                elif candidate is None:
                    candidate = a
                else:
                    break
        if candidate is None and args:
            candidate = args[0] if args else None
    if candidate is None:
        candidate = _first(kwargs, TOKEN_KEYS)
    if candidate is None:
        for v in kwargs.values():
            if isinstance(v, str) and "." in v:
                candidate = v
                break
    if candidate is None:
        return None
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("cameraId") or kwargs.get("camera") or kwargs.get("cam")
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
    token_cam = str(payload.get("cam", ""))
    if camera_id is not None and not _camera_matches(token_cam, camera_id):
        return None
    link_id = str(payload.get("jti", ""))
    with _lock:
        record = STREAM_TOKENS.get(link_id)
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
            if not _camera_matches(str(record.get("camera_id", token_cam)), camera_id):
                return None
            try:
                record["usage_count"] = int(record.get("usage_count", 0)) + 1
                record["use_count"] = int(record.get("usage_count", 0))
                record["last_used_at"] = _now_iso()
                record["updated_at"] = record["last_used_at"]
            except Exception:  # noqa: BLE001
                pass
            out = dict(record)
            out["payload"] = payload
            out["valid"] = True
            return out
    # Stateless fallback: signature + expiry valid, id not tracked.
    scope = str(payload.get("scope", "mjpeg"))
    ref = str(payload.get("cam", ""))
    return {
        "id": link_id,
        "token_id": link_id,
        "jti": link_id,
        "token": str(candidate),
        "stream_token": str(candidate),
        "camera_id": ref,
        "cameraId": ref,
        "cam": ref,
        "scope": scope,
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


def verify_for_camera(token: Any, camera_id: Any) -> Optional[Dict[str, Any]]:
    """Gate helper for MJPEG access: token must be valid for this camera."""
    return verify_stream_token(token, camera_id=camera_id)


def is_token_valid_for_camera(token: Any, camera_id: Any) -> bool:
    return verify_stream_token(token, camera_id=camera_id) is not None


def get_stream_token(token_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    lookup = token_id
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
            verified = verify_stream_token(lookup)
            if verified is not None:
                with _lock:
                    return STREAM_TOKENS.get(str(verified.get("id")))
            return None
    if lookup is None:
        return None
    key = str(lookup).strip()
    with _lock:
        if key in STREAM_TOKENS:
            return STREAM_TOKENS[key]
        for record in STREAM_TOKENS.values():
            if record.get("token") == key:
                return record
    return None


def list_stream_tokens(include_revoked: bool = True, **kwargs: Any) -> List[Dict[str, Any]]:
    if "includeRevoked" in kwargs:
        include_revoked = bool(kwargs["includeRevoked"])
    with _lock:
        records = list(STREAM_TOKENS.values())
    if not include_revoked:
        records = [r for r in records if not r.get("revoked")]
    camera_id = kwargs.get("camera_id") or kwargs.get("cameraId") or kwargs.get("camera") or kwargs.get("cam")
    if camera_id is not None:
        records = [r for r in records if str(r.get("camera_id")) == str(camera_id)]
    scope = kwargs.get("scope")
    if scope is not None:
        records = [r for r in records if str(r.get("scope", "")).lower() == str(scope).lower()]
    status_filter = kwargs.get("status") or kwargs.get("state")
    if status_filter is not None:
        records = [r for r in records if str(r.get("status", "")).lower() == str(status_filter).lower()]
    return records


def revoke_stream_token(token_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    record = get_stream_token(token_id, *args, **kwargs)
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


def delete_stream_token(token_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    record = get_stream_token(token_id, *args, **kwargs)
    if record is None:
        raw = str(token_id) if token_id is not None else None
        if raw is None:
            return False
        with _lock:
            return STREAM_TOKENS.pop(raw, None) is not None
    with _lock:
        return STREAM_TOKENS.pop(str(record.get("id")), None) is not None


def clear(*args: Any, **kwargs: Any) -> int:
    with _lock:
        n = len(STREAM_TOKENS)
        STREAM_TOKENS.clear()
        return n


def count() -> int:
    with _lock:
        return len(STREAM_TOKENS)


def get_status() -> Dict[str, Any]:
    with _lock:
        total = len(STREAM_TOKENS)
        active = sum(1 for r in STREAM_TOKENS.values() if not r.get("revoked"))
    return {
        "ok": True,
        "service": "stream_tokens",
        "status": "idle" if total == 0 else "active",
        "count": total,
        "total": total,
        "size": total,
        "active": active,
        "revoked": total - active,
    }


class StreamTokenRecord(dict):
    """Dict subclass allowing attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


class StreamTokensService:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.store = STREAM_TOKENS

    def create_stream_token(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_stream_token(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_stream_token(*args, **kwargs)

    def issue(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_stream_token(*args, **kwargs)

    def generate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_stream_token(*args, **kwargs)

    def mint(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_stream_token(*args, **kwargs)

    def verify(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return verify_stream_token(*args, **kwargs)

    def verify_stream_token(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return verify_stream_token(*args, **kwargs)

    def verify_for_camera(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        token = args[0] if args else kwargs.get("token")
        camera_id = args[1] if len(args) > 1 else kwargs.get("camera_id")
        return verify_stream_token(token, camera_id=camera_id)

    def validate(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return verify_stream_token(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_stream_token(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        include_revoked = True
        if args and isinstance(args[0], bool):
            include_revoked = args[0]
        if "include_revoked" in kwargs:
            include_revoked = bool(kwargs.pop("include_revoked"))
        return list_stream_tokens(include_revoked=include_revoked, **kwargs)

    def revoke(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return revoke_stream_token(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return delete_stream_token(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    def count(self) -> int:
        return count()

    def get_status(self) -> Dict[str, Any]:
        return get_status()


stream_tokens_service = StreamTokensService()
stream_token_service = stream_tokens_service
service = stream_tokens_service
manager = stream_tokens_service

StreamTokenService = StreamTokensService
StreamTokensManager = StreamTokensService
Service = StreamTokensService
Manager = StreamTokensService


def create(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_stream_token(*args, **kwargs)


def issue(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_stream_token(*args, **kwargs)


def generate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_stream_token(*args, **kwargs)


def mint(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return create_stream_token(*args, **kwargs)


def verify(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return verify_stream_token(*args, **kwargs)


def validate(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return verify_stream_token(*args, **kwargs)


def check(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return verify_stream_token(*args, **kwargs)


def get(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_stream_token(*args, **kwargs)


def fetch(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_stream_token(*args, **kwargs)


def lookup(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_stream_token(*args, **kwargs)


def list_tokens(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return stream_tokens_service.list(*args, **kwargs)


def revoke(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return revoke_stream_token(*args, **kwargs)


def remove(*args: Any, **kwargs: Any) -> bool:
    return delete_stream_token(*args, **kwargs)


def delete(*args: Any, **kwargs: Any) -> bool:
    return delete_stream_token(*args, **kwargs)


def reset(*args: Any, **kwargs: Any) -> int:
    return clear(*args, **kwargs)


__all__ = [
    "STREAM_TOKENS",
    "StreamTokensService",
    "StreamTokenRecord",
    "stream_tokens_service",
    "create_stream_token",
    "verify_stream_token",
    "verify_for_camera",
    "is_token_valid_for_camera",
    "get_stream_token",
    "list_stream_tokens",
    "revoke_stream_token",
    "delete_stream_token",
    "build_stream_url",
    "clear",
    "count",
    "get_status",
]
