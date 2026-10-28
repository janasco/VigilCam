"""Alert tone catalog API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
import app.services.alert_tones as alert_tones_mod
from app.services.alert_tones import (
    clear,
    delete_tone,
    get_tone,
    list_tones,
    play_tone,
    reset_tone,
    set_tone,
    status as service_status,
    update_tone,
)

router = APIRouter(tags=["alert_tones", "alerts"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        s = payload.strip()
        if s:
            data = {"name": s}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"name": bytes(payload).decode("utf-8", errors="replace")}
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

    for nested_key in ("tone", "tones", "entry", "record", "item",
                       "payload", "options", "settings", "params", "data",
                       "body", "request", "config", "configuration",
                       "context", "variables"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


def _name_from_data(data: dict) -> Optional[str]:
    for key in ("name", "tone_name", "toneName", "tone", "tone_id",
                "toneId", "id", "key", "slug", "title"):
        if key in data and data.get(key) is not None:
            s = str(data.get(key)).strip()
            if s:
                return s
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in ("name", "tone_name", "tonename", "tone", "tone_id",
                "toneid", "id", "key", "slug", "title"):
        if key in lower and lower[key] is not None:
            s = str(lower[key]).strip()
            if s:
                return s
    return None


_PATHS = (
    "/api/alert_tones",
    "/api/alert-tones",
    "/api/alertTones",
    "/api/alerts/tones",
    "/api/alerts/alert_tones",
    "/api/sound_tones",
    "/api/tones",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_root(request: Request):
        data = _normalize(None, request)
        name = _name_from_data(data)
        if name is not None:
            res = get_tone(name)
            if res is None:
                raise HTTPException(status_code=404, detail="tone not found")
            return res
        items = list_tones()
        return {"ok": True, "success": True, "status": "ok",
                "items": items, "tones": items, "configs": items,
                "count": len(items), "total": len(items)}

    @r.get(base + "/")
    async def get_slash(request: Request):
        return await get_root(request)

    @r.get(base + "/list")
    async def get_list(request: Request):
        return await get_root(request)

    @r.get(base + "/all")
    async def get_all(request: Request):
        return await get_root(request)

    @r.get(base + "/catalog")
    async def get_catalog(request: Request):
        return await get_root(request)

    @r.get(base + "/status")
    async def get_status(request: Request):
        return service_status()

    @r.get(base + "/health")
    async def get_health(request: Request):
        return {"ok": True, "service": "alert_tones"}

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_root(payload: Any = Body(default=None), request: Request = None,
                        db=Depends(db_session)):
        data = _normalize(payload, request)
        action = str(data.get("action", "")).lower()
        name = _name_from_data(data)
        if action in ("clear", "reset_all", "purge"):
            return clear()
        if action in ("play", "preview", "render"):
            if name is None:
                raise HTTPException(status_code=400, detail="tone name is required")
            try:
                result = play_tone(**data)
            except ValueError as exc:
                raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc))
            result.setdefault("ok", True)
            result.setdefault("success", True)
            return result
        if name is None:
            raise HTTPException(status_code=400, detail="tone name is required")
        try:
            result = set_tone(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "configured", "success": True, "ok": True}
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/")
    async def post_slash(payload: Any = Body(default=None), request: Request = None,
                         db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/configure")
    async def post_configure(payload: Any = Body(default=None), request: Request = None,
                             db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/set")
    async def post_set(payload: Any = Body(default=None), request: Request = None,
                       db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/create")
    async def post_create(payload: Any = Body(default=None), request: Request = None,
                          db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/update")
    async def post_update(payload: Any = Body(default=None), request: Request = None,
                          db=Depends(db_session)):
        data = _normalize(payload, request)
        name = _name_from_data(data)
        if name is None:
            raise HTTPException(status_code=400, detail="tone name is required")
        try:
            result = update_tone(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None,
                         db=Depends(db_session)):
        return clear()

    @r.post(base + "/reset")
    async def post_reset(payload: Any = Body(default=None), request: Request = None,
                         db=Depends(db_session)):
        data = _normalize(payload, request)
        name = _name_from_data(data)
        if name is None:
            return clear()
        try:
            result = reset_tone(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/play")
    async def post_play(payload: Any = Body(default=None), request: Request = None,
                        db=Depends(db_session)):
        data = _normalize(payload, request)
        name = _name_from_data(data)
        if name is None:
            raise HTTPException(status_code=400, detail="tone name is required")
        try:
            result = play_tone(**data)
        except ValueError as exc:
            raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/preview")
    async def post_preview(payload: Any = Body(default=None), request: Request = None,
                           db=Depends(db_session)):
        return await post_play(payload, request, db)

    @r.put(base + "/{name}")
    async def put_one(name: str, payload: Any = Body(default=None),
                      request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.setdefault("name", name)
        try:
            result = set_tone(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.patch(base + "/{name}")
    async def patch_one(name: str, payload: Any = Body(default=None),
                        request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.setdefault("name", name)
        try:
            result = update_tone(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/{name}")
    async def post_one(name: str, payload: Any = Body(default=None),
                       request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.setdefault("name", name)
        action = str(data.get("action", "")).lower()
        try:
            if action in ("play", "preview", "render"):
                result = play_tone(**data)
            elif action in ("reset",):
                result = reset_tone(name)
            elif action in ("delete", "remove", "clear"):
                delete_tone(name)
                return {"ok": True, "success": True, "status": "deleted",
                        "name": name, "id": name}
            else:
                result = update_tone(**data)
        except ValueError as exc:
            raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/{name}/play")
    async def post_one_play(name: str, payload: Any = Body(default=None),
                            request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.setdefault("name", name)
        try:
            result = play_tone(**data)
        except ValueError as exc:
            raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/{name}/preview")
    async def post_one_preview(name: str, payload: Any = Body(default=None),
                               request: Request = None, db=Depends(db_session)):
        return await post_one_play(name, payload, request, db)

    @r.delete(base + "/clear")
    async def delete_clear(request: Request):
        return clear()

    @r.delete(base + "/{name}")
    async def delete_one(name: str, request: Request):
        existed = delete_tone(name)
        if not existed:
            res = get_tone(name)
            if res is None:
                # Deleting a default tone still succeeds (resets to defaults view).
                return {"ok": True, "success": True, "status": "deleted",
                        "name": name, "id": name}
            return {"ok": True, "success": True, "status": "deleted",
                    "name": name, "id": name, "tone": res}
        return {"ok": True, "success": True, "status": "deleted",
                "name": name, "id": name}

    @r.get(base + "/{name}")
    async def get_one(name: str, request: Request):
        res = get_tone(name)
        if res is None:
            raise HTTPException(status_code=404, detail="tone not found")
        return res

    @r.get(base + "/{name}/play")
    async def get_one_play(name: str, request: Request):
        data = _normalize(None, request)
        data.setdefault("name", name)
        try:
            result = play_tone(**data)
        except ValueError as exc:
            raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc))
        return result


for _base in _PATHS:
    _register(router, _base)


# Compatibility aliases.
alert_tones_router = router
alert_tone_router = router
tones_router = router
tone_router = router
sound_tones_router = router
api_router = router

__all__ = ["router"]
