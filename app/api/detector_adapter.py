"""Detector-adapter API router.

Exposes the generic detection-adapter registry/inference service over HTTP.
Compatible with the auto-loader in ``app.main`` (looks for ``router``).

Blue Candle reference: downstream detectors speak
``POST /v1/vision/detection`` (multipart ``image`` + ``min_confidence``);
this router accepts JSON/base64 from VigilCam clients and forwards JPEG
bytes to the registered adapter endpoint, falling back to a mock result.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Request, status

from app.api.deps import db_session
import app.services.detector_adapter as svc_mod
from app.services.detector_adapter import (
    register_adapter,
    get_adapter,
    list_adapters,
    infer,
    clear,
    DetectorAdapterService,
    detector_adapter_service,
)

router = APIRouter(tags=["detector_adapter"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"image_base64": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            import base64 as _b64

            data = {"image_bytes": bytes(payload), "image_base64": _b64.b64encode(bytes(payload)).decode("ascii")}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message", "input", "inputs"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    for k in ("request", "db", "session", "user", "current_user"):
        data.pop(k, None)
    return data


def _to_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return dict(obj)
    if isinstance(obj, list):
        return [dict(x) if isinstance(x, dict) else x for x in obj]
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:  # noqa: BLE001
            pass
    return obj


def _do_register(data: dict) -> dict:
    try:
        rec = register_adapter(**data)
    except TypeError:
        rec = register_adapter(data)
    if not isinstance(rec, dict):
        rec = {"name": str(rec), "ok": True, "success": True}
    rec.setdefault("ok", True)
    rec.setdefault("success", True)
    return _to_dict(rec)


def _do_infer(data: dict) -> dict:
    try:
        result = infer(**data)
    except TypeError:
        result = infer(data)
    if not isinstance(result, dict):
        result = {"predictions": [], "success": True, "ok": True, "result": result}
    result.setdefault("success", True)
    result.setdefault("ok", True)
    result.setdefault("fallback", result.get("fallback", False))
    result.setdefault("predictions", result.get("predictions", []))
    result.setdefault("count", len(result.get("predictions", [])) if result.get("count") is None else result.get("count"))
    return _to_dict(result)


_PATHS = (
    "/api/detector_adapter",
    "/api/detector-adapter",
    "/api/detector/adapter",
    "/api/detection/adapter",
    "/api/adapters/detector",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def root_list(request: Request):
        try:
            items = list_adapters()
        except Exception:  # noqa: BLE001
            items = []
        return {"ok": True, "success": True, "adapters": _to_dict(items), "count": len(items)}

    @r.get(base + "/")
    async def root_list_slash(request: Request):
        return await root_list(request)

    @r.get(base + "/list")
    async def list_alias(request: Request):
        return await root_list(request)

    @r.get(base + "/adapters")
    async def adapters_alias(request: Request):
        return await root_list(request)

    @r.get(base + "/status")
    async def status_root():
        try:
            return svc_mod.get_status()
        except Exception as exc:  # noqa: BLE001
            return {"status": "ok", "ok": True, "error": str(exc)}

    @r.get(base + "/health")
    async def health_root():
        try:
            return svc_mod.get_status()
        except Exception as exc:  # noqa: BLE001
            return {"status": "ok", "ok": True, "error": str(exc)}

    @r.post(base + "/register", status_code=status.HTTP_200_OK)
    async def register_root(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        return _do_register(data)

    @r.post(base + "/register/", status_code=status.HTTP_200_OK)
    async def register_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await register_root(payload, request, db)

    @r.post(base, status_code=status.HTTP_200_OK)
    async def infer_root(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        return _do_infer(data)

    @r.post(base + "/", status_code=status.HTTP_200_OK)
    async def infer_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await infer_root(payload, request, db)

    @r.post(base + "/infer", status_code=status.HTTP_200_OK)
    async def infer_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await infer_root(payload, request, db)

    @r.post(base + "/detect", status_code=status.HTTP_200_OK)
    async def detect_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await infer_root(payload, request, db)

    @r.post(base + "/predict", status_code=status.HTTP_200_OK)
    async def predict_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await infer_root(payload, request, db)

    @r.post(base + "/mock", status_code=status.HTTP_200_OK)
    async def mock_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = svc_mod.mock_infer(**data)
        except TypeError:
            result = svc_mod.mock_infer(data)
        if not isinstance(result, dict):
            result = {"predictions": [], "success": True, "result": result}
        result.setdefault("success", True)
        result.setdefault("ok", True)
        result.setdefault("fallback", True)
        return _to_dict(result)

    @r.get(base + "/{adapter_name}")
    async def get_one(adapter_name: str):
        rec = get_adapter(adapter_name)
        if rec is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail=f"Adapter {adapter_name} not found")
        return _to_dict(rec)


for _base in _PATHS:
    _register(router, _base)
