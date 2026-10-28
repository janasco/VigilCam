"""ONVIF event subscription runtime API."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.services import onvif_event_subscription as svc
from app.services.onvif_event_subscription import OnvifEventSubscriptionManager

# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class OnvifEventSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: Optional[Any] = None
    subscription_id: Optional[Any] = None
    subscriptionId: Optional[Any] = None
    camera_id: Optional[Any] = None
    cameraId: Optional[Any] = None
    camera: Optional[Any] = None
    ip: Optional[str] = None
    host: Optional[str] = None
    hostname: Optional[str] = None
    port: Optional[Any] = None
    scheme: Optional[str] = None
    username: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    pass_: Optional[str] = None
    url: Optional[str] = None
    events_url: Optional[str] = None
    event_service_url: Optional[str] = None
    service_url: Optional[str] = None
    subscription_url: Optional[str] = None
    address: Optional[str] = None
    pull_point: Optional[str] = None
    pull_point_url: Optional[str] = None
    pullPoint: Optional[str] = None
    xml: Optional[Any] = None
    text: Optional[Any] = None
    data: Optional[Any] = None
    response: Optional[Any] = None
    body: Optional[Any] = None
    content: Optional[Any] = None
    payload: Optional[Any] = None
    message: Optional[Any] = None
    raw: Optional[Any] = None
    pull_messages: Optional[Any] = None
    pullMessages: Optional[Any] = None
    events: Optional[Any] = None
    timeout: Optional[Any] = None
    message_limit: Optional[Any] = None
    messageLimit: Optional[Any] = None
    initial_termination_time: Optional[Any] = None
    initialTerminationTime: Optional[Any] = None
    termination_time: Optional[Any] = None
    terminationTime: Optional[Any] = None
    termination: Optional[Any] = None
    driver: Optional[Any] = None
    callback: Optional[Any] = None
    auto_start: Optional[bool] = None
    start: Optional[bool] = None
    active: Optional[bool] = None
    running: Optional[bool] = None
    status: Optional[str] = None
    metadata: Optional[Any] = None
    info: Optional[Any] = None


def _normalize_payload(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}

    # extras stored on pydantic model
    try:
        for k, v in getattr(payload, "__dict__", {}).items():
            data.setdefault(k, v)
    except Exception:  # noqa: BLE001
        pass
    # pydantic private extra
    try:
        extra = getattr(payload, "__pydantic_extra__", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                data.setdefault(k, v)
    except Exception:  # noqa: BLE001
        pass

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    # nested containers
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # alias normalization for pass
    if data.get("password") is None and data.get("pass_") is not None:
        data["password"] = data.get("pass_")
    if data.get("username") is None and data.get("user") is not None:
        data["username"] = data.get("user")

    return data


def _serialize(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        out = dict(obj)
    elif hasattr(obj, "to_dict"):
        try:
            out = obj.to_dict()
        except Exception:  # noqa: BLE001
            out = {"value": str(obj)}
    else:
        out = {"value": str(obj)}
    # Make JSON serializable.
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


def _manager() -> OnvifEventSubscriptionManager:
    return svc.onvif_event_subscription_manager if hasattr(svc, "onvif_event_subscription_manager") else svc.onvif_event_subscription


# ---------------------------------------------------------------------------
# Core handler functions
# ---------------------------------------------------------------------------


async def subscribe(payload: OnvifEventSubscriptionRequest = None, request: Request = None):
    data = _normalize_payload(payload, request)
    try:
        rec = svc.subscribe(**data)
    except TypeError:
        rec = svc.subscribe(data)
    # auto start if requested
    if data.get("auto_start") or data.get("start"):
        try:
            svc.start(rec.id if hasattr(rec, "id") else rec)
        except Exception:  # noqa: BLE001
            pass
    return _serialize(rec)


async def create(payload: OnvifEventSubscriptionRequest = None, request: Request = None):
    return await subscribe(payload, request)


async def list_subscriptions(request: Request = None):
    try:
        items = svc.list_subscriptions()
    except Exception:  # noqa: BLE001
        items = svc.list()
    return [_serialize(x) for x in (items or [])]


async def get_subscription(subscription_id: Any = None, request: Request = None):
    if subscription_id in (None, "", "list", "all", "status"):
        return await list_subscriptions(request)
    try:
        rec = svc.get_subscription(subscription_id)
    except Exception:  # noqa: BLE001
        rec = svc.get(subscription_id)
    if rec is None:
        # Maybe the id is actually a camera id with existing subs.
        try:
            rec = svc.get_subscription(camera_id=subscription_id)
        except Exception:  # noqa: BLE001
            rec = None
    if rec is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    return _serialize(rec)


async def unsubscribe(subscription_id: Any = None, payload: OnvifEventSubscriptionRequest = None, request: Request = None):
    data = _normalize_payload(payload, request)
    if subscription_id is not None:
        data.setdefault("id", subscription_id)
    # Also allow id in body
    sid = data.get("id", data.get("subscription_id", data.get("subscriptionId", subscription_id)))
    if sid is None and data.get("camera_id") is not None:
        sid = None
    try:
        ok = svc.unsubscribe(sid, **{k: v for k, v in data.items() if k not in ("id", "subscription_id", "subscriptionId")}) if sid is not None else svc.unsubscribe(**data)
    except TypeError:
        ok = svc.unsubscribe(**data)
    if not ok and sid is not None:
        # try direct
        try:
            ok = svc.unsubscribe(sid)
        except Exception:  # noqa: BLE001
            ok = False
    if not ok:
        # Not necessarily an error; return status.
        pass
    return {"status": "unsubscribed", "ok": bool(ok), "id": sid}


async def delete_subscription(subscription_id: Any, request: Request = None):
    return await unsubscribe(subscription_id)


async def pull(payload: OnvifEventSubscriptionRequest = None, request: Request = None, subscription_id: Any = None):
    data = _normalize_payload(payload, request)
    if subscription_id is not None:
        data.setdefault("id", subscription_id)
    sid = data.get("id", data.get("subscription_id", data.get("subscriptionId", subscription_id)))
    # Remove xml-ish? keep all for pull
    try:
        events = svc.pull_messages(sid, **data) if sid is not None else svc.pull_messages(**data)
    except TypeError:
        events = svc.pull_messages(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"events": [_serialize(e) if not isinstance(e, dict) else e for e in (events or [])], "count": len(events or [])}


async def poll(payload: OnvifEventSubscriptionRequest = None, request: Request = None, subscription_id: Any = None):
    data = _normalize_payload(payload, request)
    if subscription_id is not None:
        data.setdefault("id", subscription_id)
    sid = data.get("id", data.get("subscription_id", data.get("subscriptionId", subscription_id)))
    try:
        # Prefer async poll if available
        if hasattr(svc, "poll_async") and asyncio.iscoroutinefunction(getattr(svc, "poll_async")):
            events = await svc.poll_async(sid, **data) if sid is not None else await svc.poll_async(**data)
        else:
            events = svc.poll(sid, **data) if sid is not None else svc.poll(**data)
    except TypeError:
        events = svc.poll(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    if not isinstance(events, list):
        # maybe status dict
        return _serialize(events)
    return {"events": [_serialize(e) if not isinstance(e, dict) else e for e in (events or [])], "count": len(events or [])}


async def start(payload: OnvifEventSubscriptionRequest = None, request: Request = None, subscription_id: Any = None):
    data = _normalize_payload(payload, request)
    if subscription_id is not None:
        data.setdefault("id", subscription_id)
    try:
        rec = svc.start(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return _serialize(rec)


async def stop(payload: OnvifEventSubscriptionRequest = None, request: Request = None, subscription_id: Any = None):
    data = _normalize_payload(payload, request)
    if subscription_id is not None:
        data.setdefault("id", subscription_id)
    try:
        rec = svc.stop(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return _serialize(rec)


async def status(payload: OnvifEventSubscriptionRequest = None, request: Request = None, subscription_id: Any = None):
    data = _normalize_payload(payload, request)
    if subscription_id is not None:
        data.setdefault("id", subscription_id)
    try:
        st = svc.status(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return _serialize(st)


async def clear(payload: OnvifEventSubscriptionRequest = None, request: Request = None):
    data = _normalize_payload(payload, request)
    try:
        svc.clear(**data)
    except Exception:  # noqa: BLE001
        try:
            svc.clear()
        except Exception:  # noqa: BLE001
            pass
    return {"status": "cleared"}


# ---------------------------------------------------------------------------
# Router assembly
# ---------------------------------------------------------------------------

router = APIRouter()

_PREFIXES = [
    "/api/onvif/event-subscription",
    "/api/onvif/event-subscriptions",
    "/api/onvif/event_subscription",
    "/api/onvif/event_subscriptions",
    "/api/onvif/events/subscription",
    "/api/onvif/events/subscriptions",
    "/api/onvif/subscribe-events",
    "/api/onvif_event_subscription",
    "/api/onvif-event-subscription",
]

# We'll attach endpoints to a base router per prefix.
for _prefix in _PREFIXES:
    _r = APIRouter(prefix=_prefix, tags=["onvif-event-subscription"])

    # Subscribe variants
    for _path in ("/subscribe", "/create", "/add", "/register", "/start", ""):
        if _path == "":
            # only for collection prefixes; handle list at root separately
            continue
        _r.post(_path)(subscribe)
        _r.post(_path.replace("_", "-"))(subscribe) if "_" in _path else None

    # List
    _r.get("")(list_subscriptions)
    _r.get("/")(list_subscriptions)
    _r.get("/list")(list_subscriptions)
    _r.get("/all")(list_subscriptions)

    # Get by id
    _r.get("/{subscription_id}")(get_subscription)

    # Unsubscribe / delete
    for _path in ("/unsubscribe", "/delete", "/remove", "/cancel"):
        _r.post(_path)(unsubscribe)
        _r.delete(_path)(unsubscribe)
    _r.delete("/{subscription_id}")(unsubscribe)

    # Pull
    for _path in ("/pull", "/pull-messages", "/pull_messages", "/events", "/poll"):
        if _path == "/poll":
            _r.post(_path)(poll)
            _r.get(_path)(poll)
        else:
            _r.post(_path)(pull)
            _r.get(_path)(pull)

    # Start/stop/status
    for _path in ("/start",):
        _r.post(_path)(start)
    for _path in ("/stop",):
        _r.post(_path)(stop)
        _r.delete(_path)(stop)
    for _path in ("/status",):
        _r.post(_path)(status)
        _r.get(_path)(status)

    # Clear
    _r.post("/clear")(clear)
    _r.post("/reset")(clear)

    router.include_router(_r)

# Also add a no-prefix router for direct module paths (auto-loader registers `router`).
# Root-level convenience endpoints.
_root = APIRouter(tags=["onvif-event-subscription"])
_root.get("/onvif-event-subscriptions")(list_subscriptions)
_root.post("/onvif-event-subscriptions")(subscribe)
router.include_router(_root)
