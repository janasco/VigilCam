"""Maintenance calendar API router — per-day maintenance-window counts.

Exposes :mod:`app.services.maint_calendar` over HTTP under a broad set of
alias prefixes so calendar UI clients can discover window density at any
reasonable path. Auto-registered by ``app.main`` (it imports every
``app.api`` module and includes ``router``), so no ``app/main.py`` edit is
required. Read-only: this router never creates or deletes windows.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["maint_calendar"])


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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "filter", "filters"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # Year aliases.
    if data.get("year") is None:
        for k in ("y", "yr", "calendar_year", "yearMonth", "year_month"):
            if data.get(k) is not None:
                data["year"] = data.get(k)
                break
    # Month aliases.
    if data.get("month") is None:
        for k in ("m", "mon", "mo", "calendar_month"):
            if data.get(k) is not None:
                data["month"] = data.get(k)
                break
    # Camera id aliases.
    if data.get("camera_id") is None:
        for k in ("cameraId", "camera-id", "cam_id", "camId", "cam", "camera"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    # Camera name aliases.
    if data.get("camera_name") is None:
        for k in ("cameraName", "camera_slug", "slug"):
            if data.get(k) is not None and not isinstance(data.get(k), dict):
                data["camera_name"] = data.get(k)
                break
    return data


def _do_calendar(data: dict) -> Any:
    from app.services.maint_calendar import get_calendar

    return get_calendar(**data)


async def _calendar_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    year: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    camera_name: Optional[str] = Query(default=None),
    include_disabled: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if year is not None and data.get("year") is None:
        data["year"] = year
    if month is not None and data.get("month") is None:
        data["month"] = month
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if camera_name is not None and data.get("camera_name") is None:
        data["camera_name"] = camera_name
    if include_disabled is not None and data.get("include_disabled") is None:
        data["include_disabled"] = include_disabled
    return _do_calendar(data)


async def _calendar_camera(
    camera_id: Any,
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    year: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None),
    include_disabled: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    data.setdefault("camera_id", camera_id)
    if year is not None and data.get("year") is None:
        data["year"] = year
    if month is not None and data.get("month") is None:
        data["month"] = month
    if include_disabled is not None and data.get("include_disabled") is None:
        data["include_disabled"] = include_disabled
    return _do_calendar(data)


async def _health() -> dict:
    return {"ok": True, "success": True, "service": "maint_calendar"}


_prefixes = [
    "/api/maint_calendar",
    "/api/maint-calendar",
    "/api/maintenance_calendar",
    "/api/maintenance-calendar",
    "/api/maintenance/calendar",
    "/api/maintenance/calendar-view",
    "/api/maintenance/calendar_view",
    "/api/maintenance/daily",
    "/api/maintenance/daily-counts",
    "/api/maintenance/daily_counts",
    "/api/maintenance/per-day",
    "/api/maintenance/per_day",
    "/api/maintenance-windows/calendar",
    "/api/maintenance-windows/calendar-view",
    "/api/calendar/maintenance",
]

_suffixes = [
    "",
    "/",
    "/counts",
    "/daily",
    "/daily-counts",
    "/daily_counts",
    "/days",
    "/per-day",
    "/per_day",
    "/summary",
    "/status",
    "/month",
    "/monthly",
]

_camera_suffixes = [
    "",
    "/counts",
    "/summary",
    "/status",
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
        _add(_p, _calendar_all)
    _add(f"{_prefix}/health", _health, methods=("GET",))
    for _suf in _camera_suffixes:
        _base1 = f"{_prefix}/cameras/{{camera_id}}" + (_suf or "")
        _base2 = f"{_prefix}/camera/{{camera_id}}" + (_suf or "")
        for _p in (_base1, _base2):
            _add(_p, _calendar_camera)

for _p in (
    "/api/cameras/{camera_id}/maintenance/calendar",
    "/api/cameras/{camera_id}/maintenance-calendar",
):
    _add(_p, _calendar_camera)


# Compatibility aliases.
maint_calendar_router = router
router_maint_calendar = router
calendar_router = router
maintenance_calendar_router = router
maint_calendar_api_router = router

__all__ = ["router"]
