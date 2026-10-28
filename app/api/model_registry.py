from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

from app.services import model_registry as svc


def _svc():
    return svc


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"name": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"data": payload.decode("utf-8", "ignore")}
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

    for nested_key in ("model", "models", "entry", "record", "item", "payload", "options", "config", "params", "data", "body", "request", "settings", "meta", "metadata", "info", "details"):
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


router = APIRouter(tags=["model_registry"])


_PATHS = (
    "/api/model_registry",
    "/api/model-registry",
    "/api/modelRegistry",
    "/api/models",
    "/api/ai/models",
    "/api/v1/models",
    "/api/v1/model_registry",
    "/api/v1/model-registry",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def list_models(request: Request):
        try:
            items = svc.list_models()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        # support filters via query params
        params = dict(request.query_params)
        if params:
            try:
                items = svc.list_models(**params)
            except Exception:
                try:
                    items = svc.list_models(params)
                except Exception:
                    pass
        return [_to_dict(m) for m in items]

    @r.get(base + "/")
    async def list_models_slash(request: Request):
        return await list_models(request)

    @r.get(base + "/list")
    async def list_alias(request: Request):
        return await list_models(request)

    @r.get(base + "/all")
    async def list_all(request: Request):
        return await list_models(request)

    @r.post(base)
    async def register_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            model = svc.register_model(**data)
        except TypeError:
            try:
                model = svc.register_model(data)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _to_dict(model)

    @r.post(base + "/")
    async def register_post_slash(payload: Any = Body(default=None), request: Request = None):
        return await register_post(payload, request)

    @r.post(base + "/register")
    async def register_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await register_post(payload, request)

    @r.post(base + "/register/")
    async def register_endpoint_slash(payload: Any = Body(default=None), request: Request = None):
        return await register_post(payload, request)

    @r.post(base + "/add")
    async def add_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await register_post(payload, request)

    @r.post(base + "/create")
    async def create_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await register_post(payload, request)

    @r.post(base + "/upsert")
    async def upsert_endpoint(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            model = svc.upsert(**data)
        except TypeError:
            try:
                model = svc.upsert(data)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _to_dict(model)

    @r.get(base + "/active")
    async def active_get(request: Request):
        params = dict(request.query_params)
        try:
            model = svc.get_active(**params) if params else svc.get_active()
        except Exception:
            model = svc.get_active()
        if model is None:
            raise HTTPException(status_code=404, detail="no active model")
        return _to_dict(model)

    @r.get(base + "/health")
    async def health():
        try:
            return svc.health()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/status")
    async def status():
        try:
            return svc.status()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/count")
    async def count():
        try:
            return {"count": svc.count()}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/clear")
    async def clear():
        try:
            return {"cleared": svc.clear()}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/reset")
    async def reset():
        try:
            return {"reset": svc.reset()}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/{model_id}/activate")
    async def activate(model_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", model_id)
        try:
            model = svc.set_active_model(model_id, **data)
        except TypeError:
            try:
                model = svc.set_active_model(data)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if model is None:
            raise HTTPException(status_code=404, detail="model not found")
        return _to_dict(model)

    @r.post(base + "/{model_id}/active")
    async def active_post(model_id: str, payload: Any = Body(default=None), request: Request = None):
        return await activate(model_id, payload, request)

    @r.post(base + "/{model_id}/select")
    async def select(model_id: str, payload: Any = Body(default=None), request: Request = None):
        return await activate(model_id, payload, request)

    @r.put(base + "/{model_id}")
    async def update(model_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", model_id)
        try:
            model = svc.update_model(model_id, **data)
        except TypeError:
            try:
                model = svc.update_model(data)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if model is None:
            raise HTTPException(status_code=404, detail="model not found")
        return _to_dict(model)

    @r.patch(base + "/{model_id}")
    async def patch(model_id: str, payload: Any = Body(default=None), request: Request = None):
        return await update(model_id, payload, request)

    @r.delete(base + "/{model_id}")
    async def delete(model_id: str, request: Request = None):
        try:
            ok = svc.delete(model_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not ok:
            raise HTTPException(status_code=404, detail="model not found")
        return {"deleted": True, "id": model_id}

    @r.get(base + "/{model_id}")
    async def get_model(model_id: str, request: Request = None):
        data = {}
        if request is not None:
            try:
                data.update(request.query_params.items() if hasattr(request.query_params, "items") else {})
            except Exception:
                pass
        data.setdefault("id", model_id)
        try:
            model = svc.get_model(model_id, **data)
        except Exception:
            model = svc.get_model(model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="model not found")
        return _to_dict(model)


for base in _PATHS:
    _register(router, base)

# Prefixed alias routers
underscore_router = APIRouter(prefix="/api/model_registry", tags=["model_registry"])
hyphen_router = APIRouter(prefix="/api/model-registry", tags=["model_registry"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root(request: Request):
        items = svc.list_models()
        return [_to_dict(m) for m in items]

    @r.get("/")
    async def slash(request: Request):
        return await root(request)

    @r.post("")
    async def create_root(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            model = svc.register_model(**data)
        except TypeError:
            model = svc.register_model(data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _to_dict(model)

    @r.post("/")
    async def create_slash(payload: Any = Body(default=None), request: Request = None):
        return await create_root(payload, request)

    @r.get("/active")
    async def active_root(request: Request):
        model = svc.get_active()
        if model is None:
            raise HTTPException(status_code=404, detail="no active model")
        return _to_dict(model)

    @r.get("/{model_id}")
    async def get_root(model_id: str, request: Request = None):
        model = svc.get_model(model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="model not found")
        return _to_dict(model)

    @r.delete("/{model_id}")
    async def del_root(model_id: str):
        if not svc.delete(model_id):
            raise HTTPException(status_code=404, detail="model not found")
        return {"deleted": True, "id": model_id}

    @r.post("/{model_id}/activate")
    async def act_root(model_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", model_id)
        model = svc.set_active_model(model_id, **data)
        if model is None:
            raise HTTPException(status_code=404, detail="model not found")
        return _to_dict(model)


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)
