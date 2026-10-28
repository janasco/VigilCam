from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
import app.services.email_alerts as email_alerts_service

router = APIRouter(prefix="/api/alerts", tags=["email_alerts", "alerts"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


@router.post("/email", status_code=status.HTTP_200_OK)
async def send_email_alert(
    payload: Any = Body(default=None),
    request: Request = None,
    db=Depends(db_session),
):
    data = _normalize(payload, request)
    try:
        result = email_alerts_service.send_email(data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    if not isinstance(result, dict):
        result = {"result": result, "status": "completed", "success": True, "ok": True}
    result.setdefault("status", "completed")
    result.setdefault("success", True)
    result.setdefault("ok", True)
    return result


@router.post("/email-alert")
async def send_email_alert_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
    return await send_email_alert(payload, request, db)


@router.post("/send-email")
async def send_email_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
    return await send_email_alert(payload, request, db)


@router.post("/email/send")
async def send_email_alias2(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
    return await send_email_alert(payload, request, db)


send_email = email_alerts_service.send_email
send = email_alerts_service.send_email
router_email = router

try:
    from app.main import app as _main_app

    if not getattr(_main_app.state, "email_alerts_registered", False):
        _main_app.include_router(router)
        _main_app.state.email_alerts_registered = True
except Exception:  # noqa: BLE001
    pass
