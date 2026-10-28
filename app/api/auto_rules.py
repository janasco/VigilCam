"""Auto event rules API router (zmfilter-style condition -> action rules).

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no
``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.auto_rules as rules_mod
from app.services.auto_rules import (
    create_rule,
    delete_rule,
    evaluate_rule,
    evaluate_rules,
    get_rule,
    list_rules,
    run_rules,
    update_rule,
)

router = APIRouter(tags=["auto_rules"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"name": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"name": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {"name": str(payload)}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass

    for nested_key in ("rule", "auto_rule", "autoRule", "filter", "filters",
                       "payload", "options", "config", "params", "data", "body",
                       "request", "fields"):
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


def _rule_or_404(item: Optional[dict], rule_id: Any = None) -> dict:
    if item is None:
        raise HTTPException(status_code=404, detail="Auto rule not found")
    return item


def _resolve_id(rule_id: Any) -> Any:
    if isinstance(rule_id, str):
        s = rule_id.strip()
        if not s:
            raise HTTPException(status_code=400, detail="rule_id is required")
        nid = _coerce_int_or_none(s)
        return nid if nid is not None else s
    if rule_id is None or isinstance(rule_id, bool):
        raise HTTPException(status_code=400, detail="rule_id is required")
    return rule_id


def _create_from_data(data: dict) -> dict:
    try:
        return create_rule(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _extract_events(data: dict) -> list:
    for key in ("events", "event_list", "eventList", "items", "matches", "candidates"):
        val = data.get(key)
        if isinstance(val, list):
            return val
    # A single inline event dict.
    for key in ("event", "item", "candidate"):
        val = data.get(key)
        if isinstance(val, dict):
            return [val]
    return []


def _dry_run_from(data: dict, request: Optional[Request] = None) -> bool:
    for key in ("dry_run", "dryRun", "dry-run", "dry", "preview", "simulate"):
        if key in data and data.get(key) is not None:
            v = data.get(key)
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    if request is not None:
        try:
            for key in ("dry_run", "dryRun", "dry", "preview"):
                if key in request.query_params:
                    return str(request.query_params.get(key)).strip().lower() in {"1", "true", "yes", "y", "on"}
        except Exception:
            pass
    return False


def _register(r: APIRouter, base: str) -> None:
    @r.get(base, summary="List auto event rules")
    async def _list(
        request: Request,
        q: Optional[str] = Query(None),
        name: Optional[str] = Query(None),
        enabled: Optional[str] = Query(None),
        limit: Optional[int] = Query(None),
        offset: Optional[int] = Query(None),
        skip: Optional[int] = Query(None),
    ):
        params: dict = {}
        if q is not None:
            params["q"] = q
        if name is not None:
            params["name"] = name
        if enabled is not None:
            params["enabled"] = enabled
        if limit is not None:
            params["limit"] = limit
        params["offset"] = offset if offset is not None else (skip if skip is not None else 0)
        try:
            for k, v in request.query_params.items():
                params.setdefault(k, v)
        except Exception:
            pass
        return list_rules(**params)

    @r.get(base + "/", include_in_schema=False)
    async def _list_slash(request: Request, q: Optional[str] = Query(None),
                          name: Optional[str] = Query(None)):
        return await _list(request, q, name, None, None, None, None)

    @r.post(base, status_code=status.HTTP_200_OK, summary="Create auto event rule")
    async def _create(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        return _create_from_data(data)

    @r.post(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _create_slash(payload: Any = Body(default=None), request: Request = None):
        return await _create(payload, request)

    @r.post(base + "/create", include_in_schema=False)
    async def _create_named(payload: Any = Body(default=None), request: Request = None):
        return await _create(payload, request)

    @r.post(base + "/evaluate", summary="Evaluate auto rules against events (dry-run)")
    @r.post(base + "/preview", include_in_schema=False)
    @r.post(base + "/run", include_in_schema=False)
    @r.post(base + "/check", include_in_schema=False)
    async def _evaluate_all(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        events = _extract_events(data)
        # Allow rule scoping: {"rule_id": N} or {"name": "..."}.
        scope = None
        rid = data.get("rule_id", data.get("ruleId", data.get("filter_id", data.get("filterId"))))
        if rid is not None or data.get("name"):
            scope = get_rule(rid if rid is not None else data.get("name"))
            if scope is None:
                raise HTTPException(status_code=404, detail="Auto rule not found")
        dry = _dry_run_from(data, request)
        # Default evaluate endpoints to dry-run reporting unless caller opts out.
        if not any(k in data for k in ("dry_run", "dryRun", "dry-run", "dry", "preview", "simulate")):
            dry = True
        if scope is not None:
            return evaluate_rule(scope, events, dry_run=dry)
        return run_rules(events, dry_run=dry)

    @r.get(base + "/{rule_id}", summary="Get auto event rule")
    async def _get(rule_id: str):
        fid = _resolve_id(rule_id)
        return _rule_or_404(get_rule(fid), fid)

    @r.get(base + "/{rule_id}/query", summary="Get auto rule conditions")
    async def _get_query(rule_id: str):
        fid = _resolve_id(rule_id)
        item = _rule_or_404(get_rule(fid), fid)
        conditions = item.get("conditions") or item.get("criteria") or {}
        terms = item.get("terms") or []
        actions = item.get("actions") or {}
        return {"rule": item, "filter": item, "conditions": conditions,
                "criteria": conditions, "terms": terms, "query": conditions,
                "actions": actions}

    @r.get(base + "/{rule_id}/apply", include_in_schema=False)
    async def _apply(rule_id: str):
        return await _get_query(rule_id)

    @r.post(base + "/{rule_id}/evaluate", summary="Evaluate one auto rule (dry-run)")
    @r.post(base + "/{rule_id}/preview", include_in_schema=False)
    @r.post(base + "/{rule_id}/run", include_in_schema=False)
    @r.post(base + "/{rule_id}/check", include_in_schema=False)
    async def _evaluate_one(rule_id: str, payload: Any = Body(default=None), request: Request = None):
        fid = _resolve_id(rule_id)
        item = _rule_or_404(get_rule(fid), fid)
        data = _normalize(payload, request)
        events = _extract_events(data)
        dry = _dry_run_from(data, request)
        if not any(k in data for k in ("dry_run", "dryRun", "dry-run", "dry", "preview", "simulate")):
            dry = True
        return evaluate_rule(item, events, dry_run=dry)

    @r.put(base + "/{rule_id}", summary="Update auto event rule")
    async def _update(rule_id: str, payload: Any = Body(default=None), request: Request = None):
        fid = _resolve_id(rule_id)
        _rule_or_404(get_rule(fid), fid)
        data = _normalize(payload, request)
        for k in ("id", "rule_id", "ruleId", "filter_id", "filterId"):
            data.pop(k, None)
        updated = update_rule(fid, **data) if data else update_rule(fid)
        return _rule_or_404(updated, fid)

    @r.patch(base + "/{rule_id}", include_in_schema=False)
    async def _patch(rule_id: str, payload: Any = Body(default=None), request: Request = None):
        return await _update(rule_id, payload, request)

    @r.post(base + "/{rule_id}", include_in_schema=False)
    async def _post_update(rule_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not data:
            fid = _resolve_id(rule_id)
            return _rule_or_404(get_rule(fid), fid)
        return await _update(rule_id, payload, request)

    @r.delete(base + "/{rule_id}", summary="Delete auto event rule")
    async def _delete(rule_id: str):
        fid = _resolve_id(rule_id)
        ok = delete_rule(fid)
        if not ok:
            raise HTTPException(status_code=404, detail="Auto rule not found")
        return {"success": True, "ok": True, "deleted": True, "id": fid}


_BASES = (
    "/api/auto-rules",
    "/api/auto_rules",
    "/api/event-auto-rules",
    "/api/zmfilter",
)
_seen: set = set()
for _base in _BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen:
        continue
    _seen.add(_norm)
    _register(router, _norm)


# Compatibility aliases.
auto_rules_router = router
auto_rule_router = router
event_auto_rules_router = router
zmfilter_router = router
api_router = router

__all__ = ["router"]
