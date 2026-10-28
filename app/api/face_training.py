from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from app.services import face_training as svc


def _normalize(payload: Any, request: Request | None = None) -> dict:
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

    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "person", "face", "record", "item", "metadata"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    if data.get("image_base64") is None:
        for k in ("image", "img", "frame", "snapshot", "photo", "picture",
                  "bytes", "raw", "content", "base64", "b64", "encoded",
                  "data_url", "file", "file_base64"):
            if data.get(k) is not None:
                data["image_base64"] = data.get(k)
                break

    if data.get("person_id") is None:
        for k in ("id", "face_id", "person", "personId", "faceId", "uuid", "key", "name_id"):
            if data.get(k) is not None:
                data["person_id"] = data.get(k)
                break

    if data.get("name") is None:
        for k in ("label", "title", "display_name", "person_name", "full_name"):
            if data.get(k) is not None:
                data["name"] = data.get(k)
                break

    if data.get("embedding") is None:
        for k in ("vector", "embeddings", "features", "feature_vector",
                  "descriptor", "vec", "values"):
            if data.get(k) is not None:
                data["embedding"] = data.get(k)
                break

    for k in ("request", "db", "session", "user", "current_user"):
        data.pop(k, None)
    return data


def _to_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return dict(obj)
    if isinstance(obj, (list, tuple)):
        return [dict(x) if isinstance(x, dict) else x for x in obj]
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:  # noqa: BLE001
            pass
    return obj


router = APIRouter(tags=["face_training"])


_PATHS = (
    "/api/face_training",
    "/api/face-training",
    "/api/facetraining",
    "/api/v1/face_training",
    "/api/v1/face-training",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def list_enrolled(request: Request):
        try:
            return [_to_dict(x) for x in svc.list_faces()]
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/")
    async def list_enrolled_slash(request: Request):
        return await list_enrolled(request)

    @r.get(base + "/list")
    async def list_alias(request: Request):
        return await list_enrolled(request)

    @r.get(base + "/all")
    async def all_alias(request: Request):
        return await list_enrolled(request)

    @r.get(base + "/people")
    async def people_alias(request: Request):
        return await list_enrolled(request)

    @r.post(base)
    async def enroll_root(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not data.get("name") and not data.get("person_id"):
            raise HTTPException(status_code=400, detail="name is required")
        try:
            return _to_dict(svc.enroll_face(**data))
        except TypeError:
            try:
                return _to_dict(svc.enroll_face(data))
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/")
    async def enroll_root_slash(payload: Any = Body(default=None), request: Request = None):
        return await enroll_root(payload, request)

    @r.post(base + "/enroll")
    async def enroll_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await enroll_root(payload, request)

    @r.post(base + "/enroll/{name}")
    async def enroll_named(name: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("name", name)
        try:
            return _to_dict(svc.enroll_face(**data))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/add")
    async def add_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await enroll_root(payload, request)

    @r.post(base + "/add/{name}")
    async def add_named(name: str, payload: Any = Body(default=None), request: Request = None):
        return await enroll_named(name, payload, request)

    @r.post(base + "/register")
    async def register_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await enroll_root(payload, request)

    async def _do_train(payload: Any = None, request: Request = None, name: str | None = None):
        data = _normalize(payload, request)
        if name is not None:
            data.setdefault("name", name)
        try:
            res = svc.train(**data)
        except TypeError:
            try:
                res = svc.train(data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if isinstance(res, list):
            return [_to_dict(x) for x in res]
        return _to_dict(res)

    @r.post(base + "/train")
    async def train_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await _do_train(payload, request)

    @r.post(base + "/retrain")
    async def retrain_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await _do_train(payload, request)

    @r.post(base + "/train/{name}")
    async def train_named(name: str, payload: Any = Body(default=None), request: Request = None):
        return await _do_train(payload, request, name)

    @r.get(base + "/retrain/{name}")
    async def retrain_named_get(name: str, request: Request = None):
        return await _do_train(None, request, name)

    @r.get(base + "/train/{name}")
    async def train_named_get(name: str, request: Request = None):
        return await _do_train(None, request, name)

    async def _do_match(payload: Any = None, request: Request = None):
        data = _normalize(payload, request)
        try:
            res = svc.recognize(**data)
        except TypeError:
            try:
                res = svc.recognize(data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        return _to_dict(res)

    @r.post(base + "/recognize")
    async def recognize_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await _do_match(payload, request)

    @r.post(base + "/match")
    async def match_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await _do_match(payload, request)

    @r.post(base + "/identify")
    async def identify_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await _do_match(payload, request)

    @r.get(base + "/status")
    async def status_endpoint():
        try:
            return _to_dict(svc.status())
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/count")
    async def count_endpoint():
        try:
            return {"count": svc.count()}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/clear")
    async def clear_endpoint():
        try:
            return {"cleared": svc.clear()}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/reset")
    async def reset_endpoint():
        try:
            return {"reset": svc.clear()}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/{face_id}")
    async def get_one(face_id: str, request: Request = None):
        try:
            model = svc.get_face(face_id)
        except Exception:  # noqa: BLE001
            model = None
        if model is None:
            raise HTTPException(status_code=404, detail="face not found")
        return _to_dict(model)

    @r.delete(base + "/{face_id}")
    async def delete_one(face_id: str):
        try:
            ok = svc.delete_face(face_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not ok:
            raise HTTPException(status_code=404, detail="face not found")
        return {"deleted": True, "id": face_id}


for _base in _PATHS:
    _register(router, _base)
