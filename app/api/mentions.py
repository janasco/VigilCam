"""@mention notifications API router (parsing + per-user inboxes).

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` or ``app/templates/index.html`` are required.

Notes themselves stay plain text (see ``app.services.event_notes`` and
``app.services.camera_notes``); this router only *reads* note text and
records ``@username`` notifications via ``app.services.mentions``.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.mentions as mentions_mod
from app.services.mentions import (
    clear,
    delete_notification,
    get_notification,
    list_notifications,
    mark_all_read,
    mark_read,
    notify_for_camera_note,
    notify_for_event_note,
    notify_for_text,
    parse_mentions,
    unread_count,
)

router = APIRouter(tags=["mentions"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"text": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"text": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {"text": str(payload)}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass
    for nested_key in ("note", "comment", "payload", "options", "config",
                       "params", "data", "body", "request", "fields",
                       "notification", "mention"):
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


def _note_or_404(notif: Optional[dict]) -> dict:
    if notif is None:
        raise HTTPException(status_code=404, detail="Mention notification not found")
    return notif


def _extract_text(data: dict) -> str:
    for k in ("text", "body", "content", "message", "note", "comment",
              "description", "summary", "value", "title"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if v is not None and not isinstance(v, str):
            s = str(v).strip()
            if s:
                return s
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in ("text", "body", "content", "message", "note", "comment", "title"):
        v = lower.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if v is not None and not isinstance(v, str):
            s = str(v).strip()
            if s:
                return s
    return ""


def _register(r: APIRouter, base: str) -> None:
    tag = base.replace("/", "_")

    @r.get(base, operation_id=f"mentions_list{tag}")
    async def _list(
        request: Request,
        user: Optional[str] = Query(None),
        username: Optional[str] = Query(None),
        author: Optional[str] = Query(None),
        note_id: Optional[int] = Query(None),
        note_type: Optional[str] = Query(None),
        event_id: Optional[int] = Query(None),
        camera_id: Optional[str] = Query(None),
        unread_only: Optional[bool] = Query(None),
        read: Optional[bool] = Query(None),
        limit: Optional[int] = Query(None),
        offset: Optional[int] = Query(None),
        q: Optional[str] = Query(None),
    ):
        params: dict = {}
        u = user or username
        if u is None:
            try:
                for alias in ("user", "username", "mentioned", "mentioned_user", "recipient", "to"):
                    if alias in request.query_params:
                        u = request.query_params.get(alias)
                        break
            except Exception:
                pass
        if u is not None:
            params["user"] = u
        if author is not None:
            params["author"] = author
        if note_id is not None:
            params["note_id"] = note_id
        if note_type is not None:
            params["note_type"] = note_type
        if event_id is not None:
            params["event_id"] = event_id
        if camera_id is not None:
            params["camera_id"] = camera_id
        if unread_only is not None:
            params["unread_only"] = unread_only
        if read is not None:
            params["read"] = read
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if q is not None:
            params["q"] = q
        try:
            for k, v in request.query_params.items():
                params.setdefault(k, v)
        except Exception:
            pass
        return list_notifications(**params)

    @r.get(base + "/unread-count", operation_id=f"mentions_unread{tag}")
    async def _unread_count(request: Request, user: Optional[str] = Query(None)):
        params: dict = {}
        u = user
        if u is None:
            try:
                for alias in ("user", "username", "mentioned", "mentioned_user", "recipient", "to"):
                    if alias in request.query_params:
                        u = request.query_params.get(alias)
                        break
            except Exception:
                pass
        if u is not None:
            params["user"] = u
        try:
            for k, v in request.query_params.items():
                params.setdefault(k, v)
        except Exception:
            pass
        n = unread_count(**params)
        total = len(list_notifications(**{k: v for k, v in params.items() if k != "unread_only"})) if params.get("user") is not None else len(list_notifications())
        return {"ok": True, "success": True, "user": u, "unread": n, "unread_count": n, "count": n, "total": total}

    @r.get(base + "/{notif_id}", operation_id=f"mentions_get{tag}")
    async def _get(notif_id: int):
        nid = _coerce_int_or_none(notif_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="notification id is required")
        return _note_or_404(get_notification(nid))

    @r.post(base + "/parse", operation_id=f"mentions_parse{tag}")
    async def _parse(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        text = _extract_text(data)
        users = parse_mentions(text)
        return {"ok": True, "success": True, "text": text, "mentions": users,
                "users": users, "usernames": users, "count": len(users)}

    @r.post(base + "/notify", operation_id=f"mentions_notify{tag}")
    @r.post(base + "/scan", operation_id=f"mentions_scan{tag}", include_in_schema=False)
    @r.post(base + "/check", operation_id=f"mentions_check{tag}", include_in_schema=False)
    async def _notify(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        text = _extract_text(data)
        if not text.strip():
            raise HTTPException(status_code=400, detail="text/body is required")
        created = notify_for_text(data)
        users = parse_mentions(text)
        return {"ok": True, "success": True, "mentions": users, "users": users,
                "notifications": created, "items": created, "count": len(created)}

    @r.post(base + "/event-note", operation_id=f"mentions_event_note{tag}")
    async def _notify_event(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        text = _extract_text(data)
        if not text.strip():
            raise HTTPException(status_code=400, detail="text/body is required")
        created = notify_for_event_note(data)
        users = parse_mentions(text)
        return {"ok": True, "success": True, "mentions": users, "users": users,
                "notifications": created, "items": created, "count": len(created)}

    @r.post(base + "/camera-note", operation_id=f"mentions_camera_note{tag}")
    async def _notify_camera(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        text = _extract_text(data)
        if not text.strip():
            raise HTTPException(status_code=400, detail="text/body is required")
        created = notify_for_camera_note(data)
        users = parse_mentions(text)
        return {"ok": True, "success": True, "mentions": users, "users": users,
                "notifications": created, "items": created, "count": len(created)}

    @r.post(base + "/{notif_id}/read", operation_id=f"mentions_read{tag}")
    async def _mark_read(notif_id: int):
        nid = _coerce_int_or_none(notif_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="notification id is required")
        updated = mark_read(nid)
        return _note_or_404(updated)

    @r.post(base + "/read", operation_id=f"mentions_read_all{tag}")
    async def _mark_all_read(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.pop("text", None)
        data.pop("body", None)
        # Only keep filter keys; empty data marks everything.
        filters = {k: v for k, v in data.items() if v is not None}
        n = mark_all_read(**filters) if filters else mark_all_read()
        return {"ok": True, "success": True, "marked": n, "count": n}

    @r.delete(base + "/{notif_id}", operation_id=f"mentions_delete{tag}")
    async def _delete(notif_id: int):
        nid = _coerce_int_or_none(notif_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="notification id is required")
        ok = delete_notification(nid)
        if not ok:
            raise HTTPException(status_code=404, detail="Mention notification not found")
        return {"success": True, "ok": True, "deleted": [int(nid)], "id": int(nid)}

    @r.delete(base, operation_id=f"mentions_clear{tag}", include_in_schema=False)
    async def _clear(request: Request):
        params: dict = {}
        try:
            for k, v in request.query_params.items():
                params[k] = v
        except Exception:
            pass
        if params:
            # Filtered clear: delete matching only.
            targets = list_notifications(**params)
            n = 0
            for t in targets:
                if delete_notification(t["id"]):
                    n += 1
            return {"success": True, "ok": True, "deleted_count": n, "count": n}
        n = clear()
        return {"success": True, "ok": True, "deleted_count": n, "count": n}


_BASES = (
    "/api/mentions",
    "/api/mention",
)
_seen: set = set()
for _base in _BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen:
        continue
    _seen.add(_norm)
    _register(router, _norm)


# Compatibility aliases.
mentions_router = router
mention_router = router
api_router = router
notifications_router = router

__all__ = ["router", "mentions_router", "mention_router"]
