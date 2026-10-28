"""Event-rate anomaly API router (baseline vs current window).

Compares the baseline event rate against the recent current-window rate
and emits spike / quiet alerts. Distinct from ``app.api.event_stats``
(count aggregates, no spike logic) and ``app.api.incidents`` (spatial
multi-camera grouping, no rate logic).

Auto-registered by ``app.main`` (it imports every ``app.api`` module and
includes ``router``), so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query, Request

from app.api.deps import db_session

router = APIRouter(tags=["rate_anomaly"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"events": []}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (list, tuple)):
        data = {"events": list(payload)}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    vals = request.query_params.getlist(k)
                    raw = vals[0] if len(vals) == 1 else vals
                    # Coerce numeric query params.
                    if isinstance(raw, str):
                        try:
                            data[k] = float(raw)
                            continue
                        except (TypeError, ValueError):
                            pass
                    data[k] = raw
        except Exception:
            pass
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "filter", "filters", "rate", "anomaly",
                       "detection"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


async def _do_analyze(data: dict, db: Any = None) -> Any:
    from app.services import rate_anomaly as _svc

    # Inline events -> pure analysis (no DB needed).
    for k in ("events", "items", "rows", "detections", "alerts", "event_list"):
        if isinstance(data.get(k), (list, tuple)) and data.get(k):
            return _svc.analyze_rate(data.get(k), **{kk: vv for kk, vv in data.items() if kk != k})
    if db is None:
        kwargs = dict(data)
        events = kwargs.pop("events", kwargs.pop("items", []))
        if not isinstance(events, list):
            events = []
        return _svc.analyze_rate(events, **kwargs)
    try:
        return await _svc.get_rate_anomaly(db, **data)
    except Exception:
        kwargs = dict(data)
        kwargs.pop("db", None)
        events = kwargs.pop("events", kwargs.pop("items", []))
        if not isinstance(events, list):
            events = []
        return _svc.analyze_rate(events, **kwargs)


async def _analyze_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    db: Any = Depends(db_session),
    baseline_minutes: Optional[float] = Query(default=None),
    baseline_hours: Optional[float] = Query(default=None),
    current_minutes: Optional[float] = Query(default=None),
    current_seconds: Optional[float] = Query(default=None),
    spike_factor: Optional[float] = Query(default=None),
    quiet_factor: Optional[float] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
):
    data = _normalize(payload, request)
    if baseline_minutes is not None and data.get("baseline_minutes") is None:
        data["baseline_minutes"] = baseline_minutes
    if baseline_hours is not None and data.get("baseline_hours") is None:
        data["baseline_hours"] = baseline_hours
    if current_minutes is not None and data.get("current_minutes") is None:
        data["current_minutes"] = current_minutes
    if current_seconds is not None and data.get("current_seconds") is None:
        data["current_seconds"] = current_seconds
    if spike_factor is not None and data.get("spike_factor") is None:
        data["spike_factor"] = spike_factor
    if quiet_factor is not None and data.get("quiet_factor") is None:
        data["quiet_factor"] = quiet_factor
    if camera_id is not None and data.get("camera_id") is None:
        data["camera_id"] = camera_id
    if limit is not None and data.get("limit") is None:
        data["limit"] = limit
    session = db if hasattr(db, "execute") else None
    return await _do_analyze(data, session)


async def _analyze_camera(
    camera_id: Any,
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    db: Any = Depends(db_session),
):
    data = _normalize(payload, request)
    try:
        data.setdefault("camera_id", int(camera_id))
    except (TypeError, ValueError):
        if data.get("camera_name") is None:
            data["camera_name"] = str(camera_id)
        data.setdefault("camera_id", camera_id)
    session = db if hasattr(db, "execute") else None
    return await _do_analyze(data, session)


_prefixes = [
    "/api/rate_anomaly",
    "/api/rate-anomaly",
    "/api/rateanomaly",
    "/api/event_rate_anomaly",
    "/api/event-rate-anomaly",
    "/api/event_rate",
    "/api/event-rate",
    "/api/events/rate-anomaly",
    "/api/events/rate_anomaly",
    "/api/analytics/rate-anomaly",
    "/api/analytics/rate_anomaly",
    "/api/analytics/event-rate",
]

_suffixes = [
    "",
    "/",
    "/status",
    "/detect",
    "/detection",
    "/analyze",
    "/analysis",
    "/check",
    "/evaluate",
    "/summary",
]

_camera_suffixes = [
    "",
    "/status",
    "/detect",
    "/analyze",
    "/check",
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
        except Exception:
            pass


for _prefix in _prefixes:
    for _suf in _suffixes:
        _p = _prefix if _suf in ("", "/") else f"{_prefix}{_suf}"
        _add(_p, _analyze_all)
    for _suf in _camera_suffixes:
        _base1 = f"{_prefix}/cameras/{{camera_id}}" + (_suf or "")
        _base2 = f"{_prefix}/camera/{{camera_id}}" + (_suf or "")
        for _p in (_base1, _base2):
            _add(_p, _analyze_camera)


# Side-effect registration so the router is live even when imported
# after startup (mirrors app.api.resource_watch).
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    try:
        if id(router) not in _ids:
            _main_app.include_router(router)
            _ids = _ids | {id(router)}
    except Exception:
        pass
    try:
        _main_app._registered_router_ids = _ids
    except Exception:
        pass
except Exception:
    pass


# Compatibility aliases.
rate_anomaly_router = router
router_rate_anomaly = router
rateanomaly_router = router
event_rate_router = router
api_router = router

__all__ = ["router"]
