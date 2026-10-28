"""Allowlisted script runner API for VigilCam.

Auto-registered by ``app.main`` (any module in ``app.api`` exposing
``router`` is included), so no ``app/main.py`` edit is needed.
Both ``/api/script-runner`` (canonical) and ``/api/script_runner``
(underscore alias) are served.
"""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

import app.services.script_runner as svc

router = APIRouter(prefix="/api/script-runner", tags=["script-runner"])
underscore_router = APIRouter(prefix="/api/script_runner", tags=["script-runner"])


class ScriptRunRequest(BaseModel):
    script: Optional[str] = None
    name: Optional[str] = None
    args: Optional[Any] = None
    argv: Optional[Any] = None
    arguments: Optional[Any] = None
    timeout: Optional[float] = None
    timeout_seconds: Optional[float] = None

    class Config:
        extra = "allow"


def _payload_dict(payload: Any) -> dict:
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
            return dict(data) if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, str):
        return {"script": payload}
    return {}


def _extract_run_params(data: dict) -> tuple[Any, Any, Any]:
    script = (
        data.get("script")
        or data.get("name")
        or data.get("target")
        or data.get("id")
    )
    args = data.get("args")
    if args is None:
        args = data.get("argv", data.get("arguments"))
    timeout = data.get("timeout")
    if timeout is None:
        timeout = data.get("timeout_seconds", data.get("timeoutSeconds"))
    return script, args, timeout


def _do_run(data: dict) -> dict:
    script, args, timeout = _extract_run_params(data)
    if script is None or (isinstance(script, str) and not script.strip()):
        raise HTTPException(status_code=422, detail="Provide 'script' (allowlisted name) to run")
    result = svc.run_script(script, args=args, timeout=timeout)
    payload = result.to_dict()
    if result.blocked:
        raise HTTPException(status_code=403, detail=payload.get("detail") or "script is not allowlisted")
    return payload


def _scripts_payload() -> dict:
    scripts = svc.list_scripts()
    summary = svc.get_summary()
    return {
        **summary,
        "scripts": scripts,
        "names": [entry["name"] for entry in scripts],
        "ok": True,
        "success": True,
    }


@router.get("/scripts")
@underscore_router.get("/scripts")
async def list_allowlisted_scripts():
    return _scripts_payload()


@router.get("/status")
@underscore_router.get("/status")
async def get_status():
    return _scripts_payload()


@router.get("/summary")
@underscore_router.get("/summary")
async def get_summary():
    return _scripts_payload()


@router.get("/health")
@underscore_router.get("/health")
async def get_health():
    return _scripts_payload()


@router.get("/run")
@underscore_router.get("/run")
async def run_via_query(
    script: Optional[str] = Query(default=None),
    name: Optional[str] = Query(default=None),
    timeout: Optional[float] = Query(default=None),
):
    chosen = script or name
    if not chosen or not chosen.strip():
        raise HTTPException(status_code=422, detail="Provide 'script' (allowlisted name) to run")
    return _do_run({"script": chosen, "timeout": timeout})


@router.post("/run")
@underscore_router.post("/run")
async def run_script_post(payload: Any = Body(default=None)):
    return _do_run(_payload_dict(payload))


@router.post("/execute")
@underscore_router.post("/execute")
async def execute_script_post(payload: Any = Body(default=None)):
    return _do_run(_payload_dict(payload))


# Side-effect registration so the routers are live even when this module is
# imported after app startup (mirrors stream-probe/url-tester pattern). The
# main app also auto-discovers app.api.* routers at import time, so this is
# best-effort only and must NOT trigger a heavyweight ``app.main`` import on
# its own (keep ``import app.api.script_runner`` lightweight for tests).
try:
    import sys as _sys

    if "app.main" in _sys.modules:
        from app.main import app as _main_app  # noqa: E402

        _ids = getattr(_main_app, "_registered_router_ids", set())
        for _r in (router, underscore_router):
            if id(_r) not in _ids:
                _main_app.include_router(_r)
                try:
                    _main_app._registered_router_ids = getattr(_main_app, "_registered_router_ids", set()) | {id(_r)}
                    _ids = _main_app._registered_router_ids
                except Exception:  # noqa: BLE001
                    pass
except Exception:  # noqa: BLE001
    pass
