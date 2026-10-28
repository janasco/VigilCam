"""Escalation-chain (time-based escalation policy) API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request, status

import app.services.escalation as svc
from app.services.escalation import (
    clear,
    create_policy,
    delete_policy,
    due_for_event,
    evaluate,
    get_policy,
    list_escalations,
    list_policies,
    list_tracked,
    process_due,
    track_event,
    untrack_event,
    update_policy,
)

router = APIRouter(tags=["escalation"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        s = payload.strip()
        if s:
            data = {"policy_id": s}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"policy_id": bytes(payload).decode("utf-8", errors="replace")}
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

    for nested in ("payload", "options", "config", "params", "data", "body",
                   "request", "fields", "policy", "escalation", "chain", "rule"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    return data


def _policy_id_from(data: dict, fallback: Any = None) -> Optional[str]:
    for key in ("policy_id", "policyId", "id", "policy", "key"):
        if data.get(key) is not None and str(data.get(key)).strip():
            return str(data.get(key)).strip()
    if data.get("name") is not None and str(data.get("name")).strip():
        return str(data.get("name")).strip()
    if fallback is not None and str(fallback).strip():
        return str(fallback).strip()
    return None


def _coerce_int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


_PATHS = (
    "/api/escalation",
    "/api/escalations",
    "/api/escalation-chains",
    "/api/alerts/escalation",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base, summary="List escalation policies")
    async def get_root(request: Request):
        data = _normalize(None, request)
        pid = _policy_id_from(data)
        if pid is not None:
            res = get_policy(pid)
            if res is None:
                raise HTTPException(status_code=404, detail="policy not found")
            return res
        if data.get("event_id") is not None or data.get("eventId") is not None:
            return {"ok": True, "success": True, "items": due_for_event(**data),
                    "due": due_for_event(**data)}
        items = list_policies(**({"enabled": data["enabled"]} if "enabled" in data else {}))
        return {"ok": True, "success": True, "items": items, "policies": items,
                "count": len(items), "total": len(items)}

    @r.get(base + "/")
    async def get_slash(request: Request):
        return await get_root(request)

    @r.get(base + "/policies", summary="List escalation policies")
    async def get_policies(request: Request):
        data = _normalize(None, request)
        items = list_policies(**({"enabled": data["enabled"]} if "enabled" in data else {}))
        return {"ok": True, "success": True, "items": items, "policies": items,
                "count": len(items), "total": len(items)}

    @r.post(base + "/policies", status_code=status.HTTP_200_OK, summary="Create escalation policy")
    async def post_policy(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            result = create_policy(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_root(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        action = str(data.get("action", "")).lower()
        if action in ("clear", "reset", "purge"):
            return clear()
        if action in ("evaluate", "check", "scan", "due"):
            items = evaluate(**data)
            return {"ok": True, "success": True, "items": items, "due": items,
                    "count": len(items)}
        if action in ("process", "run", "fire", "trigger", "dispatch"):
            items = process_due(**data)
            return {"ok": True, "success": True, "items": items, "fired": items,
                    "count": len(items)}
        if action in ("track", "watch", "register"):
            try:
                result = track_event(**data)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            result.setdefault("ok", True)
            result.setdefault("success", True)
            return result
        # default: create a policy
        try:
            result = create_policy(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.get(base + "/policies/{policy_id}", summary="Get escalation policy")
    async def get_one_policy(policy_id: str):
        res = get_policy(policy_id)
        if res is None:
            raise HTTPException(status_code=404, detail="policy not found")
        return res

    @r.put(base + "/policies/{policy_id}", summary="Update escalation policy")
    async def put_policy(policy_id: str, payload: Any = Body(default=None),
                         request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("policy_id", policy_id)
        try:
            result = update_policy(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.patch(base + "/policies/{policy_id}", summary="Patch escalation policy")
    async def patch_policy(policy_id: str, payload: Any = Body(default=None),
                           request: Request = None):
        return await put_policy(policy_id, payload, request)

    @r.post(base + "/policies/{policy_id}", summary="Update escalation policy by action")
    async def post_one_policy(policy_id: str, payload: Any = Body(default=None),
                              request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("policy_id", policy_id)
        action = str(data.get("action", "")).lower()
        if action in ("delete", "remove"):
            ok = delete_policy(policy_id)
            if not ok:
                raise HTTPException(status_code=404, detail="policy not found")
            return {"ok": True, "success": True, "status": "deleted",
                    "policy_id": policy_id, "id": policy_id}
        if action in ("enable", "activate", "on"):
            data["enabled"] = True
        elif action in ("disable", "deactivate", "off"):
            data["enabled"] = False
        try:
            result = update_policy(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.delete(base + "/policies/{policy_id}", summary="Delete escalation policy")
    async def delete_one_policy(policy_id: str):
        ok = delete_policy(policy_id)
        if not ok:
            raise HTTPException(status_code=404, detail="policy not found")
        return {"ok": True, "success": True, "status": "deleted",
                "policy_id": policy_id, "id": policy_id}

    @r.post(base + "/track", status_code=status.HTTP_200_OK, summary="Track newly-raised event")
    async def post_track(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            result = track_event(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/untrack", status_code=status.HTTP_200_OK)
    async def post_untrack(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        eid = _coerce_int_or_none(data.get("event_id", data.get("eventId", data.get("id"))))
        if eid is None:
            raise HTTPException(status_code=400, detail="event_id is required")
        ok = untrack_event(eid)
        return {"ok": True, "success": True, "untracked": ok, "event_id": eid}

    @r.get(base + "/tracked", summary="List tracked events")
    async def get_tracked(request: Request):
        return {"ok": True, "success": True, "items": list_tracked(),
                "tracked": list_tracked()}

    @r.get(base + "/due", summary="Evaluate due escalations (read-only)")
    async def get_due(request: Request, event_id: Optional[int] = None,
                      now: Optional[str] = None):
        data = _normalize(None, request)
        if event_id is not None:
            data["event_id"] = event_id
        if now is not None:
            data["now"] = now
        items = evaluate(**data)
        return {"ok": True, "success": True, "items": items, "due": items,
                "count": len(items)}

    @r.post(base + "/evaluate", status_code=status.HTTP_200_OK, summary="Evaluate due escalations")
    async def post_evaluate(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        items = evaluate(**data)
        return {"ok": True, "success": True, "items": items, "due": items,
                "count": len(items)}

    @r.post(base + "/process", status_code=status.HTTP_200_OK, summary="Fire due escalations")
    async def post_process(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        items = process_due(**data)
        return {"ok": True, "success": True, "items": items, "fired": items,
                "count": len(items)}

    @r.post(base + "/run", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def post_run(payload: Any = Body(default=None), request: Request = None):
        return await post_process(payload, request)

    @r.get(base + "/history", summary="List fired escalation history")
    async def get_history(request: Request):
        data = _normalize(None, request)
        items = list_escalations(**data)
        return {"ok": True, "success": True, "items": items, "history": items,
                "fired": items, "count": len(items)}

    @r.get(base + "/status", summary="Escalation service status")
    async def get_status(request: Request):
        return svc.status(**_normalize(None, request))

    @r.get(base + "/health")
    async def get_health():
        return {"ok": True, "success": True, "service": "escalation"}

    @r.post(base + "/clear", status_code=status.HTTP_200_OK)
    async def post_clear(payload: Any = Body(default=None), request: Request = None):
        return clear(**_normalize(payload, request))

    @r.delete(base + "/clear")
    async def delete_clear(request: Request):
        return clear(**_normalize(None, request))


for _base in _PATHS:
    _register(router, _base)


# Compatibility aliases.
escalation_router = router
escalations_router = router
escalation_chain_router = router
escalation_chains_router = router
api_router = router

__all__ = ["router"]
