"""Auto-bookmark on alert API router -- toggle + explicit bookmark + backfill.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no
``app/main.py`` edits are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.alert_bookmark as bookmark_mod
from app.services.alert_bookmark import (
    backfill,
    bookmark_for_alert,
    disable,
    enable,
    get_status,
    set_config,
    set_enabled,
    toggle,
)

router = APIRouter(tags=["alert_bookmark"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"enabled": payload} if payload.strip().lower() in {
            "enable", "enabled", "on", "true", "disable", "disabled", "off", "false",
            "toggle", "switch", "flip",
        } else {"label": payload}
    elif isinstance(payload, bool):
        data = {"enabled": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"enabled": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, (list, tuple)):
        data = {"alerts": list(payload)}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {"enabled": str(payload)}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    vals = request.query_params.getlist(k)
                    data[k] = vals[0] if len(vals) == 1 else vals
        except Exception:  # noqa: BLE001
            pass
    for nested_key in ("alert_bookmark", "auto_bookmark", "autoBookmark", "config", "settings",
                       "payload", "options", "params", "data", "body", "request",
                       "alert", "event", "source_event"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _coerce_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _register(r: APIRouter, base: str) -> None:
    async def _get_status(request: Request):
        data = _normalize(None, request)
        if any(k in data for k in ("enabled", "enable", "active", "on")):
            return set_enabled(data)
        return get_status()

    async def _get_config(request: Request):
        return get_status()

    async def _enable(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        return enable(**data) if data else enable()

    async def _disable(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        return disable(**data) if data else disable()

    async def _toggle(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        if not data:
            return toggle()
        return toggle(**data)

    async def _configure(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        if not data:
            return get_status()
        config_keys = {"label_prefix", "labelPrefix", "prefix",
                       "default_offset_seconds", "defaultOffsetSeconds",
                       "default_offset", "default_camera_id", "defaultCameraId",
                       "default_recording_id", "defaultRecordingId",
                       "offset_seconds", "offset", "camera_id", "recording_id"}
        if any(k in data for k in config_keys):
            return set_config(**data)
        if any(k in data for k in ("enabled", "enable", "auto_bookmark", "active", "on")):
            return set_config(**data)
        return set_config(**data)

    async def _bookmark(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        if not data:
            raise HTTPException(status_code=400, detail="alert is required")
        # Explicit bookmark bypasses the toggle.
        call_data = dict(data)
        for _k in ("alert", "event", "source", "source_event"):
            if isinstance(call_data.get(_k), dict) and len(call_data) > 1:
                pass
        out = bookmark_for_alert(call_data, force=True)
        if not out.get("success", True):
            raise HTTPException(status_code=400, detail=str(out.get("error", "bookmark failed")))
        out.setdefault("success", True)
        out.setdefault("ok", True)
        return out

    async def _backfill(
        payload: Any = Body(default=None),
        request: Request = None,  # type: ignore[assignment]
        limit: Optional[int] = Query(default=None),
        dry_run: Optional[str] = Query(default=None),
        camera_id: Optional[str] = Query(default=None),
    ):
        data = _normalize(payload, request)
        if limit is not None:
            data.setdefault("limit", limit)
        if dry_run is not None:
            data.setdefault("dry_run", dry_run)
        if camera_id is not None:
            data.setdefault("camera_id", camera_id)
        try:
            from app.api.deps import db_session as _db_session  # noqa: E402
            from fastapi import Depends as _Depends  # noqa: E402
            _ = (_Depends, _db_session)
        except Exception:  # noqa: BLE001
            pass
        out = backfill(**data)
        return out

    _get_status.__name__ = f"alertbm_status_{abs(hash(base)) % 10_000_000}"
    _get_config.__name__ = f"alertbm_config_{abs(hash(base)) % 10_000_000}"
    _enable.__name__ = f"alertbm_enable_{abs(hash(base)) % 10_000_000}"
    _disable.__name__ = f"alertbm_disable_{abs(hash(base)) % 10_000_000}"
    _toggle.__name__ = f"alertbm_toggle_{abs(hash(base)) % 10_000_000}"
    _configure.__name__ = f"alertbm_configure_{abs(hash(base)) % 10_000_000}"
    _bookmark.__name__ = f"alertbm_bookmark_{abs(hash(base)) % 10_000_000}"
    _backfill.__name__ = f"alertbm_backfill_{abs(hash(base)) % 10_000_000}"

    r.get(base, summary="Get auto-bookmark on alert status")(_get_status)
    r.get(base + "/", include_in_schema=False)(_get_status)
    r.get(base + "/status", summary="Get auto-bookmark on alert status")(_get_status)
    r.get(base + "/config", summary="Get auto-bookmark config")(_get_config)
    r.get(base + "/settings", include_in_schema=False)(_get_config)
    r.post(base + "/enable", summary="Enable auto-bookmark on alert")(_enable)
    r.post(base + "/disable", summary="Disable auto-bookmark on alert")(_disable)
    r.post(base + "/toggle", summary="Toggle auto-bookmark on alert")(_toggle)
    r.post(base + "/config", summary="Configure auto-bookmark on alert")(_configure)
    r.put(base + "/config", summary="Configure auto-bookmark on alert")(_configure)
    r.patch(base + "/config", include_in_schema=False)(_configure)
    r.post(base, status_code=status.HTTP_200_OK, include_in_schema=False)(_configure)
    r.post(base + "/bookmark", summary="Bookmark one alert (explicit)")(_bookmark)
    r.post(base + "/create", include_in_schema=False)(_bookmark)
    r.post(base + "/auto-bookmark", include_in_schema=False)(_bookmark)
    r.post(base + "/alert", include_in_schema=False)(_bookmark)
    r.post(base + "/backfill", summary="Backfill bookmarks for alerts")(_backfill)


_BASES = (
    "/api/alert-bookmarks",
    "/api/alert_bookmarks",
    "/api/alerts/bookmarks",
    "/api/bookmarks/auto",
)
_seen: set = set()
for _base in _BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen:
        continue
    _seen.add(_norm)
    _register(router, _norm)


# Compatibility aliases.
alert_bookmark_router = router
auto_bookmark_router = router
alert_bookmarks_router = router
api_router = router

__all__ = ["router"]
