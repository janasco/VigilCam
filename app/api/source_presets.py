from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Query

from app.services import source_presets as source_presets_service

# Primary router with no prefix so we can serve multiple path variants.
router = APIRouter(tags=["source_presets"])


def _categorized(*args: Any, **kwargs: Any):
    return source_presets_service.parse_source_presets(*args, **kwargs)


def _list(*args: Any, **kwargs: Any):
    return source_presets_service.list_source_presets(*args, **kwargs)


_PATHS = ("/api/source_presets", "/api/source-presets", "/api/sourcePresets")


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def categorized_root(path: Optional[str] = Query(default=None)):
        return _categorized(path=path) if path else _categorized()

    @r.get(base + "/")
    async def categorized_slash(path: Optional[str] = Query(default=None)):
        return _categorized(path=path) if path else _categorized()

    @r.get(base + "/categorized")
    async def categorized(path: Optional[str] = Query(default=None)):
        return _categorized(path=path) if path else _categorized()

    @r.get(base + "/categories")
    async def categories(path: Optional[str] = Query(default=None)):
        return _list(path=path) if path else _list()

    @r.get(base + "/list")
    async def list_categories(path: Optional[str] = Query(default=None)):
        return _list(path=path) if path else _list()

    @r.get(base + "/flat")
    async def flat(path: Optional[str] = Query(default=None)):
        return source_presets_service.list_flat(path=path) if path else source_presets_service.list_flat()

    @r.get(base + "/presets")
    async def presets(path: Optional[str] = Query(default=None)):
        return _categorized(path=path) if path else _categorized()


for base in _PATHS:
    _register(router, base)

# Prefixed alias routers (for callers expecting router.prefix to be set)
underscore_router = APIRouter(prefix="/api/source_presets", tags=["source_presets"])
hyphen_router = APIRouter(prefix="/api/source-presets", tags=["source_presets"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root(path: Optional[str] = Query(default=None)):
        return _categorized(path=path) if path else _categorized()

    @r.get("/")
    async def slash(path: Optional[str] = Query(default=None)):
        return _categorized(path=path) if path else _categorized()

    @r.get("/categories")
    async def cats(path: Optional[str] = Query(default=None)):
        return _list(path=path) if path else _list()

    @r.get("/list")
    async def lst(path: Optional[str] = Query(default=None)):
        return _list(path=path) if path else _list()


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)


# Compatibility aliases
source_presets_router = underscore_router
sourcePresetsRouter = underscore_router
source_presets_api_router = underscore_router
source_presets_hyphen_router = hyphen_router

__all__ = [
    "router",
    "underscore_router",
    "hyphen_router",
    "source_presets_router",
    "sourcePresetsRouter",
    "source_presets_api_router",
    "source_presets_hyphen_router",
]
