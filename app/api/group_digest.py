"""Per-group notification digest router (auto-loaded by ``app.main``).

No edits to ``app/main.py`` are needed: this module exposes ``router``
and the pkgutil auto-loader in ``app.main`` picks it up.

Endpoints (tolerant of alias keys and query/body placement):

- ``POST /api/group_digest/record`` (also ``/notify``/``/add``/``/push``/``/send``)
- ``POST /api/group_digest/build`` (also ``/summary``/``/digest``/``/summarize``/``/generate``)
  with ``{"group": "<name>"}`` -- single-group digest
- ``POST /api/group_digest/build-all`` -- one digest per group
- ``GET /api/group_digest/summary?group=<name>`` -- single-group digest
- ``GET /api/group_digest/groups`` -- distinct groups
- ``GET /api/group_digest/status`` / ``/health``
- ``GET /api/group_digest/{group}`` -- digest for a named group

Hyphen/underscore and ``/api/groups/digest`` style aliases are registered
for every endpoint.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
import app.services.group_digest as group_digest_service_mod
from app.services.group_digest import (
    build_all_digests,
    build_digest,
    clear,
    get_group_counts,
    list_groups,
    list_notifications,
    record_notification,
    status as service_status,
)

router = APIRouter(tags=["group_digest"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"group": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"message": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            data = {}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "message", "notification", "event"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


_PATHS = (
    "/api/group_digest",
    "/api/group-digest",
    "/api/groupDigest",
    "/api/groups/digest",
    "/api/groups_digest",
    "/api/groups-digest",
    "/api/notification_group_digest",
    "/api/notification-group-digest",
    "/api/notify_group_digest",
    "/api/digest/group",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_root(request: Request):
        return build_all_digests(**_normalize(None, request))

    @r.get(base + "/")
    async def get_slash(request: Request):
        return await get_root(request)

    @r.get(base + "/summary")
    async def get_summary(request: Request):
        return build_digest(**_normalize(None, request))

    @r.get(base + "/digest")
    async def get_digest(request: Request):
        return await get_summary(request)

    @r.get(base + "/summarize")
    async def get_summarize(request: Request):
        return await get_summary(request)

    @r.get(base + "/build")
    async def get_build(request: Request):
        return await get_summary(request)

    @r.get(base + "/build-all")
    async def get_build_all(request: Request):
        return build_all_digests(**_normalize(None, request))

    @r.get(base + "/build_all")
    async def get_build_all_us(request: Request):
        return await get_build_all(request)

    @r.get(base + "/groups")
    async def get_groups(request: Request):
        groups = list_groups(**_normalize(None, request))
        counts = get_group_counts(**_normalize(None, request))
        return {
            "ok": True,
            "success": True,
            "status": "ok",
            "groups": groups,
            "group_names": groups,
            "counts": counts,
            "counts_by_group": counts,
            "by_group": counts,
            "count": len(groups),
            "total_groups": len(groups),
        }

    @r.get(base + "/list")
    async def get_list(request: Request):
        items = list_notifications(**_normalize(None, request))
        return {"ok": True, "success": True, "status": "ok", "items": items, "notifications": items, "count": len(items), "total": len(items)}

    @r.get(base + "/notifications")
    async def get_notifications(request: Request):
        return await get_list(request)

    @r.get(base + "/items")
    async def get_items(request: Request):
        return await get_list(request)

    @r.get(base + "/status")
    async def get_status(request: Request):
        return service_status()

    @r.get(base + "/health")
    async def get_health(request: Request):
        return {"ok": True, "service": "group_digest"}

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_root(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        # If payload looks like a digest-build request, build; otherwise record.
        build_keys = {"build", "summarize", "summary", "digest", "generate", "limit", "since", "until", "filter_type", "build_all", "build-all"}
        group_filter_keys = {"group", "group_id", "group_name", "groups", "group_ids"}
        has_build_hint = any(k in data for k in build_keys)
        has_group_only = any(k in data for k in group_filter_keys) and not any(
            k in data for k in ("message", "body", "text", "title", "type", "event_type", "severity", "camera_id", "camera", "channel")
        )
        if has_build_hint or has_group_only:
            # build-all when explicitly requested or plural groups supplied
            if data.get("build_all") or data.get("build-all") or isinstance(data.get("groups"), list):
                try:
                    # Single group in list of one -> single digest; else all.
                    groups_val = data.get("groups")
                    if isinstance(groups_val, list) and len(groups_val) == 1 and not has_build_hint:
                        result = build_digest(**data)
                    elif "group" in data or "group_id" in data or "group_name" in data:
                        result = build_digest(**data)
                    else:
                        result = build_all_digests(**data)
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(status_code=400, detail=str(exc))
            else:
                try:
                    result = build_digest(**data)
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(status_code=400, detail=str(exc))
        else:
            try:
                result = record_notification(**data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/")
    async def post_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/record")
    async def post_record(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = record_notification(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "recorded", "success": True, "ok": True}
        result.setdefault("status", "recorded")
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/notify")
    async def post_notify(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_record(payload, request, db)

    @r.post(base + "/add")
    async def post_add(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_record(payload, request, db)

    @r.post(base + "/push")
    async def post_push(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_record(payload, request, db)

    @r.post(base + "/send")
    async def post_send(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_record(payload, request, db)

    @r.post(base + "/build")
    async def post_build(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = build_digest(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post(base + "/summary")
    async def post_summary(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_build(payload, request, db)

    @r.post(base + "/digest")
    async def post_digest(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_build(payload, request, db)

    @r.post(base + "/summarize")
    async def post_summarize(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_build(payload, request, db)

    @r.post(base + "/generate")
    async def post_generate(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_build(payload, request, db)

    @r.post(base + "/build-all")
    async def post_build_all(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = build_all_digests(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "completed", "success": True, "ok": True}
        result.setdefault("status", "completed")
        result.setdefault("success", True)
        result.setdefault("ok", True)
        return result

    @r.post(base + "/build_all")
    async def post_build_all_us(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_build_all(payload, request, db)

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return clear()

    @r.delete(base + "/clear")
    async def delete_clear(request: Request):
        return clear()

    @r.get(base + "/{group}")
    async def get_one_group(group: str, request: Request):
        # Named-group digest; falls back to notification lookup for compatibility.
        digest = build_digest(group=group, **_normalize(None, request))
        # _normalize already merged query params; ensure path group wins.
        if digest.get("total", 0) == 0:
            res = group_digest_service_mod.get_notification(group)
            if res is not None:
                return res
        return digest


for base in _PATHS:
    _register(router, base)
