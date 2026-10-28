"""Action template variables API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request

import app.services.action_templates as action_templates_mod
from app.services.action_templates import (
    SUPPORTED_VARIABLES,
    build_context,
    render_action,
    render_body,
    render_string,
    render_subject,
    render_url,
    status as service_status,
)

router = APIRouter(tags=["action_templates"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        s = payload.strip()
        if s:
            # A bare string is treated as a template body.
            data = {"template": s, "body": s}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"template": bytes(payload).decode("utf-8", errors="replace")}
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

    for nested_key in (
        "context", "variables", "vars", "event", "alert", "detection",
        "payload", "options", "settings", "params", "data", "body_",
        "request", "config",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


def _perform_render(data: dict) -> dict:
    # Resolve template inputs from tolerant aliases.
    url = None
    for k in ("url", "target", "uri", "endpoint", "webhook_url", "webhook", "href"):
        if data.get(k) is not None:
            url = data.get(k)
            break
    body = None
    for k in ("body", "payload", "data", "message", "text", "content", "params", "template", "template_body"):
        if data.get(k) is not None:
            body = data.get(k)
            break
    subject = None
    for k in ("subject", "title", "headline", "topic", "email_subject", "emailSubject"):
        if data.get(k) is not None:
            subject = data.get(k)
            break
    # Single "template"/"text" string renders as body when no url/subject given.
    text_template = data.get("template") if isinstance(data.get("template"), str) else None
    if text_template is None and isinstance(data.get("text"), str):
        text_template = data.get("text")

    result = render_action(url=url, body=body, subject=subject, context=data)
    # Also expose generic rendered text for single-string callers.
    if text_template is not None and body is None and url is None and subject is None:
        rendered_text = render_string(text_template, data)
        result["text"] = rendered_text
        result["rendered_text"] = rendered_text
        result["template"] = text_template
        result["rendered_template"] = rendered_text
    else:
        # Convenience single-string views.
        if isinstance(body, str):
            result["text"] = result.get("rendered_body")
            result["rendered_text"] = result.get("rendered_body")
            result["rendered_template"] = result.get("rendered_body")
        elif text_template is not None:
            rendered_text = render_string(text_template, data)
            result.setdefault("text", rendered_text)
            result.setdefault("rendered_text", rendered_text)
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_root(request: Request):
        data = _normalize(None, request)
        # ?action=render support; default returns variables help.
        action = str(data.get("action", "")).lower()
        if action in ("render", "preview", "substitute", "apply"):
            return _perform_render(data)
        return {
            "ok": True,
            "success": True,
            "status": "ok",
            "service": "action_templates",
            "variables": list(SUPPORTED_VARIABLES),
            "example_url": "http://example.com/hook?cam={CAMERA}&event={EVENT_ID}",
            "example_body": "Motion on {CAMERA} at {TIME}: {LABEL} ({CONFIDENCE}) #{EVENT_ID}",
            "example_subject": "Alert from {CAMERA}: {LABEL}",
        }

    @r.get(base + "/")
    async def get_slash(request: Request):
        return await get_root(request)

    @r.get(base + "/status")
    async def get_status(request: Request = None):
        return service_status()

    @r.get(base + "/variables")
    async def get_variables(request: Request = None):
        return {
            "ok": True,
            "success": True,
            "status": "ok",
            "variables": list(SUPPORTED_VARIABLES),
            "count": len(SUPPORTED_VARIABLES),
        }

    @r.get(base + "/health")
    async def get_health(request: Request = None):
        return {"ok": True, "success": True, "service": "action_templates"}

    @r.post(base)
    async def post_root(payload: Any = Body(default=None), request: Request = None):
        return _perform_render(_normalize(payload, request))

    @r.post(base + "/")
    async def post_slash(payload: Any = Body(default=None), request: Request = None):
        return _perform_render(_normalize(payload, request))

    @r.post(base + "/render")
    async def post_render(payload: Any = Body(default=None), request: Request = None):
        return _perform_render(_normalize(payload, request))

    @r.post(base + "/preview")
    async def post_preview(payload: Any = Body(default=None), request: Request = None):
        return _perform_render(_normalize(payload, request))

    @r.post(base + "/substitute")
    async def post_substitute(payload: Any = Body(default=None), request: Request = None):
        return _perform_render(_normalize(payload, request))

    @r.post(base + "/apply")
    async def post_apply(payload: Any = Body(default=None), request: Request = None):
        return _perform_render(_normalize(payload, request))

    @r.get(base + "/render")
    async def get_render(request: Request):
        return _perform_render(_normalize(None, request))


_PATHS = (
    "/api/action_templates",
    "/api/action-templates",
    "/api/actionTemplates",
    "/api/actions/templates",
)

for _base in _PATHS:
    _register(router, _base)


# Compatibility aliases.
action_templates_router = router
action_template_router = router
webhook_templates_router = router
api_router = router

__all__ = ["router"]
