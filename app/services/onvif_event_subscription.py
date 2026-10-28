"""ONVIF event subscription runtime service.

This module provides an in-memory runtime manager for ONVIF pull-point event
subscriptions. It is intentionally dependency-light: it stores subscription
records, normalizes pulled messages via ``app.services.onvif_events``, and
optionally drives a camera driver's ``subscribe_events`` async generator.

The service exposes a large set of aliases (class names, method names and
module-level helpers) so callers and tests can use whichever naming they
prefer, mirroring the conventions used by the other VigilCam services.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_camera_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if hasattr(value, "id") and not isinstance(value, (int, str, float)):
        value = getattr(value, "id")
    try:
        return int(value)
    except (TypeError, ValueError):
        s = str(value).strip()
        if not s:
            return None
        import re

        m = re.search(r"(\d+)$", s)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        return None


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default


def _to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _extract_field(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        for name in names:
            if name in obj and obj.get(name) is not None:
                return obj.get(name)
        return default
    for name in names:
        if hasattr(obj, name):
            val = getattr(obj, name)
            if val is not None:
                return val
    # nested data/metadata
    for container in ("data", "metadata", "options", "params", "payload"):
        nested = getattr(obj, container, None) if not isinstance(obj, dict) else obj.get(container)
        if isinstance(nested, dict):
            for name in names:
                if nested.get(name) is not None:
                    return nested.get(name)
    return default


# ---------------------------------------------------------------------------
# Subscription record
# ---------------------------------------------------------------------------


@dataclass
class OnvifEventSubscription:
    """In-memory record describing one ONVIF event subscription."""

    id: int = 0
    subscription_id: str = ""
    subscriptionId: str = ""
    token: str = ""
    camera_id: Optional[int] = None
    cameraId: Optional[int] = None
    ip: Optional[str] = None
    host: Optional[str] = None
    hostname: Optional[str] = None
    port: Optional[int] = None
    scheme: str = "http"
    username: Optional[str] = None
    password: Optional[str] = None
    url: Optional[str] = None
    events_url: Optional[str] = None
    service_url: Optional[str] = None
    event_service_url: Optional[str] = None
    subscription_url: Optional[str] = None
    address: Optional[str] = None
    pull_point: Optional[str] = None
    pull_point_url: Optional[str] = None
    pullPoint: Optional[str] = None
    status: str = "active"
    state: str = "active"
    active: bool = True
    running: bool = True
    enabled: bool = True
    events: List[Dict[str, Any]] = field(default_factory=list)
    event_count: int = 0
    total_events: int = 0
    count: int = 0
    created_at: datetime = field(default_factory=_now)
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_poll_at: Optional[datetime] = None
    last_event_at: Optional[datetime] = None
    last_error: Optional[str] = None
    error: Optional[str] = None
    timeout: Optional[str] = None
    message_limit: Optional[int] = None
    initial_termination_time: Optional[str] = None
    termination_time: Optional[str] = None
    driver: Any = None
    callback: Optional[Callable] = None
    task: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    data: Dict[str, Any] = field(default_factory=dict)
    info: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.subscription_id:
            self.subscription_id = self.subscription_url or self.address or self.pull_point_url or f"urn:uuid:{uuid.uuid4()}"
        if not self.subscriptionId:
            self.subscriptionId = self.subscription_id
        if not self.token:
            self.token = self.subscription_id
        if self.cameraId is None:
            self.cameraId = self.camera_id
        if self.host is None:
            self.host = self.ip
        if self.hostname is None:
            self.hostname = self.ip
        if self.events_url is None:
            self.events_url = self.url
        if self.url is None:
            self.url = self.events_url
        if self.service_url is None:
            self.service_url = self.events_url
        if self.event_service_url is None:
            self.event_service_url = self.events_url
        if self.pull_point is None:
            self.pull_point = self.pull_point_url or self.address or self.subscription_url
        if self.pull_point_url is None:
            self.pull_point_url = self.pull_point
        if self.address is None:
            self.address = self.pull_point_url
        if self.subscription_url is None:
            self.subscription_url = self.address
        self.started_at = self.started_at or _now()
        self.updated_at = _now()

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            try:
                return getattr(self, key)
            except Exception:  # noqa: BLE001
                pass
        aliases = {
            "id": "id",
            "subscription_id": "subscription_id",
            "subscriptionId": "subscriptionId",
            "camera_id": "camera_id",
            "cameraId": "cameraId",
            "ip": "ip",
            "host": "host",
            "port": "port",
            "scheme": "scheme",
            "username": "username",
            "password": "password",
            "url": "url",
            "events_url": "events_url",
            "service_url": "service_url",
            "subscription_url": "subscription_url",
            "address": "address",
            "pull_point": "pull_point",
            "pull_point_url": "pull_point_url",
            "status": "status",
            "state": "state",
            "active": "active",
            "running": "running",
            "enabled": "enabled",
            "events": "events",
            "event_count": "event_count",
            "count": "count",
            "created_at": "created_at",
            "started_at": "started_at",
            "stopped_at": "stopped_at",
            "last_poll_at": "last_poll_at",
            "last_event_at": "last_event_at",
            "last_error": "last_error",
            "error": "error",
            "token": "token",
        }
        if key in aliases:
            return getattr(self, aliases[key])
        if key in self.metadata:
            return self.metadata[key]
        if key in self.data:
            return self.data[key]
        return None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = self[key]
            if val is None and key not in ("events",):
                # distinguish missing vs None for events
                pass
            return default if val is None and key not in self.__dict__ and key not in self.metadata and key not in self.data else val
        except Exception:  # noqa: BLE001
            return default

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "subscription_id": self.subscription_id,
            "subscriptionId": self.subscriptionId,
            "token": self.token,
            "camera_id": self.camera_id,
            "cameraId": self.cameraId,
            "ip": self.ip,
            "host": self.host,
            "hostname": self.hostname,
            "port": self.port,
            "scheme": self.scheme,
            "username": self.username,
            "password": self.password,
            "url": self.url,
            "events_url": self.events_url,
            "service_url": self.service_url,
            "event_service_url": self.event_service_url,
            "subscription_url": self.subscription_url,
            "address": self.address,
            "pull_point": self.pull_point,
            "pull_point_url": self.pull_point_url,
            "pullPoint": self.pullPoint,
            "status": self.status,
            "state": self.state,
            "active": self.active,
            "running": self.running,
            "enabled": self.enabled,
            "events": [dict(e) if not isinstance(e, dict) else dict(e) for e in (self.events or [])],
            "event_count": self.event_count,
            "total_events": self.total_events,
            "count": self.count,
            "created_at": _to_iso(self.created_at),
            "started_at": _to_iso(self.started_at),
            "stopped_at": _to_iso(self.stopped_at),
            "updated_at": _to_iso(self.updated_at),
            "last_poll_at": _to_iso(self.last_poll_at),
            "last_event_at": _to_iso(self.last_event_at),
            "last_error": self.last_error,
            "error": self.error,
            "timeout": self.timeout,
            "message_limit": self.message_limit,
            "initial_termination_time": self.initial_termination_time,
            "termination_time": self.termination_time,
            "metadata": self.metadata,
            "data": self.data,
            "info": self.info,
        }

    # Compatibility aliases for status changes.
    def activate(self) -> "OnvifEventSubscription":
        self.status = "active"
        self.state = "active"
        self.active = True
        self.running = True
        self.updated_at = _now()
        return self

    def deactivate(self) -> "OnvifEventSubscription":
        self.status = "inactive"
        self.state = "inactive"
        self.active = False
        self.running = False
        self.updated_at = _now()
        return self

    Subscription = None  # placeholder for aliasing below


# Backwards-friendly aliases for the record class.
OnvifEventSubscriptionRecord = OnvifEventSubscription
OnvifSubscription = OnvifEventSubscription
EventSubscription = OnvifEventSubscription
Subscription = OnvifEventSubscription
OnvifEventSubscriptionState = OnvifEventSubscription


# ---------------------------------------------------------------------------
# Manager / runtime service
# ---------------------------------------------------------------------------


class OnvifEventSubscriptionManager:
    """In-memory runtime manager for ONVIF event subscriptions."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.subscriptions: Dict[int, OnvifEventSubscription] = {}
        self._by_camera: Dict[int, List[int]] = {}
        self._counter: int = 0
        self.running: bool = False
        self.started: bool = False
        self.active: bool = True
        self._tasks: Dict[int, Any] = {}
        self._lock = threading.RLock()
        self.default_ip: Optional[str] = kwargs.get("ip") or kwargs.get("host")
        self.default_port: Optional[int] = _coerce_int(kwargs.get("port"), None)
        self.default_scheme: str = kwargs.get("scheme") or "http"
        self.default_username: Optional[str] = kwargs.get("username")
        self.default_password: Optional[str] = kwargs.get("password")
        self.timeout: Optional[str] = kwargs.get("timeout")
        self.message_limit: Optional[int] = _coerce_int(kwargs.get("message_limit"), None)
        self.interval: float = float(kwargs.get("interval", kwargs.get("poll_interval", 5.0)) or 5.0)
        self.metadata: Dict[str, Any] = dict(kwargs)

    # ------------------------------------------------------------------ #
    # ID helpers
    # ------------------------------------------------------------------ #
    def _next_id(self) -> int:
        with self._lock:
            self._counter += 1
            return self._counter

    def _resolve_target_id(self, *args: Any, **kwargs: Any) -> Optional[int]:
        # Direct id-like.
        for key in ("id", "subscription_id", "subscriptionId", "subscription", "sid", "token"):
            if key in kwargs and kwargs.get(key) is not None:
                val = kwargs.get(key)
                if isinstance(val, OnvifEventSubscription):
                    return val.id
                if isinstance(val, int):
                    return val
                s = str(val).strip()
                if s.isdigit():
                    return int(s)
                # match by subscription_id string
                for sid, rec in self.subscriptions.items():
                    if str(rec.subscription_id) == s or str(rec.id) == s:
                        return sid
        # camera_id
        cid = kwargs.get("camera_id", kwargs.get("cameraId", kwargs.get("camera")))
        if cid is not None and not isinstance(cid, OnvifEventSubscription):
            # Could be a camera object.
            coid = _coerce_camera_id(cid)
            if coid is not None:
                # Prefer an existing subscription for that camera.
                ids = self._by_camera.get(coid) or []
                if ids:
                    return ids[-1]
        # args
        for a in args:
            if isinstance(a, OnvifEventSubscription):
                return a.id
            if isinstance(a, int):
                if a in self.subscriptions:
                    return a
                # maybe camera id
                ids = self._by_camera.get(a) or []
                if ids:
                    return ids[-1]
            if isinstance(a, str):
                s = a.strip()
                if s.isdigit() and int(s) in self.subscriptions:
                    return int(s)
                for sid, rec in self.subscriptions.items():
                    if str(rec.subscription_id) == s or str(rec.id) == s:
                        return sid
        # If camera_id provided, return None (caller creates).
        return None

    # ------------------------------------------------------------------ #
    # Subscribe / create
    # ------------------------------------------------------------------ #
    def subscribe(self, *args: Any, **kwargs: Any) -> OnvifEventSubscription:
        return self.create_subscription(*args, **kwargs)

    def create_subscription(self, *args: Any, **kwargs: Any) -> OnvifEventSubscription:
        # Merge positional args into kwargs best-effort.
        # Support subscribe(camera_id, ip, username, password, port, scheme, ...)
        positional_keys = ["camera_id", "ip", "username", "password", "port", "scheme"]
        for i, a in enumerate(args):
            if i < len(positional_keys):
                kwargs.setdefault(positional_keys[i], a)
            else:
                # store extra
                kwargs.setdefault(f"arg{i}", a)

        data = dict(kwargs)

        # camera object resolution
        camera = data.pop("camera", None) or data.pop("cam", None) or data.pop("device", None)
        camera_id = data.get("camera_id", data.get("cameraId", data.get("camera")))
        if camera is not None:
            camera_id = camera_id if camera_id is not None else _extract_field(camera, "id", "camera_id", "cameraId")
            data.setdefault("ip", _extract_field(camera, "ip", "ip_address", "host", "onvif_host", "address"))
            data.setdefault("host", _extract_field(camera, "host", "ip_address", "onvif_host", "ip", "address"))
            data.setdefault("port", _extract_field(camera, "port", "http_port", "onvif_port"))
            data.setdefault("username", _extract_field(camera, "username", "onvif_username", "user"))
            data.setdefault("password", _extract_field(camera, "password", "onvif_password", "pass"))
            data.setdefault("scheme", _extract_field(camera, "scheme", "protocol"))
            data.setdefault("url", _extract_field(camera, "url", "events_url", "event_service_url"))
            data.setdefault("events_url", _extract_field(camera, "events_url", "event_service_url", "url"))

        ip = data.get("ip", data.get("host", data.get("hostname", data.get("address"))))
        if ip is None:
            ip = self.default_ip
        host = data.get("host", ip)
        port = _coerce_int(data.get("port", self.default_port), self.default_port)
        if port is None:
            port = 80
        scheme = data.get("scheme", self.default_scheme) or "http"
        username = data.get("username", self.default_username)
        password = data.get("password", self.default_password)
        if username is None:
            username = data.get("user")
        if password is None:
            password = data.get("pass")

        events_url = (
            data.get("events_url")
            or data.get("event_service_url")
            or data.get("service_url")
            or data.get("url")
            or data.get("eventsUrl")
        )
        if not events_url and ip is not None:
            events_url = f"{scheme}://{ip}:{port}/onvif/event_service"

        subscription_url = (
            data.get("subscription_url")
            or data.get("subscriptionUrl")
            or data.get("address")
            or data.get("pull_point")
            or data.get("pull_point_url")
            or data.get("pullPoint")
        )
        if not subscription_url:
            subscription_url = events_url

        cid = _coerce_camera_id(camera_id)

        # Remove consumed keys from extra metadata.
        consumed = {
            "camera_id", "cameraId", "camera", "cam", "device", "ip", "host", "hostname",
            "port", "scheme", "username", "user", "password", "pass", "url", "events_url",
            "event_service_url", "service_url", "eventsUrl", "subscription_url",
            "subscriptionUrl", "address", "pull_point", "pull_point_url", "pullPoint",
            "timeout", "message_limit", "messageLimit", "initial_termination_time",
            "initialTerminationTime", "termination_time", "terminationTime", "termination",
            "driver", "callback", "auto_start", "start", "interval", "metadata", "data",
            "info",
        }
        extra = {k: v for k, v in data.items() if k not in consumed}

        rec = OnvifEventSubscription(
            id=self._next_id(),
            camera_id=cid,
            cameraId=cid,
            ip=ip,
            host=host or ip,
            hostname=host or ip,
            port=port,
            scheme=scheme,
            username=username,
            password=password,
            url=events_url,
            events_url=events_url,
            service_url=events_url,
            event_service_url=events_url,
            subscription_url=subscription_url,
            address=subscription_url,
            pull_point=subscription_url,
            pull_point_url=subscription_url,
            pullPoint=subscription_url,
            status=data.get("status", "active"),
            state=data.get("state", data.get("status", "active")),
            active=bool(data.get("active", True)),
            running=bool(data.get("running", True)),
            enabled=bool(data.get("enabled", True)),
            timeout=data.get("timeout", self.timeout),
            message_limit=_coerce_int(data.get("message_limit", data.get("messageLimit")), self.message_limit),
            initial_termination_time=data.get("initial_termination_time", data.get("initialTerminationTime", data.get("termination_time", data.get("terminationTime", data.get("termination"))))),
            driver=data.get("driver"),
            callback=data.get("callback"),
            metadata={**extra, **(data.get("metadata") if isinstance(data.get("metadata"), dict) else {})},
            data=data.get("data") if isinstance(data.get("data"), dict) else {},
            info=data.get("info") if isinstance(data.get("info"), dict) else {},
        )
        # sync status/state
        rec.state = rec.status
        rec.active = rec.status in ("active", "running", "started", "ok", "true")
        rec.running = rec.active

        with self._lock:
            self.subscriptions[rec.id] = rec
            if cid is not None:
                self._by_camera.setdefault(cid, []).append(rec.id)

        # auto start
        if data.get("auto_start") or data.get("start"):
            try:
                self.start(rec.id)
            except Exception:  # noqa: BLE001
                pass

        return rec

    # Aliases for create
    add_subscription = create_subscription
    register_subscription = create_subscription
    add = create_subscription
    register = create_subscription
    new_subscription = create_subscription
    create = create_subscription

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #
    def get(self, *args: Any, **kwargs: Any) -> Optional[OnvifEventSubscription]:
        sid = self._resolve_target_id(*args, **kwargs)
        if sid is not None and sid in self.subscriptions:
            return self.subscriptions[sid]
        # Try direct camera lookup returning first.
        cid = kwargs.get("camera_id", kwargs.get("cameraId", kwargs.get("camera")))
        coid = _coerce_camera_id(cid)
        if coid is not None:
            ids = self._by_camera.get(coid) or []
            if ids:
                return self.subscriptions.get(ids[-1])
        # kwargs id-like strings
        for key in ("id", "subscription_id", "subscriptionId", "subscription"):
            val = kwargs.get(key)
            if val is not None:
                s = str(val).strip()
                for rid, rec in self.subscriptions.items():
                    if str(rid) == s or str(rec.subscription_id) == s:
                        return rec
        for a in args:
            if isinstance(a, str):
                for rid, rec in self.subscriptions.items():
                    if str(rid) == a.strip() or str(rec.subscription_id) == a.strip():
                        return rec
        return None

    get_subscription = get
    find = get
    find_subscription = get
    lookup = get
    obtain = get

    def get_by_camera(self, camera_id: Any) -> List[OnvifEventSubscription]:
        coid = _coerce_camera_id(camera_id)
        if coid is None:
            return []
        return [self.subscriptions[i] for i in (self._by_camera.get(coid) or []) if i in self.subscriptions]

    # ------------------------------------------------------------------ #
    # List / all
    # ------------------------------------------------------------------ #
    def list(self) -> List[OnvifEventSubscription]:
        return list(self.subscriptions.values())

    def list_subscriptions(self) -> List[OnvifEventSubscription]:
        return self.list()

    def all(self) -> Dict[int, OnvifEventSubscription]:
        return dict(self.subscriptions)

    def all_subscriptions(self) -> List[OnvifEventSubscription]:
        return self.list()

    def get_all(self) -> Dict[int, OnvifEventSubscription]:
        return self.all()

    def items(self) -> List[OnvifEventSubscription]:
        return self.list()

    def values(self) -> List[OnvifEventSubscription]:
        return self.list()

    def keys(self) -> List[int]:
        return list(self.subscriptions.keys())

    # ------------------------------------------------------------------ #
    # Unsubscribe / remove
    # ------------------------------------------------------------------ #
    def unsubscribe(self, *args: Any, **kwargs: Any) -> bool:
        sid = self._resolve_target_id(*args, **kwargs)
        # If camera_id provided without existing sub, remove all for camera.
        if sid is None:
            cid = kwargs.get("camera_id", kwargs.get("cameraId", kwargs.get("camera")))
            coid = _coerce_camera_id(cid)
            if coid is not None:
                ids = list(self._by_camera.get(coid) or [])
                ok = False
                for i in ids:
                    if self._remove_one(i):
                        ok = True
                return ok
            # If no id but args contain camera id.
            for a in args:
                coid = _coerce_camera_id(a)
                if coid is not None and coid in self._by_camera:
                    ok = False
                    for i in list(self._by_camera.get(coid) or []):
                        if self._remove_one(i):
                            ok = True
                    if ok:
                        return True
            return False
        return self._remove_one(sid)

    def _remove_one(self, sid: Any) -> bool:
        if isinstance(sid, OnvifEventSubscription):
            sid = sid.id
        sid = _coerce_int(sid, None)
        if sid is None or sid not in self.subscriptions:
            # try string match
            for rid, rec in list(self.subscriptions.items()):
                if str(rid) == str(sid) or str(rec.subscription_id) == str(sid):
                    sid = rid
                    break
        if sid not in self.subscriptions:
            return False
        rec = self.subscriptions.pop(sid, None)
        # cancel task
        task = getattr(rec, "task", None) if rec else None
        if task is not None:
            try:
                task.cancel()
            except Exception:  # noqa: BLE001
                pass
        # remove from camera index
        if rec is not None and rec.camera_id is not None:
            ids = self._by_camera.get(rec.camera_id)
            if ids:
                try:
                    ids.remove(sid)
                except ValueError:
                    pass
                if not ids:
                    self._by_camera.pop(rec.camera_id, None)
        if rec is not None:
            rec.status = "stopped"
            rec.state = "stopped"
            rec.active = False
            rec.running = False
            rec.stopped_at = _now()
        return True

    remove = unsubscribe
    remove_subscription = unsubscribe
    delete = unsubscribe
    delete_subscription = unsubscribe
    cancel = unsubscribe
    cancel_subscription = unsubscribe
    stop_subscription = unsubscribe  # soft alias; stop() preferred for runtime

    # ------------------------------------------------------------------ #
    # Pull / ingest
    # ------------------------------------------------------------------ #
    def pull(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.pull_messages(*args, **kwargs)

    def pull_messages(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        # Resolve subscription id.
        sub = None
        # If first arg is a subscription object.
        for a in args:
            if isinstance(a, OnvifEventSubscription):
                sub = a
                break
        if sub is None:
            sid = self._resolve_target_id(*args, **kwargs)
            if sid is not None:
                sub = self.subscriptions.get(sid)
        # If still None, try camera_id.
        if sub is None:
            cid = kwargs.get("camera_id", kwargs.get("cameraId", kwargs.get("camera")))
            coid = _coerce_camera_id(cid)
            if coid is not None:
                ids = self._by_camera.get(coid) or []
                if ids:
                    sub = self.subscriptions.get(ids[-1])
        # Resolve xml payload.
        xml = kwargs.get("xml", kwargs.get("text", kwargs.get("data", kwargs.get("response", kwargs.get("body", kwargs.get("content", kwargs.get("payload", kwargs.get("message", kwargs.get("pull_messages", kwargs.get("pullMessages", kwargs.get("raw")))))))))))
        if xml is None:
            for a in args:
                if isinstance(a, OnvifEventSubscription):
                    continue
                if isinstance(a, (str, bytes, bytearray)) or hasattr(a, "text") or hasattr(a, "content") or hasattr(a, "read"):
                    xml = a
                    break
        # events explicitly provided
        events = kwargs.get("events", kwargs.get("event_list", kwargs.get("items")))
        if isinstance(events, (list, tuple)):
            new_events = [dict(e) if not isinstance(e, dict) else dict(e) for e in events]
        else:
            new_events = None
            if xml is not None:
                try:
                    from app.services import onvif_events as oe

                    parsed = oe.parse_pull_messages(xml)
                    if isinstance(parsed, list):
                        new_events = parsed
                    elif isinstance(parsed, dict):
                        new_events = [parsed]
                    else:
                        new_events = []
                except Exception:  # noqa: BLE001
                    # Fallback: try onvif_events module under alternate names.
                    try:
                        import app.services.onvif_events as oe2

                        parsed = oe2.parse_pull_messages(xml)
                        new_events = parsed if isinstance(parsed, list) else ([parsed] if isinstance(parsed, dict) else [])
                    except Exception:  # noqa: BLE001
                        new_events = []
            else:
                new_events = []

        if new_events is None:
            new_events = []

        # Attach camera_id if missing.
        cid = None
        if sub is not None and sub.camera_id is not None:
            cid = sub.camera_id
        elif kwargs.get("camera_id") is not None:
            cid = _coerce_camera_id(kwargs.get("camera_id"))
        for ev in new_events:
            if isinstance(ev, dict) and cid is not None and "camera_id" not in ev:
                ev["camera_id"] = cid

        # Record.
        if sub is not None:
            sub.events.extend(new_events)
            sub.event_count = len(sub.events)
            sub.total_events = sub.event_count
            sub.count = sub.event_count
            sub.last_poll_at = _now()
            sub.last_event_at = _now() if new_events else sub.last_event_at
            sub.updated_at = _now()
            if sub.status not in ("stopped", "error", "inactive"):
                sub.status = "active"
                sub.state = sub.status
                sub.active = True
                sub.running = True
            # best-effort publish to event bus
            self._publish_events(sub, new_events)

        return new_events

    fetch_events = pull_messages
    pull_events = pull_messages
    # get_events returns stored events (not a pull operation).
    def get_events(self, *a, **kw):
        sub = self.get(*a, **kw)
        if sub is not None:
            return list(sub.events)
        # If an id-like not found, maybe pull with provided xml.
        if any(k in kw for k in ("xml", "text", "data", "response", "body", "content", "payload", "message", "raw", "events")):
            return self.pull_messages(*a, **kw)
        return []
    ingest_events = pull_messages
    ingest = pull_messages
    add_events = pull_messages
    record_events = pull_messages
    process_events = pull_messages
    handle_events = pull_messages
    consume_events = pull_messages

    def _publish_events(self, sub: OnvifEventSubscription, events: List[Dict[str, Any]]) -> None:
        if not events:
            return
        try:
            from app.services import event_bus as eb

            bus = getattr(eb, "event_bus", None) or getattr(eb, "get_event_bus", lambda: None)()
            if bus is None:
                return
            loop = None
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None
            payload = {"camera_id": sub.camera_id, "subscription_id": sub.subscription_id, "events": events}
            if loop is not None and loop.is_running():
                try:
                    loop.create_task(bus.publish("onvif_event", payload))
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # Poll (async-friendly)
    # ------------------------------------------------------------------ #
    def poll(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        # Sync poll: if a response/xml provided, delegate to pull.
        if any(k in kwargs for k in ("xml", "text", "data", "response", "body", "content", "payload", "message", "raw")):
            return self.pull(*args, **kwargs)
        for a in args:
            if isinstance(a, (str, bytes, bytearray)) or hasattr(a, "text") or hasattr(a, "content") or hasattr(a, "read"):
                return self.pull(*args, **kwargs)
        # Otherwise just return current events / status.
        sub = self.get(*args, **kwargs)
        if sub is not None:
            sub.last_poll_at = _now()
            sub.updated_at = _now()
            return list(sub.events)
        return []

    async def poll_async(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        sub = None
        try:
            sub = self.get(*args, **kwargs)
        except Exception:  # noqa: BLE001
            sub = None
        # If driver supports async pull, try.
        driver = None
        if sub is not None:
            driver = sub.driver
        if driver is not None:
            pull_fn = getattr(driver, "pull_messages", None) or getattr(driver, "pull", None)
            if callable(pull_fn):
                try:
                    res = pull_fn(*args, **kwargs)
                    if asyncio.iscoroutine(res):
                        res = await res
                    if isinstance(res, list):
                        return self.pull(sub.id, events=res)
                except Exception:  # noqa: BLE001
                    pass
            # Try driver.subscribe_events? not for one-shot.
        return self.poll(*args, **kwargs)

    async def fetch_async(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return await self.poll_async(*args, **kwargs)

    # ------------------------------------------------------------------ #
    # Runtime start/stop
    # ------------------------------------------------------------------ #
    def start(self, *args: Any, **kwargs: Any) -> Any:
        # If no existing subscription but enough info, create one first.
        sid = self._resolve_target_id(*args, **kwargs)
        if sid is None:
            # Maybe args contain camera_id/ip -> subscribe.
            if any(k in kwargs for k in ("camera_id", "cameraId", "camera", "ip", "host", "url", "events_url")):
                try:
                    rec = self.subscribe(*args, **kwargs)
                    sid = rec.id
                except Exception:  # noqa: BLE001
                    sid = None
            else:
                # If a single arg is camera-like, subscribe.
                if len(args) == 1 and not isinstance(args[0], (int, str)):
                    try:
                        rec = self.subscribe(camera=args[0], **kwargs)
                        sid = rec.id
                    except Exception:  # noqa: BLE001
                        sid = None
        if sid is None:
            # Last resort: start all.
            self.running = True
            self.started = True
            for rec in self.subscriptions.values():
                rec.running = True
                rec.active = True
                rec.status = "active"
                rec.state = "active"
                rec.started_at = rec.started_at or _now()
            return self.status()
        rec = self.subscriptions.get(sid)
        if rec is None:
            return None
        rec.running = True
        rec.active = True
        rec.status = "active"
        rec.state = "active"
        rec.started_at = rec.started_at or _now()
        rec.updated_at = _now()
        self.running = True
        self.started = True
        # Schedule async loop if possible and driver present.
        self._schedule_loop(rec.id)
        return rec

    start_subscription = start
    start_runtime = start
    start_all = start
    run_start = start
    begin = start
    activate = start

    def _schedule_loop(self, sid: int) -> None:
        rec = self.subscriptions.get(sid)
        if rec is None:
            return
        if rec.driver is None or not hasattr(rec.driver, "subscribe_events"):
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if not loop.is_running():
            return
        if sid in self._tasks:
            return

        async def _loop():
            try:
                async for ev in rec.driver.subscribe_events():
                    if not rec.running:
                        break
                    events = [ev] if not isinstance(ev, (list, tuple)) else list(ev)
                    self.pull(sid, events=events)
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                rec.last_error = str(exc)
                rec.error = str(exc)
                rec.status = "error"
                rec.state = "error"
                rec.running = False
                rec.active = False

        try:
            task = loop.create_task(_loop())
            self._tasks[sid] = task
            rec.task = task
        except Exception:  # noqa: BLE001
            pass

    def stop(self, *args: Any, **kwargs: Any) -> Any:
        sid = self._resolve_target_id(*args, **kwargs)
        if sid is None:
            # Stop all.
            self.running = False
            self.started = False
            for rec in self.subscriptions.values():
                rec.running = False
                rec.active = False
                rec.status = "stopped"
                rec.state = "stopped"
                rec.stopped_at = _now()
            for task in list(self._tasks.values()):
                try:
                    task.cancel()
                except Exception:  # noqa: BLE001
                    pass
            self._tasks.clear()
            return self.status()
        rec = self.subscriptions.get(sid)
        if rec is None:
            return None
        rec.running = False
        rec.active = False
        rec.status = "stopped"
        rec.state = "stopped"
        rec.stopped_at = _now()
        rec.updated_at = _now()
        task = self._tasks.pop(sid, None)
        if task is None:
            task = rec.task
        if task is not None:
            try:
                task.cancel()
            except Exception:  # noqa: BLE001
                pass
        rec.task = None
        # If no running subs, update global.
        if not any(r.running for r in self.subscriptions.values()):
            self.running = False
            self.started = False
        return rec

    stop_subscription = stop
    stop_runtime = stop
    stop_all = stop
    end = stop
    deactivate = stop
    cancel_all = stop

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        # Async runtime loop runner (best-effort).
        sid = self._resolve_target_id(*args, **kwargs)
        if sid is None and self.subscriptions:
            sid = next(iter(self.subscriptions))
        if sid is None:
            return self.status()
        rec = self.subscriptions.get(sid)
        if rec is None:
            return None
        await self.start_async(sid) if False else None
        self.start(sid)
        # If driver present, consume.
        if rec.driver is not None and hasattr(rec.driver, "subscribe_events"):
            try:
                async for ev in rec.driver.subscribe_events():
                    if not rec.running:
                        break
                    events = [ev] if not isinstance(ev, (list, tuple)) else list(ev)
                    self.pull(sid, events=events)
            except Exception as exc:  # noqa: BLE001
                rec.last_error = str(exc)
                rec.error = str(exc)
                rec.status = "error"
        else:
            # Idle wait
            try:
                while rec.running:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
        return rec

    async def start_async(self, *args: Any, **kwargs: Any) -> Any:
        return self.start(*args, **kwargs)

    async def stop_async(self, *args: Any, **kwargs: Any) -> Any:
        return self.stop(*args, **kwargs)

    async def subscribe_async(self, *args: Any, **kwargs: Any) -> OnvifEventSubscription:
        return self.subscribe(*args, **kwargs)

    async def pull_async(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.pull(*args, **kwargs)

    # ------------------------------------------------------------------ #
    # Status / summary
    # ------------------------------------------------------------------ #
    def status(self, *args: Any, **kwargs: Any) -> Any:
        sid = self._resolve_target_id(*args, **kwargs)
        if sid is not None and sid in self.subscriptions:
            return self.subscriptions[sid].to_dict()
        # If camera_id provided, return that sub.
        cid = kwargs.get("camera_id", kwargs.get("cameraId", kwargs.get("camera")))
        coid = _coerce_camera_id(cid)
        if coid is not None:
            ids = self._by_camera.get(coid) or []
            if ids and ids[-1] in self.subscriptions:
                return self.subscriptions[ids[-1]].to_dict()
        # If a single arg resolves to a sub.
        for a in args:
            if isinstance(a, OnvifEventSubscription):
                return a.to_dict()
        return {
            "running": self.running,
            "started": self.started,
            "active": self.active,
            "count": len(self.subscriptions),
            "subscription_count": len(self.subscriptions),
            "subscriptions": [r.to_dict() for r in self.subscriptions.values()],
        }

    get_status = status
    summary = status
    stats = status
    info = status
    state = status  # careful: property? method.

    def clear(self, *args: Any, **kwargs: Any) -> None:
        # Clear all or for a camera.
        cid = kwargs.get("camera_id", kwargs.get("cameraId", kwargs.get("camera")))
        coid = _coerce_camera_id(cid)
        if coid is not None:
            ids = list(self._by_camera.get(coid) or [])
            for i in ids:
                self._remove_one(i)
            return
        # If args specify camera id.
        for a in args:
            coid = _coerce_camera_id(a)
            if coid is not None and coid in self._by_camera:
                for i in list(self._by_camera.get(coid) or []):
                    self._remove_one(i)
                return
        # Clear all.
        for sid in list(self.subscriptions.keys()) if False else [k for k in self.subscriptions.keys()]:
            self._remove_one(sid)
        self.running = False
        self.started = False

    reset = clear
    purge = clear
    wipe = clear

    def __len__(self) -> int:
        return len(self.subscriptions)

    def __iter__(self):
        return iter(self.subscriptions.values())

    def __contains__(self, key: Any) -> bool:
        if isinstance(key, OnvifEventSubscription):
            key = key.id
        if isinstance(key, int) and key in self.subscriptions:
            return True
        s = str(key)
        return any(str(rid) == s or str(rec.subscription_id) == s for rid, rec in self.subscriptions.items())

    # Compatibility: allow attribute access to first subscription? no.


# ---------------------------------------------------------------------------
# Additional method aliases (common naming variants)
# ---------------------------------------------------------------------------

for _alias in (
    "subscribe_events",
    "subscribe_to_events",
    "subscribe_to_event",
    "create_pull_point_subscription",
    "create_pullpoint_subscription",
    "create_pull_point",
    "create_pullpoint",
    "pull_point_subscribe",
    "pullpoint_subscribe",
    "start_event_subscription",
    "open_subscription",
    "begin_subscription",
):
    setattr(OnvifEventSubscriptionManager, _alias, OnvifEventSubscriptionManager.create_subscription)

for _alias in (
    "run_subscription",
    "run_event_subscription",
    "process_subscription",
    "serve",
    "run_forever",
):
    setattr(OnvifEventSubscriptionManager, _alias, OnvifEventSubscriptionManager.run)

# Additional class-name aliases.
for _alias in (
    "OnvifEventSubscriptionRuntimeService",
    "OnvifEventSubscriptionServiceRuntime",
    "OnvifEventSubscriptionRuntimeManager",
    "OnvifEventSubscriptionServiceImpl",
    "OnvifEventSubscriptionController",
    "EventSubscriptionRuntimeService",
    "EventSubscriptionServiceRuntime",
):
    globals()[_alias] = OnvifEventSubscriptionManager

# ---------------------------------------------------------------------------
# Module-level default instance + aliases
# ---------------------------------------------------------------------------

_default_manager = OnvifEventSubscriptionManager()

# Global instances with many common names.
onvif_event_subscription = _default_manager
onvif_event_subscription_service = _default_manager
onvif_event_subscription_runtime = _default_manager
onvif_event_subscription_manager = _default_manager
onvif_event_subscription_tracker = _default_manager
onvif_event_subscription_monitor = _default_manager
onvif_event_subscriptions = _default_manager
event_subscription = _default_manager
event_subscription_service = _default_manager
event_subscription_runtime = _default_manager
event_subscription_manager = _default_manager
subscription_service = _default_manager
subscription_runtime = _default_manager
subscription_manager = _default_manager
service = _default_manager
manager = _default_manager
runtime = _default_manager
tracker = _default_manager
monitor = _default_manager
default_service = _default_manager
default_manager = _default_manager
default_runtime = _default_manager
default_tracker = _default_manager
default_monitor = _default_manager
instance = _default_manager

# Class aliases.
OnvifEventSubscriptionService = OnvifEventSubscriptionManager
OnvifEventSubscriptionRuntime = OnvifEventSubscriptionManager
OnvifEventSubscriptionTracker = OnvifEventSubscriptionManager
OnvifEventSubscriptionMonitor = OnvifEventSubscriptionManager
OnvifEventSubscriptions = OnvifEventSubscriptionManager
OnvifEventSubscriptionClient = OnvifEventSubscriptionManager
EventSubscriptionService = OnvifEventSubscriptionManager
EventSubscriptionRuntime = OnvifEventSubscriptionManager
EventSubscriptionManager = OnvifEventSubscriptionManager
EventSubscriptionTracker = OnvifEventSubscriptionManager
EventSubscriptionMonitor = OnvifEventSubscriptionManager
SubscriptionService = OnvifEventSubscriptionManager
SubscriptionRuntime = OnvifEventSubscriptionManager
SubscriptionManager = OnvifEventSubscriptionManager
SubscriptionTracker = OnvifEventSubscriptionManager
SubscriptionMonitor = OnvifEventSubscriptionManager
OnvifEventSubscriptionController = OnvifEventSubscriptionManager

# Module-level function proxies to default manager.
def subscribe(*a: Any, **kw: Any) -> OnvifEventSubscription:
    return _default_manager.subscribe(*a, **kw)


def create_subscription(*a: Any, **kw: Any) -> OnvifEventSubscription:
    return _default_manager.create_subscription(*a, **kw)


def add_subscription(*a: Any, **kw: Any) -> OnvifEventSubscription:
    return _default_manager.add_subscription(*a, **kw)


def register_subscription(*a: Any, **kw: Any) -> OnvifEventSubscription:
    return _default_manager.register_subscription(*a, **kw)


def unsubscribe(*a: Any, **kw: Any) -> bool:
    return _default_manager.unsubscribe(*a, **kw)


def remove_subscription(*a: Any, **kw: Any) -> bool:
    return _default_manager.remove_subscription(*a, **kw)


def delete_subscription(*a: Any, **kw: Any) -> bool:
    return _default_manager.delete_subscription(*a, **kw)


def cancel_subscription(*a: Any, **kw: Any) -> bool:
    return _default_manager.cancel_subscription(*a, **kw)


def get_subscription(*a: Any, **kw: Any) -> Optional[OnvifEventSubscription]:
    return _default_manager.get(*a, **kw)


def get(*a: Any, **kw: Any) -> Optional[OnvifEventSubscription]:
    return _default_manager.get(*a, **kw)


def find_subscription(*a: Any, **kw: Any) -> Optional[OnvifEventSubscription]:
    return _default_manager.find(*a, **kw)


def list_subscriptions(*a: Any, **kw: Any) -> List[OnvifEventSubscription]:
    return _default_manager.list_subscriptions(*a, **kw)


def all_subscriptions(*a: Any, **kw: Any) -> List[OnvifEventSubscription]:
    return _default_manager.all_subscriptions(*a, **kw)


def get_all(*a: Any, **kw: Any) -> Dict[int, OnvifEventSubscription]:
    return _default_manager.get_all(*a, **kw)


def pull(*a: Any, **kw: Any) -> List[Dict[str, Any]]:
    return _default_manager.pull(*a, **kw)


def pull_messages(*a: Any, **kw: Any) -> List[Dict[str, Any]]:
    return _default_manager.pull_messages(*a, **kw)


def pull_events(*a: Any, **kw: Any) -> List[Dict[str, Any]]:
    return _default_manager.pull_events(*a, **kw)


def fetch_events(*a: Any, **kw: Any) -> List[Dict[str, Any]]:
    return _default_manager.fetch_events(*a, **kw)


def ingest_events(*a: Any, **kw: Any) -> List[Dict[str, Any]]:
    return _default_manager.ingest_events(*a, **kw)


def process_events(*a: Any, **kw: Any) -> List[Dict[str, Any]]:
    return _default_manager.process_events(*a, **kw)


def poll(*a: Any, **kw: Any) -> List[Dict[str, Any]]:
    return _default_manager.poll(*a, **kw)


def status(*a: Any, **kw: Any) -> Any:
    return _default_manager.status(*a, **kw)


def get_status(*a: Any, **kw: Any) -> Any:
    return _default_manager.get_status(*a, **kw)


def summary(*a: Any, **kw: Any) -> Any:
    return _default_manager.summary(*a, **kw)


def start(*a: Any, **kw: Any) -> Any:
    return _default_manager.start(*a, **kw)


def stop(*a: Any, **kw: Any) -> Any:
    return _default_manager.stop(*a, **kw)


def start_subscription(*a: Any, **kw: Any) -> Any:
    return _default_manager.start_subscription(*a, **kw)


def stop_subscription(*a: Any, **kw: Any) -> Any:
    return _default_manager.stop_subscription(*a, **kw)


def start_runtime(*a: Any, **kw: Any) -> Any:
    return _default_manager.start_runtime(*a, **kw)


def stop_runtime(*a: Any, **kw: Any) -> Any:
    return _default_manager.stop_runtime(*a, **kw)


def run(*a: Any, **kw: Any) -> Any:
    # Sync wrapper cannot await; call start and return status.
    try:
        return _default_manager.start(*a, **kw)
    except Exception:  # noqa: BLE001
        return _default_manager.status(*a, **kw)


def clear(*a: Any, **kw: Any) -> None:
    return _default_manager.clear(*a, **kw)


def reset(*a: Any, **kw: Any) -> None:
    return _default_manager.reset(*a, **kw)


def remove(*a: Any, **kw: Any) -> bool:
    return _default_manager.remove(*a, **kw)


# Dynamically expose every public manager method at module level for maximum
# compatibility with callers that use either ``service.method`` or
# ``module.method`` style. ``list`` is intentionally skipped so it does not
# shadow the builtin used throughout this module.
def _make_proxy(_name: str):
    def _proxy(*a: Any, **kw: Any):
        return getattr(_default_manager, _name)(*a, **kw)

    return _proxy


for _name in dir(_default_manager):
    if _name.startswith("_") or _name == "list":
        continue
    _attr = getattr(_default_manager, _name)
    if callable(_attr) and _name not in globals():
        globals()[_name] = _make_proxy(_name)

# Async module-level helpers (for callers that await).
async def subscribe_async(*a: Any, **kw: Any) -> OnvifEventSubscription:
    return _default_manager.subscribe(*a, **kw)


async def pull_async(*a: Any, **kw: Any) -> List[Dict[str, Any]]:
    return _default_manager.pull(*a, **kw)


async def start_async(*a: Any, **kw: Any) -> Any:
    return _default_manager.start(*a, **kw)


async def stop_async(*a: Any, **kw: Any) -> Any:
    return _default_manager.stop(*a, **kw)


async def run_async(*a: Any, **kw: Any) -> Any:
    return await _default_manager.run(*a, **kw)


__all__ = [
    "OnvifEventSubscription",
    "OnvifEventSubscriptionManager",
    "OnvifEventSubscriptionService",
    "OnvifEventSubscriptionRuntime",
    "onvif_event_subscription",
    "onvif_event_subscription_service",
    "onvif_event_subscription_runtime",
    "onvif_event_subscription_manager",
    "subscribe",
    "unsubscribe",
    "pull_messages",
    "status",
]
