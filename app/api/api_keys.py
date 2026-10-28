from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from app.api.deps import db_session
import app.services.api_keys as api_keys_service_mod
from app.services.api_keys import api_keys_service

router = APIRouter(tags=["api_keys"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"name": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"name": bytes(payload).decode("utf-8", errors="replace")}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


_PATHS = (
    "/api/api_keys",
    "/api/api-keys",
    "/api/keys",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def list_keys(request: Request):
        try:
            include_revoked = request.query_params.get("include_revoked", "true")
            include = str(include_revoked).lower() not in ("0", "false", "no", "off")
        except Exception:  # noqa: BLE001
            include = True
        items = api_keys_service_mod.list_api_keys(include_revoked=include)
        return [_public(item) for item in items]

    @r.get(base + "/")
    async def list_keys_slash(request: Request):
        return await list_keys(request)

    @r.get(base + "/list")
    async def list_keys_alias(request: Request):
        return await list_keys(request)

    @r.get(base + "/status")
    async def keys_status():
        return api_keys_service_mod.get_status()

    @r.get(base + "/health")
    async def keys_health():
        svc_status = api_keys_service_mod.get_status()
        svc_status.setdefault("ok", True)
        return svc_status

    @r.post(base + "/verify")
    async def verify_key(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        candidate = (
            data.get("key")
            or data.get("api_key")
            or data.get("apiKey")
            or data.get("token")
            or data.get("value")
            or data.get("plaintext")
        )
        if candidate is None:
            raise HTTPException(status_code=400, detail="key is required")
        record = api_keys_service_mod.verify_api_key(candidate)
        if record is None:
            return {"valid": False, "ok": False, "success": False, "status": "invalid"}
        return {
            "valid": True,
            "ok": True,
            "success": True,
            "status": "active",
            "id": record.get("id"),
            "key_id": record.get("id"),
            "name": record.get("name"),
            "scopes": record.get("scopes", []),
        }

    @r.get(base + "/{key_id}")
    async def get_key(key_id: str):
        record = api_keys_service_mod.get_api_key(key_id)
        if record is None:
            raise HTTPException(status_code=404, detail="API key not found")
        return _public(record)

    @r.post(base, status_code=status.HTTP_200_OK)
    async def create_key(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = api_keys_service_mod.create_api_key(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "active", "success": True, "ok": True}
        result.setdefault("status", "active")
        result.setdefault("state", "active")
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/")
    async def create_key_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await create_key(payload, request, db)

    @r.post(base + "/create")
    async def create_key_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await create_key(payload, request, db)

    @r.post(base + "/generate")
    async def generate_key_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await create_key(payload, request, db)

    @r.delete(base + "/{key_id}")
    async def revoke_key(key_id: str):
        record = api_keys_service_mod.revoke_api_key(key_id)
        if record is None:
            raise HTTPException(status_code=404, detail="API key not found")
        return {
            "ok": True,
            "success": True,
            "status": "revoked",
            "id": record.get("id"),
            "key_id": record.get("id"),
            "revoked": True,
        }

    @r.post(base + "/{key_id}/revoke")
    async def revoke_key_post(key_id: str):
        return await revoke_key(key_id)

    @r.post(base + "/{key_id}/rotate")
    async def rotate_key_post(key_id: str):
        rotated = api_keys_service_mod.api_keys_service.rotate_api_key(key_id)
        if rotated is None:
            raise HTTPException(status_code=404, detail="API key not found")
        rotated.setdefault("status", "active")
        rotated.setdefault("ok", True)
        rotated.setdefault("success", True)
        return rotated


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
    # Do not leak full plaintext on list/get; keep masked + prefix.
    # Creation endpoint returns plaintext intentionally (show once).
    # For list/get, drop raw key fields if present to avoid leakage.
    for secret_field in ("key", "api_key", "plaintext", "value"):
        if secret_field in out and out.get("masked"):
            out.pop(secret_field, None)
    out.setdefault("status", "revoked" if out.get("revoked") else "active")
    out.setdefault("ok", True)
    out.setdefault("success", True)
    return out


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix
underscore_router = APIRouter(prefix="/api/api_keys", tags=["api_keys"])
hyphen_router = APIRouter(prefix="/api/api-keys", tags=["api_keys"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        return [_public(x) for x in api_keys_service_mod.list_api_keys()]

    @r.get("/")
    async def slash_get(request: Request):
        return [_public(x) for x in api_keys_service_mod.list_api_keys()]

    @r.get("/list")
    async def list_get(request: Request):
        return [_public(x) for x in api_keys_service_mod.list_api_keys()]

    @r.get("/status")
    async def status_get():
        return api_keys_service_mod.get_status()

    @r.get("/health")
    async def health_get():
        svc_status = api_keys_service_mod.get_status()
        svc_status.setdefault("ok", True)
        return svc_status

    @r.get("/{key_id}")
    async def one_get(key_id: str):
        record = api_keys_service_mod.get_api_key(key_id)
        if record is None:
            raise HTTPException(status_code=404, detail="API key not found")
        return _public(record)

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = api_keys_service_mod.create_api_key(**data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("status", "active")
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.post("/verify")
    async def verify_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        candidate = data.get("key") or data.get("api_key") or data.get("token") or data.get("value")
        if candidate is None:
            raise HTTPException(status_code=400, detail="key is required")
        record = api_keys_service_mod.verify_api_key(candidate)
        if record is None:
            return {"valid": False, "ok": False, "success": False, "status": "invalid"}
        return {"valid": True, "ok": True, "success": True, "status": "active", "id": record.get("id")}

    @r.delete("/{key_id}")
    async def one_delete(key_id: str):
        record = api_keys_service_mod.revoke_api_key(key_id)
        if record is None:
            raise HTTPException(status_code=404, detail="API key not found")
        return {"ok": True, "success": True, "status": "revoked", "id": record.get("id"), "revoked": True}


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)
