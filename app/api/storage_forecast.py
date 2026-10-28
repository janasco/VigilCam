"""Storage forecast API router.

Exposes :class:`app.services.storage_forecast.StorageForecastService`
over HTTP. Each response carries current usage plus a forward-looking
projection (growth bytes/day, ``days_until_full``, projected full date,
per-camera history/forecast) so dashboards can render "storage full in
N days" without extra transformation.

Registered paths intentionally avoid colliding with the existing
``app.api.storage_analyzer`` (``/api/storage/*`` current-usage routes)
and ``app.api.storage_breakdown`` (``/api/storage-breakdown*`` trend
routes); this router uses ``forecast``/``projection``/``predict`` variants.
Auto-registered by ``app.main`` (it imports every ``app.api.*`` module
with a ``router`` attribute), so no ``app/main.py`` edit is needed.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["storage_forecast"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "filter", "filters", "forecast", "projection", "prediction"):
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
    # Camera name aliases.
    if data.get("camera_name") is None:
        for k in ("cameraName", "camera", "cam", "name", "camera_slug", "slug"):
            if data.get(k) is not None and not isinstance(data.get(k), dict):
                data["camera_name"] = data.get(k)
                break
    # Camera id aliases.
    if data.get("camera_id") is None:
        for k in ("cameraId", "camera-id", "cam_id", "camId"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    # Trend-window aliases.
    if data.get("days") is None:
        for k in ("window_days", "window", "trend_days", "trend_window", "range_days", "last_days", "ndays", "trendDays", "windowDays", "history_days", "lookback_days"):
            if data.get(k) is not None:
                data["days"] = data.get(k)
                break
    # Capacity aliases -> canonical capacity_bytes.
    if data.get("capacity_bytes") is None:
        for k in (
            "capacity", "total_capacity", "total_capacity_bytes", "disk_total",
            "disk_total_bytes", "disk_capacity", "filesystem_total", "quota",
            "quota_bytes", "max_bytes", "max_total_bytes", "max_size", "max",
            "limit", "limit_bytes", "allowed_bytes", "capacityBytes",
            "totalCapacity", "diskTotal", "quotaBytes", "maxBytes",
        ):
            if data.get(k) is not None:
                data["capacity_bytes"] = data.get(k)
                break
    # Growth-rate aliases -> canonical growth_bytes_per_day.
    if data.get("growth_bytes_per_day") is None:
        for k in (
            "growth_per_day", "daily_growth_bytes", "daily_growth", "bytes_per_day",
            "avg_bytes_per_day", "average_bytes_per_day", "growth_rate",
            "ingestion_rate", "ingestion_bytes_per_day", "growth",
        ):
            if data.get(k) is not None:
                data["growth_bytes_per_day"] = data.get(k)
                break
    return data


async def _do_forecast(data: dict) -> Any:
    from app.services.storage_forecast import StorageForecastService

    return StorageForecastService().forecast(**data)


async def _forecast_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    camera_name: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    path: Optional[str] = Query(default=None),
    days: Optional[int] = Query(default=None),
    capacity_bytes: Optional[str] = Query(default=None),
    growth_bytes_per_day: Optional[str] = Query(default=None),
    top_n: Optional[int] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    sort_by: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if camera_name is not None and data.get("camera_name") is None:
        data["camera_name"] = camera_name
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if path is not None and data.get("path") is None:
        data["path"] = path
    if days is not None and data.get("days") is None:
        data["days"] = days
    if capacity_bytes is not None and data.get("capacity_bytes") is None:
        data["capacity_bytes"] = capacity_bytes
    if growth_bytes_per_day is not None and data.get("growth_bytes_per_day") is None:
        data["growth_bytes_per_day"] = growth_bytes_per_day
    if top_n is not None and data.get("top_n") is None:
        data["top_n"] = top_n
    if limit is not None and data.get("limit") is None:
        data["limit"] = limit
    if sort_by is not None and data.get("sort_by") is None:
        data["sort_by"] = sort_by
    return await _do_forecast(data)


async def _forecast_camera(
    camera_id: Any,
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    path: Optional[str] = Query(default=None),
    days: Optional[int] = Query(default=None),
    capacity_bytes: Optional[str] = Query(default=None),
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
    if days is not None and data.get("days") is None:
        data["days"] = days
    if capacity_bytes is not None and data.get("capacity_bytes") is None:
        data["capacity_bytes"] = capacity_bytes
    return await _do_forecast(data)


_prefixes = [
    "/api/storage-forecast",
    "/api/storage_forecast",
    "/api/storage/forecast",
    "/api/storage/forecasts",
    "/api/storage/forecast-ui",
    "/api/storage/forecast/ui",
    "/api/storage/projection",
    "/api/storage/projections",
    "/api/storage/predict",
]

_suffixes = [
    "",
    "/",
    "/summary",
    "/status",
    "/forecast",
    "/predict",
    "/projection",
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
                operation_id=f"{handler.__name__}_forecast_{_op_counter['n']}",
            )
            _registered.add(key)
        except Exception:  # noqa: BLE001
            pass


for _prefix in _prefixes:
    for _suf in _suffixes:
        _p = _prefix if _suf in ("", "/") else f"{_prefix}{_suf}"
        _add(_p, _forecast_all)

for _p in (
    "/api/storage-forecast/cameras/{camera_id}",
    "/api/storage_forecast/cameras/{camera_id}",
    "/api/storage/forecast/cameras/{camera_id}",
    "/api/cameras/{camera_id}/storage-forecast",
    "/api/cameras/{camera_id}/storage_forecast",
    "/api/cameras/{camera_id}/storage/forecast",
):
    _add(_p, _forecast_camera)


# Compatibility aliases.
storage_forecast_router = router
router_storage_forecast = router
forecast_router = router
storage_forecast_api_router = router
projection_router = router
prediction_router = router

__all__ = ["router"]
