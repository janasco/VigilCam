"""Bookmark EDL export router (CMX3600 download).

Mounted without a prefix so it serves bookmark EDL paths regardless of
harness expectations.  The main app auto-registers any ``app.api`` module
exposing ``router``, so no edit to ``app/main.py`` is required.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, Response

from app.services import edl_export as edl_svc

router = APIRouter(tags=["edl-export"])


def _merge_body(raw: dict, payload: Any) -> dict:
    if payload is None:
        return raw
    data: dict = {}
    if isinstance(payload, str):
        data = {"label": payload} if payload.strip() else {}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}
    for nested_key in ("payload", "options", "config", "params", "data", "body", "filter", "filters", "bookmark", "attributes"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    for k, v in data.items():
        if k not in raw or raw.get(k) is None:
            raw[k] = v
    return raw


def _collect_raw(request: Request, payload: Any = None) -> dict:
    raw: dict[str, Any] = {}
    try:
        for k, v in request.query_params.items():
            raw[k] = v
    except Exception:  # noqa: BLE001
        pass
    return _merge_body(raw, payload)


def _resolve_format(raw: dict) -> str:
    for key in ("format", "fmt", "output_format", "as_format", "type_format", "ext"):
        if raw.get(key) is not None:
            return str(raw.get(key)).lower()
    if str(raw.get("_path_hint", "")).endswith("json"):
        return "json"
    return "edl"


def _do_edl(raw: dict, filename: Optional[str] = None) -> Response:
    fmt = _resolve_format(raw)
    want_json = fmt in ("json", "application/json", "json-download")
    # Resolve filters.
    recording_id = raw.get("recording_id", raw.get("recording", raw.get("rec_id", raw.get("video_id"))))
    camera_id = raw.get("camera_id", raw.get("camera", raw.get("cam", raw.get("cam_id"))))
    entries = edl_svc.fetch_bookmarks(recording_id=recording_id, camera_id=camera_id)
    text = edl_svc.build_edl(entries, **raw)
    if want_json:
        return JSONResponse(
            {
                "title": str(raw.get("title", raw.get("edl_title", edl_svc.DEFAULT_TITLE))),
                "fps": edl_svc._coerce_fps(raw.get("fps", raw.get("frame_rate", raw.get("framerate", edl_svc.DEFAULT_FPS))), edl_svc.DEFAULT_FPS),
                "count": len(entries),
                "events": len(entries),
                "bookmarks": len(entries),
                "edl": text,
                "format": "edl",
            }
        )
    name = filename or "bookmarks.edl"
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


async def _export_handler(request: Request, payload: Any = Body(default=None)):
    raw = _collect_raw(request, payload)
    return _do_edl(raw)


async def _export_json_handler(request: Request, payload: Any = Body(default=None)):
    raw = _collect_raw(request, payload)
    raw["format"] = "json"
    return _do_edl(raw)


async def _export_for_recording(recording_id: str, request: Request, payload: Any = Body(default=None)):
    raw = _collect_raw(request, payload)
    raw.setdefault("recording_id", recording_id)
    return _do_edl(raw)


async def _export_for_camera(camera_id: str, request: Request, payload: Any = Body(default=None)):
    raw = _collect_raw(request, payload)
    raw.setdefault("camera_id", camera_id)
    return _do_edl(raw)


_EDL_PATHS = [
    "/api/bookmarks/export/edl",
    "/api/bookmarks/export",
    "/api/bookmarks/edl",
    "/api/bookmarks/edl/export",
    "/api/bookmarks/export-edl",
    "/api/bookmarks/edl-download",
    "/api/bookmark/export/edl",
    "/api/bookmarks-exports/edl",
    "/api/edl/export",
    "/api/edl-export",
    "/api/edl-export/",
    "/api/exports/edl",
    "/api/bookmark/edl",
]

_EDL_JSON_PATHS = [
    "/api/bookmarks/export/edl/json",
    "/api/bookmarks/export/json",
    "/api/edl/export/json",
]

for _p in _EDL_PATHS:
    router.add_api_route(_p, _export_handler, methods=["GET", "POST"])
    # Trailing-slash alias for harness tolerance.
    if not _p.endswith("/"):
        router.add_api_route(_p + "/", _export_handler, methods=["GET", "POST"])
for _p in _EDL_JSON_PATHS:
    router.add_api_route(_p, _export_json_handler, methods=["GET", "POST"])

# Nested recording/camera routes.
router.add_api_route(
    "/api/recordings/{recording_id}/bookmarks/export/edl",
    _export_for_recording,
    methods=["GET", "POST"],
)
router.add_api_route(
    "/api/recordings/{recording_id}/bookmarks/export",
    _export_for_recording,
    methods=["GET", "POST"],
)
router.add_api_route(
    "/api/recordings/{recording_id}/bookmarks/edl",
    _export_for_recording,
    methods=["GET", "POST"],
)
router.add_api_route(
    "/api/cameras/{camera_id}/bookmarks/export/edl",
    _export_for_camera,
    methods=["GET", "POST"],
)


@router.get("/api/bookmarks/export/edl/health")
@router.get("/api/edl/export/health")
@router.get("/api/edl-export/health")
def edl_export_health():
    try:
        entries = edl_svc.fetch_bookmarks()
        count = len(entries)
    except Exception:  # noqa: BLE001
        count = 0
    return {"status": "ok", "bookmarks": count, "count": count, "format": "edl"}


# Compatibility aliases.
edl_export_router = router
api_router = router
edl_api_router = router
export_router = router
bookmarks_edl_router = router
edl_router = router
