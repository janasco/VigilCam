"""TOTP two-factor auth API (stateless; no DB changes required)."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field

from app.services import totp as totp_service

router = APIRouter(prefix="/api/auth/totp", tags=["totp"])


class SetupRequest(BaseModel):
    account: str = Field(default="user", min_length=1, max_length=256)
    issuer: str = Field(default="VigilCam", min_length=1, max_length=128)
    interval: int = Field(default=30, ge=10, le=300)
    digits: int = Field(default=6)
    digest: str = Field(default="SHA1")


class VerifyRequest(BaseModel):
    secret: str = Field(min_length=16)
    code: str = Field(min_length=6, max_length=8)
    window: int = Field(default=1, ge=0, le=10)
    interval: int = Field(default=30, ge=10, le=300)
    digits: int = Field(default=6)
    digest: str = Field(default="SHA1")


def _normalize(body: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if body is None:
        data = {}
    elif hasattr(body, "model_dump"):
        try:
            data = body.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(body, dict):
        data = dict(body)
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    return data


@router.get("/status")
async def totp_status():
    return {"ok": True, "enabled": True, "interval": 30, "digits": 6, "algorithm": "SHA1"}


@router.get("/health")
async def totp_health():
    status = await totp_status()
    status.setdefault("ok", True)
    return status


@router.post("/setup")
async def totp_setup(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    account = str(data.get("account") or "user")
    issuer = str(data.get("issuer") or "VigilCam")
    try:
        interval = int(data.get("interval") or 30)
    except (TypeError, ValueError):
        interval = 30
    try:
        digits = int(data.get("digits") or 6)
    except (TypeError, ValueError):
        digits = 6
    digest = str(data.get("digest") or "SHA1")
    if digits not in (6, 8):
        raise HTTPException(status_code=400, detail="digits must be 6 or 8")
    try:
        secret = totp_service.generate_secret()
        uri = totp_service.provisioning_uri(
            secret, account, issuer=issuer, interval=interval, digits=digits, digest=digest
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "success": True,
        "secret": secret,
        "account": account,
        "issuer": issuer,
        "provisioning_uri": uri,
        "otpauth_url": uri,
        "interval": interval,
        "digits": digits,
        "algorithm": digest.upper(),
    }


@router.post("/verify")
async def totp_verify(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    secret = data.get("secret")
    code = data.get("code")
    if secret is None or code is None:
        raise HTTPException(status_code=400, detail="secret and code are required")
    try:
        window = int(data.get("window", 1))
    except (TypeError, ValueError):
        window = 1
    try:
        interval = int(data.get("interval", 30))
    except (TypeError, ValueError):
        interval = 30
    try:
        digits = int(data.get("digits", 6))
    except (TypeError, ValueError):
        digits = 6
    digest = str(data.get("digest", "SHA1"))
    if digits not in (6, 8):
        raise HTTPException(status_code=400, detail="digits must be 6 or 8")
    digest_name = digest.lower()
    try:
        valid = totp_service.verify_code(
            str(secret), str(code), window=window, interval=interval, digits=digits, digest=digest_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "valid": valid,
        "ok": valid,
        "success": valid,
        "status": "valid" if valid else "invalid",
    }
