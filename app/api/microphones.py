"""Microphone / audio-input device registry API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

from app.services import microphones as svc_mod
from app.services.microphones import (
    create_microphone,
    delete_microphone,
    get_microphone,
    list_microphones,
    update_microphone,
)

router = APIRouter(tags=["microphones"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        s = payload.strip()
        data = {"name": s} if s else {}
    elif isinstance(payload, list):
        # Single-item list payload treated as one microphone.
        data = dict(payload[0]) if payload and isinstance(payload[0], dict) else {"microphones": list(payload)}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"name": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {"name": str(payload)}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass

    for nested_key in (
        "microphone",
        "microphones",
        "audio_input",
        "audioInput",
        "audio_inputs",
        "audio-inputs",
        "device",
        "input",
        "payload",
        "params",
        "data",
        "body",
        "config",
        "request",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _coerce_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    s = str(value).strip()
    return s if s else None


def _register_collection(r: APIRouter, base: str) -> None:
    @r.get(base, summary="List microphones / audio inputs")
    @r.get(base + "/", include_in_schema=False)
    async def _list(
        request: Request,
        q: Optional[str] = Query(None),
        search: Optional[str] = Query(None),
        name: Optional[str] = Query(None),
        kind: Optional[str] = Query(None),
        type: Optional[str] = Query(None),  # noqa: A002
        enabled: Optional[str] = Query(None),
    ):
        data = _normalize(None, request)
        # Explicit query params win over generic normalization.
        if q is not None:
            data["q"] = q
        if search is not None:
            data["search"] = search
        if name is not None:
            data["q"] = name
        if kind is not None:
            data["kind"] = kind
        if type is not None:
            data.setdefault("kind", type)
        if enabled is not None:
            data["enabled"] = enabled
        return list_microphones(data)

    @r.post(base, status_code=status.HTTP_201_CREATED, summary="Register microphone / audio input")
    @r.post(base + "/", status_code=status.HTTP_201_CREATED, include_in_schema=False)
    async def _create(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        try:
            return create_microphone(data)
        except ValueError as exc:
            msg = str(exc)
            code = status.HTTP_409_CONFLICT if "already exists" in msg else status.HTTP_422_UNPROCESSABLE_ENTITY
            raise HTTPException(status_code=code, detail=msg)


def _register_item(r: APIRouter, base: str) -> None:
    @r.get(base + "/{microphone_id}", summary="Get microphone / audio input")
    @r.get(base + "/{microphone_id}/", include_in_schema=False)
    async def _get_one(microphone_id: str, request: Request):
        _normalize(None, request)
        mid = _coerce_id(microphone_id)
        item = get_microphone(mid)
        if item is None:
            raise HTTPException(status_code=404, detail="Microphone not found")
        return item

    @r.put(base + "/{microphone_id}", summary="Update microphone / audio input")
    @r.put(base + "/{microphone_id}/", include_in_schema=False)
    @r.patch(base + "/{microphone_id}", include_in_schema=False)
    @r.patch(base + "/{microphone_id}/", include_in_schema=False)
    async def _update(microphone_id: str, payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _normalize(payload, request)
        mid = _coerce_id(microphone_id)
        if mid is None or not mid:
            raise HTTPException(status_code=400, detail="microphone_id is required")
        updated = update_microphone(mid, data)
        if updated is None:
            raise HTTPException(status_code=404, detail="Microphone not found")
        return updated

    @r.delete(base + "/{microphone_id}", summary="Delete microphone / audio input")
    @r.delete(base + "/{microphone_id}/", include_in_schema=False)
    async def _delete(microphone_id: str):
        mid = _coerce_id(microphone_id)
        ok = delete_microphone(mid)
        if not ok:
            raise HTTPException(status_code=404, detail="Microphone not found")
        return {"ok": True, "deleted": True, "id": mid, "microphone_id": mid}

    @r.delete(base, summary="Clear microphones / audio inputs")
    @r.delete(base + "/", include_in_schema=False)
    async def _clear():
        removed = svc_mod.clear()
        return {"ok": True, "cleared": removed, "count": 0, "microphones": []}


_BASES = (
    "/api/microphones",
    "/api/audio_inputs",
    "/api/audio-inputs",
)

_seen: set = set()
for _base in _BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen:
        continue
    _seen.add(_norm)
    _register_collection(router, _norm)
    _register_item(router, _norm)


# Compatibility aliases.
microphones_router = router
microphone_router = router
mics_router = router
mic_router = router
audio_inputs_router = router
audio_input_router = router
audio_router = router
api_router = router

__all__ = ["router"]
