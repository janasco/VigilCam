"""Tour shuffle mode API endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, status

import app.services.tour_shuffle as shuffle_mod
from app.services.tour_shuffle import (
    clear,
    disable_shuffle,
    enable_shuffle,
    get_shuffle,
    is_shuffle_enabled,
    list_shuffles,
    reshuffle,
    set_shuffle,
    step_shuffled,
)

router = APIRouter(tags=["tour_shuffle"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"tour_id": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"tour_id": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "settings", "message", "tour", "patrol"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _ok(result: Any, default_status: str = "ok") -> dict:
    if not isinstance(result, dict):
        result = {"result": result, "status": default_status, "ok": True, "success": True}
    result.setdefault("status", default_status)
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


_PATHS = (
    "/api/tour_shuffle",
    "/api/tour-shuffle",
    "/api/tour_shuffles",
    "/api/tour-shuffles",
    "/api/tour/shuffle",
    "/api/tours/shuffle",
    "/api/ptz_tour/shuffle",
    "/api/ptz-tour/shuffle",
    "/api/patrol_shuffle",
    "/api/patrol/shuffle",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_shuffles(request: Request):
        data = dict(request.query_params)
        try:
            if data.get("id") or data.get("tour_id") or data.get("patrol_id"):
                return shuffle_mod.get_shuffle(**data)
            return shuffle_mod.list_shuffles(**data)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.get(base + "/")
    async def get_shuffles_slash(request: Request):
        return await get_shuffles(request)

    @r.get(base + "/list")
    async def get_shuffles_list(request: Request):
        return await get_shuffles(request)

    @r.get(base + "/status")
    async def get_shuffle_status(request: Request):
        data = dict(request.query_params)
        try:
            if data.get("id") or data.get("tour_id") or data.get("patrol_id"):
                return shuffle_mod.get_shuffle(**data)
            items = shuffle_mod.list_shuffles(**data)
            return {"shuffles": items, "count": len(items), "status": "ok",
                    "ok": True, "success": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.post(base, status_code=status.HTTP_200_OK)
    async def post_shuffles(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        action = ""
        for k in ("action", "op", "operation"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                action = v.strip().lower()
                break
        try:
            if action in ("step", "next", "advance", "tick", "step_shuffled"):
                result = step_shuffled(**data)
            elif action in ("enable", "enable_shuffle", "on"):
                result = enable_shuffle(**data)
            elif action in ("disable", "disable_shuffle", "off"):
                result = disable_shuffle(**data)
            elif action in ("reshuffle", "reshuffle_order", "shuffle", "randomize"):
                result = reshuffle(**data)
            elif action in ("status", "get", "state"):
                result = get_shuffle(**data)
            elif action in ("list",):
                return list_shuffles(**data)
            elif action in ("clear", "reset", "purge"):
                n = clear(**data)
                return {"cleared": n, "count": n, "ok": True, "success": True}
            elif action in ("delete", "remove"):
                result = disable_shuffle(**data)
            elif action in ("set", "update", "set_shuffle"):
                result = set_shuffle(**data)
            elif data.get("id") or data.get("tour_id") or data.get("patrol_id"):
                # Default with a tour id: report state (or enable when asked).
                if "enabled" in data or "shuffle" in data or "shuffle_enabled" in data:
                    result = set_shuffle(**data)
                else:
                    result = get_shuffle(**data)
            else:
                result = set_shuffle(**data)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if isinstance(result, list):
            return result
        return _ok(result, "ok")

    @r.post(base + "/")
    async def post_shuffles_slash(payload: Any = Body(default=None), request: Request = None):
        return await post_shuffles(payload, request)

    @r.post(base + "/enable")
    async def post_enable(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(enable_shuffle(**_normalize(payload, request)), "enabled")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/disable")
    async def post_disable(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(disable_shuffle(**_normalize(payload, request)), "disabled")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/set")
    async def post_set(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(set_shuffle(**_normalize(payload, request)), "ok")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/step")
    async def post_step(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(step_shuffled(**_normalize(payload, request)), "running")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/next")
    async def post_next(payload: Any = Body(default=None), request: Request = None):
        return await post_step(payload, request)

    @r.post(base + "/reshuffle")
    async def post_reshuffle(payload: Any = Body(default=None), request: Request = None):
        try:
            return _ok(reshuffle(**_normalize(payload, request)), "ok")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None):
        n = clear(**_normalize(payload, request))
        return {"cleared": n, "count": n, "removed": n, "ok": True, "success": True}

    @r.delete(base + "/{tour_id}")
    async def delete_shuffle_by_id(tour_id: str):
        try:
            return _ok(disable_shuffle(tour_id), "disabled")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @r.get(base + "/{tour_id}")
    async def get_shuffle_by_id(tour_id: str):
        try:
            return shuffle_mod.get_shuffle(tour_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))


for _base in _PATHS:
    _register(router, _base)
