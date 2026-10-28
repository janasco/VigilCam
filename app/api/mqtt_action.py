"""MQTT publish / Home Assistant discovery endpoints.

Auto-registered by ``app.main``'s ``pkgutil`` loader (module exposes
``router``), so no ``app/main.py`` edit is needed. Service work lives in
``app.services.mqtt_action``; this module only normalises input, merges
query params, and maps errors to HTTP status codes.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request

import app.services.mqtt_action as mqtt_mod

router = APIRouter(tags=["mqtt_action"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        s = payload.strip()
        # bare topic vs bare host heuristic
        if "/" in s or s.startswith("homeassistant/"):
            data = {"topic": s}
        else:
            data = {"host": s}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"payload": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


def _perform_publish(data: dict) -> dict:
    result = mqtt_mod.publish_mqtt(**dict(data or {}))
    if not isinstance(result, dict):
        result = {"result": result}
    result.setdefault("status", "success" if result.get("ok") else "error")
    result.setdefault("ok", False)
    result.setdefault("action", "mqtt_publish")
    result.setdefault("type", "mqtt")
    return result


def _perform_event(data: dict) -> dict:
    result = mqtt_mod.publish_event(**dict(data or {})) if data else mqtt_mod.publish_event()
    if not isinstance(result, dict):
        result = {"result": result}
    result.setdefault("status", "success" if result.get("ok") else "error")
    result.setdefault("ok", False)
    result.setdefault("action", "mqtt_publish")
    result.setdefault("type", "mqtt")
    return result


def _perform_discovery(data: dict) -> dict:
    result = mqtt_mod.publish_ha_discovery(**dict(data or {})) if data else mqtt_mod.publish_ha_discovery()
    if not isinstance(result, dict):
        result = {"result": result}
    result.setdefault("status", "success" if result.get("ok") else "error")
    result.setdefault("ok", False)
    result.setdefault("action", "mqtt_ha_discovery")
    result.setdefault("type", "mqtt")
    return result


def _preview_discovery(data: dict) -> dict:
    cam = data.get("camera_id") or data.get("camera") or "camera"
    name = data.get("camera_name") or data.get("name")
    base = data.get("base_topic") or mqtt_mod.DEFAULT_BASE_TOPIC
    prefix = data.get("discovery_prefix") or mqtt_mod.DEFAULT_DISCOVERY_PREFIX
    configs = mqtt_mod.build_ha_discovery_config(cam, name, str(base), str(prefix))
    return {
        "status": "ok", "ok": True, "success": True,
        "action": "mqtt_ha_discovery_preview", "type": "mqtt",
        "camera_id": str(cam), "base_topic": str(base),
        "discovery_prefix": str(prefix), "configs": configs,
        "count": len(configs),
    }


@router.post("/api/mqtt/publish")
async def mqtt_publish(payload: Any = Body(default=None), request: Request = None):
    return _perform_publish(_normalize(payload, request))


@router.post("/api/mqtt/publish/")
async def mqtt_publish_slash(payload: Any = Body(default=None), request: Request = None):
    return _perform_publish(_normalize(payload, request))


@router.post("/api/mqtt_action/publish")
async def mqtt_action_publish(payload: Any = Body(default=None), request: Request = None):
    return _perform_publish(_normalize(payload, request))


@router.post("/api/mqtt_action/publish/")
async def mqtt_action_publish_slash(payload: Any = Body(default=None), request: Request = None):
    return _perform_publish(_normalize(payload, request))


@router.post("/api/mqtt-action/publish")
async def mqtt_hyphen_publish(payload: Any = Body(default=None), request: Request = None):
    return _perform_publish(_normalize(payload, request))


@router.post("/api/mqtt-action/publish/")
async def mqtt_hyphen_publish_slash(payload: Any = Body(default=None), request: Request = None):
    return _perform_publish(_normalize(payload, request))


@router.post("/api/mqtt/send")
async def mqtt_send(payload: Any = Body(default=None), request: Request = None):
    return _perform_publish(_normalize(payload, request))


@router.post("/api/mqtt")
async def mqtt_root(payload: Any = Body(default=None), request: Request = None):
    return _perform_publish(_normalize(payload, request))


@router.post("/api/mqtt/")
async def mqtt_root_slash(payload: Any = Body(default=None), request: Request = None):
    return _perform_publish(_normalize(payload, request))


@router.post("/api/mqtt/event")
async def mqtt_event(payload: Any = Body(default=None), request: Request = None):
    return _perform_event(_normalize(payload, request))


@router.post("/api/mqtt/event/")
async def mqtt_event_slash(payload: Any = Body(default=None), request: Request = None):
    return _perform_publish(_normalize(payload, request)) if False else _perform_event(_normalize(payload, request))


@router.post("/api/mqtt_action/event")
async def mqtt_action_event(payload: Any = Body(default=None), request: Request = None):
    return _perform_event(_normalize(payload, request))


@router.post("/api/mqtt/discovery")
async def mqtt_discovery(payload: Any = Body(default=None), request: Request = None):
    return _perform_discovery(_normalize(payload, request))


@router.post("/api/mqtt/discovery/")
async def mqtt_discovery_slash(payload: Any = Body(default=None), request: Request = None):
    return _perform_discovery(_normalize(payload, request))


@router.post("/api/mqtt_action/discovery")
async def mqtt_action_discovery(payload: Any = Body(default=None), request: Request = None):
    return _perform_discovery(_normalize(payload, request))


@router.get("/api/mqtt/discovery/preview")
async def mqtt_discovery_preview(request: Request = None):
    return _preview_discovery(_normalize(None, request))


@router.post("/api/mqtt/discovery/preview")
async def mqtt_discovery_preview_post(payload: Any = Body(default=None), request: Request = None):
    return _preview_discovery(_normalize(payload, request))


@router.get("/api/mqtt_action/discovery/preview")
async def mqtt_action_discovery_preview(request: Request = None):
    return _preview_discovery(_normalize(None, request))


@router.get("/api/mqtt/status")
async def mqtt_status(request: Request = None):
    return {
        "status": "idle", "ok": True, "success": True,
        "action": "mqtt_publish", "type": "mqtt",
        "default_port": mqtt_mod.DEFAULT_PORT,
        "default_base_topic": mqtt_mod.DEFAULT_BASE_TOPIC,
        "default_discovery_prefix": mqtt_mod.DEFAULT_DISCOVERY_PREFIX,
    }


@router.get("/api/mqtt_action/status")
async def mqtt_action_status(request: Request = None):
    return await mqtt_status(request)
