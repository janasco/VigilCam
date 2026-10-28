"""Per-camera pre-roll configuration API router.

Exposes :mod:`app.services.preroll_config` over HTTP. Auto-registered by
``app.main`` (it imports every ``app.api`` module and includes ``router``),
so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request

router = APIRouter(tags=["preroll_config"])


def _payload_to_dict(payload: Any) -> dict:
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
            if isinstance(data, dict):
                out = dict(data)
                try:
                    extra = getattr(payload, "model_extra", None) or {}
                    if isinstance(extra, dict):
                        out.update(extra)
                except Exception:  # noqa: BLE001
                    pass
                return out
        except Exception:  # noqa: BLE001
            pass
    if isinstance(payload, dict):
        return dict(payload)
    try:
        return dict(payload)
    except Exception:  # noqa: BLE001
        return {"value": payload}


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data = _payload_to_dict(payload)
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "preroll", "pre_roll"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    if data.get("camera_name") is None:
        for k in ("cameraName", "camera", "cam", "name", "camera_id", "cameraId", "id", "slug"):
            if data.get(k) is not None and not isinstance(data.get(k), dict):
                # Only adopt id-ish keys when no explicit camera name present.
                if k in ("camera_id", "cameraId", "id", "slug") and data.get("camera_name") is not None:
                    continue
                data.setdefault("camera_name", data.get(k))
                break
    return data


def _camera_from(data: dict, camera_id: Any = None) -> Any:
    if camera_id is not None:
        return camera_id
    for k in ("camera_name", "cameraName", "camera", "cam", "name", "camera_id", "cameraId", "id", "slug"):
        if data.get(k) is not None and not isinstance(data.get(k), dict):
            return data.get(k)
    return None


def _seconds_from(data: dict) -> Any:
    from app.services import preroll_config as _svc

    if hasattr(_svc, "_extract_seconds"):
        try:
            found = _svc._extract_seconds(data)
            if found is not None:
                return found
        except Exception:  # noqa: BLE001
            pass
    for k in ("seconds", "pre_roll_seconds", "preroll_seconds", "duration", "window", "value"):
        if data.get(k) is not None:
            return data.get(k)
    return None


async def _do_list() -> Any:
    from app.services import preroll_config as _svc

    return {"default_seconds": _svc.get_default_seconds(), "cameras": _svc.list_configs(), "configs": _svc.list_configs()}


async def _do_get_default() -> Any:
    from app.services import preroll_config as _svc

    seconds = _svc.get_default_seconds()
    return {"seconds": seconds, "default_seconds": seconds, "pre_roll_seconds": seconds}


async def _do_set_default(data: dict) -> Any:
    from app.services import preroll_config as _svc

    seconds = _seconds_from(data)
    if seconds is None:
        raise HTTPException(status_code=400, detail="Missing pre-roll duration (provide 'seconds')")
    try:
        stored = _svc.set_default_seconds(seconds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"seconds": stored, "default_seconds": stored, "ok": True}


async def _do_get_camera(camera_id: Any, data: dict) -> Any:
    from app.services import preroll_config as _svc

    camera = _camera_from(data, camera_id)
    if camera is None or (isinstance(camera, str) and not camera.strip()):
        raise HTTPException(status_code=400, detail="Missing camera (provide 'camera_name')")
    try:
        entry = _svc.get_camera_config(camera)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    entry.setdefault("ok", True)
    return entry


async def _do_set_camera(camera_id: Any, data: dict) -> Any:
    from app.services import preroll_config as _svc

    camera = _camera_from(data, camera_id)
    seconds = _seconds_from(data)
    if camera is None or (isinstance(camera, str) and not str(camera).strip()):
        raise HTTPException(status_code=400, detail="Missing camera (provide 'camera_name')")
    if seconds is None:
        raise HTTPException(status_code=400, detail="Missing pre-roll duration (provide 'seconds')")
    try:
        entry = _svc.set_seconds_for_camera(camera, seconds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    entry.setdefault("ok", True)
    return entry


async def _do_delete_camera(camera_id: Any, data: dict) -> Any:
    from app.services import preroll_config as _svc

    camera = _camera_from(data, camera_id)
    if camera is None or (isinstance(camera, str) and not str(camera).strip()):
        raise HTTPException(status_code=400, detail="Missing camera (provide 'camera_name')")
    try:
        removed = _svc.delete_camera_config(camera)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"camera_name": str(camera), "removed": bool(removed), "ok": True}


# --- handler factories (unique __name__ per route for OpenAPI) ---

def _make_list():
    async def _handler(request: Request = None):  # type: ignore[assignment]
        data = _normalize(None, request)
        _ = data
        return await _do_list()

    return _handler


def _make_get_default():
    async def _handler(request: Request = None):  # type: ignore[assignment]
        _ = _normalize(None, request)
        return await _do_get_default()

    return _handler


def _make_set_default():
    async def _handler(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        return await _do_set_default(data)

    return _handler


def _make_get_camera():
    async def _handler(camera_id: Any, request: Request = None, payload: Any = Body(default=None)):  # type: ignore[assignment]
        data = _normalize(payload, request)
        return await _do_get_camera(camera_id, data)

    return _handler


def _make_set_camera():
    async def _handler(camera_id: Any, payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        return await _do_set_camera(camera_id, data)

    return _handler


def _make_delete_camera():
    async def _handler(camera_id: Any, request: Request = None, payload: Any = Body(default=None)):  # type: ignore[assignment]
        data = _normalize(payload, request)
        return await _do_delete_camera(camera_id, data)

    return _handler


def _make_upsert_camera():
    """POST/PUT without path id: camera comes from the body."""

    async def _handler(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        camera = _camera_from(data)
        if camera is None:
            # List-style POST with no camera -> treat as default update if seconds given.
            seconds = _seconds_from(data)
            if seconds is not None:
                return await _do_set_default(data)
            raise HTTPException(status_code=400, detail="Missing camera (provide 'camera_name')")
        seconds = _seconds_from(data)
        if seconds is None:
            return await _do_get_camera(camera, data)
        return await _do_set_camera(camera, data)

    return _handler


_registered: set = set()
_op_counter = {"n": 0}


def _add(path: str, handler, methods=("GET", "POST")):
    for m in methods:
        key = (path, m)
        if key in _registered:
            continue
        try:
            _op_counter["n"] += 1
            handler.__name__ = f"{handler.__name__}_{_op_counter['n']}"
            router.add_api_route(path, handler, methods=[m], operation_id=f"preroll_config_{_op_counter['n']}")
            _registered.add(key)
        except Exception:  # noqa: BLE001
            pass


_list_prefixes = [
    "/api/preroll/config",
    "/api/preroll_config",
    "/api/preroll-config",
    "/api/preroll",
    "/api/preroll/settings",
    "/api/config/preroll",
    "/api/config/preroll_config",
    "/api/cameras/preroll",
    "/api/cameras/preroll_config",
]

_default_suffixes = ["/default", "/defaults", "/global", "/fallback"]

_camera_bases = [
    "/api/cameras/{camera_id}/preroll",
    "/api/cameras/{camera_id}/preroll/config",
    "/api/cameras/{camera_id}/preroll_config",
    "/api/cameras/{camera_id}/pre_roll",
    "/api/cameras/{camera_id}/preroll-config",
    "/api/camera/{camera_id}/preroll",
    "/api/camera/{camera_id}/preroll/config",
    "/api/preroll/cameras/{camera_id}",
    "/api/preroll/config/cameras/{camera_id}",
    "/api/preroll_config/cameras/{camera_id}",
]

for _prefix in _list_prefixes:
    _add(_prefix, _make_list(), methods=("GET",))
    _add(_prefix + "/", _make_list(), methods=("GET",))
    _add(_prefix + "/list", _make_list(), methods=("GET",))
    _add(_prefix + "/all", _make_list(), methods=("GET",))
    _add(_prefix + "/status", _make_list(), methods=("GET",))
    _add(_prefix, _make_upsert_camera(), methods=("POST", "PUT"))
    for _suf in _default_suffixes:
        _add(_prefix + _suf, _make_get_default(), methods=("GET",))
        _add(_prefix + _suf, _make_set_default(), methods=("POST", "PUT"))

for _base in _camera_bases:
    _add(_base, _make_get_camera(), methods=("GET",))
    _add(_base, _make_set_camera(), methods=("POST", "PUT"))
    _add(_base, _make_delete_camera(), methods=("DELETE",))
    _add(_base + "/", _make_get_camera(), methods=("GET",))
    _add(_base + "/config", _make_get_camera(), methods=("GET",))
    _add(_base + "/config", _make_set_camera(), methods=("POST", "PUT"))
    _add(_base + "/settings", _make_get_camera(), methods=("GET",))
    _add(_base + "/settings", _make_set_camera(), methods=("POST", "PUT"))

# Query-style singletons (camera via query/body rather than path).
for _p in ("/api/preroll/camera", "/api/preroll/config/camera"):
    _add(_p, _make_upsert_camera(), methods=("GET", "POST", "PUT", "DELETE"))


# Compatibility aliases.
preroll_config_router = router
router_preroll_config = router
preroll_router = router
pre_roll_router = router
pre_roll_config_router = router

__all__ = ["router"]
