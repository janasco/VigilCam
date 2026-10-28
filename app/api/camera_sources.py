from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Query

from app.services import sources as sources_service

router = APIRouter(prefix="/api/camera_sources", tags=["sources"])


def _list():
    return sources_service.list_sources()


@router.get("")
async def list_sources():
    return _list()


@router.get("/")
async def list_sources_slash():
    return _list()


@router.get("/presets")
async def presets():
    return _list()


@router.get("/cameras")
async def camera_sources():
    return _list()


# Alias router for /api/camera_sources
camera_sources_router = APIRouter(prefix="/api/camera_sources", tags=["sources"])


@camera_sources_router.get("")
async def cs_list():
    return _list()


@camera_sources_router.get("/")
async def cs_list_slash():
    return _list()
