"""Camera URL template tester API for VigilCam.

Auto-registered by ``app.main`` (any module in ``app.api`` exposing
``router`` is included), so no ``app/main.py`` edit is needed.
Both ``/api/url-tester`` (canonical) and ``/api/url_tester``
(underscore alias) are served.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

import app.services.url_tester as svc

router = APIRouter(prefix="/api/url-tester", tags=["url-tester"])
underscore_router = APIRouter(prefix="/api/url_tester", tags=["url-tester"])


class UrlTestRequest(BaseModel):
    url: Optional[str] = None
    target: Optional[str] = None
    stream_url: Optional[str] = None
    source: Optional[str] = None
    source_kind: Optional[str] = None
    host: Optional[str] = None
    port: Optional[Any] = None
    prefix: Optional[str] = None
    scheme: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    channel: Optional[Any] = None
    width: Optional[Any] = None
    height: Optional[Any] = None
    timeout: Optional[float] = None
    timeout_seconds: Optional[float] = None

    class Config:
        extra = "allow"


def _payload_dict(payload: Any) -> dict:
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
    if isinstance(payload, str):
        return {"url": payload}
    return {}


def _timeout_from(data: dict) -> float:
    for key in ("timeout", "timeout_seconds", "timeout_ms"):
        if data.get(key) is not None:
            try:
                val = float(data[key])
                if key == "timeout_ms":
                    val = val / 1000.0
                return val
            except (TypeError, ValueError):
                continue
    return svc.DEFAULT_TIMEOUT_SECONDS


def _do_test(data: dict) -> dict:
    url = data.get("url") or data.get("target") or data.get("stream_url") or data.get("source_url") or data.get("uri")
    host = data.get("host", data.get("hostname", data.get("ip")))
    # A bare template path + host is valid even without a full URL.
    if (not url or not str(url).strip()) and not host:
        raise HTTPException(status_code=422, detail="Provide 'url' (or template 'url' + 'host') to test")
    kwargs = {
        k: v for k, v in data.items()
        if k not in (
            "url", "target", "stream_url", "streamUrl", "source_url", "uri", "href",
            "timeout", "timeout_ms", "timeout_seconds",
        )
    }
    result = svc.test_url(url or "", _timeout_from(data), **kwargs)
    return result.to_dict()


def _summary_payload() -> dict:
    summary = svc.get_summary()
    return {**summary, "results": svc.list_results_dicts(), "ok": True, "success": True}


@router.get("/status")
@underscore_router.get("/status")
async def get_status():
    return _summary_payload()


@router.get("/summary")
@underscore_router.get("/summary")
async def get_summary():
    return {**svc.get_summary(), "ok": True, "success": True}


@router.get("/health")
@underscore_router.get("/health")
async def get_health():
    return _summary_payload()


@router.get("/results")
@underscore_router.get("/results")
async def list_results():
    return svc.list_results_dicts()


@router.delete("/results")
@underscore_router.delete("/results")
async def clear_results():
    svc.clear()
    return {"cleared": True, **svc.get_summary(), "ok": True, "success": True}


@router.get("/classify")
@underscore_router.get("/classify")
async def classify_get(url: Optional[str] = Query(default=None), source: Optional[str] = Query(default=None)):
    if not url or not url.strip():
        raise HTTPException(status_code=422, detail="Provide 'url' to classify")
    info = svc.classify_url(url, source=source)
    return {**info, "url": url, "ok": True, "success": True}


@router.post("/classify")
@underscore_router.post("/classify")
async def classify_post(payload: Any = Body(default=None)):
    data = _payload_dict(payload)
    url = data.get("url") or data.get("target") or data.get("stream_url")
    if not url or not str(url).strip():
        raise HTTPException(status_code=422, detail="Provide 'url' to classify")
    info = svc.classify_url(url, source=data.get("source", data.get("source_kind")))
    return {**info, "url": url, "ok": True, "success": True}


@router.post("/expand")
@underscore_router.post("/expand")
async def expand_template(payload: Any = Body(default=None)):
    data = _payload_dict(payload)
    template = data.get("template", data.get("url", data.get("path", "")))
    if template is None or str(template).strip() == "":
        raise HTTPException(status_code=422, detail="Provide 'template' to expand")
    extra = {k: v for k, v in data.items() if k not in ("template", "url", "path", "built_url", "expanded")}
    expanded = svc.expand_template(template, **extra)
    built = expanded
    if data.get("host") or data.get("hostname") or data.get("ip"):
        host = data.get("host", data.get("hostname", data.get("ip")))
        rest = {k: v for k, v in extra.items() if k not in ("host", "hostname", "ip", "port", "prefix", "scheme")}
        built = svc.build_url(
            template, host=host, port=data.get("port"),
            prefix=data.get("prefix"), scheme=data.get("scheme"), **rest,
        )
    return {
        "template": template,
        "expanded": expanded,
        "built_url": built,
        "has_tokens": svc.has_template_tokens(expanded),
        "classification": svc.classify_url(built, source=data.get("source")),
        "ok": True,
        "success": True,
    }


@router.get("/test")
@underscore_router.get("/test")
async def test_via_query(
    url: Optional[str] = Query(default=None),
    host: Optional[str] = Query(default=None),
    port: Optional[str] = Query(default=None),
    prefix: Optional[str] = Query(default=None),
    username: Optional[str] = Query(default=None),
    password: Optional[str] = Query(default=None),
    channel: Optional[str] = Query(default=None),
    timeout: Optional[float] = Query(default=None),
):
    data = {
        "url": url, "host": host, "port": port, "prefix": prefix,
        "username": username, "password": password, "channel": channel,
        "timeout": timeout or svc.DEFAULT_TIMEOUT_SECONDS,
    }
    return _do_test(data)


@router.post("/test")
@underscore_router.post("/test")
async def test_url_post(payload: Any = Body(default=None)):
    return _do_test(_payload_dict(payload))


@router.post("/fetch")
@underscore_router.post("/fetch")
async def fetch_snapshot(payload: Any = Body(default=None)):
    return _do_test(_payload_dict(payload))


@router.get("/{target:path}")
@underscore_router.get("/{target:path}")
async def get_cached_result(target: str):
    st = svc.get_status(target)
    if st is None:
        raise HTTPException(status_code=404, detail="No test result for target")
    return st.to_dict()


# Side-effect registration so the routers are live even when this module is
# imported after app startup (mirrors stream-probe pattern). The main app also
# auto-discovers app.api.* routers at import time.
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    for _r in (router, underscore_router):
        if id(_r) not in _ids:
            _main_app.include_router(_r)
            try:
                _main_app._registered_router_ids = getattr(_main_app, "_registered_router_ids", set()) | {id(_r)}
                _ids = _main_app._registered_router_ids
            except Exception:  # noqa: BLE001
                pass
except Exception:  # noqa: BLE001
    pass
