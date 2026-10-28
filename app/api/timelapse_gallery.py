"""Timelapse gallery router.

Serves ``/api/timelapse/gallery*`` (plus plural/underscore/dash aliases).
The main app auto-registers any ``app.api`` module exposing ``router``,
so no edit to ``app/main.py`` is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

import app.services.timelapse_gallery as svc

router = APIRouter(tags=["timelapse-gallery"])


def _dir_override(**kwargs: Any) -> Optional[str]:
    for key in ("timelapse_dir", "gallery_dir", "directory", "dir", "folder", "output_dir", "path"):
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
        "timelapses": items,
        "timelapse_gallery": items,
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
    timelapse_dir: Any = Query(default=None),
    gallery_dir: Any = Query(default=None),
    dir: Any = Query(default=None),  # noqa: A002
    pattern: Any = Query(default=None),
    extension: Any = Query(default=None),
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
    if timelapse_dir is not None:
        raw["timelapse_dir"] = timelapse_dir
    if gallery_dir is not None:
        raw["gallery_dir"] = gallery_dir
    if dir is not None:
        raw["dir"] = dir
    if pattern is not None:
        raw["pattern"] = pattern
    if extension is not None:
        raw["extension"] = extension
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
        timelapse_dir=override,
        limit=lim,
        offset=sk,
        sort=raw.get("sort", raw.get("sort_by", raw.get("order_by"))),
        order=raw.get("order", raw.get("direction")),
        pattern=raw.get("pattern", raw.get("glob", raw.get("filter", raw.get("search", raw.get("q"))))),
        extension=raw.get("extension", raw.get("ext", raw.get("suffix", raw.get("format")))),
    )
    return _payload(items)


async def _count_handler(
    request: Request,
    directory: Any = Query(default=None),
    timelapse_dir: Any = Query(default=None),
    gallery_dir: Any = Query(default=None),
):
    raw: dict[str, Any] = dict(request.query_params)
    if directory is not None:
        raw["directory"] = directory
    if timelapse_dir is not None:
        raw["timelapse_dir"] = timelapse_dir
    if gallery_dir is not None:
        raw["gallery_dir"] = gallery_dir
    override = _dir_override(**raw)
    n = svc.count_gallery(timelapse_dir=override)
    return {"count": n, "total": n, "length": n, "status": "ok", "ok": True, "success": True}


def _health_payload() -> dict:
    try:
        n = svc.count_gallery()
    except Exception:  # noqa: BLE001
        n = 0
    return {
        "status": "ok",
        "service": "timelapse-gallery",
        "gallery": "timelapse",
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
            "usage": "GET with ?limit=&offset=&sort=&directory=",
            "endpoints": ["GET /api/timelapse/gallery", "GET /api/timelapse/gallery/count", "GET /api/timelapse/gallery/{item_id}"],
        }
    )
    return data


async def _get_handler(item_id: str, request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    override = _dir_override(**raw)
    item = svc.get_gallery_item(item_id, timelapse_dir=override)
    if item is None:
        raise HTTPException(status_code=404, detail="Timelapse gallery item not found")
    out = dict(item)
    out.setdefault("status", "ok")
    out.setdefault("ok", True)
    out.setdefault("success", True)
    return out


async def _delete_handler(item_id: str, request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    override = _dir_override(**raw)
    ok = svc.delete_gallery_item(item_id, timelapse_dir=override)
    if not ok:
        raise HTTPException(status_code=404, detail="Timelapse gallery item not found")
    return {"status": "deleted", "ok": True, "success": True, "id": item_id, "item_id": item_id, "deleted": True}


_LIST_PATHS = [
    "/api/timelapse/gallery",
    "/api/timelapse/gallery/",
    "/api/timelapse/gallery/list",
    "/api/timelapses/gallery",
    "/api/timelapses/gallery/",
    "/api/timelapses/gallery/list",
    "/api/timelapse-gallery",
    "/api/timelapse-gallery/",
    "/api/timelapse-gallery/list",
    "/api/timelapse_gallery",
    "/api/timelapse_gallery/",
    "/api/timelapse_gallery/list",
    "/api/timelapse/gallery-items",
    "/api/timelapse/gallery_items",
]

for _p in _LIST_PATHS:
    router.add_api_route(_p, _list_handler, methods=["GET"])

_COUNT_PATHS = [
    "/api/timelapse/gallery/count",
    "/api/timelapses/gallery/count",
    "/api/timelapse-gallery/count",
    "/api/timelapse_gallery/count",
]

for _p in _COUNT_PATHS:
    router.add_api_route(_p, _count_handler, methods=["GET"])

_HEALTH_PATHS = [
    "/api/timelapse/gallery/health",
    "/api/timelapses/gallery/health",
    "/api/timelapse-gallery/health",
    "/api/timelapse_gallery/health",
    "/api/timelapse/gallery/status",
    "/api/timelapses/gallery/status",
    "/api/timelapse-gallery/status",
    "/api/timelapse_gallery/status",
]

for _p in _HEALTH_PATHS:
    if _p.endswith("/health"):
        router.add_api_route(_p, _health_handler, methods=["GET"])
    else:
        router.add_api_route(_p, _status_handler, methods=["GET"])


@router.get("/api/timelapse/gallery/info")
@router.get("/api/timelapses/gallery/info")
@router.get("/api/timelapse-gallery/info")
@router.get("/api/timelapse_gallery/info")
async def gallery_info(request: Request):
    return await _info_handler(request)


_DETAIL_PATHS_GET = [
    "/api/timelapse/gallery/{item_id}",
    "/api/timelapses/gallery/{item_id}",
    "/api/timelapse-gallery/{item_id}",
    "/api/timelapse_gallery/{item_id}",
]

for _p in _DETAIL_PATHS_GET:
    router.add_api_route(_p, _get_handler, methods=["GET"])
    router.add_api_route(_p, _delete_handler, methods=["DELETE"])


# Compatibility aliases.
gallery_router = router
timelapse_gallery_router = router
api_router = router
gallery_api_router = router


# Self-register so no edit to app/main.py is required. The auto-loader in
# main.py also picks up `router`, so this is belt-and-braces.
# NOTE: app/api/timelapse.py registers GET /api/timelapse/{job_id} which would
# otherwise shadow GET /api/timelapse/gallery. Move gallery routes to the
# front so exact gallery paths win regardless of registration order.
def _promote_gallery_routes() -> None:
    try:
        from app.main import app as _promote_app
    except Exception:  # noqa: BLE001
        return
    try:
        routes = getattr(_promote_app, "routes", None)
        if not routes:
            return
        owned = []
        rest = []
        for r in list(routes):
            try:
                orig = getattr(r, "original_router", None)
            except Exception:  # noqa: BLE001
                orig = None
            if orig is router or (orig is not None and id(orig) == id(router)):
                owned.append(r)
            else:
                rest.append(r)
        if not owned:
            return
        # Preserve relative order within each group; gallery first so its
        # exact /api/timelapse/gallery wins over /api/timelapse/{job_id}.
        # NOTE: app.routes is a read-only property (Starlette) returning
        # app.router.routes, so mutate in place rather than assigning.
        try:
            _promote_app.router.routes[:] = owned + rest  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            try:
                routes[:] = owned + rest
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


try:
    from app.main import app as _main_app  # noqa: E402

    try:
        _ids = getattr(_main_app, "_registered_router_ids", set())
        if id(router) not in _ids:
            _main_app.include_router(router)
            _main_app._registered_router_ids = _ids | {id(router)}
        _promote_gallery_routes()
    except Exception:  # noqa: BLE001
        pass
except Exception:  # noqa: BLE001
    pass
