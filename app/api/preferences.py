"""User preferences API router (per-user get/set/update/reset).

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.preferences as prefs_mod
from app.services.preferences import (
    get_preferences,
    reset_preferences,
    set_preferences,
    update_preferences,
)

router = APIRouter(tags=["preferences"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"theme": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"theme": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {"theme": str(payload)}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass

    for nested_key in ("preferences", "prefs", "settings", "options", "config",
                       "params", "data", "body", "request", "fields"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _coerce_user_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    s = str(value).strip()
    return s if s else None


def _user_id_from_data(data: dict) -> Optional[str]:
    for key in ("user_id", "userId", "user", "username", "user_name",
                "uid", "owner", "account_id", "accountId", "id"):
        if key in data and data.get(key) is not None:
            uid = _coerce_user_id(data.get(key))
            if uid is not None:
                return uid
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in ("user_id", "userid", "user", "username", "uid", "id"):
        if key in lower and lower[key] is not None:
            uid = _coerce_user_id(lower[key])
            if uid is not None:
                return uid
    return None


def _require_user_id(value: Any) -> str:
    uid = _coerce_user_id(value)
    if uid is None:
        raise HTTPException(status_code=400, detail="user_id is required")
    return uid


def _strip_user_id(data: dict) -> dict:
    skip = {"user_id", "userid", "user", "username", "user_name",
            "uid", "owner", "account_id", "accountid", "id"}
    return {k: v for k, v in data.items() if k.lower() not in skip}


def _register_top_level(r: APIRouter, base: str) -> None:
    @r.get(base, summary="Get user preferences")
    @r.get(base + "/", include_in_schema=False)
    async def _get(
        request: Request,
        user_id: Optional[str] = Query(None),
    ):
        data = _normalize(None, request)
        uid = _coerce_user_id(user_id)
        if uid is None:
            uid = _user_id_from_data(data)
        _require_user_id(uid)
        assert uid is not None
        return get_preferences(uid)

    @r.put(base, status_code=status.HTTP_200_OK, summary="Replace user preferences")
    @r.put(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _put(
        payload: Any = Body(default=None),
        request: Request = None,  # type: ignore[assignment]
        user_id: Optional[str] = Query(None),
    ):
        data = _normalize(payload, request)
        if user_id is not None and _user_id_from_data(data) is None:
            data["user_id"] = user_id
        uid = _user_id_from_data(data)
        _require_user_id(uid)
        assert uid is not None
        prefs = _strip_user_id(data)
        return set_preferences(uid, prefs)

    @r.patch(base, status_code=status.HTTP_200_OK, summary="Update user preferences")
    @r.patch(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)
    @r.post(base, status_code=status.HTTP_200_OK, include_in_schema=False)
    @r.post(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _patch(
        payload: Any = Body(default=None),
        request: Request = None,  # type: ignore[assignment]
        user_id: Optional[str] = Query(None),
    ):
        data = _normalize(payload, request)
        if user_id is not None and _user_id_from_data(data) is None:
            data["user_id"] = user_id
        uid = _user_id_from_data(data)
        _require_user_id(uid)
        assert uid is not None
        prefs = _strip_user_id(data)
        return update_preferences(uid, prefs)

    @r.delete(base, summary="Reset user preferences to defaults")
    @r.delete(base + "/", include_in_schema=False)
    async def _delete(
        request: Request,
        user_id: Optional[str] = Query(None),
    ):
        data = _normalize(None, request)
        uid = _coerce_user_id(user_id)
        if uid is None:
            uid = _user_id_from_data(data)
        _require_user_id(uid)
        assert uid is not None
        return reset_preferences(uid)


def _register_user_scoped(r: APIRouter, base: str) -> None:
    @r.get(base, summary="Get preferences for user")
    async def _get_scoped(user_id: str):
        uid = _require_user_id(user_id)
        return get_preferences(uid)

    @r.put(base, status_code=status.HTTP_200_OK, summary="Replace preferences for user")
    async def _put_scoped(user_id: str, payload: Any = Body(default=None)):
        uid = _require_user_id(user_id)
        data = _normalize(payload, None)
        prefs = _strip_user_id(data)
        return set_preferences(uid, prefs)

    @r.patch(base, status_code=status.HTTP_200_OK, include_in_schema=False)
    @r.post(base, status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _patch_scoped(user_id: str, payload: Any = Body(default=None)):
        uid = _require_user_id(user_id)
        data = _normalize(payload, None)
        prefs = _strip_user_id(data)
        return update_preferences(uid, prefs)

    @r.delete(base, include_in_schema=False)
    async def _delete_scoped(user_id: str):
        uid = _require_user_id(user_id)
        return reset_preferences(uid)


_TOP_BASES = (
    "/api/preferences",
    "/api/user-preferences",
    "/api/user_preferences",
)
_seen_top: set = set()
for _base in _TOP_BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen_top:
        continue
    _seen_top.add(_norm)
    _register_top_level(router, _norm)


_USER_BASES = (
    "/api/users/{user_id}/preferences",
    "/api/user/{user_id}/preferences",
)
for _base in _USER_BASES:
    _register_user_scoped(router, _base)


# Compatibility aliases.
preferences_router = router
preference_router = router
user_preferences_router = router
user_prefs_router = router
api_router = router

__all__ = ["router"]
