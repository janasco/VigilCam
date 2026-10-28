"""Dashboard widgets config API router (per-user get/set/update/reset).

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

from app.services.widgets import (
    add_widget,
    get_widget,
    get_widgets,
    normalize_widget,
    remove_widget,
    reset_widgets,
    set_widgets,
    update_widget,
    update_widgets,
)

router = APIRouter(tags=["widgets"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"type": payload}
    elif isinstance(payload, list):
        data = {"widgets": list(payload)}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"type": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {"type": str(payload)}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass

    for nested_key in ("widget", "preferences", "prefs", "settings", "options",
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


def _widgets_from_data(data: dict) -> Optional[list]:
    for key in ("widgets", "items", "layout", "dashboard",
                "dashboard_widgets", "dashboardWidgets"):
        val = data.get(key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            for inner in val.values():
                if isinstance(inner, list):
                    return inner
    # single-widget payload
    has_widget_keys = any(
        k in data for k in ("type", "widget_type", "widgetType", "kind",
                            "widget_id", "widgetId", "title", "enabled",
                            "x", "y", "w", "h", "config")
    )
    if has_widget_keys:
        return [data]
    return None


def _register_top_level(r: APIRouter, base: str) -> None:
    @r.get(base, summary="Get dashboard widgets")
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
        return get_widgets(uid)

    @r.put(base, status_code=status.HTTP_200_OK, summary="Replace dashboard widgets")
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
        widgets = _widgets_from_data(data)
        if widgets is None:
            raise HTTPException(status_code=422, detail="widgets list is required")
        return set_widgets(uid, widgets)

    @r.patch(base, status_code=status.HTTP_200_OK, summary="Merge dashboard widgets")
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
        widgets = _widgets_from_data(data)
        if widgets is None:
            # single-widget upsert via POST/PATCH body
            widget = normalize_widget(data)
            return update_widgets(uid, [widget])
        return update_widgets(uid, widgets)

    @r.delete(base, summary="Reset dashboard widgets to defaults")
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
        return reset_widgets(uid)


def _register_user_scoped(r: APIRouter, base: str) -> None:
    @r.get(base, summary="Get widgets for user")
    async def _get_scoped(user_id: str):
        uid = _require_user_id(user_id)
        return get_widgets(uid)

    @r.put(base, status_code=status.HTTP_200_OK, summary="Replace widgets for user")
    async def _put_scoped(user_id: str, payload: Any = Body(default=None)):
        uid = _require_user_id(user_id)
        data = _normalize(payload, None)
        widgets = _widgets_from_data(data)
        if widgets is None:
            raise HTTPException(status_code=422, detail="widgets list is required")
        return set_widgets(uid, widgets)

    @r.patch(base, status_code=status.HTTP_200_OK, include_in_schema=False)
    @r.post(base, status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _patch_scoped(user_id: str, payload: Any = Body(default=None)):
        uid = _require_user_id(user_id)
        data = _normalize(payload, None)
        widgets = _widgets_from_data(data)
        if widgets is None:
            widget = normalize_widget(data)
            return update_widgets(uid, [widget])
        return update_widgets(uid, widgets)

    @r.delete(base, include_in_schema=False)
    async def _delete_scoped(user_id: str):
        uid = _require_user_id(user_id)
        return reset_widgets(uid)


def _register_item(r: APIRouter, base: str) -> None:
    @r.get(base, summary="Get single dashboard widget")
    async def _get_item(user_id: str, widget_id: str):
        uid = _require_user_id(user_id)
        widget = get_widget(uid, widget_id)
        if widget is None:
            raise HTTPException(status_code=404, detail="Widget not found")
        return widget

    @r.put(base, status_code=status.HTTP_200_OK, include_in_schema=False)
    @r.patch(base, status_code=status.HTTP_200_OK, include_in_schema=False)
    @r.post(base, status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _upsert_item(user_id: str, widget_id: str, payload: Any = Body(default=None)):
        uid = _require_user_id(user_id)
        if not str(widget_id).strip():
            raise HTTPException(status_code=400, detail="widget_id is required")
        data = _normalize(payload, None)
        data.setdefault("id", widget_id)
        existing = get_widget(uid, widget_id)
        if existing is None:
            return add_widget(uid, data)
        updated = update_widget(uid, widget_id, data)
        if updated is None:
            raise HTTPException(status_code=404, detail="Widget not found")
        return updated

    @r.delete(base, include_in_schema=False)
    async def _delete_item(user_id: str, widget_id: str):
        uid = _require_user_id(user_id)
        ok = remove_widget(uid, widget_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Widget not found")
        return get_widgets(uid)


_TOP_BASES = (
    "/api/widgets",
    "/api/dashboard/widgets",
    "/api/dashboard-widgets",
    "/api/dashboard_widgets",
)
_seen_top: set = set()
for _base in _TOP_BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen_top:
        continue
    _seen_top.add(_norm)
    _register_top_level(router, _norm)


_USER_BASES = (
    "/api/users/{user_id}/widgets",
    "/api/user/{user_id}/widgets",
)
for _base in _USER_BASES:
    _register_user_scoped(router, _base)

_ITEM_BASES = (
    "/api/users/{user_id}/widgets/{widget_id}",
    "/api/user/{user_id}/widgets/{widget_id}",
)
for _base in _ITEM_BASES:
    _register_item(router, _base)


# Compatibility aliases.
widgets_router = router
widget_router = router
dashboard_widgets_router = router
dashboard_router = router
api_router = router

__all__ = ["router"]
