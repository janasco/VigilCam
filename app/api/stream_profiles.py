"""Stream profiles API router (main/sub with detect/record roles).

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required. Mirrors Shinobi monitor stream options
(``stream_*``/record/detector sections + sub-stream channels) and Frigate
role separation (``detect``/``record``/``record_sub``/``audio`` inputs).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.stream_profiles as svc_mod
from app.services.stream_profiles import (
    build_ffmpeg_inputs,
    delete_profile,
    delete_profiles_for_camera,
    get_profile,
    get_stream_profiles,
    ingestion_urls,
    resolve_roles,
    set_profile,
    set_stream_profiles,
    verify_roles,
    verify_single_source_camera_model,
)

router = APIRouter(tags=["stream_profiles"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        s = payload.strip()
        # Bare URL string means {url: ...}.
        if s and ("://" in s or s.startswith("/")):
            data = {"url": s}
        else:
            data = {"url": s} if s else {}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {"url": str(payload)}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass
    for nested in ("profile", "config", "params", "data", "options", "payload"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    return data


def _require_camera_id(camera_id: Any) -> Any:
    if camera_id is None or (isinstance(camera_id, bool)):
        raise HTTPException(status_code=400, detail="camera_id is required")
    s = str(camera_id).strip()
    if not s:
        raise HTTPException(status_code=400, detail="camera_id is required")
    try:
        return int(float(s)) if float(s) == int(float(s)) else s
    except (TypeError, ValueError):
        return s


def _profile_or_404(camera_id: Any, name: Any) -> dict:
    prof = get_profile(camera_id, name)
    if prof is None:
        raise HTTPException(status_code=404, detail="Stream profile not found")
    return prof


def _register_camera_routes(r: APIRouter, base: str) -> None:
    @r.get(base, summary="Get main/sub stream profiles + role mapping")
    async def _get_all(camera_id: Any, request: Request):
        cid = _require_camera_id(camera_id)
        try:
            data = get_stream_profiles(cid)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        # Merge query-sourced fallback for ingestion convenience.
        try:
            qp = dict(request.query_params)
            if qp.get("source") and not data.get("profiles"):
                data["fallback_source"] = qp.get("source")
        except Exception:
            pass
        return data

    @r.put(base, summary="Bulk set main/sub stream profiles")
    @r.post(base, include_in_schema=False)
    @r.patch(base, include_in_schema=False)
    async def _bulk_set(camera_id: Any, payload: Any = Body(default=None), request: Request = None):
        cid = _require_camera_id(camera_id)
        data = _normalize(payload, request)
        # Accept {main: {...}, sub: {...}} or {profiles: {...}} or single-profile body.
        profiles = data.get("profiles", None)
        if profiles is None and ("main" in data or "sub" in data):
            profiles = {k: data[k] for k in ("main", "sub") if k in data}
        if profiles is None:
            # Single profile body with name/url -> treat as one upsert then return all.
            if data.get("name") or data.get("profile") or data.get("url") or data.get("path"):
                single = dict(data)
                single.pop("profiles", None)
                try:
                    set_profile(camera_id=cid, **single)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc))
                return get_stream_profiles(cid)
            profiles = data
        try:
            return set_stream_profiles(cid, profiles)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.delete(base, summary="Delete all stream profiles for camera")
    async def _delete_all(camera_id: Any):
        cid = _require_camera_id(camera_id)
        n = delete_profiles_for_camera(cid)
        return {"success": True, "ok": True, "deleted_count": n, "camera_id": cid}

    @r.get(base + "/verify", summary="Verify single-source model + role assignment")
    async def _verify(camera_id: Any):
        cid = _require_camera_id(camera_id)
        model = verify_single_source_camera_model()
        roles = verify_roles(cid)
        return {"camera_id": cid, "model": model, "roles": roles}

    @r.get(base + "/roles", summary="Effective detect/record role mapping")
    async def _roles(camera_id: Any):
        cid = _require_camera_id(camera_id)
        return {"camera_id": cid, "roles": resolve_roles(cid)}

    @r.get(base + "/ffmpeg-inputs", summary="Frigate-style ffmpeg inputs")
    @r.get(base + "/ffmpeg_inputs", include_in_schema=False)
    async def _ffmpeg(camera_id: Any, request: Request, source: Optional[str] = Query(default=None)):
        cid = _require_camera_id(camera_id)
        fallback = source
        try:
            if fallback is None:
                for alias in ("fallback_source", "main_url", "url", "path"):
                    if alias in request.query_params:
                        fallback = request.query_params.get(alias)
                        break
        except Exception:
            pass
        return {"camera_id": cid, "inputs": build_ffmpeg_inputs(cid, fallback)}

    @r.get(base + "/ingestion", summary="Ingestion main/sub URLs")
    async def _ingest(camera_id: Any, request: Request, source: Optional[str] = Query(default=None)):
        cid = _require_camera_id(camera_id)
        fallback = source
        try:
            if fallback is None:
                for alias in ("fallback_source", "main_url", "url", "path"):
                    if alias in request.query_params:
                        fallback = request.query_params.get(alias)
                        break
        except Exception:
            pass
        urls = ingestion_urls(cid, fallback)
        return {"camera_id": cid, **urls}

    @r.get(base + "/{name}", summary="Get one stream profile")
    async def _get_one(camera_id: Any, name: str):
        cid = _require_camera_id(camera_id)
        return _profile_or_404(cid, name)

    @r.put(base + "/{name}", summary="Create or replace one stream profile")
    @r.post(base + "/{name}", include_in_schema=False)
    @r.patch(base + "/{name}", include_in_schema=False)
    async def _upsert_one(camera_id: Any, name: str, payload: Any = Body(default=None), request: Request = None):
        cid = _require_camera_id(camera_id)
        data = _normalize(payload, request)
        # Path name wins unless body names a valid profile explicitly.
        data["name"] = name
        data["camera_id"] = cid
        try:
            return set_profile(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.delete(base + "/{name}", summary="Delete one stream profile")
    async def _delete_one(camera_id: Any, name: str):
        cid = _require_camera_id(camera_id)
        ok = delete_profile(cid, name)
        if not ok:
            raise HTTPException(status_code=404, detail="Stream profile not found")
        return {"success": True, "ok": True, "deleted": [name], "camera_id": cid}


_CAMERA_BASES = (
    "/api/cameras/{camera_id}/stream-profiles",
    "/api/cameras/{camera_id}/stream_profiles",
    "/api/cameras/{camera_id}/streamProfiles",
)
for _base in _CAMERA_BASES:
    _register_camera_routes(router, _base)


def _register_top_level(r: APIRouter, base: str) -> None:
    @r.get(base, summary="List stream profiles (optionally filter by camera)")
    @r.get(base + "/", include_in_schema=False)
    async def _list_all(request: Request, camera_id: Optional[str] = Query(default=None)):
        cid = camera_id
        if cid is None:
            try:
                for alias in ("camera_id", "cameraId", "camera", "cam_id", "cam"):
                    if alias in request.query_params:
                        cid = request.query_params.get(alias)
                        break
            except Exception:
                pass
        if cid is not None:
            norm = _require_camera_id(cid)
            return get_stream_profiles(norm)
        from app.services.stream_profiles import list_stream_profiles as _list

        return {"profiles": _list(), "count": len(_list())}

    @r.post(base, status_code=status.HTTP_200_OK, summary="Create stream profile")
    @r.post(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def _create_top(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        raw_cid = data.get("camera_id", data.get("camera", data.get("cam", data.get("cameraId"))))
        if raw_cid is None:
            raise HTTPException(status_code=400, detail="camera_id is required")
        cid = _require_camera_id(raw_cid)
        data["camera_id"] = cid
        if "name" not in data and "profile" not in data:
            # Bulk-style {main: {...}, sub: {...}}.
            if "main" in data or "sub" in data or "profiles" in data:
                profiles = data.get("profiles", {k: data[k] for k in ("main", "sub") if k in data})
                try:
                    return set_stream_profiles(cid, profiles)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc))
            raise HTTPException(status_code=400, detail="profile name must be one of 'main' | 'sub'")
        try:
            created = set_profile(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return created

    @r.get(base + "/verify-single-source", summary="Verify cameras use a single source")
    async def _verify_model():
        return verify_single_source_camera_model()


_TOP_BASES = (
    "/api/stream-profiles",
    "/api/stream_profiles",
    "/api/streamProfiles",
)
_seen: set = set()
for _base in _TOP_BASES:
    if _base in _seen:
        continue
    _seen.add(_base)
    _register_top_level(router, _base)


# Compatibility aliases.
stream_profiles_router = router
profiles_router = router
api_router = router

__all__ = ["router"]
