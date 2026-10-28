"""Scoped API token router.

Serves ``/api/key_scopes*`` and ``/api/key-scopes*`` paths (plus
``/api/api_key_scopes*`` / ``/api/scopes*`` aliases). The main app
auto-registers any ``app.api`` module exposing ``router``, so no edit to
``app/main.py`` is required.

Baseline: ``app.services.api_keys.verify_api_key`` only checks key hash,
``revoked`` flag and expiry — it stores ``scopes`` but never enforces them.
This router enforces scopes opt-in via :mod:`app.services.key_scopes`:

- ``POST <base>/verify`` (or ``/check``): verify raw key + required scope.
- ``GET <base>/scopes``: list known scopes.
- ``GET <base>/key/{key_id}`` / ``GET <base>/{key_id}``: scopes for a key.
- ``POST <base>``: create a scoped key (delegates to api_keys).
- ``POST <base>/{key_id}/grant|revoke|scopes``: mutate scopes.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

import app.services.key_scopes as svc
import app.services.api_keys as api_keys_mod

router = APIRouter(tags=["key-scopes"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"key": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"key": bytes(payload).decode("utf-8", errors="replace")}
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
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "key", "api_key", "settings", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _pick_key(data: dict) -> Any:
    for k in ("key", "api_key", "apiKey", "token", "value", "plaintext", "secret", "raw"):
        if data.get(k) is not None:
            return data.get(k)
    return None


def _pick_scope(data: dict) -> Any:
    for k in ("scope", "scopes", "required_scope", "required_scopes",
              "permission", "permissions", "perm", "perms"):
        if data.get(k) is not None:
            return data.get(k)
    return None


def _pick_key_id(data: dict) -> Any:
    for k in ("key_id", "keyId", "kid", "id", "api_key_id"):
        if data.get(k) is not None:
            return data.get(k)
    return None


def _public(record: Any) -> dict:
    if isinstance(record, dict):
        out = dict(record)
    else:
        try:
            out = dict(record)
        except Exception:  # noqa: BLE001
            return {"value": str(record)}
    out.pop("key_hash", None)
    out.pop("keyHash", None)
    for secret_field in ("key", "api_key", "plaintext", "value"):
        if secret_field in out and out.get("masked"):
            out.pop(secret_field, None)
    out.setdefault("status", "revoked" if out.get("revoked") else "active")
    out.setdefault("ok", True)
    out.setdefault("success", True)
    return out


async def _list_handler(request: Request):
    """List all keys with their scopes (scoped view over api_keys store)."""
    raw: dict = dict(request.query_params)
    scope_filter = raw.get("scope") or raw.get("scopes") or raw.get("permission")
    items = api_keys_mod.list_api_keys()
    out = []
    for rec in items:
        scopes = svc.get_key_scopes(rec)
        if scope_filter:
            wanted = svc.normalize_scopes(scope_filter)
            if wanted and not svc.has_any_scope(rec, wanted):
                continue
        entry = _public(rec)
        entry["scopes"] = scopes
        entry["permissions"] = scopes
        out.append(entry)
    return JSONResponse(out)


async def _create_handler(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    try:
        result = api_keys_mod.create_api_key(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    if not isinstance(result, dict):
        result = {"result": result, "status": "active", "success": True, "ok": True}
    result.setdefault("status", "active")
    result.setdefault("state", "active")
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


async def _verify_handler(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    candidate = _pick_key(data)
    if candidate is None:
        # Allow raw string body: {"key": ...} missing -> 400 like api_keys.
        raise HTTPException(status_code=400, detail="key is required")
    required = _pick_scope(data)
    record = svc.verify_with_scope(candidate, required)
    scopes = svc.get_key_scopes(record) if record is not None else []
    if record is None:
        # Distinguish invalid key vs insufficient scope for observability.
        base = api_keys_mod.verify_api_key(candidate)
        if base is None:
            return {
                "valid": False, "ok": False, "success": False,
                "authorized": False, "has_scope": False,
                "status": "invalid", "reason": "invalid",
            }
        return {
            "valid": False, "ok": False, "success": False,
            "authorized": False, "has_scope": False,
            "status": "forbidden", "reason": "insufficient_scope",
            "id": base.get("id"), "key_id": base.get("id"),
            "scopes": svc.get_key_scopes(base),
            "required": svc.normalize_scopes(required),
        }
    return {
        "valid": True, "ok": True, "success": True,
        "authorized": True, "has_scope": True,
        "status": "active",
        "id": record.get("id"), "key_id": record.get("id"),
        "name": record.get("name"), "scopes": scopes,
        "required": svc.normalize_scopes(required),
    }


async def _check_handler(payload: Any = Body(default=None), request: Request = None):
    return await _verify_handler(payload, request)


async def _scopes_handler(request: Request):
    return {
        "ok": True, "success": True,
        "scopes": svc.list_scopes(),
        "available_scopes": svc.list_scopes(),
        "count": len(svc.list_scopes()),
    }


async def _status_handler():
    return svc.get_status()


async def _health_handler():
    st = svc.get_status()
    st.setdefault("ok", True)
    st.setdefault("status", "ok")
    return st


async def _key_scopes_handler(key_id: str):
    record = api_keys_mod.get_api_key(key_id)
    if record is None:
        raise HTTPException(status_code=404, detail="API key not found")
    scopes = svc.get_key_scopes(record)
    out = _public(record)
    out["scopes"] = scopes
    out["permissions"] = scopes
    out["required"] = []
    return out


async def _grant_handler(key_id: str, payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    scope = _pick_scope(data) or data.get("grant") or data.get("add")
    if scope is None:
        raise HTTPException(status_code=400, detail="scope is required")
    record = svc.grant_scope(key_id, scope)
    if record is None:
        raise HTTPException(status_code=404, detail="API key not found")
    out = _public(record)
    out["scopes"] = svc.get_key_scopes(record)
    out["permissions"] = out["scopes"]
    out["ok"] = True
    out["success"] = True
    return out


async def _revoke_scope_handler(key_id: str, payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    scope = _pick_scope(data) or data.get("revoke") or data.get("remove")
    if scope is None:
        raise HTTPException(status_code=400, detail="scope is required")
    record = svc.revoke_scope(key_id, scope)
    if record is None:
        # Could be unknown key or empty scope; check key existence.
        if api_keys_mod.get_api_key(key_id) is None:
            raise HTTPException(status_code=404, detail="API key not found")
        raise HTTPException(status_code=400, detail="scope is required")
    out = _public(record)
    out["scopes"] = svc.get_key_scopes(record)
    out["permissions"] = out["scopes"]
    out["ok"] = True
    out["success"] = True
    return out


async def _set_scopes_handler(key_id: str, payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    scope = _pick_scope(data)
    # Allow explicit empty list to clear scopes.
    if scope is None and "scopes" not in data and "scope" not in data \
            and "permissions" not in data and "perms" not in data:
        raise HTTPException(status_code=400, detail="scope is required")
    record = svc.set_scopes(key_id, scope if scope is not None else [])
    if record is None:
        raise HTTPException(status_code=404, detail="API key not found")
    out = _public(record)
    out["scopes"] = svc.get_key_scopes(record)
    out["permissions"] = out["scopes"]
    out["ok"] = True
    out["success"] = True
    return out


_BASES = (
    "/api/key_scopes",
    "/api/key-scopes",
    "/api/api_key_scopes",
    "/api/api-key-scopes",
    "/api/scopes",
)


def _register(r: APIRouter, base: str) -> None:
    r.add_api_route(base, _list_handler, methods=["GET"])
    r.add_api_route(base + "/", _list_handler, methods=["GET"])
    r.add_api_route(base, _create_handler, methods=["POST"])
    r.add_api_route(base + "/", _create_handler, methods=["POST"])
    r.add_api_route(base + "/create", _create_handler, methods=["POST"])
    r.add_api_route(base + "/generate", _create_handler, methods=["POST"])
    r.add_api_route(base + "/list", _list_handler, methods=["GET", "POST"])
    r.add_api_route(base + "/scopes", _scopes_handler, methods=["GET", "POST"])
    r.add_api_route(base + "/available", _scopes_handler, methods=["GET", "POST"])
    r.add_api_route(base + "/status", _status_handler, methods=["GET"])
    r.add_api_route(base + "/health", _health_handler, methods=["GET"])
    r.add_api_route(base + "/verify", _verify_handler, methods=["GET", "POST"])
    r.add_api_route(base + "/check", _check_handler, methods=["GET", "POST"])
    r.add_api_route(base + "/authorize", _check_handler, methods=["GET", "POST"])
    r.add_api_route(base + "/key/{key_id}", _key_scopes_handler, methods=["GET"])
    r.add_api_route(base + "/{key_id}/scopes", _key_scopes_handler, methods=["GET"])
    r.add_api_route(base + "/{key_id}", _key_scopes_handler, methods=["GET"])
    r.add_api_route(base + "/{key_id}/grant", _grant_handler, methods=["POST"])
    r.add_api_route(base + "/{key_id}/revoke-scope", _revoke_scope_handler, methods=["POST"])
    r.add_api_route(base + "/{key_id}/revoke_scope", _revoke_scope_handler, methods=["POST"])
    r.add_api_route(base + "/{key_id}/scopes", _set_scopes_handler, methods=["POST", "PUT", "PATCH"])


for _base in _BASES:
    _register(router, _base)


# Prefixed routers for explicit mounting compatibility.
underscore_router = APIRouter(prefix="/api/key_scopes", tags=["key-scopes"])
hyphen_router = APIRouter(prefix="/api/key-scopes", tags=["key-scopes"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_list(request: Request):
        return await _list_handler(request)

    @r.get("/")
    async def slash_list(request: Request):
        return await _list_handler(request)

    @r.get("/list")
    async def list_list(request: Request):
        return await _list_handler(request)

    @r.post("")
    async def root_create(payload: Any = Body(default=None), request: Request = None):
        return await _create_handler(payload, request)

    @r.post("/")
    async def slash_create(payload: Any = Body(default=None), request: Request = None):
        return await _create_handler(payload, request)

    @r.post("/create")
    async def alias_create(payload: Any = Body(default=None), request: Request = None):
        return await _create_handler(payload, request)

    @r.post("/generate")
    async def alias_generate(payload: Any = Body(default=None), request: Request = None):
        return await _create_handler(payload, request)

    @r.get("/scopes")
    async def scopes_get(request: Request):
        return await _scopes_handler(request)

    @r.post("/scopes")
    async def scopes_post(request: Request):
        return await _scopes_handler(request)

    @r.get("/available")
    async def avail_get(request: Request):
        return await _scopes_handler(request)

    @r.get("/status")
    async def status_get():
        return await _status_handler()

    @r.get("/health")
    async def health_get():
        return await _health_handler()

    @r.get("/verify")
    async def verify_get(request: Request):
        return await _verify_handler(None, request)

    @r.post("/verify")
    async def verify_post(payload: Any = Body(default=None), request: Request = None):
        return await _verify_handler(payload, request)

    @r.get("/check")
    async def check_get(request: Request):
        return await _check_handler(None, request)

    @r.post("/check")
    async def check_post(payload: Any = Body(default=None), request: Request = None):
        return await _check_handler(payload, request)

    @r.get("/key/{key_id}")
    async def by_key(key_id: str):
        return await _key_scopes_handler(key_id)

    @r.get("/{key_id}/scopes")
    async def one_scopes(key_id: str):
        return await _key_scopes_handler(key_id)

    @r.get("/{key_id}")
    async def one_get(key_id: str):
        return await _key_scopes_handler(key_id)

    @r.post("/{key_id}/grant")
    async def one_grant(key_id: str, payload: Any = Body(default=None), request: Request = None):
        return await _grant_handler(key_id, payload, request)

    @r.post("/{key_id}/revoke-scope")
    async def one_revoke(key_id: str, payload: Any = Body(default=None), request: Request = None):
        return await _revoke_scope_handler(key_id, payload, request)

    @r.post("/{key_id}/revoke_scope")
    async def one_revoke_us(key_id: str, payload: Any = Body(default=None), request: Request = None):
        return await _revoke_scope_handler(key_id, payload, request)

    @r.post("/{key_id}/scopes")
    async def one_set(key_id: str, payload: Any = Body(default=None), request: Request = None):
        return await _set_scopes_handler(key_id, payload, request)

    @r.put("/{key_id}/scopes")
    async def one_put(key_id: str, payload: Any = Body(default=None), request: Request = None):
        return await _set_scopes_handler(key_id, payload, request)


for _r in (underscore_router, hyphen_router):
    _make_prefixed(_r)


# Compatibility aliases.
key_scopes_router = router
api_router = router
scopes_router = router
scope_router = router
