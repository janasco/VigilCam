"""Recording stop-condition limits API router.

Exposes :mod:`app.services.rec_limits` (max-duration split, min-duration
discard gate, inactivity-stop) over HTTP.  Auto-registered by ``app.main``
(it imports every ``app.api`` module and includes ``router``), so no
``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["rec_limits"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"camera": payload}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "policy", "settings", "limits", "filter", "filters"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # Coerce numeric-looking strings for duration fields.
    for key in (
        "max_duration_seconds", "max_duration", "maxDurationSeconds", "maxDuration",
        "min_duration_seconds", "min_duration", "minDurationSeconds", "minDuration",
        "inactivity_timeout_seconds", "inactivity_timeout", "inactivityTimeoutSeconds",
        "inactivityTimeout", "inactivity_seconds", "inactive_seconds",
        "idle_seconds", "elapsed_seconds", "elapsed", "duration_seconds", "duration",
        "final_duration_seconds", "final_duration",
    ):
        if data.get(key) is not None and isinstance(data.get(key), str):
            s = str(data.get(key)).strip()
            low = s.lower()
            if low in ("none", "null", "disabled", "off", "auto", ""):
                data[key] = None
                continue
            try:
                data[key] = float(s) if "." in s else int(s)
            except (TypeError, ValueError):
                pass  # e.g. "5m" handled by the service coercer
    return data


def _svc():
    from app.services import rec_limits as mod

    return mod


async def _do_get(data: dict) -> dict:
    mod = _svc()
    return mod.get_limits(**data)


async def _do_set(data: dict) -> dict:
    mod = _svc()
    return mod.set_limits(**data)


async def _do_clear(data: dict) -> dict:
    mod = _svc()
    return mod.clear_limits(**data)


async def _do_evaluate(data: dict) -> dict:
    mod = _svc()
    return mod.evaluate(**data)


async def _get_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    camera: Optional[str] = Query(default=None),
    camera_name: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if camera is not None and data.get("camera") is None and data.get("camera_name") is None:
        data["camera"] = camera
    if camera_name is not None and data.get("camera") is None and data.get("camera_name") is None:
        data["camera"] = camera_name
    return await _do_get(data)


async def _set_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    camera: Optional[str] = Query(default=None),
    max_duration_seconds: Optional[str] = Query(default=None),
    min_duration_seconds: Optional[str] = Query(default=None),
    inactivity_timeout_seconds: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if camera is not None and data.get("camera") is None and data.get("camera_name") is None:
        data["camera"] = camera
    if max_duration_seconds is not None and data.get("max_duration_seconds") is None and data.get("max_duration") is None:
        data["max_duration_seconds"] = max_duration_seconds
    if min_duration_seconds is not None and data.get("min_duration_seconds") is None and data.get("min_duration") is None:
        data["min_duration_seconds"] = min_duration_seconds
    if inactivity_timeout_seconds is not None and data.get("inactivity_timeout_seconds") is None and data.get("inactivity_timeout") is None:
        data["inactivity_timeout_seconds"] = inactivity_timeout_seconds
    return await _do_set(data)


async def _clear_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    camera: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if camera is not None and data.get("camera") is None and data.get("camera_name") is None:
        data["camera"] = camera
    return await _do_clear(data)


async def _eval_ep(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    camera: Optional[str] = Query(default=None),
    elapsed_seconds: Optional[str] = Query(default=None),
    inactive_seconds: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if camera is not None and data.get("camera") is None and data.get("camera_name") is None:
        data["camera"] = camera
    if elapsed_seconds is not None and data.get("elapsed_seconds") is None and data.get("elapsed") is None and data.get("duration_seconds") is None:
        data["elapsed_seconds"] = elapsed_seconds
    if inactive_seconds is not None and data.get("inactive_seconds") is None and data.get("inactive") is None:
        data["inactive_seconds"] = inactive_seconds
    return await _do_evaluate(data)


_prefixes = [
    "/api/rec-limits",
    "/api/rec_limits",
    "/api/reclimits",
    "/api/recording-limits",
    "/api/recording_limits",
    "/api/recordings/limits",
    "/api/recordings/rec-limits",
    "/api/rec-limits-config",
]
_get_suffixes = ["", "/", "/get", "/list", "/all", "/show", "/status"]
_set_suffixes = ["/set", "/update", "/save", "/put", "/create", "/configure", "/config"]
_clear_suffixes = ["/clear", "/reset", "/purge", "/remove", "/delete"]
_eval_suffixes = [
    "/evaluate", "/check", "/should-stop", "/should_stop", "/shouldstop",
    "/stop-check", "/stop_check", "/split-check", "/discard-check",
    "/preview", "/test", "/decide",
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
    for _suf in _get_suffixes:
        if _suf in ("", "/"):
            continue
        _add(f"{_prefix}{_suf}", _get_ep)
    for _suf in _set_suffixes:
        _add(f"{_prefix}{_suf}", _set_ep)
    for _suf in _clear_suffixes:
        _add(f"{_prefix}{_suf}", _clear_ep)
    for _suf in _eval_suffixes:
        _add(f"{_prefix}{_suf}", _eval_ep)

# Bare-prefix conventions: GET -> fetch limits, POST -> set limits,
# plus explicit POST evaluate alias on the bare prefix is covered by suffixes.
for _prefix in _prefixes:
    _add(_prefix, _get_ep, methods=("GET",))
    _add(_prefix, _set_ep, methods=("POST",))
    _add(f"{_prefix}/", _get_ep, methods=("GET",))
    _add(f"{_prefix}/evaluate", _eval_ep, methods=("GET", "POST"))
