"""Admin broadcast / announcement banner service for VigilCam.

Single global banner shown at the top of the UI (e.g. maintenance notice,
outage warning, policy message). In-memory, thread-safe, dependency-free.

How this differs from neighbouring "broadcast" concepts (do NOT merge):

* ``app.core.discovery.start_broadcast`` is mDNS/ZeroConf network
  advertisement (``_vigilcam._tcp``). It advertises the server on the LAN.
* ``app.services.web_push.broadcast`` / ``send_web_push`` delivers push
  notifications to subscribed browsers/devices.
* ``app.services.talk.broadcast`` (a.k.a. ``play`` / ``queue_talk``) sends
  audio to camera speakers (two-way talk).
* ``app.services.event_bus.broadcast`` fans out live WS/queue events.
* ``app.services.node_dispatch.broadcast`` forwards work across nodes.
* ``app.api.agent_compat`` ``GET /command/broadcast`` is an Agent DVR
  compatibility shim that publishes an event-bus message.

This module is none of those: it stores one admin-authored announcement
banner (message + level + enabled) served via ``app.api.announce`` and
rendered by the frontend. No ``app/main.py`` or template edits required
(the API auto-loader in ``app/main.py`` picks up the router).
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_lock = threading.RLock()

_BANNER: Optional[Dict[str, Any]] = None

DEFAULT_LEVEL = "info"

MESSAGE_KEYS = (
    "message",
    "text",
    "body",
    "content",
    "announcement",
    "banner",
    "title",
    "msg",
    "notice",
    "description",
)

LEVEL_KEYS = (
    "level",
    "severity",
    "kind",
    "type",
    "priority",
    "tone",
    "style",
    "variant",
)

ENABLED_KEYS = (
    "enabled",
    "active",
    "is_enabled",
    "is_active",
    "on",
    "visible",
    "show",
    "display",
)

AUTHOR_KEYS = (
    "author",
    "created_by",
    "createdBy",
    "by",
    "user",
    "admin",
    "username",
    "user_name",
    "owner",
)

EXPIRES_KEYS = (
    "expires_at",
    "expiresAt",
    "expires",
    "expiry",
    "until",
    "valid_until",
    "validUntil",
    "ends_at",
)

ID_KEYS = ("id", "banner_id", "announcement_id", "uuid", "key")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_ts(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, (int, float)):
        v = float(value)
        if abs(v) > 1e12:  # millis
            v = v / 1000.0
        return v
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            v = float(s)
            if abs(v) > 1e12:
                v = v / 1000.0
            return v
        except ValueError:
            pass
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _to_iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        lk = str(k).lower()
        if lk in lower and lower[lk] is not None:
            return lower[lk]
    return None


def _to_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("false", "0", "no", "off", "disabled", "hidden", "hide", "inactive"):
            return False
        if s in ("true", "1", "yes", "on", "enabled", "visible", "show", "active"):
            return True
        return bool(s)
    return bool(value)


def _normalize_level(value: Any) -> str:
    if value is None:
        return DEFAULT_LEVEL
    if isinstance(value, bool):
        return DEFAULT_LEVEL
    s = str(value).strip().lower()
    if not s:
        return DEFAULT_LEVEL
    if s == "warn":
        return "warning"
    return s


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (bytes, bytearray)):
            try:
                s = bytes(a).decode("utf-8", errors="replace").strip()
            except Exception:  # noqa: BLE001
                continue
            if s and data.get("message") is None:
                data["message"] = s
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if data.get("message") is None and data.get("text") is None:
                data["message"] = s
            elif _first(data, LEVEL_KEYS) is None and s.lower() in (
                "info", "notice", "success", "warning", "warn",
                "danger", "error", "critical", "alert", "primary", "secondary",
            ):
                data["level"] = s
            elif _first(data, AUTHOR_KEYS) is None:
                # Second free string is most likely an author, not a message.
                # Keep first string as the message.
                data.setdefault("author", s)
            else:
                data.setdefault("note", s)
        elif hasattr(a, "model_dump"):
            try:
                data.update(dict(a.model_dump()))
            except Exception:  # noqa: BLE001
                pass
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
    for nested_key in (
        "payload", "options", "config", "params", "data", "body",
        "request", "settings", "announcement", "banner", "notice",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _banner_to_dict(b: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(b)


def _is_expired(banner: Dict[str, Any], now_ts: Optional[float] = None) -> bool:
    exp = banner.get("expires_at") or banner.get("expiresAt")
    if exp is None:
        return False
    # Stored as ISO string; also accept epoch.
    ts = _to_ts(exp)
    if ts is None:
        return False
    now = now_ts if now_ts is not None else time.time()
    return now >= ts


# ---------------------------------------------------------------------------
# Core public API
# ---------------------------------------------------------------------------

def set_announcement(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Set (create or replace) the global announcement banner.

    Accepts flexible spellings: ``set_announcement("hello")``,
    ``set_announcement(message="hello")``, ``set_announcement(text="hello")``,
    or a dict payload. Optional ``level`` (default ``"info"``),
    ``enabled`` (default True), ``author``, ``expires_at``.
    """
    data = _normalize_inputs(*args, **kwargs)
    message = _first(data, MESSAGE_KEYS)
    if message is None or (isinstance(message, str) and not message.strip()):
        return {
            "ok": False,
            "success": False,
            "error": "message is required",
        }
    message = str(message).strip()
    level = _normalize_level(_first(data, LEVEL_KEYS))
    enabled_raw = _first(data, ENABLED_KEYS)
    enabled = _to_bool(enabled_raw, True) if enabled_raw is not None else True
    author = _first(data, AUTHOR_KEYS)
    author = str(author).strip() if author is not None and str(author).strip() else None
    expires_raw = _first(data, EXPIRES_KEYS)
    expires_ts = _to_ts(expires_raw) if expires_raw is not None else None
    expires_iso = _to_iso(expires_ts) if expires_ts is not None else None

    now_iso = _now_iso()
    with _lock:
        existing = _BANNER
        bid_raw = _first(data, ID_KEYS)
        if existing is not None and (bid_raw is None or not str(bid_raw).strip()):
            bid = str(existing.get("id") or uuid.uuid4())
            created_at = existing.get("created_at") or now_iso
        elif bid_raw is not None and str(bid_raw).strip():
            bid = str(bid_raw).strip()
            created_at = (existing.get("created_at") if existing and existing.get("id") == bid else now_iso)
        else:
            bid = str(uuid.uuid4())
            created_at = now_iso
        banner = {
            "id": bid,
            "banner_id": bid,
            "announcement_id": bid,
            "message": message,
            "text": message,
            "content": message,
            "announcement": message,
            "banner": message,
            "level": level,
            "severity": level,
            "kind": level,
            "enabled": bool(enabled),
            "active": bool(enabled),
            "visible": bool(enabled),
            "author": author,
            "created_by": author,
            "expires_at": expires_iso,
            "created_at": created_at,
            "updated_at": now_iso,
            "ok": True,
            "success": True,
        }
        globals()["_BANNER"] = banner
        return _banner_to_dict(banner)


