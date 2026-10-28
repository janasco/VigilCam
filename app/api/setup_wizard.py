"""Guided setup wizard API.

Auto-registered by ``app.main`` (any ``app.api`` module exposing
``router`` is included), so no ``app/main.py`` edit is needed.

Steps: environment, discovery, storage, recording, finish (+ progress).
Persistent state remains the single ``config/initialized.lock`` flag.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
import app.services.setup_wizard as svc

router = APIRouter(prefix="/api/setup-wizard", tags=["setup-wizard"])
underscore_router = APIRouter(prefix="/api/setup_wizard", tags=["setup-wizard"])


def _register(r: APIRouter, suffix: str = "") -> None:
    @r.get("/steps", operation_id=f"setup_wizard_get_steps{suffix}")
    async def get_steps() -> Any:
        return svc.list_steps()

    @r.get("/progress", operation_id=f"setup_wizard_get_progress{suffix}")
    async def get_progress(
        current_step: Optional[str] = Query(default=None),
        step: Optional[str] = Query(default=None),
        completed: Optional[str] = Query(default=None),
    ) -> Any:
        cur = current_step if current_step is not None else step
        return svc.get_progress(current_step=cur, completed_steps=completed)

    @r.get("/environment", operation_id=f"setup_wizard_get_environment{suffix}")
    async def get_environment() -> Any:
        return svc.get_environment_info()

    @r.get("/env", operation_id=f"setup_wizard_get_env{suffix}")
    async def get_environment_alias() -> Any:
        return svc.get_environment_info()

    @r.get("/discovery", operation_id=f"setup_wizard_get_discovery{suffix}")
    async def get_discovery(
        subnet: Optional[str] = Query(default=None),
        scan: bool = Query(default=False),
        db: AsyncSession = Depends(db_session),
    ) -> Any:
        cameras: list = []
        try:
            from app.services import cameras as _cams

            cameras = await _cams.list_cameras(db)
        except Exception:  # noqa: BLE001
            cameras = []
        devices: list = []
        subnets: list = []
        if scan or subnet:
            try:
                from app.services import network_discovery as nd

                if subnet:
                    devices = await nd.scan_subnet(subnet, timeout=0.5)
                else:
                    subnets = nd.detect_local_subnets() or []
                    if subnets:
                        devices = await nd.scan_subnet(subnets[0], timeout=0.5)
            except Exception:  # noqa: BLE001
                devices = []
        else:
            try:
                from app.services import network_discovery as nd

                subnets = nd.detect_local_subnets() or []
            except Exception:  # noqa: BLE001
                subnets = []
        summary = svc.summarize_discovery(devices, cameras)
        summary["subnets"] = subnets
        summary["subnet"] = subnet
        return summary

    @r.get("/storage", operation_id=f"setup_wizard_get_storage{suffix}")
    async def get_storage(path: Optional[str] = Query(default=None)) -> Any:
        return svc.check_storage(path)

    @r.get("/storage/summary", operation_id=f"setup_wizard_get_storage_summary{suffix}")
    async def get_storage_summary(path: Optional[str] = Query(default=None)) -> Any:
        return svc.check_storage(path)

    @r.get("/recording/status", operation_id=f"setup_wizard_get_recording_status{suffix}")
    async def get_recording_status() -> Any:
        try:
            from app.services.recording_manager import recording_manager

            return svc.summarize_recording(recording_manager.status())
        except Exception:  # noqa: BLE001
            return svc.summarize_recording({})

    @r.get("/recording", operation_id=f"setup_wizard_get_recording{suffix}")
    async def get_recording_alias() -> Any:
        try:
            from app.services.recording_manager import recording_manager

            return svc.summarize_recording(recording_manager.status())
        except Exception:  # noqa: BLE001
            return svc.summarize_recording({})

    @r.get("/status", operation_id=f"setup_wizard_get_status{suffix}")
    async def get_status(
        current_step: Optional[str] = Query(default=None),
        db: AsyncSession = Depends(db_session),
    ) -> Any:
        try:
            state = await svc.get_wizard_state(db, current_step=current_step)
        except Exception:  # noqa: BLE001
            state = await svc.get_wizard_state(None, current_step=current_step)
        return state

    @r.get("/state", operation_id=f"setup_wizard_get_state{suffix}")
    async def get_state(
        current_step: Optional[str] = Query(default=None),
        db: AsyncSession = Depends(db_session),
    ) -> Any:
        try:
            return await svc.get_wizard_state(db, current_step=current_step)
        except Exception:  # noqa: BLE001
            return await svc.get_wizard_state(None, current_step=current_step)

    @r.post("/finish", operation_id=f"setup_wizard_post_finish{suffix}")
    async def post_finish() -> Any:
        return svc.finish_setup()

    @r.post("/complete", operation_id=f"setup_wizard_post_complete{suffix}")
    async def post_complete() -> Any:
        return svc.finish_setup()

    @r.post("/finish-setup", operation_id=f"setup_wizard_post_finish_setup{suffix}")
    async def post_finish_setup() -> Any:
        return svc.finish_setup()


_register(router, suffix="_hyphen")
_register(underscore_router, suffix="_underscore")


# Side-effect registration so the underscore alias is live even when this
# module is imported after app startup (mirrors ws-discovery pattern).
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    for _r in (router, underscore_router):
        if id(_r) not in _ids:
            _main_app.include_router(_r)
            try:
                _main_app._registered_router_ids = getattr(
                    _main_app, "_registered_router_ids", set()
                ) | {id(_r)}
                _ids = _main_app._registered_router_ids
            except Exception:  # noqa: BLE001
                pass
except Exception:  # noqa: BLE001
    pass
