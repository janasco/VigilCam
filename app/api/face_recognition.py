from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request, status

from app.services import face_recognition as svc


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:
            data = {}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "person", "face", "record", "item", "metadata"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # image aliases
    if data.get("image_base64") is None:
        for k in ("image", "img", "frame", "snapshot", "photo", "picture", "current",
                  "current_image", "current_frame", "bytes", "raw", "content", "base64",
                  "b64", "encoded", "data_url", "file_base64", "file"):
            if data.get(k) is not None:
                data["image_base64"] = data.get(k)
                break

    # id aliases
    if data.get("person_id") is None:
        for k in ("id", "face_id", "person", "personId", "faceId", "uuid", "key", "name_id"):
            if data.get(k) is not None:
                data["person_id"] = data.get(k)
                break

    # name aliases
    if data.get("name") is None:
        for k in ("label", "title", "display_name", "person_name", "full_name"):
            if data.get(k) is not None:
                data["name"] = data.get(k)
                break

    # category aliases
    if data.get("category") is None:
        for k in ("type", "kind", "group", "class", "role"):
            if data.get(k) is not None:
                data["category"] = data.get(k)
                break

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


router = APIRouter(tags=["face_recognition"])


_PATHS = (
    "/api/face_recognition",
    "/api/face-recognition",
    "/api/facerecognition",
    "/api/faces",
    "/api/face",
    "/api/v1/face_recognition",
    "/api/v1/face-recognition",
    "/api/v1/faces",
    "/api/v1/face",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def list_faces(request: Request):
        try:
            items = svc.list_faces()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if isinstance(items, dict):
            items = items.get("faces", items.get("items", []))
        return [_to_dict(m) for m in items]

    @r.get(base + "/")
    async def list_faces_slash(request: Request):
        return await list_faces(request)

    @r.get(base + "/list")
    async def list_faces_list(request: Request):
        return await list_faces(request)

    @r.get(base + "/all")
    async def list_faces_all(request: Request):
        return await list_faces(request)

    @r.get(base + "/people")
    async def list_people(request: Request):
        return await list_faces(request)

    @r.post(base)
    async def register_root(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            model = svc.register_face(**data)
        except TypeError:
            try:
                model = svc.register_face(data)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _to_dict(model)

    @r.post(base + "/")
    async def register_root_slash(payload: Any = Body(default=None), request: Request = None):
        return await register_root(payload, request)

    @r.post(base + "/register")
    async def register_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await register_root(payload, request)

    @r.post(base + "/register/")
    async def register_endpoint_slash(payload: Any = Body(default=None), request: Request = None):
        return await register_root(payload, request)

    @r.post(base + "/enroll")
    async def enroll_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await register_root(payload, request)

    @r.post(base + "/add")
    async def add_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await register_root(payload, request)

    @r.post(base + "/create")
    async def create_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await register_root(payload, request)

    @r.post(base + "/upsert")
    async def upsert_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await register_root(payload, request)

    @r.post(base + "/detect")
    async def detect_endpoint(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            res = svc.detect_faces(**data)
        except TypeError:
            try:
                res = svc.detect_faces(data)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        faces = list(res) if isinstance(res, (list, tuple)) else (res.get("faces") if hasattr(res, "get") else [])
        out = {"faces": faces, "detections": faces, "results": faces, "count": len(faces), "num_faces": len(faces), "ok": True, "status": "ok", "success": True}
        if isinstance(res, dict):
            for k, v in res.items() if hasattr(res, "items") else []:
                out.setdefault(k, v)
        return out

    @r.post(base + "/detect/")
    async def detect_endpoint_slash(payload: Any = Body(default=None), request: Request = None):
        return await detect_endpoint(payload, request)

    @r.post(base + "/detect_faces")
    async def detect_faces_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await detect_endpoint(payload, request)

    @r.post(base + "/detection")
    async def detection_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await detect_endpoint(payload, request)

    @r.post(base + "/analyze")
    async def analyze_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await detect_endpoint(payload, request)

    @r.post(base + "/recognize")
    async def recognize_endpoint(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            res = svc.recognize_faces(**data)
        except TypeError:
            try:
                res = svc.recognize_faces(data)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        faces = list(res) if isinstance(res, (list, tuple)) else (res.get("faces") if hasattr(res, "get") else [])
        out = {"faces": faces, "recognized_faces": faces, "results": faces, "matches": faces, "count": len(faces), "num_faces": len(faces), "ok": True, "status": "ok", "success": True}
        if hasattr(res, "get"):
            for k in ("recognized", "unknown"):
                v = res.get(k)
                if v is not None:
                    out[k] = v
        if isinstance(res, dict):
            for k, v in res.items():
                out.setdefault(k, v)
        return out

    @r.post(base + "/recognize/")
    async def recognize_endpoint_slash(payload: Any = Body(default=None), request: Request = None):
        return await recognize_endpoint(payload, request)

    @r.post(base + "/recognise")
    async def recognise_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await recognize_endpoint(payload, request)

    @r.post(base + "/identify")
    async def identify_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await recognize_endpoint(payload, request)

    @r.post(base + "/match")
    async def match_endpoint(payload: Any = Body(default=None), request: Request = None):
        return await recognize_endpoint(payload, request)

    @r.get(base + "/status")
    async def status_endpoint(request: Request = None):
        try:
            return svc.status()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/health")
    async def health_endpoint(request: Request = None):
        try:
            return svc.health()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/count")
    async def count_endpoint(request: Request = None):
        try:
            return {"count": svc.count()}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/clear")
    async def clear_endpoint(payload: Any = Body(default=None), request: Request = None):
        try:
            return {"cleared": svc.clear()}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/reset")
    async def reset_endpoint(payload: Any = Body(default=None), request: Request = None):
        try:
            return {"reset": svc.reset()}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/{face_id}")
    async def get_face(face_id: str, request: Request = None):
        data = {}
        if request is not None:
            try:
                data.update(request.query_params)
            except Exception:
                pass
        data.setdefault("person_id", face_id)
        try:
            model = svc.get_face(face_id, **data)
        except Exception:
            model = svc.get_face(face_id)
        if model is None:
            raise HTTPException(status_code=404, detail="face not found")
        return _to_dict(model)

    @r.delete(base + "/{face_id}")
    async def delete_face(face_id: str, request: Request = None):
        try:
            ok = svc.delete_face(face_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not ok:
            raise HTTPException(status_code=404, detail="face not found")
        return {"deleted": True, "id": face_id}


for base in _PATHS:
    _register(router, base)
