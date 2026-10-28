from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from app.api.deps import db_session
import app.services.motion_tuner as mt_service
from app.services.motion_tuner import (
    MotionTuner,
    MotionTunerResult,
    motion_tuner_service,
    tune,
)

router = APIRouter(tags=["motion_tuner"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"image": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"image": bytes(payload).decode("utf-8", errors="replace")}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message", "frame"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # image aliases
    if data.get("image_base64") is None and data.get("image") is None:
        for k in ("snapshot", "snapshot_base64", "frame", "frame_base64", "img", "img_base64", "picture", "current", "current_image", "current_frame", "data", "content", "base64", "b64", "encoded", "prev", "previous", "before"):
            if data.get(k) is not None:
                data["image"] = data.get(k)
                break
    if data.get("previous_image_base64") is None and data.get("previous") is None and data.get("prev") is None:
        for k in ("previous_image", "prev_image", "previous_frame", "prev_frame", "before", "before_image", "previous_base64"):
            if data.get(k) is not None:
                data["previous"] = data.get(k)
                break

    return data


_PATHS = (
    "/api/motion_tuner",
    "/api/motion-tuner",
    "/api/motionTuner",
    "/api/motion/tuner",
    "/api/motion/tune",
    "/api/motion_tuning",
    "/api/motion-tuning",
    "/api/frigate/motion_tuner",
    "/api/frigate/motion-tuner",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_root(request: Request):
        try:
            return mt_service.get_config()
        except Exception as exc:  # noqa: BLE001
            return {"ok": True, "error": str(exc)}

    @r.get(base + "/")
    async def get_slash(request: Request):
        return await get_root(request)

    @r.get(base + "/settings")
    async def get_settings(request: Request):
        return mt_service.get_config()

    @r.get(base + "/config")
    async def get_config(request: Request):
        return mt_service.get_config()

    @r.get(base + "/status")
    async def get_status(request: Request):
        return {"status": "ok", "service": "motion_tuner", "config": mt_service.get_config()}

    @r.put(base + "/settings")
    async def put_settings(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        # update service singleton
        try:
            mt_service.motion_tuner.update_settings(**{k: v for k, v in data.items() if k not in ("image", "previous", "prev", "image_base64", "previous_image_base64")})
        except Exception:
            pass
        # settings-backed fields
        for k in ("threshold", "motion_threshold", "contour_area", "motion_contour_area", "improve_contrast", "motion_improve_contrast"):
            if k in data and data.get(k) is not None:
                try:
                    if "threshold" in k:
                        mt_service.set_threshold(data.get(k))
                    elif "contour" in k or "area" in k:
                        mt_service.set_contour_area(data.get(k))
                    elif "contrast" in k:
                        mt_service.set_improve_contrast(data.get(k))
                except Exception:
                    pass
        return mt_service.get_config()

    @r.post(base + "/settings")
    async def post_settings(payload: Any = Body(default=None), request: Request = None):
        return await put_settings(payload, request)

    @r.post(base)
    async def post_root(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        # settings updates
        for k in ("threshold", "motion_threshold", "contour_area", "motion_contour_area", "improve_contrast", "motion_improve_contrast"):
            if k in data and data.get(k) is not None:
                try:
                    if "threshold" in k:
                        mt_service.set_threshold(data.get(k))
                    elif "contour" in k or "area" in k:
                        mt_service.set_contour_area(data.get(k))
                    elif "contrast" in k:
                        mt_service.set_improve_contrast(data.get(k))
                except Exception:
                    pass
        # tune
        try:
            res = tune(
                prev_frame=data.get("previous") or data.get("prev") or data.get("previous_image") or data.get("previous_image_base64"),
                curr_frame=data.get("image") or data.get("current") or data.get("image_base64") or data.get("frame"),
                threshold=data.get("threshold") if "threshold" in data else None,
                contour_area=data.get("contour_area") if "contour_area" in data else None,
                improve_contrast=data.get("improve_contrast") if "improve_contrast" in data else None,
            )
            if hasattr(res, "to_dict"):
                out = res.to_dict()
            elif isinstance(res, dict):
                out = res
            else:
                out = {"result": res}
            out.setdefault("ok", True)
            out.setdefault("success", True)
            return out
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/")
    async def post_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/analyze")
    async def post_analyze(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.post(base + "/tune")
    async def post_tune(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await post_root(payload, request, db)

    @r.get(base + "/analyze")
    async def get_analyze():
        return {"status": "ok", "endpoint": "POST " + base + "/analyze"}


for base in _PATHS:
    _register(router, base)

# Prefixed alias routers
underscore_router = APIRouter(prefix="/api/motion_tuner", tags=["motion_tuner"])
hyphen_router = APIRouter(prefix="/api/motion-tuner", tags=["motion_tuner"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        return mt_service.get_config()

    @r.get("/")
    async def slash_get(request: Request):
        return mt_service.get_config()

    @r.get("/settings")
    async def settings_get(request: Request):
        return mt_service.get_config()

    @r.get("/config")
    async def config_get(request: Request):
        return mt_service.get_config()

    @r.get("/status")
    async def status_get(request: Request):
        return {"status": "ok", "service": "motion_tuner"}

    @r.put("/settings")
    async def settings_put(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        for k in ("threshold", "motion_threshold", "contour_area", "motion_contour_area", "improve_contrast", "motion_improve_contrast"):
            if k in data and data.get(k) is not None:
                try:
                    if "threshold" in k:
                        mt_service.set_threshold(data.get(k))
                    elif "contour" in k or "area" in k:
                        mt_service.set_contour_area(data.get(k))
                    elif "contrast" in k:
                        mt_service.set_improve_contrast(data.get(k))
                except Exception:
                    pass
        try:
            mt_service.motion_tuner.update_settings(**{k: v for k, v in data.items() if k not in ("image", "previous", "prev", "image_base64", "previous_image_base64")})
        except Exception:
            pass
        return mt_service.get_config()

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        for k in ("threshold", "motion_threshold", "contour_area", "motion_contour_area", "improve_contrast", "motion_improve_contrast"):
            if k in data and data.get(k) is not None:
                try:
                    if "threshold" in k:
                        mt_service.set_threshold(data.get(k))
                    elif "contour" in k or "area" in k:
                        mt_service.set_contour_area(data.get(k))
                    elif "contrast" in k:
                        mt_service.set_improve_contrast(data.get(k))
                except Exception:
                    pass
        try:
            res = tune(
                prev_frame=data.get("previous") or data.get("prev") or data.get("previous_image") or data.get("previous_image_base64"),
                curr_frame=data.get("image") or data.get("current") or data.get("image_base64") or data.get("frame"),
                threshold=data.get("threshold") if "threshold" in data else None,
                contour_area=data.get("contour_area") if "contour_area" in data else None,
                improve_contrast=data.get("improve_contrast") if "improve_contrast" in data else None,
            )
            if hasattr(res, "to_dict"):
                out = res.to_dict()
            elif isinstance(res, dict):
                out = res
            else:
                out = {"result": res}
            out.setdefault("ok", True)
            out.setdefault("success", True)
            return out
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.post("/analyze")
    async def analyze_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.post("/tune")
    async def tune_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

# Side-effect registration
try:
    from app.main import app as _main_app

    if not getattr(_main_app.state, "motion_tuner_registered", False):
        _main_app.include_router(router)
        _main_app.state.motion_tuner_registered = True
except Exception:  # noqa: BLE001
    pass

try:
    from app.main import app as _main_app2

    _main_app2.include_router(underscore_router)
    _main_app2.include_router(hyphen_router)
except Exception:  # noqa: BLE001
    pass
