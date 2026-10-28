"""Motion auto-tune API router.

Analyses sample frames / noise and recommends a motion threshold.
Complements the static ``motion_presets`` levels (which perform no scene
analysis) with a data-driven recommendation.

Auto-registered by ``app.main`` (any ``app.api`` module exposing
``router`` is included automatically), so no ``main.py`` edit is needed.
"""

from __future__ import annotations

import base64
from typing import Any, List, Optional

import cv2
import numpy as np
from fastapi import APIRouter, Body, HTTPException, Request

from app.services import motion_autotune as autotune_service

router = APIRouter(tags=["motion_autotune"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"frames": [payload]}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"frames": [bytes(payload).decode("utf-8", errors="replace")]}
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, (list, tuple)):
        data = {"frames": list(payload)}
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

    for nested_key in ("payload", "options", "config", "params", "data",
                       "body", "request", "settings", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _collect_frames(data: dict) -> List[Any]:
    frames: List[Any] = []
    for k in ("frames", "images", "samples", "frames_base64", "images_base64",
              "samples_base64", "frame_list"):
        v = data.get(k)
        if isinstance(v, (list, tuple)):
            frames.extend(list(v))
        elif isinstance(v, str) and v.strip():
            frames.append(v)
    for k in ("image_base64", "image", "frame", "frame_base64", "current",
              "current_image", "current_frame", "snapshot", "snapshot_base64",
              "previous_image_base64", "previous", "prev", "prev_image",
              "previous_image", "previous_frame", "prev_frame", "before",
              "before_image", "b64", "base64", "content", "encoded", "img",
              "picture"):
        v = data.get(k)
        if v is not None and not isinstance(v, (list, tuple, dict)):
            frames.append(v)
        elif isinstance(v, (list, tuple)) and k not in ("frames",):
            frames.extend(list(v))
    # De-duplicate while preserving order is unnecessary; keep as-is.
    return [f for f in frames if f is not None and f != ""]


def _synthetic_frames(width: int, height: int, count: int = 8,
                      noise_sigma: float = 5.0, seed: int = 7) -> List[str]:
    rng = np.random.default_rng(int(seed))
    out: List[str] = []
    base = np.full((height, width, 3), 128, dtype=np.int16)
    for _ in range(max(2, count)):
        noisy = base + rng.normal(0.0, float(noise_sigma), base.shape)
        img = np.clip(noisy, 0, 255).astype(np.uint8)
        ok, buf = cv2.imencode(".png", img)
        if ok:
            out.append(base64.b64encode(buf.tobytes()).decode("ascii"))
    return out


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _run_autotune(data: dict) -> dict:
    width = _as_int(data.get("width", data.get("w", data.get("frame_width"))), None)
    height = _as_int(data.get("height", data.get("h", data.get("frame_height"))), None)
    if width is None:
        width = 640
    if height is None:
        height = 480
    width = max(16, min(4096, width))
    height = max(16, min(4096, height))

    margin = _as_float(data.get("margin", data.get("threshold_margin",
                         autotune_service.DEFAULT_MARGIN)),
                       float(autotune_service.DEFAULT_MARGIN))
    min_thr = _as_int(data.get("minimum_threshold", data.get("min_threshold",
                       data.get("min", autotune_service.MIN_THRESHOLD))),
                      autotune_service.MIN_THRESHOLD)
    max_thr = _as_int(data.get("maximum_threshold", data.get("max_threshold",
                       data.get("max", autotune_service.MAX_THRESHOLD))),
                      autotune_service.MAX_THRESHOLD)

    frames = _collect_frames(data)

    def _truthy(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}

    synthetic = data.get("synthetic", data.get("use_synthetic", data.get("demo")))
    if not frames and synthetic is not None and _truthy(synthetic):
        count = _as_int(data.get("count", data.get("num_frames", 8)), 8) or 8
        count = max(2, min(32, count))
        noise_sigma = _as_float(data.get("noise_sigma",
                                data.get("noise", data.get("sigma", 5.0))), 5.0)
        seed = _as_int(data.get("seed", 7), 7) or 7
        frames = _synthetic_frames(width, height, count=count,
                                   noise_sigma=noise_sigma, seed=seed)
    if not frames:
        raise HTTPException(
            status_code=400,
            detail="No sample frames provided (frames/image_base64 or synthetic=true)",
        )
    try:
        return autotune_service.autotune(
            frames,
            width=width,
            height=height,
            margin=margin,
            minimum_threshold=min_thr,
            maximum_threshold=max_thr,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Auto-tune failed: {exc}")


_PATHS = (
    "/api/motion_autotune",
    "/api/motion-autotune",
    "/api/motion/autotune",
    "/api/motion/auto-tune",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_root(request: Request):
        return {
            "status": "ok",
            "service": "motion_autotune",
            "endpoint": f"POST {base}/analyze",
            "description": "Analyse sample frames/noise and recommend a motion threshold.",
        }

    @r.get(base + "/")
    async def get_slash(request: Request):
        return await get_root(request)

    @r.get(base + "/status")
    async def get_status(request: Request):
        return {"status": "ok", "service": "motion_autotune"}

    @r.post(base + "/analyze")
    async def post_analyze(payload: Any = Body(default=None), request: Request = None):
        return _run_autotune(_normalize(payload, request))

    @r.post(base + "/recommend")
    async def post_recommend(payload: Any = Body(default=None), request: Request = None):
        return _run_autotune(_normalize(payload, request))

    @r.post(base + "/autotune")
    async def post_autotune(payload: Any = Body(default=None), request: Request = None):
        return _run_autotune(_normalize(payload, request))

    @r.post(base + "/auto-tune")
    async def post_auto_tune(payload: Any = Body(default=None), request: Request = None):
        return _run_autotune(_normalize(payload, request))

    @r.post(base + "/tune")
    async def post_tune(payload: Any = Body(default=None), request: Request = None):
        return _run_autotune(_normalize(payload, request))

    @r.post(base)
    async def post_root(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not _collect_frames(data) and data.get("synthetic") is None:
            return await get_root(request)
        return _run_autotune(data)

    @r.post(base + "/")
    async def post_slash(payload: Any = Body(default=None), request: Request = None):
        return await post_root(payload, request)

    @r.get(base + "/analyze")
    async def get_analyze():
        return {"status": "ok", "endpoint": f"POST {base}/analyze"}


for base in _PATHS:
    _register(router, base)


# Prefixed alias routers (for callers expecting router.prefix to be set).
underscore_router = APIRouter(prefix="/api/motion_autotune", tags=["motion_autotune"])
hyphen_router = APIRouter(prefix="/api/motion-autotune", tags=["motion_autotune"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root(request: Request):
        return {"status": "ok", "service": "motion_autotune",
                "endpoint": "POST /analyze"}

    @r.get("/")
    async def slash(request: Request):
        return await root(request)

    @r.get("/status")
    async def status(request: Request):
        return {"status": "ok", "service": "motion_autotune"}

    @r.post("/analyze")
    async def analyze(payload: Any = Body(default=None), request: Request = None):
        return _run_autotune(_normalize(payload, request))

    @r.post("/recommend")
    async def recommend(payload: Any = Body(default=None), request: Request = None):
        return _run_autotune(_normalize(payload, request))

    @r.post("/autotune")
    async def autotune(payload: Any = Body(default=None), request: Request = None):
        return _run_autotune(_normalize(payload, request))

    @r.post("/tune")
    async def tune(payload: Any = Body(default=None), request: Request = None):
        return _run_autotune(_normalize(payload, request))

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        if not _collect_frames(data) and data.get("synthetic") is None:
            return await root(request)
        return _run_autotune(data)

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None):
        return await root_post(payload, request)

    @r.get("/analyze")
    async def analyze_get():
        return {"status": "ok", "endpoint": "POST /analyze"}


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)


# Compatibility aliases.
motion_autotune_router = underscore_router
motionAutotuneRouter = underscore_router
motion_autotune_api_router = underscore_router
motion_autotune_hyphen_router = hyphen_router
motion_auto_tune_router = underscore_router

__all__ = [
    "router",
    "underscore_router",
    "hyphen_router",
    "motion_autotune_router",
    "motionAutotuneRouter",
    "motion_autotune_api_router",
    "motion_autotune_hyphen_router",
    "motion_auto_tune_router",
]
