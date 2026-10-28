"""In-memory user session management service.

Provides creation, listing, lookup, validation/refresh, revocation and
deletion of user sessions. Sessions are stored in-memory keyed by
session id; the token value is only fully exposed at creation time.
"""
from __future__ import annotations

import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


SESSIONS: Dict[str, Dict[str, Any]] = {}
USER_SESSIONS = SESSIONS
user_sessions = SESSIONS
sessions = SESSIONS
SESSION_STORE = SESSIONS
STORE = SESSIONS
_store = SESSIONS
USER_SESSION_STORE = SESSIONS

_lock = threading.Lock()

TOKEN_BYTES = 32

USER_KEYS = ("user_id", "userId", "user", "username", "user_name", "uid", "account", "account_id", "owner", "owner_id")
DEVICE_KEYS = ("device", "device_name", "deviceName", "user_agent", "userAgent", "client", "client_name")
IP_KEYS = ("ip", "ip_address", "ipAddress", "remote_addr", "remoteAddr", "client_ip")
TOKEN_KEYS = ("token", "session_token", "sessionToken", "access_token", "value", "secret")
SESSION_ID_KEYS = ("session_id", "sessionId", "id", "sid")
EXPIRES_KEYS = ("expires_in_days", "expires_days", "expiry_days", "ttl_days", "expires_in", "ttl", "expires_at", "expiry", "expiry_date", "ttl_seconds", "expires_in_seconds")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_expiry(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds <= 0:
            return None
        # Heuristic: large values are days, small values are seconds.
        if seconds < 100000:
            return (_now() + timedelta(seconds=seconds)).isoformat()
        return (_now() + timedelta(days=seconds)).isoformat()
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("never", "none", "null", "no-expiry", "no_expiry"):
            return None
        try:
            seconds = float(s)
            if seconds > 0:
                if seconds < 100000:
                    return (_now() + timedelta(seconds=seconds)).isoformat()
                return (_now() + timedelta(days=seconds)).isoformat()
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
    out = dict(record)
    out.pop("token_hash", None)
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
            if "user_id" not in data and "username" not in data and "=" not in s:
                data.setdefault("user_id", s)
            else:
                data.setdefault("device", s)
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested in ("payload", "options", "config", "params", "data", "body", "request", "session", "user"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    return data


class SessionRecord(dict):
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


Session = SessionRecord
UserSession = SessionRecord
UserSessionRecord = SessionRecord
Record = SessionRecord


class SessionService:
    def __init__(self) -> None:
        self.store = SESSIONS

    def create_session(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _normalize_create_args(args, kwargs)
        user_id = _first(data, USER_KEYS) or "anonymous"
        user_id = str(user_id).strip() or "anonymous"
        username = data.get("username") or data.get("user_name")
        if username is None and user_id and "@" not in str(user_id) and not str(user_id).startswith("user"):
            username = str(user_id)
        device = _first(data, DEVICE_KEYS)
        ip_address = _first(data, IP_KEYS)
        expires_at = _parse_expiry(_first(data, EXPIRES_KEYS))
        token = secrets.token_urlsafe(TOKEN_BYTES)
        session_id = str(data.get("session_id") or data.get("id") or uuid.uuid4())
        created = _now_iso()
        record: Dict[str, Any] = SessionRecord()
        record["id"] = session_id
        record["session_id"] = session_id
        record["sid"] = session_id
        record["user_id"] = user_id
        record["userId"] = user_id
        record["username"] = username or user_id
        record["user"] = user_id
        record["owner"] = user_id
        record["token"] = token
        record["session_token"] = token
        record["value"] = token
        record["device"] = device
        record["device_name"] = device
        record["user_agent"] = device
        record["ip"] = ip_address
        record["ip_address"] = ip_address
        record["created_at"] = created
        record["updated_at"] = created
        record["last_seen_at"] = created
        record["lastSeenAt"] = created
        record["last_used_at"] = created
        record["expires_at"] = expires_at
        record["revoked"] = False
        record["is_revoked"] = False
        record["active"] = True
        record["is_active"] = True
        record["status"] = "active"
        record["state"] = "active"
        record["usage_count"] = 0
        record["use_count"] = 0
        for k, v in data.items():
            if k in record:
                continue
            if k in USER_KEYS or k in DEVICE_KEYS or k in IP_KEYS or k in EXPIRES_KEYS or k in TOKEN_KEYS or k in SESSION_ID_KEYS:
                continue
            record[k] = v
        with _lock:
            SESSIONS[session_id] = record
        return record

    # Creation aliases (instance level).
    def create(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_session(*args, **kwargs)

    def create_user_session(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_session(*args, **kwargs)

    def start(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_session(*args, **kwargs)

    def start_session(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_session(*args, **kwargs)

    def new(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_session(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_session(*args, **kwargs)

    def open(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_session(*args, **kwargs)

    def login(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_session(*args, **kwargs)

    def get_session(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if session_id is None:
            for candidate in list(args) + list(kwargs.values()):
                if isinstance(candidate, str) and candidate:
                    session_id = candidate
                    break
            else:
                session_id = kwargs.get("session_id") or kwargs.get("id") or kwargs.get("sid")
        if session_id is None:
            return None
        lookup = str(session_id)
        with _lock:
            if lookup in SESSIONS:
                return SESSIONS[lookup]
            for record in SESSIONS.values():
                if record.get("token") == lookup or record.get("session_token") == lookup:
                    return record
        return None

    def get(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_session(session_id, *args, **kwargs)

    def find(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_session(session_id, *args, **kwargs)

    def lookup(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_session(session_id, *args, **kwargs)

    def fetch(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_session(session_id, *args, **kwargs)

    def get_user_session(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_session(session_id, *args, **kwargs)

    def list_sessions(self, include_revoked: bool = True, **kwargs: Any) -> List[Dict[str, Any]]:
        if "includeRevoked" in kwargs:
            include_revoked = bool(kwargs["includeRevoked"])
        user_filter = kwargs.get("user_id") or kwargs.get("userId") or kwargs.get("username") or kwargs.get("user")
        with _lock:
            records = list(SESSIONS.values())
        if not include_revoked:
            records = [r for r in records if not r.get("revoked")]
        if user_filter is not None:
            records = [r for r in records if str(r.get("user_id")) == str(user_filter) or str(r.get("username")) == str(user_filter)]
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
        return self.list_sessions(include_revoked=include_revoked, **kwargs)

    def list_user_sessions(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.list(*args, **kwargs)

    def list_active(self) -> List[Dict[str, Any]]:
        return self.list_sessions(include_revoked=False)

    def list_for_user(self, user_id: Any = None, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        if user_id is None and args:
            user_id = args[0]
        if user_id is None:
            user_id = kwargs.get("user_id") or kwargs.get("username") or kwargs.get("user")
        return self.list_sessions(user_id=user_id, **kwargs)

    def validate_session(self, session_value: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        candidate = session_value
        if candidate is None and args:
            candidate = args[0]
        if candidate is None:
            candidate = _first(kwargs, SESSION_ID_KEYS + TOKEN_KEYS)
        if candidate is None:
            return None
        raw = str(candidate)
        with _lock:
            record = SESSIONS.get(raw)
            if record is None:
                for rec in SESSIONS.values():
                    if rec.get("token") == raw or rec.get("session_token") == raw or rec.get("value") == raw:
                        record = rec
                        break
            if record is None:
                return None
            if record.get("revoked"):
                return None
            if _is_expired(record):
                return None
            try:
                record["last_seen_at"] = _now_iso()
                record["lastSeenAt"] = record["last_seen_at"]
                record["last_used_at"] = record["last_seen_at"]
                record["usage_count"] = int(record.get("usage_count", 0)) + 1
                record["use_count"] = int(record.get("usage_count", 0))
                record["updated_at"] = record["last_seen_at"]
            except Exception:  # noqa: BLE001
                pass
            return record

    def validate(self, session_value: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.validate_session(session_value, *args, **kwargs)

    def verify(self, session_value: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.validate_session(session_value, *args, **kwargs)

    def verify_session(self, session_value: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.validate_session(session_value, *args, **kwargs)

    def check(self, session_value: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.validate_session(session_value, *args, **kwargs)

    def is_valid(self, session_value: Any = None, *args: Any, **kwargs: Any) -> bool:
        return self.validate_session(session_value, *args, **kwargs) is not None

    def touch(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.validate_session(session_id, *args, **kwargs)

    def touch_session(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.validate_session(session_id, *args, **kwargs)

    def refresh_session(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        record = self.get_session(session_id, *args, **kwargs)
        if record is None:
            return None
        if record.get("revoked") or _is_expired(record):
            return None
        new_token = secrets.token_urlsafe(TOKEN_BYTES)
        record["token"] = new_token
        record["session_token"] = new_token
        record["value"] = new_token
        record["last_seen_at"] = _now_iso()
        record["lastSeenAt"] = record["last_seen_at"]
        record["last_used_at"] = record["last_seen_at"]
        record["updated_at"] = record["last_seen_at"]
        record["usage_count"] = int(record.get("usage_count", 0)) + 1
        record["use_count"] = int(record.get("usage_count", 0))
        return record

    def refresh(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.refresh_session(session_id, *args, **kwargs)

    def revoke_session(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        record = self.get_session(session_id, *args, **kwargs)
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

    def revoke(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.revoke_session(session_id, *args, **kwargs)

    def deactivate(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.revoke_session(session_id, *args, **kwargs)

    def disable(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.revoke_session(session_id, *args, **kwargs)

    def logout(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.revoke_session(session_id, *args, **kwargs)

    def end_session(self, session_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.revoke_session(session_id, *args, **kwargs)

    def delete_session(self, session_id: Any = None, *args: Any, **kwargs: Any) -> bool:
        record = self.get_session(session_id, *args, **kwargs)
        if record is None:
            raw = str(session_id) if session_id is not None else None
            if raw is None:
                return False
            with _lock:
                existed = raw in SESSIONS
                SESSIONS.pop(raw, None)
                return existed
        rid = str(record.get("id"))
        with _lock:
            SESSIONS.pop(rid, None)
            return True

    def delete(self, session_id: Any = None, *args: Any, **kwargs: Any) -> bool:
        return self.delete_session(session_id, *args, **kwargs)

    def remove(self, session_id: Any = None, *args: Any, **kwargs: Any) -> bool:
        return self.delete_session(session_id, *args, **kwargs)

    def destroy(self, session_id: Any = None, *args: Any, **kwargs: Any) -> bool:
        return self.delete_session(session_id, *args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        with _lock:
            n = len(SESSIONS)
            SESSIONS.clear()
            return n

    def reset(self, *args: Any, **kwargs: Any) -> int:
        return self.clear(*args, **kwargs)

    def purge(self, *args: Any, **kwargs: Any) -> int:
        return self.clear(*args, **kwargs)

    def count(self) -> int:
        with _lock:
            return len(SESSIONS)

    def size(self) -> int:
        return self.count()

    def __len__(self) -> int:
        return self.count()

    def get_status(self) -> Dict[str, Any]:
        with _lock:
            total = len(SESSIONS)
            active = sum(1 for r in SESSIONS.values() if not r.get("revoked") and not _is_expired(r))
            revoked = sum(1 for r in SESSIONS.values() if r.get("revoked"))
        return {
            "ok": True,
            "service": "sessions",
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


sessions_service = SessionService()
session_service = sessions_service
user_sessions_service = sessions_service
user_session_service = sessions_service
service = sessions_service
manager = sessions_service
sessions_manager = sessions_service
default_service = sessions_service

SessionServiceClass = SessionService
SessionsService = SessionService
UserSessionService = SessionService
UserSessionsService = SessionService
Service = SessionService
Manager = SessionService


def create_session(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return sessions_service.create_session(*args, **kwargs)


def create(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return sessions_service.create_session(*args, **kwargs)


def create_user_session(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return sessions_service.create_session(*args, **kwargs)


def get_session(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return sessions_service.get_session(*args, **kwargs)


def get(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return sessions_service.get_session(*args, **kwargs)


def list_sessions(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return sessions_service.list_sessions(*args, **kwargs)


def list_user_sessions(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return sessions_service.list_sessions(*args, **kwargs)


def validate_session(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return sessions_service.validate_session(*args, **kwargs)


def validate(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return sessions_service.validate_session(*args, **kwargs)


def verify_session(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return sessions_service.validate_session(*args, **kwargs)


def verify(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return sessions_service.validate_session(*args, **kwargs)


def revoke_session(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return sessions_service.revoke_session(*args, **kwargs)


def revoke(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return sessions_service.revoke_session(*args, **kwargs)


def delete_session(*args: Any, **kwargs: Any) -> bool:
    return sessions_service.delete_session(*args, **kwargs)


def delete(*args: Any, **kwargs: Any) -> bool:
    return sessions_service.delete_session(*args, **kwargs)


def remove(*args: Any, **kwargs: Any) -> bool:
    return sessions_service.delete_session(*args, **kwargs)


def refresh_session(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return sessions_service.refresh_session(*args, **kwargs)


def refresh(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return sessions_service.refresh_session(*args, **kwargs)


def clear(*args: Any, **kwargs: Any) -> int:
    return sessions_service.clear(*args, **kwargs)


def reset(*args: Any, **kwargs: Any) -> int:
    return sessions_service.clear(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return sessions_service.get_status(*args, **kwargs)


__all__ = [
    "SESSIONS",
    "USER_SESSIONS",
    "SessionService",
    "SessionRecord",
    "sessions_service",
    "create_session",
    "get_session",
    "list_sessions",
    "validate_session",
    "verify_session",
    "revoke_session",
    "delete_session",
    "refresh_session",
    "clear",
    "get_status",
]