def update_announcement(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Partially update the banner; creates it if none exists and message given."""
    data = _normalize_inputs(*args, **kwargs)
    with _lock:
        existing = copy.deepcopy(_BANNER) if _BANNER is not None else None
    if existing is None:
        return set_announcement(**data)
    patch: Dict[str, Any] = {}
    msg = _first(data, MESSAGE_KEYS)
    if msg is not None and str(msg).strip():
        patch["message"] = str(msg).strip()
    lvl = _first(data, LEVEL_KEYS)
    if lvl is not None and str(lvl).strip():
        patch["level"] = _normalize_level(lvl)
    en = _first(data, ENABLED_KEYS)
    if en is not None:
        patch["enabled"] = _to_bool(en, True)
    auth = _first(data, AUTHOR_KEYS)
    if auth is not None:
        patch["author"] = str(auth).strip() or None
    exp = _first(data, EXPIRES_KEYS)
    if exp is not None:
        ts = _to_ts(exp)
        patch["expires_at"] = _to_iso(ts) if ts is not None else None
    if not patch:
        return _banner_to_dict(existing)
    merged = dict(existing)
    merged.update(patch)
    # Re-normalize through set path to keep aliases consistent.
    merged.pop("ok", None)
    merged.pop("success", None)
    return set_announcement(**merged)


def get_announcement(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return the current banner dict copy, or None when no banner is set."""
    with _lock:
        if _BANNER is None:
            return None
        return _banner_to_dict(_BANNER)


def list_announcements(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return the banner as a 0/1-length list (for uniform API shapes)."""
    b = get_announcement()
    return [b] if b is not None else []


def clear_announcement(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Dismiss the banner. Idempotent."""
    with _lock:
        existed = _BANNER is not None
        globals()["_BANNER"] = None
    return {
        "ok": True,
        "success": True,
        "cleared": bool(existed),
        "deleted": bool(existed),
        "existed": bool(existed),
        "count": 1 if existed else 0,
    }


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Alias for :func:`clear_announcement`."""
    return clear_announcement(*args, **kwargs)


def is_active(*args: Any, **kwargs: Any) -> bool:
    """Return True when a visible (enabled, unexpired) banner exists."""
    data = _normalize_inputs(*args, **kwargs)
    now_raw = data.get("now", data.get("at", data.get("when")))
    now_ts = _to_ts(now_raw) if now_raw is not None else time.time()
    with _lock:
        b = _BANNER
        if b is None:
            return False
        if not b.get("enabled", True):
            return False
        snapshot = copy.deepcopy(b)
    return not _is_expired(snapshot, now_ts)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Structured status for the banner (for polling by the UI)."""
    data = _normalize_inputs(*args, **kwargs)
    now_raw = data.get("now", data.get("at", data.get("when")))
    now_ts = _to_ts(now_raw) if now_raw is not None else time.time()
    with _lock:
        b = copy.deepcopy(_BANNER) if _BANNER is not None else None
    if b is None:
        return {
            "ok": True,
            "success": True,
            "active": False,
            "is_active": False,
            "enabled": False,
            "announcement": None,
            "banner": None,
            "message": None,
            "text": None,
            "level": None,
        }
    expired = _is_expired(b, now_ts)
    active = bool(b.get("enabled", True)) and not expired
    return {
        "ok": True,
        "success": True,
        "active": bool(active),
        "is_active": bool(active),
        "enabled": bool(b.get("enabled", True)),
        "expired": bool(expired),
        "announcement": _banner_to_dict(b),
        "banner": _banner_to_dict(b),
        "current": _banner_to_dict(b),
        "message": b.get("message"),
        "text": b.get("message"),
        "level": b.get("level"),
        "severity": b.get("level"),
        "id": b.get("id"),
        "updated_at": b.get("updated_at"),
    }


def set_enabled(enabled: bool = True, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Enable/disable the current banner."""
    with _lock:
        if _BANNER is None:
            return {"ok": False, "success": False, "error": "no announcement is set"}
        current = copy.deepcopy(_BANNER)
    current["enabled"] = _to_bool(enabled, True)
    return set_announcement(**current)


def enable(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    # Allow enable(message=...) to create+enable in one call.
    if _BANNER is None and _first(data, MESSAGE_KEYS) is not None:
        data["enabled"] = True
        return set_announcement(**data)
    return set_enabled(True)


def disable(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    if _BANNER is None and _first(data, MESSAGE_KEYS) is not None:
        data["enabled"] = False
        return set_announcement(**data)
    return set_enabled(False)


# ---------------------------------------------------------------------------
# Service class (singleton mirrors module functions)
# ---------------------------------------------------------------------------

class AnnounceService:
    def set_announcement(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_announcement(*args, **kwargs)

    def update_announcement(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return update_announcement(*args, **kwargs)

    def get_announcement(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_announcement(*args, **kwargs)

    def list_announcements(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_announcements(*args, **kwargs)

    def clear_announcement(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return clear_announcement(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return clear(*args, **kwargs)

    def is_active(self, *args: Any, **kwargs: Any) -> bool:
        return is_active(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)

    def set_enabled(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_enabled(*args, **kwargs)

    def enable(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return enable(*args, **kwargs)

    def disable(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return disable(*args, **kwargs)

    # aliases
    def set(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_announcement(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_announcement(*args, **kwargs)

    def publish(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_announcement(*args, **kwargs)

    def broadcast(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_announcement(*args, **kwargs)

    def announce(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_announcement(*args, **kwargs)

    def dismiss(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return clear_announcement(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)


Announce = AnnounceService
AnnouncementService = AnnounceService
BannerService = AnnounceService

announce_service = AnnounceService()
announcement_service = announce_service
banner_service = announce_service
service = announce_service
manager = announce_service
default_service = announce_service


def get_service() -> AnnounceService:
    return announce_service


def create_service() -> AnnounceService:
    return AnnounceService()


# Module-level aliases for flexible callers
set_banner = set_announcement
set = set_announcement
create = set_announcement
create_announcement = set_announcement
publish = set_announcement
publish_announcement = set_announcement
broadcast = set_announcement
broadcast_banner = set_announcement
announce = set_announcement
post = set_announcement
save = set_announcement
update = update_announcement
patch = update_announcement
edit = update_announcement
get_banner = get_announcement
get = get_announcement
fetch = get_announcement
fetch_announcement = get_announcement
read = get_announcement
current = get_announcement
list_all = list_announcements
all_announcements = list_announcements
clear_banner = clear_announcement
dismiss = clear_announcement
remove = clear_announcement
delete = clear_announcement
delete_announcement = clear_announcement
reset = clear_announcement
clear_all = clear_announcement
status = get_status
check = get_status


__all__ = [
    "DEFAULT_LEVEL",
    "set_announcement",
    "update_announcement",
    "get_announcement",
    "list_announcements",
    "clear_announcement",
    "clear",
    "is_active",
    "get_status",
    "set_enabled",
    "enable",
    "disable",
    "AnnounceService",
    "announce_service",
    "get_service",
    "create_service",
]
