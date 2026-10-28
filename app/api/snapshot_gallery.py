"""Snapshot gallery router.

Serves ``/api/snapshots/gallery*`` (plus singular/underscore/dash aliases).
The main app auto-registers any ``app.api`` module exposing ``router``,
so no edit to ``app/main.py`` is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

import app.services.snapshot_gallery as svc

router = APIRouter(tags=["snapshot-gallery"])


def _dir_override(**kwargs: Any) -> Optional[str]:
    for key in ("snapshot_dir", "snapshots_dir", "gallery_dir", "directory", "dir", "folder", "output_dir", "path"):
        if kwargs.get(key) is not None:
            return kwargs.get(key)
    return None


def _parse_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(str(value).strip())) if isinstance(value, str) else int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _payload(items: Any) -> dict:
    items = list(items or [])
    return {
        "items": items,
        "snapshots": items,
        "snapshot_gallery": items,
        "gallery": items,
        "entries": items,
        "files": items,
        "results": items,
        "data": items,
        "count": len(items),
        "total": len(items),
        "length": len(items),
        "status": "ok",
        "ok": True,
        "success": True,
    }


async def _list_handler(
    request: Request,
    limit: Any = Query(default=None),
    offset: Any = Query(default=None),
    skip: Any = Query(default=None),
    sort: Any = Query(default=None),
    order: Any = Query(default=None),
    directory: Any = Query(default=None),
    snapshot_dir: Any = Query(default=None),
    gallery_dir: Any = Query(default=None),
    dir: Any = Query(default=None),  # noqa: A002
    pattern: Any = Query(default=None),
    extension: Any = Query(default=None),
    event_id: Any = Query(default=None),
):
    raw: dict[str, Any] = dict(request.query_params)
    if limit is not None:
        raw["limit"] = limit
    if offset is not None:
        raw["offset"] = offset
    if skip is not None:
        raw["skip"] = skip
    if sort is not None:
        raw["sort"] = sort
    if order is not None:
        raw["order"] = order
    if directory is not None:
        raw["directory"] = directory
    if snapshot_dir is not None:
        raw["snapshot_dir"] = snapshot_dir
    if gallery_dir is not None:
        raw["gallery_dir"] = gallery_dir
    if dir is not None:
        raw["dir"] = dir
    if pattern is not None:
        raw["pattern"] = pattern
    if extension is not None:
        raw["extension"] = extension
    if event_id is not None:
        raw["event_id"] = event_id
    override = _dir_override(**raw)
    lim = _parse_int(raw.get("limit", raw.get("n", raw.get("count_param", raw.get("max")))))
    # Avoid clobbering: 'count' may be a pagination alias; read common keys.
    if lim is None:
        for key in ("n", "max", "top", "size", "per_page", "page_size"):
            if raw.get(key) is not None:
                lim = _parse_int(raw.get(key))
                if lim is not None:
                    break
    sk_raw = raw.get("skip", raw.get("offset", raw.get("start_index", 0)))
    try:
        sk = int(sk_raw or 0)
    except (TypeError, ValueError):
        sk = 0
    items = svc.list_gallery(
        snapshot_dir=override,
        limit=lim,
        offset=sk,
        sort=raw.get("sort", raw.get("sort_by", raw.get("order_by"))),
        order=raw.get("order", raw.get("direction")),
        pattern=raw.get("pattern", raw.get("glob", raw.get("filter", raw.get("search", raw.get("q"))))),
        extension=raw.get("extension", raw.get("ext", raw.get("suffix", raw.get("format")))),
        event_id=raw.get("event_id", raw.get("event", raw.get("eid"))),
    )
    return _payload(items)


async def _count_handler(
    request: Request,
    directory: Any = Query(default=None),
    snapshot_dir: Any = Query(default=None),
    gallery_dir: Any = Query(default=None),
    event_id: Any = Query(default=None),
):
    raw: dict[str, Any] = dict(request.query_params)
    if directory is not None:
        raw["directory"] = directory
    if snapshot_dir is not None:
        raw["snapshot_dir"] = snapshot_dir
    if gallery_dir is not None:
        raw["gallery_dir"] = gallery_dir
    if event_id is not None:
        raw["event_id"] = event_id
    override = _dir_override(**raw)
    n = svc.count_gallery(
        snapshot_dir=override,
        event_id=raw.get("event_id", raw.get("event", raw.get("eid"))),
    )
    return {"count": n, "total": n, "length": n, "status": "ok", "ok": True, "success": True}


def _health_payload() -> dict:
    try:
        n = svc.count_gallery()
    except Exception:  # noqa: BLE001
        n = 0
    return {
        "status": "ok",
        "service": "snapshot-gallery",
        "gallery": "snapshot",
        "entries": n,
        "count": n,
        "total": n,
        "ok": True,
        "success": True,
    }


async def _health_handler():
    return _health_payload()


async def _status_handler():
    return _health_payload()


async def _info_handler(request: Request):
    data = _health_payload()
    data.update(
        {
            "usage": "GET with ?limit=&offset=&sort=&event_id=&directory=",
            "endpoints": ["GET /api/snapshots/gallery", "GET /api/snapshots/gallery/count", "GET /api/snapshots/gallery/{item_id}"],
        }
    )
    return data


async def _get_handler(item_id: str, request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    override = _dir_override(**raw)
    item = svc.get_gallery_item(item_id, snapshot_dir=override)
    if item is None:
        raise HTTPException(status_code=404, detail="Snapshot gallery item not found")
    out = dict(item)
    out.setdefault("status", "ok")
    out.setdefault("ok", True)
    out.setdefault("success", True)
    return out


async def _file_handler(item_id: str, request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    override = _dir_override(**raw)
    item = svc.get_gallery_item(item_id, snapshot_dir=override)
    if item is None:
        raise HTTPException(status_code=404, detail="Snapshot gallery item not found")
    from pathlib import Path as _Path

    p = _Path(str(item.get("path", "")))
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Snapshot file not found")
    suffix = p.suffix.lower()
    media = "image/png" if suffix == ".png" else "image/jpeg"
    return FileResponse(str(p), media_type=media, filename=p.name)


async def _delete_handler(item_id: str, request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    override = _dir_override(**raw)
    ok = svc.delete_gallery_item(item_id, snapshot_dir=override)
    if not ok:
        raise HTTPException(status_code=404, detail="Snapshot gallery item not found")
    return {"status": "deleted", "ok": True, "success": True, "id": item_id, "item_id": item_id, "deleted": True}


_LIST_PATHS = [
    "/api/snapshots/gallery",
    "/api/snapshots/gallery/",
    "/api/snapshots/gallery/list",
    "/api/snapshot/gallery",
    "/api/snapshot/gallery/",
    "/api/snapshot/gallery/list",
    "/api/snapshot-gallery",
    "/api/snapshot-gallery/",
    "/api/snapshot-gallery/list",
    "/api/snapshot_gallery",
    "/api/snapshot_gallery/",
    "/api/snapshot_gallery/list",
    "/api/snapshots/gallery-items",
    "/api/snapshots/gallery_items",
]

for _p in _LIST_PATHS:
    router.add_api_route(_p, _list_handler, methods=["GET"])

_COUNT_PATHS = [
    "/api/snapshots/gallery/count",
    "/api/snapshot/gallery/count",
    "/api/snapshot-gallery/count",
    "/api/snapshot_gallery/count",
]

for _p in _COUNT_PATHS:
    router.add_api_route(_p, _count_handler, methods=["GET"])

_HEALTH_PATHS = [
    "/api/snapshots/gallery/health",
    "/api/snapshot/gallery/health",
    "/api/snapshot-gallery/health",
    "/api/snapshot_gallery/health",
    "/api/snapshots/gallery/status",
    "/api/snapshot/gallery/status",
    "/api/snapshot-gallery/status",
    "/api/snapshot_gallery/status",
]

for _p in _HEALTH_PATHS:
    if _p.endswith("/health"):
        router.add_api_route(_p, _health_handler, methods=["GET"])
    else:
        router.add_api_route(_p, _status_handler, methods=["GET"])


@router.get("/api/snapshots/gallery/info")
@router.get("/api/snapshot/gallery/info")
@router.get("/api/snapshot-gallery/info")
@router.get("/api/snapshot_gallery/info")
async def gallery_info(request: Request):
    return await _info_handler(request)


_DETAIL_PATHS_GET = [
    "/api/snapshots/gallery/{item_id}",
    "/api/snapshot/gallery/{item_id}",
    "/api/snapshot-gallery/{item_id}",
    "/api/snapshot_gallery/{item_id}",
]

for _p in _DETAIL_PATHS_GET:
    router.add_api_route(_p, _get_handler, methods=["GET"])
    router.add_api_route(_p, _delete_handler, methods=["DELETE"])

_FILE_PATHS = [
    "/api/snapshots/gallery/{item_id}/file",
    "/api/snapshot/gallery/{item_id}/file",
]

for _p in _FILE_PATHS:
    router.add_api_route(_p, _file_handler, methods=["GET"])


# Compatibility aliases.
gallery_router = router
snapshot_gallery_router = router
api_router = router
gallery_api_router = router


try:
    from app.main import app as _main_app  # noqa: E402

    try:
        _ids = getattr(_main_app, "_registered_router_ids", set())
        if id(router) not in _ids:
            _main_app.include_router(router)
            _main_app._registered_router_ids = _ids | {id(router)}
    except Exception:  # noqa: BLE001
        pass
except Exception:  # noqa: BLE001
    pass
