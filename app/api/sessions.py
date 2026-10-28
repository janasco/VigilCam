from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
import app.services.sessions as sessions_service_mod
from app.services.sessions import sessions_service

router = APIRouter(tags=["sessions"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"user_id": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"user_id": bytes(payload).decode("utf-8", errors="replace")}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message", "session", "user"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


_PATHS = (
    "/api/sessions",
    "/api/user_sessions",
    "/api/user-sessions",
    "/api/user/sessions",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def list_sessions(request: Request):
        try:
            include_revoked = request.query_params.get("include_revoked", "true")
            include = str(include_revoked).lower() not in ("0", "false", "no", "off")
        except Exception:  # noqa: BLE001
            include = True
        user_filter = request.query_params.get("user_id") or request.query_params.get("username")
        items = sessions_service_mod.list_sessions(include_revoked=include, user_id=user_filter) if user_filter else sessions_service_mod.list_sessions(include_revoked=include)
        return [_public(item) for item in items]

    @r.get(base + "/")
    async def list_sessions_slash(request: Request):
        return await list_sessions(request)

    @r.get(base + "/list")
    async def list_sessions_alias(request: Request):
        return await list_sessions(request)

    @r.get(base + "/status")
    async def sessions_status():
        return sessions_service_mod.get_status()

    @r.get(base + "/health")
    async def sessions_health():
        svc_status = sessions_service_mod.get_status()
        svc_status.setdefault("ok", True)
        return svc_status

    @r.post(base + "/verify")
    async def verify_session(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        candidate = (
            data.get("session_id")
            or data.get("sessionId")
            or data.get("id")
            or data.get("sid")
            or data.get("token")
            or data.get("session_token")
            or data.get("value")
        )
        if candidate is None:
            raise HTTPException(status_code=400, detail="session_id or token is required")
        record = sessions_service_mod.validate_session(candidate)
        if record is None:
            return {"valid": False, "ok": False, "success": False, "status": "invalid"}
        return {
            "valid": True,
            "ok": True,
            "success": True,
            "status": "active",
            "id": record.get("id"),
            "session_id": record.get("id"),
            "user_id": record.get("user_id"),
            "username": record.get("username"),
        }

    @r.post(base + "/validate")
    async def validate_session(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await verify_session(payload, request, db)

    @r.get(base + "/{session_id}")
    async def get_session(session_id: str):
        record = sessions_service_mod.get_session(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return _public(record)

    @r.post(base, status_code=status.HTTP_200_OK)
    async def create_session(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = sessions_service_mod.create_session(**data)
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
    async def create_session_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await create_session(payload, request, db)

    @r.post(base + "/create")
    async def create_session_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await create_session(payload, request, db)

    @r.post(base + "/start")
    async def start_session_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await create_session(payload, request, db)

    @r.post(base + "/{session_id}/refresh")
    async def refresh_session_post(session_id: str):
        refreshed = sessions_service_mod.sessions_service.refresh_session(session_id)
        if refreshed is None:
            raise HTTPException(status_code=404, detail="Session not found")
        refreshed.setdefault("status", "active")
        refreshed.setdefault("ok", True)
        refreshed.setdefault("success", True)
        return refreshed

    @r.post(base + "/{session_id}/revoke")
    async def revoke_session_post(session_id: str):
        return await revoke_session(session_id)

    @r.delete(base + "/{session_id}")
    async def revoke_session(session_id: str):
        record = sessions_service_mod.revoke_session(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return {
            "ok": True,
            "success": True,
            "status": "revoked",
            "id": record.get("id"),
            "session_id": record.get("id"),
            "revoked": True,
        }


def _public(record: Any) -> dict:
    if isinstance(record, dict):
        out = dict(record)
    else:
        try:
            out = dict(record)
        except Exception:  # noqa: BLE001
            return {"value": str(record)}
    # Do not leak full token on list/get; keep masked view.
    # Creation endpoint returns token intentionally (show once).
    for secret_field in ("token", "session_token", "value"):
        if secret_field in out:
            out.pop(secret_field, None)
    out.setdefault("status", "revoked" if out.get("revoked") else "active")
    out.setdefault("ok", True)
    out.setdefault("success", True)
    return out


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix
underscore_router = APIRouter(prefix="/api/user_sessions", tags=["sessions"])
hyphen_router = APIRouter(prefix="/api/user-sessions", tags=["sessions"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        return [_public(x) for x in sessions_service_mod.list_sessions()]

    @r.get("/")
    async def slash_get(request: Request):
        return [_public(x) for x in sessions_service_mod.list_sessions()]

    @r.get("/list")
    async def list_get(request: Request):
        return [_public(x) for x in sessions_service_mod.list_sessions()]

    @r.get("/status")
    async def status_get():
        return sessions_service_mod.get_status()

    @r.get("/health")
    async def health_get():
        svc_status = sessions_service_mod.get_status()
        svc_status.setdefault("ok", True)
        return svc_status

    @r.get("/{session_id}")
    async def one_get(session_id: str):
        record = sessions_service_mod.get_session(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return _public(record)

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = sessions_service_mod.create_session(**data)
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
        candidate = data.get("session_id") or data.get("id") or data.get("token") or data.get("value")
        if candidate is None:
            raise HTTPException(status_code=400, detail="session_id or token is required")
        record = sessions_service_mod.validate_session(candidate)
        if record is None:
            return {"valid": False, "ok": False, "success": False, "status": "invalid"}
        return {"valid": True, "ok": True, "success": True, "status": "active", "id": record.get("id")}

    @r.delete("/{session_id}")
    async def one_delete(session_id: str):
        record = sessions_service_mod.revoke_session(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"ok": True, "success": True, "status": "revoked", "id": record.get("id"), "revoked": True}


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)
