"""CodeProject.AI-compatible adapter API router.

Exposes the CodeProject.AI adapter registry/inference service over HTTP.
Compatible with the auto-loader in ``app.main`` (looks for ``router``).

VigilCam ``/api/cpai*`` paths accept JSON/base64 from VigilCam clients
and forward JPEG bytes to the registered CodeProject.AI Server endpoint
(multipart ``image`` + ``min_confidence``), falling back to a mock
result. Native ``/v1/vision/...`` + ``/v1/info`` + ``/v1/status`` shims
let CodeProject.AI clients (e.g. Blue Iris pointed at VigilCam) POST
images and receive camelCase CPAI wire responses.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Request, status

from app.api.deps import db_session
import app.services.cpai_adapter as svc_mod
from app.services.cpai_adapter import (
    register_adapter,
    get_adapter,
    list_adapters,
    infer,
    clear,
    CPAIAdapterService,
    cpai_adapter_service,
)

router = APIRouter(tags=["cpai_adapter"])


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


async def _extract_native_image(request: Request) -> dict:
    """Extract image bytes + options from a CPAI-native POST.

    Accepts multipart form (``image`` file field, ``min_confidence`` text)
    as sent by Blue Iris / CPAI clients, as well as JSON
    ``{image_base64, min_confidence}`` bodies.
    """
    data: dict = {}
    try:
        for k, v in request.query_params.items():
            if k not in data:
                data[k] = v
    except Exception:  # noqa: BLE001
        pass
    ctype = ""
    try:
        ctype = (request.headers.get("content-type") or "").lower()
    except Exception:  # noqa: BLE001
        ctype = ""
    if "multipart/" in ctype or "form-" in ctype:
        try:
            form = await request.form()
            for key in ("image", "file", "image_data", "picture", "frame", "snapshot"):
                if key in form and form.get(key) is not None:
                    val = form.get(key)
                    if hasattr(val, "read"):
                        try:
                            raw = await val.read()  # type: ignore[union-attr]
                        except Exception:  # noqa: BLE001
                            raw = None
                        if raw:
                            data.setdefault("image_bytes", bytes(raw))
                    elif isinstance(val, (bytes, bytearray)):
                        data.setdefault("image_bytes", bytes(val))
                    elif isinstance(val, str):
                        data.setdefault("image_base64", val)
                    break
            for key in ("min_confidence", "minConfidence", "confidence", "threshold"):
                if key in form and data.get("min_confidence") is None and data.get("minConfidence") is None:
                    try:
                        data["min_confidence"] = form.get(key)
                    except Exception:  # noqa: BLE001
                        pass
            for key in ("custom_model", "customModel", "model", "model_name", "modelName"):
                if key in form and data.get("custom_model") is None and data.get("model") is None:
                    try:
                        data["custom_model"] = form.get(key)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
        return data
    # JSON body fallback.
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if isinstance(body, dict):
        for k, v in body.items():
            data.setdefault(k, v)
    elif isinstance(body, str):
        data.setdefault("image_base64", body)
    return data


def _do_native_infer(data: dict, custom_model: Optional[str] = None) -> dict:
    if custom_model and data.get("custom_model") is None and data.get("model") is None:
        data["custom_model"] = custom_model
    result = _do_infer(data)
    try:
        wire = svc_mod.to_cpai_response(result)
        # Preserve fallback marker for VigilCam clients polling native paths.
        if result.get("fallback") is True:
            wire["fallback"] = True
        return wire
    except Exception:  # noqa: BLE001
        return result


_PATHS = (
    "/api/cpai_adapter",
    "/api/cpai-adapter",
    "/api/cpai",
    "/api/codeproject-ai",
    "/api/codeprojectai",
    "/api/codeproject",
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

    @r.get(base + "/info")
    async def info_root():
        try:
            return svc_mod.get_info()
        except Exception as exc:  # noqa: BLE001
            return {"success": True, "ok": True, "error": str(exc)}

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

    @r.post(base + "/custom", status_code=status.HTTP_200_OK)
    async def custom_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await infer_root(payload, request, db)

    @r.post(base + "/custom/{model_name}", status_code=status.HTTP_200_OK)
    async def custom_model_alias(model_name: str, payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.setdefault("custom_model", model_name)
        return _do_infer(data)

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


# ---------------------------------------------------------------------------
# CodeProject.AI native shims (server-compatible inbound paths)
# ---------------------------------------------------------------------------

@router.get("/v1/info")
async def cpai_info():
    try:
        return svc_mod.get_info()
    except Exception as exc:  # noqa: BLE001
        return {"success": True, "ok": True, "error": str(exc)}


@router.get("/v1/status")
async def cpai_status():
    try:
        st = svc_mod.get_status()
        return {
            "success": True,
            "status": "ok",
            "ok": True,
            "modules": st.get("adapters", []),
            "count": st.get("count", 0),
        }
    except Exception as exc:  # noqa: BLE001
        return {"success": True, "status": "ok", "ok": True, "error": str(exc)}


@router.post("/v1/vision/detection", status_code=status.HTTP_200_OK)
async def cpai_native_detection(request: Request, db=Depends(db_session)):
    data = await _extract_native_image(request)
    return _do_native_infer(data)


@router.post("/v1/vision/custom/{model_name}", status_code=status.HTTP_200_OK)
async def cpai_native_custom(model_name: str, request: Request, db=Depends(db_session)):
    data = await _extract_native_image(request)
    return _do_native_infer(data, custom_model=model_name)


@router.get("/v1/vision/detection/status")
async def cpai_detection_status():
    try:
        return svc_mod.get_status()
    except Exception as exc:  # noqa: BLE001
        return {"status": "ok", "ok": True, "error": str(exc)}
