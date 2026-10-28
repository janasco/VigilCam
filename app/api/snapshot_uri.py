"""ONVIF snapshot-URI resolver API (GetSnapshotUri + per-camera store).

Auto-registered by ``app.main``'s ``pkgutil`` loader (module exposes
``router``), so no ``app/main.py`` edit is needed. Service work lives in
``app.services.snapshot_uri``; this module only validates input, enriches
from the ``Camera`` row when ``camera_id`` is given, and maps errors to
HTTP status codes.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

try:
    from app.api.deps import db_session
except Exception:  # noqa: BLE001
    db_session = None  # type: ignore[assignment]

from app.services import snapshot_uri as svc


class SnapshotUriRequest(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    camera_id: Optional[int] = Field(default=None)
    camera: Optional[int] = Field(default=None)
    ip: Optional[str] = None
    host: Optional[str] = None
    onvif_host: Optional[str] = None
    username: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    port: Optional[int] = None
    onvif_port: Optional[int] = None
    control_port: Optional[int] = None
    scheme: Optional[str] = None
    control_scheme: Optional[str] = None
    profile_token: Optional[str] = None
    profileToken: Optional[str] = None
    profile: Optional[str] = None

    def resolved_camera_id(self) -> Optional[int]:
        for cand in (self.camera_id, self.camera):
            if cand is not None:
                try:
                    return int(cand)
                except (TypeError, ValueError):
                    continue
        return None

    def resolved_ip(self) -> str:
        return str(self.ip or self.host or self.onvif_host or "").strip()

    def resolved_username(self) -> str:
        return str(self.username if self.username is not None else (self.user or ""))

    def resolved_port(self):
        return self.port or self.onvif_port or self.control_port

    def resolved_scheme(self):
        return self.scheme or self.control_scheme

    def resolved_profile(self) -> Optional[str]:
        return self.profile_token or self.profileToken or self.profile


def _payload_to_dict(payload: Any) -> dict:
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
            return dict(data) if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    if isinstance(payload, dict):
        return dict(payload)
    return {}


def _merge_query(data: dict, request: Request | None) -> dict:
    if request is None:
        return data
    try:
        for k, v in request.query_params.items():
            if k not in data or data.get(k) is None or data.get(k) == "":
                data[k] = v
    except Exception:  # noqa: BLE001
        pass
    # Case-insensitive fallback for common keys.
    lower = {str(k).lower(): k for k in list(data.keys())}
    try:
        qp = dict(request.query_params)
        qp_lower = {str(k).lower(): v for k, v in qp.items()}
        for want in ("camera_id", "ip", "host", "username", "password",
                     "port", "scheme", "profile_token"):
            if not data.get(want) and want in qp_lower:
                data[want] = qp_lower[want]
            for alias, canonical in (("camera", "camera_id"), ("user", "username"),
                                     ("pass", "password"), ("profiletoken", "profile_token"),
                                     ("profile", "profile_token")):
                if canonical not in data or data.get(canonical) is None:
                    if alias in qp_lower and qp_lower[alias] not in (None, ""):
                        data[canonical] = qp_lower[alias]
        _ = lower
    except Exception:  # noqa: BLE001
        pass
    return data


async def _enrich_from_camera(data: dict, db: Any) -> dict:
    cid = data.get("camera_id")
    if cid is None:
        for key in ("camera", "cam", "id"):
            if data.get(key) is not None:
                cid = data.get(key)
                data["camera_id"] = cid
                break
    if cid is None or db is None:
        return data
    try:
        coerced = int(str(cid).strip())
    except (TypeError, ValueError):
        return data
    try:
        from app.models.entities import Camera

        cam = await db.get(Camera, coerced)
    except Exception:  # noqa: BLE001
        return data
    if cam is None:
        return data
    for attr, keys in (
        ("ip_address", ("ip", "host", "onvif_host")),
        ("onvif_host", ("ip", "host", "onvif_host")),
        ("username", ("username", "user")),
        ("password", ("password",)),
        ("onvif_port", ("port", "onvif_port")),
        ("control_scheme", ("scheme", "control_scheme")),
    ):
        try:
            val = getattr(cam, attr, None)
        except Exception:  # noqa: BLE001
            val = None
        if val in (None, ""):
            continue
        for k in keys:
            if data.get(k) in (None, ""):
                data[k] = val
                break
    return data


async def _do_resolve(data: dict, db: Any = None) -> dict:
    data = dict(data or {})
    data = await _enrich_from_camera(data, db)
    try:
        record = await svc.resolve_snapshot_uri(
            camera_id=data.get("camera_id", data.get("camera", data.get("id"))),
            ip=data.get("ip", data.get("host", data.get("onvif_host"))),
            username=data.get("username", data.get("user")),
            password=data.get("password", data.get("pass", data.get("pwd"))),
            port=data.get("port", data.get("onvif_port", data.get("control_port"))),
            scheme=data.get("scheme", data.get("control_scheme")),
            profile_token=data.get("profile_token", data.get("profileToken", data.get("profile", data.get("token")))),
            db=db,
            camera=data.get("camera_obj"),
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"status": "ok", "ok": True, "success": True, **record}


async def resolve_snapshot_uri_ep(
    payload: Any = Body(default=None), request: Request = None, db: AsyncSession = None,  # type: ignore[assignment]
):
    data = _merge_query(_payload_to_dict(payload), request)
    return await _do_resolve(data, db)


async def get_stored_ep(camera_id: int, request: Request = None, db: Any = None):
    # ``request``/``db`` accepted so GET-with-query and Depends both work.
    _ = (request, db)
    rec = svc.get_stored_snapshot_uri(camera_id)
    if rec is None:
        try:
            rec = svc.get_stored_snapshot_uri("camera:{}".format(int(camera_id)))
        except (TypeError, ValueError):
            rec = None
    if rec is None:
        raise HTTPException(status_code=404, detail="No stored snapshot URI for camera")
    return {"status": "ok", "ok": True, "success": True, "camera_id": camera_id, **rec}


async def list_stored_ep():
    return {"status": "ok", "ok": True, "success": True,
            "items": svc.list_stored_snapshot_uris(),
            "count": len(svc.list_stored_snapshot_uris())}


async def clear_stored_ep(camera_id: Any = None, request: Request = None):
    cid: Any = camera_id
    if cid is None and request is not None:
        try:
            qp = dict(request.query_params)
            for key in ("camera_id", "camera", "id", "key", "target"):
                if qp.get(key) not in (None, ""):
                    cid = qp.get(key)
                    break
        except Exception:  # noqa: BLE001
            pass
    if cid is None:
        svc.clear_all_snapshot_uris()
        return {"status": "ok", "ok": True, "success": True, "cleared": True, "cleared_all": True}
    ok = svc.clear_stored_snapshot_uri(cid)
    if not ok:
        raise HTTPException(status_code=404, detail="No stored snapshot URI for camera")
    return {"status": "ok", "ok": True, "success": True, "cleared": True, "camera_id": cid}


router = APIRouter()

_PREFIXES = [
    "/api/onvif/snapshot-uri",
    "/api/onvif/snapshot_uri",
    "/api/snapshot-uri",
    "/api/snapshot_uri",
]

for _prefix in _PREFIXES:
    _r = APIRouter(prefix=_prefix, tags=["snapshot-uri"])
    # Resolve (GetSnapshotUri + store per camera).
    if db_session is not None:
        from fastapi import Depends as _Depends

        @_r.post("/resolve")
        async def _resolve_post(payload: Any = Body(default=None), request: Request = None, db: AsyncSession = _Depends(db_session)):  # type: ignore[valid-type]
            return await resolve_snapshot_uri_ep(payload, request, db)

        @_r.post("/fetch")
        async def _fetch_post(payload: Any = Body(default=None), request: Request = None, db: AsyncSession = _Depends(db_session)):  # type: ignore[valid-type]
            return await resolve_snapshot_uri_ep(payload, request, db)

        @_r.post("/get")
        async def _get_post(payload: Any = Body(default=None), request: Request = None, db: AsyncSession = _Depends(db_session)):  # type: ignore[valid-type]
            return await resolve_snapshot_uri_ep(payload, request, db)

        @_r.post("")
        async def _resolve_root_post(payload: Any = Body(default=None), request: Request = None, db: AsyncSession = _Depends(db_session)):  # type: ignore[valid-type]
            return await resolve_snapshot_uri_ep(payload, request, db)

        @_r.get("/resolve")
        async def _resolve_get(request: Request = None, db: AsyncSession = _Depends(db_session)):  # type: ignore[valid-type]
            return await resolve_snapshot_uri_ep(None, request, db)

        @_r.get("/fetch")
        async def _fetch_get(request: Request = None, db: AsyncSession = _Depends(db_session)):  # type: ignore[valid-type]
            return await resolve_snapshot_uri_ep(None, request, db)

        @_r.get("/get")
        async def _get_get(request: Request = None, db: AsyncSession = _Depends(db_session)):  # type: ignore[valid-type]
            return await resolve_snapshot_uri_ep(None, request, db)

        @_r.get("")
        async def _resolve_root_get(request: Request = None, db: AsyncSession = _Depends(db_session)):  # type: ignore[valid-type]
            return await resolve_snapshot_uri_ep(None, request, db)

        @_r.get("/stored")
        async def _stored_list(request: Request = None, db: AsyncSession = _Depends(db_session)):  # type: ignore[valid-type]
            _ = (request, db)
            return await list_stored_ep()

        @_r.get("/list")
        async def _stored_list2(request: Request = None, db: AsyncSession = _Depends(db_session)):  # type: ignore[valid-type]
            _ = (request, db)
            return await list_stored_ep()

        @_r.get("/camera/{camera_id}")
        async def _stored_one(camera_id: int, request: Request = None, db: AsyncSession = _Depends(db_session)):  # type: ignore[valid-type]
            return await get_stored_ep(camera_id, request, db)

        @_r.delete("/camera/{camera_id}")
        async def _clear_one(camera_id: int, request: Request = None, db: AsyncSession = _Depends(db_session)):  # type: ignore[valid-type]
            _ = db
            return await clear_stored_ep(camera_id, request)

        @_r.delete("/clear")
        async def _clear_all(request: Request = None, db: AsyncSession = _Depends(db_session)):  # type: ignore[valid-type]
            _ = db
            return await clear_stored_ep(None, request)
    else:  # pragma: no cover - fallback when deps unavailable
        @_r.post("/resolve")
        async def _resolve_post(payload: Any = Body(default=None), request: Request = None):  # type: ignore[no-redef]
            return await resolve_snapshot_uri_ep(payload, request)

        @_r.post("/fetch")
        async def _fetch_post(payload: Any = Body(default=None), request: Request = None):  # type: ignore[no-redef]
            return await resolve_snapshot_uri_ep(payload, request)

        @_r.post("/get")
        async def _get_post(payload: Any = Body(default=None), request: Request = None):  # type: ignore[no-redef]
            return await resolve_snapshot_uri_ep(payload, request)

        @_r.post("")
        async def _resolve_root_post(payload: Any = Body(default=None), request: Request = None):  # type: ignore[no-redef]
            return await resolve_snapshot_uri_ep(payload, request)

        @_r.get("/resolve")
        async def _resolve_get(request: Request = None):  # type: ignore[no-redef]
            return await resolve_snapshot_uri_ep(None, request)

        @_r.get("/fetch")
        async def _fetch_get(request: Request = None):  # type: ignore[no-redef]
            return await resolve_snapshot_uri_ep(None, request)

        @_r.get("/get")
        async def _get_get(request: Request = None):  # type: ignore[no-redef]
            return await resolve_snapshot_uri_ep(None, request)

        @_r.get("")
        async def _resolve_root_get(request: Request = None):  # type: ignore[no-redef]
            return await resolve_snapshot_uri_ep(None, request)

        @_r.get("/stored")
        async def _stored_list(request: Request = None):  # type: ignore[no-redef]
            _ = request
            return await list_stored_ep()

        @_r.get("/list")
        async def _stored_list2(request: Request = None):  # type: ignore[no-redef]
            _ = request
            return await list_stored_ep()

        @_r.get("/camera/{camera_id}")
        async def _stored_one(camera_id: int, request: Request = None):  # type: ignore[no-redef]
            return await get_stored_ep(camera_id, request)

        @_r.delete("/camera/{camera_id}")
        async def _clear_one(camera_id: int, request: Request = None):  # type: ignore[no-redef]
            return await clear_stored_ep(camera_id, request)

        @_r.delete("/clear")
        async def _clear_all(request: Request = None):  # type: ignore[no-redef]
            return await clear_stored_ep(None, request)

    router.include_router(_r)


# Side-effect registration so the routers are live even when this module is
# imported after app startup (mirrors snapshot-fallback pattern).
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    if id(router) not in _ids:
        _main_app.include_router(router)
        try:
            _main_app._registered_router_ids = getattr(_main_app, "_registered_router_ids", set()) | {id(router)}
        except Exception:  # noqa: BLE001
            pass
except Exception:  # noqa: BLE001
    pass
