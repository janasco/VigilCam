"""Mask-image zone import REST endpoints.

Upload a mask PNG (white = monitored area) and receive polygon zones
compatible with ``motion_zoning`` (``PolygonZone`` dicts with normalized
0-1 ``points`` by default).

No ``app/main.py`` changes are required: the application auto-loads any
``app.api`` module exposing ``router``.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["mask_import"])

_BASES = (
    "/api/mask_import",
    "/api/mask-import",
    "/api/maskimport",
    "/api/motion/mask_import",
    "/api/motion/mask-import",
    "/api/zones/mask_import",
    "/api/zones/mask-import",
)


def _decode_b64(value: Any) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    s = str(value).strip()
    if not s:
        return None
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    s = "".join(s.split())
    pad = len(s) % 4
    if pad:
        s += "=" * (4 - pad)
    try:
        return base64.b64decode(s, validate=False)
    except Exception:
        return None


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _coerce_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _options_from_mapping(mapping: Dict[str, Any]) -> Dict[str, Any]:
    opts: Dict[str, Any] = {}
    if mapping.get("threshold") is not None:
        v = _coerce_float(mapping.get("threshold"), None)
        if v is not None:
            opts["threshold"] = v
    elif mapping.get("thresh") is not None:
        v = _coerce_float(mapping.get("thresh"), None)
        if v is not None:
            opts["threshold"] = v
    for k in ("invert", "inverted"):
        if mapping.get(k) is not None:
            opts["invert"] = _coerce_bool(mapping.get(k), False)
            break
    for k in ("min_area", "minArea", "min_area_ratio", "minAreaRatio"):
        if mapping.get(k) is not None:
            v = _coerce_float(mapping.get(k), None)
            if v is not None:
                opts[k if "ratio" in k.lower() else "min_area"] = v
            break
    for k in ("epsilon", "epsilon_ratio", "epsilonRatio", "eps", "approx_eps"):
        if mapping.get(k) is not None:
            v = _coerce_float(mapping.get(k), None)
            if v is not None:
                opts["epsilon_ratio"] = v
            break
    for k in ("max_polygons", "maxPolygons", "max_zones", "maxZones", "max", "limit"):
        if mapping.get(k) is not None:
            v = _coerce_int(mapping.get(k), None)
            if v is not None:
                opts["max_polygons"] = v
            break
    # normalize / pixels
    norm_set = False
    for k in ("normalize", "normalized", "norm"):
        if mapping.get(k) is not None:
            opts["normalize"] = _coerce_bool(mapping.get(k), True)
            norm_set = True
            break
    for k in ("pixels", "absolute", "as_pixels"):
        if mapping.get(k) is not None:
            pv = _coerce_bool(mapping.get(k), False)
            if pv and not norm_set:
                opts["normalize"] = False
            break
    for k in ("mode", "zone_mode"):
        if mapping.get(k) is not None:
            opts["mode"] = str(mapping.get(k))
            break
    for k in ("threshold_percentage", "threshold_percent", "threshold_pct", "percent"):
        if mapping.get(k) is not None and k != "threshold":
            v = _coerce_float(mapping.get(k), None)
            if v is not None:
                opts["threshold_percentage"] = v
            break
    for k in ("name_prefix", "namePrefix", "prefix"):
        if mapping.get(k) is not None:
            opts["name_prefix"] = str(mapping.get(k))
            break
    for k in ("width", "w", "frame_width", "img_width", "out_width"):
        if mapping.get(k) is not None:
            v = _coerce_float(mapping.get(k), None)
            if v is not None:
                opts["out_width"] = v
            break
    for k in ("height", "h", "frame_height", "img_height", "out_height"):
        if mapping.get(k) is not None:
            v = _coerce_float(mapping.get(k), None)
            if v is not None:
                opts["out_height"] = v
            break
    return opts


async def _extract_image_and_options(request: Request) -> tuple[bytes, Dict[str, Any]]:
    options: Dict[str, Any] = {}
    try:
        for k, v in request.query_params.items():
            options.setdefault(k, v)
    except Exception:
        pass
    ctype = (request.headers.get("content-type") or "").lower()
    image_bytes: Optional[bytes] = None

    if "multipart" in ctype:
        try:
            form = await request.form()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Could not parse upload: {exc}")
        for k, v in form.items():
            if hasattr(v, "read"):
                if image_bytes is None:
                    try:
                        data = await v.read()  # type: ignore[union-attr]
                    except Exception:
                        data = None
                    if data:
                        image_bytes = bytes(data)
                continue
            if k not in options:
                try:
                    options[k] = str(v)
                except Exception:
                    pass
        # Prefer well-known file field names when several files were sent.
        # (Already took the first; that is sufficient.)
    else:
        raw: bytes = b""
        try:
            raw = await request.body()
        except Exception:
            raw = b""
        if raw:
            # Try JSON first.
            parsed: Any = None
            try:
                import json as _json

                parsed = _json.loads(raw.decode("utf-8", errors="strict"))
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if k not in options and k not in (
                        "image", "mask", "data", "content", "base64",
                        "image_base64", "mask_base64", "image_b64", "mask_b64",
                        "png", "bytes", "file_data",
                    ):
                        options.setdefault(k, v)
                for key in (
                    "image", "mask", "data", "content", "base64",
                    "image_base64", "mask_base64", "image_b64", "mask_b64",
                    "png", "bytes", "file_data",
                ):
                    if parsed.get(key) is not None:
                        val = parsed[key]
                        if isinstance(val, str):
                            image_bytes = _decode_b64(val)
                        elif isinstance(val, (bytes, bytearray)):
                            image_bytes = bytes(val)
                        if image_bytes:
                            break
                # Also merge nested option dicts (payload/options/...).
                for nk in ("payload", "options", "config", "params", "data"):
                    nested = parsed.get(nk)
                    if isinstance(nested, dict):
                        for k, v in nested.items():
                            options.setdefault(k, v)
            elif isinstance(parsed, str):
                image_bytes = _decode_b64(parsed)
            else:
                # Raw bytes assumed to be the image itself (e.g. octet-stream).
                stripped = raw.strip() if isinstance(raw, bytes) else raw
                if stripped[:1] in (b"{", b"["):
                    raise HTTPException(status_code=400, detail="No mask image provided")
                image_bytes = bytes(raw)
        # Fallback: form without multipart content-type (TestClient edge).
        if image_bytes is None:
            try:
                form = await request.form()
                if form:
                    for k, v in form.items():
                        if hasattr(v, "read"):
                            if image_bytes is None:
                                try:
                                    data = await v.read()  # type: ignore[union-attr]
                                except Exception:
                                    data = None
                                if data:
                                    image_bytes = bytes(data)
                            continue
                        if k not in options:
                            try:
                                options[k] = str(v)
                            except Exception:
                                pass
            except Exception:
                pass

    if not image_bytes:
        raise HTTPException(status_code=400, detail="No mask image provided (upload a PNG file or send base64 JSON)")
    return image_bytes, _options_from_mapping(options)


def _zones_response(zones: List[Dict[str, Any]], width: int, height: int, normalized: bool) -> Dict[str, Any]:
    return {
        "ok": True,
        "success": True,
        "count": len(zones),
        "total": len(zones),
        "num_zones": len(zones),
        "zone_count": len(zones),
        "zones": zones,
        "polygons": zones,
        "width": width,
        "height": height,
        "normalized": normalized,
    }


def _register(r: APIRouter, base: str) -> None:
    async def _handle(request: Request) -> Dict[str, Any]:
        from app.services import mask_import as svc

        image_bytes, opts = await _extract_image_and_options(request)
        try:
            zones = svc.mask_bytes_to_zones(image_bytes, **opts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc) or "Invalid mask image")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Could not process mask image: {exc}")
        # Report decoded dims for callers.
        w, h = 0, 0
        try:
            img = svc.decode_mask_image(image_bytes)
            if img is not None:
                h, w = img.shape[:2]
        except Exception:
            pass
        normalized = bool(opts.get("normalize", True))
        return _zones_response(zones, w, h, normalized)

    # Import / convert / preview / parse aliases (all identical behavior).
    r.add_api_route(base, _handle, methods=["POST"])
    r.add_api_route(base + "/", _handle, methods=["POST"])
    r.add_api_route(base + "/import", _handle, methods=["POST"])
    r.add_api_route(base + "/preview", _handle, methods=["POST"])
    r.add_api_route(base + "/convert", _handle, methods=["POST"])
    r.add_api_route(base + "/parse", _handle, methods=["POST"])
    # PUT variants for tolerant clients.
    r.add_api_route(base + "/import", _handle, methods=["PUT"])
    r.add_api_route(base, _handle, methods=["PUT"])

    async def _status() -> Dict[str, Any]:
        return {"status": "ok", "service": "mask_import", "ok": True}

    r.add_api_route(base + "/status", _status, methods=["GET"])

    async def _usage() -> Dict[str, Any]:
        return {
            "status": "ok",
            "service": "mask_import",
            "ok": True,
            "usage": "POST a mask PNG (multipart file field 'file') or JSON {image: <base64>} to /import",
            "import_paths": [base + "/import", base + "/preview", base + "/convert", base + "/parse"],
        }

    r.add_api_route(base, _usage, methods=["GET"])


for _base in _BASES:
    _register(router, _base)


# Prefixed alias routers (for callers expecting router.prefix to be set).
underscore_router = APIRouter(prefix="/api/mask_import", tags=["mask_import"])
hyphen_router = APIRouter(prefix="/api/mask-import", tags=["mask_import"])


def _make_prefixed(r: APIRouter) -> None:
    async def _handle(request: Request) -> Dict[str, Any]:
        from app.services import mask_import as svc

        image_bytes, opts = await _extract_image_and_options(request)
        try:
            zones = svc.mask_bytes_to_zones(image_bytes, **opts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc) or "Invalid mask image")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Could not process mask image: {exc}")
        w, h = 0, 0
        try:
            img = svc.decode_mask_image(image_bytes)
            if img is not None:
                h, w = img.shape[:2]
        except Exception:
            pass
        return _zones_response(zones, w, h, bool(opts.get("normalize", True)))

    r.add_api_route("", _handle, methods=["POST"])
    r.add_api_route("/", _handle, methods=["POST"])
    r.add_api_route("/import", _handle, methods=["POST"])
    r.add_api_route("/preview", _handle, methods=["POST"])
    r.add_api_route("/convert", _handle, methods=["POST"])
    r.add_api_route("/parse", _handle, methods=["POST"])
    r.add_api_route("/import", _handle, methods=["PUT"])
    r.add_api_route("", _handle, methods=["PUT"])

    async def _status() -> Dict[str, Any]:
        return {"status": "ok", "service": "mask_import", "ok": True}

    r.add_api_route("/status", _status, methods=["GET"])

    async def _usage() -> Dict[str, Any]:
        return {"status": "ok", "service": "mask_import", "ok": True}

    r.add_api_route("", _usage, methods=["GET"])


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)


# Compatibility aliases.
mask_import_router = router
maskImportRouter = router
mask_import_api_router = router
mask_import_hyphen_router = hyphen_router

__all__ = [
    "router",
    "underscore_router",
    "hyphen_router",
    "mask_import_router",
    "maskImportRouter",
    "mask_import_api_router",
    "mask_import_hyphen_router",
]
