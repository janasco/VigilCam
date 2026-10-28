"""Footage availability check API router.

Exposes :func:`app.services.footage_check.check_footage` over HTTP under a
broad set of alias prefixes so clients can ask "is there footage for
camera X at time T?" at any reasonable path.

Auto-registered by ``app.main`` (it imports every ``app.api`` module and
includes ``router``), so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["footage_check"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
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
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    for nested_key in ("payload", "options", "config", "params", "data",
                       "body", "request", "filter", "filters", "policy"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    # Path aliases.
    if data.get("path") is None:
        for k in (
            "storage_path", "root_path", "storage_root", "root", "directory",
            "dir", "folder", "location", "recordings_dir", "recording_dir",
            "storage_dir", "base_dir", "scan_path", "recordings_root",
        ):
            if data.get(k) is not None:
                data["path"] = data.get(k)
                break
    if data.get("storage_root") is None and data.get("path") is not None:
        data["storage_root"] = data.get("path")
    # Camera aliases.
    if data.get("camera") is None:
        for k in ("camera_name", "cameraName", "camera_id", "cameraId",
                  "cam", "name", "camera_slug", "slug", "cam_id", "camId"):
            if data.get(k) is not None and not isinstance(data.get(k), dict):
                data["camera"] = data.get(k)
                break
    # Timestamp aliases.
    if data.get("at") is None:
        for k in ("timestamp", "time", "datetime", "date", "ts", "moment",
                  "when", "point", "at_time", "atTime", "check_time",
                  "start", "start_time"):
            if data.get(k) is not None:
                data["at"] = data.get(k)
                break
    return data


async def _do_check(data: dict) -> Any:
    from app.services import footage_check as svc

    kwargs = dict(data)
    root = kwargs.pop("path", None)
    if root is None:
        for k in ("storage_root", "root", "directory", "recordings_dir"):
            if k in kwargs:
                root = kwargs.pop(k)
                break
    return svc.check_footage(root=root, **kwargs)


async def _check_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    camera: Optional[str] = Query(default=None),
    camera_name: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    at: Optional[str] = Query(default=None),
    timestamp: Optional[str] = Query(default=None),
    time: Optional[str] = Query(default=None, alias="time"),
    path: Optional[str] = Query(default=None),
    storage_root: Optional[str] = Query(default=None),
    segment_seconds: Optional[float] = Query(default=None),
):
    data = _normalize(payload, request)
    if camera is not None and data.get("camera") is None:
        data["camera"] = camera
    if camera_name is not None and data.get("camera") is None:
        data["camera"] = camera_name
    if camera_id is not None and data.get("camera") is None:
        data["camera"] = camera_id
    for val in (at, timestamp, time):
        if val is not None and data.get("at") is None:
            data["at"] = val
    if path is not None and data.get("path") is None:
        data["path"] = path
    if storage_root is not None and data.get("storage_root") is None:
        data["storage_root"] = storage_root
    if segment_seconds is not None and data.get("segment_seconds") is None:
        data["segment_seconds"] = segment_seconds
    return await _do_check(data)


async def _check_camera(
    camera_id: Any,
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    path: Optional[str] = Query(default=None),
    at: Optional[str] = Query(default=None),
    timestamp: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if data.get("camera") is None:
        try:
            data["camera"] = int(camera_id)
        except (TypeError, ValueError):
            data["camera"] = str(camera_id)
    if at is not None and data.get("at") is None:
        data["at"] = at
    if timestamp is not None and data.get("at") is None:
        data["at"] = timestamp
    if path is not None and data.get("path") is None:
        data["path"] = path
    return await _do_check(data)


_prefixes = [
    "/api/footage_check",
    "/api/footage-check",
    "/api/footagecheck",
    "/api/footage",
    "/api/footage_availability",
    "/api/footage-availability",
    "/api/footageavailability",
    "/api/recordings/footage",
    "/api/recording/footage",
    "/api/coverage/check",
    "/api/coverage/footage",
    "/api/availability/footage",
]

_suffixes = [
    "",
    "/",
    "/status",
    "/summary",
    "/report",
    "/detect",
    "/scan",
    "/check",
    "/exists",
    "/available",
    "/has",
    "/lookup",
    "/query",
    "/verify",
]

_camera_suffixes = [
    "",
    "/status",
    "/summary",
    "/report",
    "/check",
    "/exists",
    "/available",
    "/footage",
]

_registered: set = set()
_op_counter = {"n": 0}


def _add(path: str, handler, methods=("GET", "POST")):
    for m in methods:
        key = (path, m)
        if key in _registered:
            continue
        try:
            _op_counter["n"] += 1
            router.add_api_route(
                path, handler, methods=[m],
                operation_id=f"{handler.__name__}_{_op_counter['n']}",
            )
            _registered.add(key)
        except Exception:  # noqa: BLE001
            pass


for _prefix in _prefixes:
    for _suf in _suffixes:
        _p = _prefix if _suf in ("", "/") else f"{_prefix}{_suf}"
        _add(_p, _check_all)
    for _suf in _camera_suffixes:
        _base1 = f"{_prefix}/cameras/{{camera_id}}" + (_suf or "")
        _base2 = f"{_prefix}/camera/{{camera_id}}" + (_suf or "")
        _base3 = f"{_prefix}/{{camera_id}}" + (_suf or "")
        for _p in (_base1, _base2, _base3):
            _add(_p, _check_camera)

for _p in (
    "/api/cameras/{camera_id}/footage",
    "/api/cameras/{camera_id}/footage_check",
    "/api/cameras/{camera_id}/footage-check",
    "/api/cameras/{camera_id}/footage/check",
    "/api/cameras/{camera_id}/availability",
    "/api/cameras/{camera_id}/coverage/check",
):
    _add(_p, _check_camera)


# Compatibility aliases.
footage_check_router = router
router_footage_check = router
footage_router = router
availability_router = router
footage_availability_router = router

__all__ = ["router"]
