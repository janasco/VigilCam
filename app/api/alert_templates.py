"""Alert email templates API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
import app.services.alert_templates as alert_templates_mod
from app.services.alert_templates import (
    clear,
    delete_template,
    get_template,
    list_templates,
    render_template,
    reset_template,
    set_template,
    status as service_status,
    update_template,
)

router = APIRouter(tags=["alert_templates", "alerts"])


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

    for nested_key in ("template", "templates", "entry", "record", "item",
                       "payload", "options", "settings", "params", "data",
                       "body", "request", "config", "configuration",
                       "context", "variables"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


def _name_from_data(data: dict) -> Optional[str]:
    for key in ("name", "template_name", "templateName", "id", "template_id",
                "templateId", "key", "slug", "title"):
        if key in data and data.get(key) is not None:
            s = str(data.get(key)).strip()
            if s:
                return s
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in ("name", "template_name", "templatename", "id", "template_id",
                "templateid", "key", "slug", "title"):
        if key in lower and lower[key] is not None:
            s = str(lower[key]).strip()
            if s:
                return s
    return None


_PATHS = (
    "/api/alert_templates",
    "/api/alert-templates",
    "/api/alertTemplates",
    "/api/alerts/templates",
    "/api/alerts/email_templates",
    "/api/alerts/email-templates",
    "/api/alerts/templates/email",
    "/api/email_templates",
    "/api/email-templates",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_root(request: Request):
        data = _normalize(None, request)
        name = _name_from_data(data)
        if name is not None:
            res = get_template(name)
            if res is None:
                raise HTTPException(status_code=404, detail="template not found")
            return res
        items = list_templates()
        return {"ok": True, "success": True, "status": "ok",
                "items": items, "templates": items, "configs": items,
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

    @r.get(base + "/status")
    async def get_status(request: Request):
        return service_status()

    @r.get(base + "/health")
    async def get_health(request: Request):
        return {"ok": True, "service": "alert_templates"}

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_root(payload: Any = Body(default=None), request: Request = None,
                        db=Depends(db_session)):
        data = _normalize(payload, request)
        action = str(data.get("action", "")).lower()
        name = _name_from_data(data)
        if action in ("clear", "reset_all", "purge"):
            return clear()
        if action in ("render", "preview"):
            if name is None:
                raise HTTPException(status_code=400, detail="template name is required")
            try:
                result = render_template(**data)
            except ValueError as exc:
                raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc))
            result.setdefault("ok", True)
            result.setdefault("success", True)
            return result
        if name is None:
            raise HTTPException(status_code=400, detail="template name is required")
        try:
            result = set_template(**data)
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
            raise HTTPException(status_code=400, detail="template name is required")
        try:
            result = update_template(**data)
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
            result = reset_template(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/render")
    async def post_render(payload: Any = Body(default=None), request: Request = None,
                          db=Depends(db_session)):
        data = _normalize(payload, request)
        name = _name_from_data(data)
        if name is None:
            raise HTTPException(status_code=400, detail="template name is required")
        try:
            result = render_template(**data)
        except ValueError as exc:
            raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/preview")
    async def post_preview(payload: Any = Body(default=None), request: Request = None,
                           db=Depends(db_session)):
        return await post_render(payload, request, db)

    @r.put(base + "/{name}")
    async def put_one(name: str, payload: Any = Body(default=None),
                      request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.setdefault("name", name)
        try:
            result = set_template(**data)
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
            result = update_template(**data)
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
            if action in ("render", "preview"):
                result = render_template(**data)
            elif action in ("reset",):
                result = reset_template(name)
            elif action in ("delete", "remove", "clear"):
                delete_template(name)
                return {"ok": True, "success": True, "status": "deleted",
                        "name": name, "id": name}
            else:
                result = update_template(**data)
        except ValueError as exc:
            raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/{name}/render")
    async def post_one_render(name: str, payload: Any = Body(default=None),
                              request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.setdefault("name", name)
        try:
            result = render_template(**data)
        except ValueError as exc:
            raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc))
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/{name}/preview")
    async def post_one_preview(name: str, payload: Any = Body(default=None),
                               request: Request = None, db=Depends(db_session)):
        return await post_one_render(name, payload, request, db)

    @r.delete(base + "/clear")
    async def delete_clear(request: Request):
        return clear()

    @r.delete(base + "/{name}")
    async def delete_one(name: str, request: Request):
        existed = delete_template(name)
        if not existed:
            res = get_template(name)
            if res is None:
                # Deleting a default template still succeeds (resets to defaults view).
                return {"ok": True, "success": True, "status": "deleted",
                        "name": name, "id": name}
            return {"ok": True, "success": True, "status": "deleted",
                    "name": name, "id": name, "template": res}
        return {"ok": True, "success": True, "status": "deleted",
                "name": name, "id": name}

    @r.get(base + "/{name}")
    async def get_one(name: str, request: Request):
        res = get_template(name)
        if res is None:
            raise HTTPException(status_code=404, detail="template not found")
        return res

    @r.get(base + "/{name}/render")
    async def get_one_render(name: str, request: Request):
        data = _normalize(None, request)
        data.setdefault("name", name)
        try:
            result = render_template(**data)
        except ValueError as exc:
            raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc))
        return result


for _base in _PATHS:
    _register(router, _base)


# Compatibility aliases.
alert_templates_router = router
alert_template_router = router
email_templates_router = router
notification_templates_router = router
channels_router = router
api_router = router

__all__ = ["router"]
