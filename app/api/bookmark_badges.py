"""Bookmark badge counts API router (counts per recording/camera for UI badges).

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required. Read-only: delegates to
``app.services.bookmark_badges`` which itself only reads
``app.services.bookmarks``.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query, Request

import app.services.bookmark_badges as badges_mod

router = APIRouter(tags=["bookmark-badges"])


def _scoped_filters(request: Optional[Request], data: Optional[dict] = None) -> dict:
    params: dict = dict(data or {})
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in params or params.get(k) is None:
                    params[k] = v
        except Exception:  # noqa: BLE001
            pass
    rec = params.get("recording_id", params.get("recording"))
    cam = params.get("camera_id", params.get("camera"))
    return {"recording_id": rec, "camera_id": cam}


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def badge_counts(
        request: Request,
        recording_id: Optional[str] = Query(default=None),
        camera_id: Optional[str] = Query(default=None),
    ):
        scoped = _scoped_filters(request, {"recording_id": recording_id, "camera_id": camera_id})
        return badges_mod.get_badge_counts(**scoped)

    @r.get(base + "/")
    async def badge_counts_slash(
        request: Request,
        recording_id: Optional[str] = Query(default=None),
        camera_id: Optional[str] = Query(default=None),
    ):
        return await badge_counts(request, recording_id, camera_id)

    @r.get(base + "/summary")
    async def badge_summary(
        request: Request,
        recording_id: Optional[str] = Query(default=None),
        camera_id: Optional[str] = Query(default=None),
    ):
        scoped = _scoped_filters(request, {"recording_id": recording_id, "camera_id": camera_id})
        return badges_mod.get_summary(**scoped)

    @r.get(base + "/counts")
    async def badge_counts_named(
        request: Request,
        recording_id: Optional[str] = Query(default=None),
        camera_id: Optional[str] = Query(default=None),
    ):
        return await badge_counts(request, recording_id, camera_id)

    @r.get(base + "/badges")
    async def badge_list(
        request: Request,
        recording_id: Optional[str] = Query(default=None),
        camera_id: Optional[str] = Query(default=None),
    ):
        scoped = _scoped_filters(request, {"recording_id": recording_id, "camera_id": camera_id})
        payload = badges_mod.get_badge_counts(**scoped)
        return {
            "total": payload["total"],
            "count": payload["count"],
            "recording_badges": payload["recording_badges"],
            "camera_badges": payload["camera_badges"],
        }

    @r.get(base + "/by-recording")
    async def badges_by_recording():
        return badges_mod.badges_for_recordings()

    @r.get(base + "/by-camera")
    async def badges_by_camera():
        return badges_mod.badges_for_cameras()


_BADGE_BASES = (
    "/api/bookmarks/badge-counts",
    "/api/bookmarks/badges",
    "/api/bookmarks/counts",
    "/api/bookmarks/summary",
    "/api/bookmark-badges",
    "/api/bookmark_badges",
    "/api/bookmark-badges/counts",
    "/api/bookmark_badges/counts",
    "/api/bookmark-counts",
    "/api/bookmark_counts",
)

for _base in _BADGE_BASES:
    _register(router, _base)


@router.get("/api/bookmarks/count")
async def bookmark_count_single(
    request: Request,
    recording_id: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
):
    scoped = _scoped_filters(request, {"recording_id": recording_id, "camera_id": camera_id})
    payload = badges_mod.get_badge_counts(**scoped)
    return {
        "count": payload["count"],
        "total": payload["total"],
        "recording_id": scoped["recording_id"],
        "camera_id": scoped["camera_id"],
    }


@router.get("/api/recordings/{recording_id}/bookmarks/count")
async def recording_bookmark_count(recording_id: str):
    return badges_mod.badge_for_recording(recording_id)


@router.get("/api/recordings/{recording_id}/bookmarks/summary")
async def recording_bookmark_summary(recording_id: str):
    badge = badges_mod.badge_for_recording(recording_id)
    items = badges_mod.badges_for_cameras()
    return {**badge, "camera_badges": [b for b in items]}


@router.get("/api/cameras/{camera_id}/bookmarks/count")
async def camera_bookmark_count(camera_id: str):
    return badges_mod.badge_for_camera(camera_id)


@router.get("/api/cameras/{camera_id}/bookmarks/summary")
async def camera_bookmark_summary(camera_id: str):
    badge = badges_mod.badge_for_camera(camera_id)
    items = badges_mod.badges_for_recordings()
    return {**badge, "recording_badges": [b for b in items]}


# Compatibility aliases.
bookmark_badges_router = router
bookmark_counts_router = router
badge_counts_router = router
api_router = router

__all__ = ["router"]
