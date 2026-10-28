from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile, status

router = APIRouter(tags=["plugins"])


def _svc():
    from app.services import plugins as svc
    return svc


def _app(request: Request):
    return request.app


@router.get("/api/plugins")
@router.get("/api/v1/plugins")
async def list_plugins(request: Request):
    svc = _svc()
    return svc.list_plugins(app=request.app)


@router.get("/api/plugins/{name}")
@router.get("/api/v1/plugins/{name}")
async def get_plugin(name: str, request: Request):
    svc = _svc()
    item = svc.get_plugin(name, app=request.app)
    if item is None:
        raise HTTPException(status_code=404, detail="Plugin not found")
    return item


@router.post("/api/plugins/install")
@router.post("/api/v1/plugins/install")
async def install_plugin(
    request: Request,
    payload: Any = Body(default=None),
    file: Optional[UploadFile] = File(default=None),
):
    svc = _svc()
    app = request.app
    code = None
    filename = None
    name = None
    path = None
    # Parse body defensively: JSON, form, or raw text.
    if payload is None:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            try:
                form = await request.form()
                payload = dict(form)
            except Exception:  # noqa: BLE001
                try:
                    raw = await request.body()
                    payload = raw.decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    payload = None
    if isinstance(payload, str):
        # treat raw body as code if it looks like python
        if code is None:
            code = payload
        payload = {}
    data: dict = {}
    if isinstance(payload, dict):
        data = dict(payload)
    elif payload is not None:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}

    # Also accept form/query-ish fields from request body already parsed.
    name = data.get("name") or data.get("plugin") or data.get("plugin_name") or data.get("id")
    filename = data.get("filename") or data.get("file_name")
    path = data.get("path") or data.get("source_path") or data.get("plugin_path")
    code = code or data.get("code") or data.get("content") or data.get("source") or data.get("source_code")
    enable = data.get("enable", True)
    if isinstance(enable, str):
        enable = enable.lower() in ("1", "true", "yes", "on")

    if file is not None:
        content = await file.read()
        code = content.decode("utf-8", errors="replace")
        if not filename:
            filename = file.filename

    try:
        info = svc.install_plugin(
            name=name,
            filename=filename,
            path=path,
            code=code,
            app=app,
            enable=bool(enable),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return info


@router.post("/api/plugins/{name}/enable")
@router.post("/api/v1/plugins/{name}/enable")
async def enable_plugin(name: str, request: Request, payload: Any = Body(default=None)):
    svc = _svc()
    app = request.app
    info = svc.enable_plugin(name, app=app)
    return info


@router.post("/api/plugins/{name}/disable")
@router.post("/api/v1/plugins/{name}/disable")
async def disable_plugin(name: str, request: Request, payload: Any = Body(default=None)):
    svc = _svc()
    info = svc.disable_plugin(name, app=request.app)
    return info


# Additional aliases for compatibility.
@router.post("/api/plugins/{name}")
@router.post("/api/v1/plugins/{name}")
async def update_plugin(name: str, request: Request, payload: Any = Body(default=None)):
    svc = _svc()
    app = request.app
    data = payload if isinstance(payload, dict) else {}
    if data.get("enabled") is True or data.get("action") == "enable" or data.get("enable") is True:
        return svc.enable_plugin(name, app=app)
    if data.get("enabled") is False or data.get("action") == "disable" or data.get("disable") is True:
        return svc.disable_plugin(name, app=app)
    if data.get("action") == "install":
        return svc.install_plugin(name=name, app=app, enable=True)
    return svc.get_plugin(name, app=app)


@router.put("/api/plugins/{name}")
@router.put("/api/v1/plugins/{name}")
async def put_plugin(name: str, request: Request, payload: Any = Body(default=None)):
    return await update_plugin(name, request, payload)


@router.post("/api/plugins/load")
@router.post("/api/v1/plugins/load")
async def load_plugins(request: Request):
    svc = _svc()
    return svc.load_plugins(request.app)


@router.post("/api/plugins/reload")
@router.post("/api/v1/plugins/reload")
async def reload_plugins(request: Request):
    svc = _svc()
    return svc.load_plugins(request.app, force=True)
