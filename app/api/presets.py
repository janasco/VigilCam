from __future__ import annotations

from fastapi import APIRouter

from app.services import sources as sources_service

def _list():
    return sources_service.list_sources()

router = APIRouter(prefix="/api/presets", tags=["presets"])

@router.get("")
async def list_presets():
    return _list()

@router.get("/")
async def list_presets_slash():
    return _list()

# additional standalone routers
camera_presets_router = APIRouter(prefix="/api/camera-presets", tags=["presets"])

@camera_presets_router.get("")
async def list_camera_presets():
    return _list()

@camera_presets_router.get("/")
async def list_camera_presets_slash():
    return _list()

camera_source_presets_router = APIRouter(prefix="/api/camera-source-presets", tags=["presets"])

@camera_source_presets_router.get("")
async def list_camera_source_presets():
    return _list()

@camera_source_presets_router.get("/")
async def list_camera_source_presets_slash():
    return _list()
