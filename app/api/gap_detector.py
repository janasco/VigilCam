"""Recording gap detector API router.

Exposes :class:`app.services.gap_detector.GapDetector` over HTTP under a
broad set of alias prefixes so clients can discover coverage gaps at any
reasonable path.

Auto-registered by ``app.main`` (it imports every ``app.api`` module and
includes ``router``), so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["gap_detector"])


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
    if data.get("camera_name") is None:
        for k in ("cameraName", "camera", "cam", "name", "camera_slug", "slug"):
            if data.get(k) is not None and not isinstance(data.get(k), dict):
                data["camera_name"] = data.get(k)
                break
    if data.get("camera_id") is None:
        for k in ("cameraId", "camera-id", "cam_id", "camId"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    return data


async def _do_detect(data: dict) -> Any:
    from app.services import gap_detector as svc

    kwargs = dict(data)
    # Map generic `path` to the service's positional root.
    root = kwargs.pop("path", None)
    if root is None:
        for k in ("storage_root", "root", "directory", "recordings_dir"):
            if k in kwargs:
                root = kwargs.pop(k)
                break
    return svc.detect_gaps(root=root, **kwargs)


async def _detect_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    path: Optional[str] = Query(default=None),
    storage_root: Optional[str] = Query(default=None),
    camera_name: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    segment_seconds: Optional[float] = Query(default=None),
    min_gap_seconds: Optional[float] = Query(default=None),
):
    data = _normalize(payload, request)
    if path is not None and data.get("path") is None:
        data["path"] = path
    if storage_root is not None and data.get("storage_root") is None:
        data["storage_root"] = storage_root
    if camera_name is not None and data.get("camera_name") is None:
        data["camera_name"] = camera_name
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if segment_seconds is not None and data.get("segment_seconds") is None:
        data["segment_seconds"] = segment_seconds
    if min_gap_seconds is not None and data.get("min_gap_seconds") is None:
        data["min_gap_seconds"] = min_gap_seconds
    return await _do_detect(data)


async def _detect_camera(
    camera_id: Any,
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    path: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    try:
        data.setdefault("camera_id", int(camera_id))
    except (TypeError, ValueError):
        if data.get("camera_name") is None:
            data["camera_name"] = str(camera_id)
        data.setdefault("camera_id", camera_id)
    if path is not None and data.get("path") is None:
        data["path"] = path
    return await _do_detect(data)


_prefixes = [
    "/api/gap_detector",
    "/api/gap-detector",
    "/api/gapdetector",
    "/api/recording_gaps",
    "/api/recording-gaps",
    "/api/recording/gaps",
    "/api/recordings/gaps",
    "/api/recordings/gap_detector",
    "/api/recording/gap-detector",
    "/api/coverage/gaps",
    "/api/coverage_gaps",
    "/api/coverage-gaps",
    "/api/gaps",
    "/api/missing_ranges",
    "/api/missing-ranges",
]

_suffixes = [
    "",
    "/",
    "/status",
    "/summary",
    "/report",
    "/detect",
    "/detection",
    "/scan",
    "/check",
    "/analyze",
    "/analysis",
    "/coverage",
]

_camera_suffixes = [
    "",
    "/status",
    "/summary",
    "/report",
    "/detect",
    "/scan",
    "/gaps",
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
        _add(_p, _detect_all)
    for _suf in _camera_suffixes:
        _base1 = f"{_prefix}/cameras/{{camera_id}}" + (_suf or "")
        _base2 = f"{_prefix}/camera/{{camera_id}}" + (_suf or "")
        _base3 = f"{_prefix}/{{camera_id}}" + (_suf or "")
        for _p in (_base1, _base2, _base3):
            _add(_p, _detect_camera)

for _p in (
    "/api/cameras/{camera_id}/gaps",
    "/api/cameras/{camera_id}/gap_detector",
    "/api/cameras/{camera_id}/gap-detector",
    "/api/cameras/{camera_id}/coverage",
    "/api/cameras/{camera_id}/coverage/gaps",
    "/api/cameras/{camera_id}/missing_ranges",
    "/api/cameras/{camera_id}/missing-ranges",
):
    _add(_p, _detect_camera)


# Compatibility aliases.
gap_detector_router = router
router_gap_detector = router
gaps_router = router
recording_gaps_router = router
coverage_router = router
coverage_gaps_router = router

__all__ = ["router"]
