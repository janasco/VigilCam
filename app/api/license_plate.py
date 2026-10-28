from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request

try:
    from app.services import license_plate as svc
except Exception:  # noqa: BLE001
    try:
        from app.services import lpr as svc
    except Exception:  # noqa: BLE001
        svc = None


def _svc():
    global svc
    if svc is None:
        try:
            from app.services import license_plate as _s

            svc = _s
        except Exception:  # noqa: BLE001
            pass
    return svc


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"image_base64": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"image_base64": payload.decode("utf-8", "ignore")}
        except Exception:
            data = {}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request",
                       "settings", "meta", "metadata", "info", "details", "image", "frame",
                       "camera", "result", "recognition"):
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
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    return obj


router = APIRouter(tags=["license_plate"])


BASES = (
    "/api/license_plate",
    "/api/license_plates",
    "/api/license-plate",
    "/api/license-plates",
    "/api/licensePlate",
    "/api/licensePlates",
    "/api/license_plate_recognition",
    "/api/license_plate_recognitions",
    "/api/license-plate-recognition",
    "/api/license-plate-recognitions",
    "/api/licensePlateRecognition",
    "/api/licensePlateRecognitions",
    "/api/lpr",
    "/api/lprs",
    "/api/anpr",
    "/api/anprs",
    "/api/plates",
    "/api/plate",
    "/api/plate_recognition",
    "/api/plate_recognitions",
    "/api/plate-recognition",
    "/api/plate-recognitions",
    "/api/v1/license_plate",
    "/api/v1/license_plates",
    "/api/v1/lpr",
    "/api/v1/anpr",
    "/api/v1/plates",
    "/api/v1/plate",
)


def _call(fn_name: str, data: dict):
    s = _svc()
    if s is None:
        raise HTTPException(status_code=500, detail="license plate service unavailable")
    fn = getattr(s, fn_name, None)
    if fn is None:
        # fallback
        fn = getattr(s, "recognize_plate", None)
    if fn is None:
        raise HTTPException(status_code=500, detail="recognize function missing")
    try:
        return fn(**data)
    except TypeError:
        try:
            return fn(data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def list_plates(request: Request):
        s = _svc()
        try:
            items = s.list_plates() if hasattr(s, "list_plates") else []
        except Exception:
            try:
                items = s.list() if hasattr(s, "list") else []
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        # support query filters
        params = dict(request.query_params)
        if params:
            try:
                items = s.list_plates(**params)
            except Exception:
                pass
        try:
            return [_to_dict(m) for m in (items if not isinstance(items, dict) else [items])]
        except Exception:
            return _to_dict(items)

    @r.get(base + "/")
    async def list_plates_slash(request: Request):
        return await list_plates(request)

    @r.get(base + "/list")
    async def list_alias(request: Request):
        return await list_plates(request)

    @r.get(base + "/all")
    async def list_all(request: Request):
        return await list_plates(request)

    @r.get(base + "/history")
    async def history(request: Request):
        return await list_plates(request)

    @r.post(base)
    async def recognize_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        res = _call("recognize_plate", data)
        # await if needed
        if hasattr(res, "__await__"):
            res = await res
        return _to_dict(res)

    @r.post(base + "/")
    async def recognize_post_slash(payload: Any = Body(default=None), request: Request = None):
        return await recognize_post(payload, request)

    @r.post(base + "/recognize")
    async def recognize_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await recognize_post(payload, request)

    @r.post(base + "/recognize/")
    async def recognize_endpoint_slash(payload: Any = Body(default=None), request: Request = None):
        return await recognize_post(payload, request)

    @r.post(base + "/detect")
    async def detect_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await recognize_post(payload, request)

    @r.post(base + "/detect/")
    async def detect_endpoint_slash(payload: Any = Body(default=None), request: Request = None):
        return await recognize_post(payload, request)

    @r.post(base + "/read")
    async def read_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await recognize_post(payload, request)

    @r.post(base + "/analyze")
    async def analyze_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await recognize_post(payload, request)

    @r.post(base + "/scan")
    async def scan_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await recognize_post(payload, request)

    @r.post(base + "/process")
    async def process_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await recognize_post(payload, request)

    @r.post(base + "/predict")
    async def predict_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await recognize_post(payload, request)

    @r.post(base + "/ocr")
    async def ocr_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await recognize_post(payload, request)

    @r.get(base + "/status")
    async def status():
        s = _svc()
        try:
            return s.status()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/health")
    async def health():
        s = _svc()
        try:
            return s.health()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/clear")
    async def clear():
        s = _svc()
        try:
            return {"cleared": s.clear()}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/reset")
    async def reset():
        return await clear()

    @r.get(base + "/{item_id}")
    async def get_one(item_id: str, request: Request):
        s = _svc()
        try:
            res = s.get_recognition(id=item_id)
        except Exception:
            try:
                res = s.get_plate(id=item_id)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        if res is None:
            raise HTTPException(status_code=404, detail="not found")
        if hasattr(res, "__await__"):
            res = await res
        return _to_dict(res)


for _base in BASES:
    _register(router, _base)


# Also expose a recognize function at module level for tests
def recognize(*args, **kwargs):
    return _svc().recognize_plate(*args, **kwargs) if _svc() else None
