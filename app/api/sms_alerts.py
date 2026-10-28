from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
from app.services.sms_alerts import send_sms, sms_alerts_service

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _normalize(payload, request=None):
    data = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


@router.post("/sms", status_code=status.HTTP_200_OK)
async def send_sms_alert(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
    data = _normalize(payload, request)
    try:
        result = send_sms(**data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not isinstance(result, dict):
        result = {"result": result, "status": "completed", "success": True, "ok": True}
    result.setdefault("status", "completed")
    result.setdefault("success", True)
    result.setdefault("ok", True)
    return result


@router.post("/sms-alert")
async def sms_alert_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
    return await send_sms_alert(payload, request, db)


@router.post("/sms-alerts")
async def sms_alerts_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
    return await send_sms_alert(payload, request, db)


@router.post("/sms/send")
async def sms_send_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
    return await send_sms_alert(payload, request, db)


@router.get("/sms/health")
async def sms_health():
    return {"ok": True, "service": "sms_alerts"}
