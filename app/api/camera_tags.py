"""Camera tagging API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required. The store is in-memory
(``app.services.camera_tags``); no DB migration, no camera-model change.

iSpy Agent DVR parity: devices there are bulk-targeted by a single ``group``
string, while ``tags`` are free-form multi-labels. VigilCam cameras likewise
carry only ``group``/``group_id`` (see ``app.models.entities.Camera``), so
this router adds the missing multi-label layer: set/add/remove/clear tags per
camera and look cameras up by tag.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.camera_tags as tags_mod
from app.services.camera_tags import (
    add_tags,
    cameras_with_tag,
    clear_tags,
    find_cameras_by_tags,
    get_tags,
    list_all_tags,
    remove_tags,
    set_tags,
    tag_counts,
)

router = APIRouter(tags=["camera_tags"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"tags": payload}
    elif isinstance(payload, (list, tuple, set)):
        data = {"tags": list(payload)}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"tags": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {"tags": str(payload)}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    # query params arrive as single values; keep ``tags``/``tag``
                    # mergeable by leaving them as strings.
                    data[k] = v
        except Exception:
            pass

    for nested_key in ("payload", "options", "config", "params", "data",
                       "body", "request", "fields", "entry", "selection"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _coerce_camera_id(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        pass
    try:
        f = float(s)
        if f.is_integer():
            return int(f)
    except (TypeError, ValueError):
        pass
    return s


def _require_camera_id(camera_id: Any) -> Any:
    cid = _coerce_camera_id(camera_id)
    if cid is None or (isinstance(cid, str) and not str(cid).strip()):
        raise HTTPException(status_code=400, detail="camera_id is required")
    return cid


def _extract_tag_value(data: dict) -> Any:
    for key in ("tags", "tag", "tag_list", "tagList", "labels", "label",
                "values", "value", "items", "item", "tagNames", "tag_names"):
        if key in data and data.get(key) is not None:
            return data.get(key)
    # case-insensitive fallback
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in ("tags", "tag"):
        if key in lower and lower[key] is not None:
            return lower[key]
    return []


def _payload(camera_id: Any, tags: list[str]) -> dict:
    return {"camera_id": camera_id, "tags": tags, "count": len(tags)}


def _register_camera_routes(r: APIRouter, base: str) -> None:
    @r.get(base, summary="Get tags for camera")
    async def _get(camera_id: Any):
        cid = _require_camera_id(camera_id)
        return _payload(cid, get_tags(cid))

    @r.put(base, summary="Replace tags for camera")
    async def _replace(
        camera_id: Any,
        payload: Any = Body(default=None),
        request: Request = None,
    ):
        cid = _require_camera_id(camera_id)
        data = _normalize(payload, request)
        tags = set_tags(cid, _extract_tag_value(data))
        return _payload(cid, tags)

    @r.post(base, summary="Replace tags for camera (alias)")
    async def _replace_alias(
        camera_id: Any,
        payload: Any = Body(default=None),
        request: Request = None,
    ):
        cid = _require_camera_id(camera_id)
        data = _normalize(payload, request)
        # ``{"tags": [...], "mode": "add"/"remove"}`` convenience.
        mode = str(data.get("mode", "")).strip().lower()
        value = _extract_tag_value(data)
        if mode in {"add", "append", "union"}:
            tags = add_tags(cid, value)
        elif mode in {"remove", "delete", "subtract"}:
            tags = remove_tags(cid, value)
        else:
            tags = set_tags(cid, value)
        return _payload(cid, tags)

    @r.post(base + "/add", summary="Add tags to camera")
    async def _add(
        camera_id: Any,
        payload: Any = Body(default=None),
        request: Request = None,
    ):
        cid = _require_camera_id(camera_id)
        data = _normalize(payload, request)
        return _payload(cid, add_tags(cid, _extract_tag_value(data)))

    @r.post(base + "/remove", summary="Remove tags from camera")
    async def _remove_post(
        camera_id: Any,
        payload: Any = Body(default=None),
        request: Request = None,
    ):
        cid = _require_camera_id(camera_id)
        data = _normalize(payload, request)
        return _payload(cid, remove_tags(cid, _extract_tag_value(data)))

    @r.delete(base + "/remove", summary="Remove tags from camera")
    async def _remove_delete(
        camera_id: Any,
        payload: Any = Body(default=None),
        request: Request = None,
    ):
        cid = _require_camera_id(camera_id)
        data = _normalize(payload, request)
        return _payload(cid, remove_tags(cid, _extract_tag_value(data)))

    @r.delete(base, summary="Clear all tags for camera")
    async def _clear(camera_id: Any):
        cid = _require_camera_id(camera_id)
        removed = clear_tags(cid)
        return {"camera_id": cid, "tags": [], "count": 0, "removed": removed,
                "removed_count": removed, "success": True, "ok": True}


_CAMERA_BASES = (
    "/api/cameras/{camera_id}/tags",
    "/api/cameras/{camera_id}/tag",
    "/api/cameras/{camera_id}/labels",
    "/api/cameras/{camera_id}/label",
)
for _base in _CAMERA_BASES:
    _register_camera_routes(router, _base)


def _register_top_level(r: APIRouter, base: str) -> None:
    @r.get(base, summary="List all camera tags")
    async def _list_all(
        request: Request,
        q: Optional[str] = Query(None),
        query: Optional[str] = Query(None),
        search: Optional[str] = Query(None),
    ):
        needle = q if q is not None else (query if query is not None else search)
        tags = list_all_tags()
        counts = tag_counts()
        if needle is not None and str(needle).strip():
            n = str(needle).strip().lower()
            tags = [t for t in tags if n in t.lower()]
            counts = {k: v for k, v in counts.items() if n in str(k).lower()}
        return {"tags": tags, "counts": counts, "count": len(tags)}

    @r.get(base + "/{tag}/cameras", summary="List cameras carrying a tag")
    async def _cameras_for_tag(tag: str):
        label = str(tag or "").strip()
        if not label:
            raise HTTPException(status_code=400, detail="tag is required")
        ids = cameras_with_tag(label)
        return {"tag": label, "tags": [label], "camera_ids": ids,
                "cameras": ids, "count": len(ids)}

    @r.post(base + "/query", summary="Find cameras by tags")
    async def _query(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        raw_match = data.get("match", data.get("mode", data.get("op", "any")))
        match = str(raw_match or "any").strip().lower()
        if match not in {"any", "all", "every", "and", "or"}:
            match = "any"
        if match == "or":
            match = "any"
        tags = tags_mod.normalize_tags(_extract_tag_value(data))
        ids = find_cameras_by_tags(tags, match=match)
        return {"tags": tags, "match": match, "camera_ids": ids,
                "cameras": ids, "count": len(ids)}


_TOP_BASES = (
    "/api/camera-tags",
    "/api/camera_tags",
    "/api/cameras/tags",
)
_seen_top: set = set()
for _base in _TOP_BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen_top:
        continue
    _seen_top.add(_norm)
    _register_top_level(router, _norm)


@router.get("/api/cameras/by-tag", summary="Find cameras by tag")
@router.get("/api/cameras/by_tag", summary="Find cameras by tag", include_in_schema=False)
async def _by_tag(
    request: Request,
    tag: Optional[str] = Query(None),
    tags: Optional[str] = Query(None),
    match: Optional[str] = Query("any"),
):
    data: dict = {}
    try:
        for k, v in request.query_params.items():
            # keep repeated ``tag=`` params: Starlette keeps the last one via
            # ``request.query_params`` mapping, so also check getlist.
            data.setdefault(k, v)
        try:
            multi = request.query_params.getlist("tag")
            if len(multi) > 1:
                data["tag"] = multi
            multi2 = request.query_params.getlist("tags")
            if len(multi2) > 1:
                data["tags"] = multi2
        except Exception:
            pass
    except Exception:
        pass
    if tag is not None and "tag" not in data:
        data["tag"] = tag
    if tags is not None and "tags" not in data:
        data["tags"] = tags
    wanted = tags_mod.normalize_tags(_extract_tag_value(data))
    mode = str(match or "any").strip().lower()
    if mode not in {"any", "all"}:
        mode = "any"
    ids = find_cameras_by_tags(wanted, match=mode) if wanted else []
    return {"tags": wanted, "match": mode, "camera_ids": ids,
            "cameras": ids, "count": len(ids)}


# Compatibility aliases.
camera_tags_router = router
camera_tag_router = router
tags_router = router
api_router = router

__all__ = ["router"]
