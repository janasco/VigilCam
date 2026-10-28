"""ONVIF relay / digital-output endpoints.

Auto-registered by ``app.main``'s ``pkgutil`` loader (module exposes
``router``), so no ``app/main.py`` edit is needed. Service work lives in
``app.services.relay_output``; this module only validates input, resolves the
control endpoint via ``onvif_digest_service`` where available, and maps
errors to HTTP status codes.

Distinct from ``app.api.relay`` (``/api/relay``, an HTTP relay-server ping
helper): every path here contains ``relay-output`` or lives under
``/api/onvif/relay`` so the two routers never collide.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.services import relay_output as relay_service
from app.services import onvif_digest_service as ods


class RelayOutputReadRequest(BaseModel):
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

    def resolved_ip(self) -> str:
        return self.ip or self.host or self.onvif_host or ""

    def resolved_username(self) -> str:
        return self.username or self.user or ""

    def resolved_port(self):
        return self.port or self.onvif_port or self.control_port

    def resolved_scheme(self):
        return self.scheme or self.control_scheme


class RelayOutputStateRequest(RelayOutputReadRequest):
    token: Optional[str] = None
    relay_token: Optional[str] = Field(default=None, alias="relay_token")
    relay: Optional[str] = None
    output: Optional[str] = None
    state: Optional[str] = None
    logical_state: Optional[str] = Field(default=None, alias="logical_state")
    action: Optional[str] = None

    def resolved_token(self) -> str:
        return self.token or self.relay_token or self.relay or self.output or ""

    def resolved_state(self) -> str:
        return self.state or self.logical_state or self.action or ""


class RelayOutputSettingsRequest(RelayOutputReadRequest):
    token: Optional[str] = None
    relay_token: Optional[str] = Field(default=None, alias="relay_token")
    relay: Optional[str] = None
    output: Optional[str] = None
    mode: Optional[str] = None
    delay_time: Optional[str] = None
    delay_seconds: Optional[float] = None
    delay: Optional[Any] = None
    idle_state: Optional[str] = Field(default=None, alias="idle_state")

    def resolved_token(self) -> str:
        return self.token or self.relay_token or self.relay or self.output or ""

    def resolved_delay(self) -> Any:
        if self.delay_time is not None:
            return self.delay_time
        if self.delay_seconds is not None:
            return self.delay_seconds
        return self.delay


async def _resolve(request: RelayOutputReadRequest):
    ip = request.resolved_ip()
    if not ip:
        raise HTTPException(status_code=422, detail="ip/host is required")
    username = request.resolved_username()
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


def _model_data(payload: Any) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        data = {}
    return data


def _merge_query(data: dict[str, Any], request: Request | None) -> dict[str, Any]:
    if request is None:
        return data
    try:
        for k, v in request.query_params.items():
            if k not in data or data.get(k) is None:
                data[k] = v
    except Exception:  # noqa: BLE001
        pass
    return data


async def list_relays(
    payload: RelayOutputReadRequest | dict | None = None, request: Request = None
):
    data = _merge_query(_model_data(payload), request)
    model = (
        payload
        if isinstance(payload, RelayOutputReadRequest)
        else RelayOutputReadRequest(**data)
    )
    if request is not None:
        try:
            qp = dict(request.query_params)
            if qp:
                merged = {
                    **model.model_dump(),
                    **{k: v for k, v in qp.items() if v != ""},
                }
                model = RelayOutputReadRequest(**merged)
        except Exception:  # noqa: BLE001
            pass
    ip, username, password, port, scheme = await _resolve(model)
    try:
        result = await relay_service.get_relay_outputs(ip, username, password, port, scheme)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"status": "ok", **result}


async def get_state_alias(
    payload: RelayOutputReadRequest | dict | None = None, request: Request = None
):
    return await list_relays(payload, request)


async def set_state(
    payload: RelayOutputStateRequest | dict | None = None, request: Request = None
):
    data = _merge_query(_model_data(payload), request)
    model = (
        payload
        if isinstance(payload, RelayOutputStateRequest)
        else RelayOutputStateRequest(**data)
    )
    ip, username, password, port, scheme = await _resolve(model)
    token = model.resolved_token()
    state = model.resolved_state()
    if not token:
        raise HTTPException(status_code=422, detail="relay token is required")
    if not state:
        raise HTTPException(status_code=422, detail="state is required (active/inactive)")
    try:
        result = await relay_service.set_relay_output_state(
            ip, username, password, token, state, port, scheme
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"status": "ok", **result}


async def trigger(
    payload: RelayOutputStateRequest | dict | None = None, request: Request = None
):
    data = _merge_query(_model_data(payload), request)
    model = (
        payload
        if isinstance(payload, RelayOutputStateRequest)
        else RelayOutputStateRequest(**data)
    )
    ip, username, password, port, scheme = await _resolve(model)
    token = model.resolved_token()
    if not token:
        raise HTTPException(status_code=422, detail="relay token is required")
    try:
        result = await relay_service.trigger_relay_output(
            ip, username, password, token, port, scheme
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"status": "ok", **result}


async def set_settings(
    payload: RelayOutputSettingsRequest | dict | None = None, request: Request = None
):
    data = _merge_query(_model_data(payload), request)
    model = (
        payload
        if isinstance(payload, RelayOutputSettingsRequest)
        else RelayOutputSettingsRequest(**data)
    )
    ip, username, password, port, scheme = await _resolve(model)
    token = model.resolved_token()
    if not token:
        raise HTTPException(status_code=422, detail="relay token is required")
    delay = model.resolved_delay()
    if (
        model.mode is None
        and delay is None
        and model.idle_state is None
        and data.get("idleState") is None
    ):
        raise HTTPException(
            status_code=422,
            detail="At least one of mode, delay_time/delay_seconds, idle_state is required",
        )
    idle = model.idle_state or data.get("idleState")
    try:
        result = await relay_service.set_relay_output_settings(
            ip,
            username,
            password,
            token,
            mode=model.mode,
            delay_time=delay,
            idle_state=idle,
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
    "/api/onvif/relay",
    "/api/onvif/relay-output",
    "/api/onvif-relay-output",
    "/api/relay-output",
]

for _prefix in _PREFIXES:
    _r = APIRouter(prefix=_prefix, tags=["onvif-relay-output"])
    for _path in ("/list", "/outputs", "/get", "/show", "/state"):
        _r.post(_path)(list_relays)
        _r.get(_path)(list_relays)
    _r.get("")(list_relays)
    for _path in ("/set", "/set-state", "/set_state", "/state/set", "/output"):
        _r.post(_path)(set_state)
        _r.put(_path)(set_state)
        _r.patch(_path)(set_state)
    for _path in ("/trigger", "/pulse", "/activate", "/trip"):
        _r.post(_path)(trigger)
    for _path in ("/settings", "/configure", "/config"):
        _r.post(_path)(set_settings)
        _r.put(_path)(set_settings)
        _r.patch(_path)(set_settings)
        _r.get(_path)(get_state_alias)
    router.include_router(_r)
