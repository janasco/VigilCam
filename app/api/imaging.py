"""ONVIF imaging settings endpoints (brightness/contrast/saturation).

Auto-registered by ``app.main``'s ``pkgutil`` loader (module exposes
``router``), so no ``app/main.py`` edit is needed. Service work lives in
``app.services.imaging``; this module only validates input, resolves the
control endpoint via ``onvif_digest_service`` where available, and maps
errors to HTTP status codes.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.services import imaging as imaging_service
from app.services import onvif_digest_service as ods


class ImagingWriteRequest(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    ip: Optional[str] = None
    host: Optional[str] = None
    onvif_host: Optional[str] = None
    username: str = ""
    password: str = ""
    user: Optional[str] = None
    port: Optional[int] = None
    onvif_port: Optional[int] = None
    control_port: Optional[int] = None
    scheme: Optional[str] = None
    control_scheme: Optional[str] = None
    brightness: Optional[int] = Field(default=None, ge=0, le=100)
    contrast: Optional[int] = Field(default=None, ge=0, le=100)
    saturation: Optional[int] = Field(default=None, ge=0, le=100)

    def resolved_ip(self) -> str:
        return self.ip or self.host or self.onvif_host or ""

    def resolved_username(self) -> str:
        return self.username or self.user or ""

    def resolved_port(self):
        return self.port or self.onvif_port or self.control_port

    def resolved_scheme(self):
        return self.scheme or self.control_scheme


class ImagingReadRequest(ImagingWriteRequest):
    pass


async def _resolve(request: ImagingWriteRequest):
    ip = request.resolved_ip()
    if not ip:
        raise HTTPException(status_code=422, detail="ip/host is required")
    username = request.resolved_username()
    # Password may legitimately be empty on some cameras; keep as "".
    password = request.password or ""
    port_hint = request.resolved_port()
    scheme_hint = request.resolved_scheme()
    resolve = getattr(ods, "resolve_control_endpoint", None)
    if callable(resolve):
        try:
            scheme, port = await resolve(ip, port_hint, scheme_hint)
            return ip, username, password, port, scheme
        except Exception:  # noqa: BLE001 - graceful fallback to hints
            pass
    return ip, username, password, (port_hint or 80), (scheme_hint or "http")


def _payload_levels(payload: Any) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        data = {}
    # Accept common aliases.
    if data.get("brightness") is None and data.get("bright") is not None:
        data["brightness"] = data.get("bright")
    if data.get("contrast") is None and data.get("cont") is not None:
        data["contrast"] = data.get("cont")
    if data.get("saturation") is None:
        for alias in ("satur", "color_saturation", "colorSaturation", "sat"):
            if data.get(alias) is not None:
                data["saturation"] = data.get(alias)
                break
    return data


async def get_imaging(payload: ImagingReadRequest | dict | None = None, request: Request = None):
    data = _payload_levels(payload)
    # Merge query params for GET callers.
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    model = ImagingReadRequest(**data) if not isinstance(payload, ImagingReadRequest) else payload
    # Re-merge query params over the model (GET without body).
    if request is not None:
        try:
            qp = dict(request.query_params)
            if qp:
                merged = {**model.model_dump(), **{k: v for k, v in qp.items() if v != ""}}
                model = ImagingReadRequest(**merged)
        except Exception:  # noqa: BLE001
            pass
    ip, username, password, port, scheme = await _resolve(model)
    try:
        result = await imaging_service.get_imaging_settings(ip, username, password, port, scheme)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"status": "ok", **result}


async def set_imaging(payload: ImagingWriteRequest | dict | None = None, request: Request = None):
    data = _payload_levels(payload)
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    model = ImagingWriteRequest(**data) if not isinstance(payload, ImagingWriteRequest) else payload
    ip, username, password, port, scheme = await _resolve(model)
    levels = _payload_levels(model)
    if all(levels.get(k) is None for k in ("brightness", "contrast", "saturation")):
        raise HTTPException(
            status_code=422,
            detail="At least one of brightness, contrast, saturation is required",
        )
    try:
        result = await imaging_service.set_imaging_settings(
            ip,
            username,
            password,
            brightness=levels.get("brightness"),
            contrast=levels.get("contrast"),
            saturation=levels.get("saturation"),
            port=port,
            scheme=scheme,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"status": "ok", **result}


router = APIRouter()

_PREFIXES = [
    "/api/onvif/imaging",
    "/api/onvif-imaging",
    "/api/imaging",
]

for _prefix in _PREFIXES:
    _r = APIRouter(prefix=_prefix, tags=["onvif-imaging"])
    for _path in ("/set", "/set-imaging", "/set_imaging", "/update", "/apply", "/write"):
        _r.post(_path)(set_imaging)
        _r.put(_path)(set_imaging)
        _r.patch(_path)(set_imaging)
    _r.post("")(set_imaging)
    for _path in ("/get", "/get-imaging", "/get_imaging", "/read", "/show"):
        _r.post(_path)(get_imaging)
        _r.get(_path)(get_imaging)
    _r.get("")(get_imaging)
    router.include_router(_r)
