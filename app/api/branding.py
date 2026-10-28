"""White-label branding settings API router.

Serves the global branding record managed by :mod:`app.services.branding`
(site name, logo URL, colors). Auto-registered by the ``pkgutil``
auto-loader in ``app/main.py``, so no edits to ``app/main.py`` or
``app/templates/index.html`` are required.
"""

from __future__ import annotations

from typing import Any, List

from fastapi import APIRouter, Body, HTTPException, Request, status

import app.services.branding as svc
from app.services.branding import (
    get_branding,
    reset_branding,
    set_branding,
    update_branding,
)

router = APIRouter(tags=["branding"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"site_name": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"site_name": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {"site_name": str(payload)}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass

    for nested_key in (
        "branding", "brand", "white_label", "whiteLabel", "whitelabel",
        "settings", "options", "config", "params", "data", "body",
        "request", "fields", "payload", "colors",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    colors = data.get("colors")
    if isinstance(colors, dict):
        for k, v in colors.items():
            data.setdefault(k, v)
    return data


def _apply(fn, data: dict) -> dict:
    try:
        return fn(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _register(r: APIRouter, base: str) -> None:
    @r.get(base, summary="Get branding settings")
    @r.get(base + "/", include_in_schema=False)
    async def _get():
        return get_branding()

    @r.put(base, status_code=status.HTTP_200_OK, summary="Replace branding settings")
    @r.put(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _put(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        return _apply(set_branding, data)

    @r.patch(base, status_code=status.HTTP_200_OK, summary="Update branding settings")
    @r.patch(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)
    @r.post(base, status_code=status.HTTP_200_OK, include_in_schema=False)
    @r.post(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _patch(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        return _apply(update_branding, data)

    @r.delete(base, summary="Reset branding settings to defaults")
    @r.delete(base + "/", include_in_schema=False)
    async def _delete():
        return reset_branding()


_BASES: List[str] = [
    "/api/branding",
    "/api/brand",
    "/api/white-label",
    "/api/whitelabel",
    "/api/site-branding",
]
_seen: set = set()
for _base in _BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen:
        continue
    _seen.add(_norm)
    _register(router, _norm)


# Compatibility aliases.
branding_router = router
brand_router = router
white_label_router = router
whitelabel_router = router
api_router = router

__all__ = ["router"]
