"""Action template variable substitution for VigilCam.

Gap (verified via grep): :mod:`app.services.actions` and
:mod:`app.services.email_snapshot` send static payloads -- neither module
contains ``{{`` nor ``{CAMERA`` substitution. Webhook URLs/bodies and email
subjects/bodies are posted/sent verbatim.

This module fills that gap **without** duplicating either path: it renders
``{CAMERA}`` ``{TIME}`` ``{LABEL}`` ``{CONFIDENCE}`` ``{EVENT_ID}`` (and the
``{{VAR}}`` equivalents) inside webhook URLs, webhook bodies/payloads and
email subjects/bodies before they are sent.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SUPPORTED_VARIABLES = ("CAMERA", "TIME", "LABEL", "CONFIDENCE", "EVENT_ID")
TEMPLATE_VARIABLES = SUPPORTED_VARIABLES
VARIABLE_NAMES = SUPPORTED_VARIABLES
VARIABLES = SUPPORTED_VARIABLES

CAMERA_KEYS = (
    "camera", "camera_name", "cameraName", "cam", "cam_name",
    "camera_id", "cameraId", "source", "device", "channel",
    "CAMERA",
)
TIME_KEYS = (
    "time", "timestamp", "event_time", "eventTime", "created_at",
    "occurred_at", "observed_at", "now", "date", "datetime",
    "TIME",
)
LABEL_KEYS = (
    "label", "event_label", "eventLabel", "event_type", "eventType",
    "event_kind", "eventKind", "kind", "type", "name",
    "detection", "object", "class", "details",
    "LABEL",
)
CONFIDENCE_KEYS = (
    "confidence", "score", "probability", "conf", "certainty",
    "CONFIDENCE",
)
EVENT_ID_KEYS = (
    "event_id", "eventId", "alert_id", "alertId", "id",
    "EVENT_ID",
)

_CONTEXT_NESTED_KEYS = (
    "context", "variables", "vars", "params", "payload", "data",
    "body", "event", "alert", "detection", "request", "options",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, datetime):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def _first_ci(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        lk = str(k).lower()
        if lk in lower and lower[lk] is not None:
            return lower[lk]
    return None


def _flatten_nested(data: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(data)
    for nk in _CONTEXT_NESTED_KEYS:
        nested = data.get(nk)
        if isinstance(nested, dict):
            for k, v in nested.items():
                merged.setdefault(k, v)
    # model_dump support
    for k, v in list(merged.items()):
        if hasattr(v, "model_dump"):
            try:
                dumped = v.model_dump()
                if isinstance(dumped, dict):
                    for nk2, nv2 in dumped.items():
                        merged.setdefault(nk2, nv2)
            except Exception:  # noqa: BLE001
                pass
    return merged


def _normalize_inputs(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            for k, v in a.items():
                data.setdefault(k, v)
        elif hasattr(a, "model_dump"):
            try:
                dumped = a.model_dump()
                if isinstance(dumped, dict):
                    for k, v in dumped.items():
                        data.setdefault(k, v)
                    continue
            except Exception:  # noqa: BLE001
                pass
            try:
                attrs = {
                    k: getattr(a, k)
                    for k in dir(a)
                    if not k.startswith("_") and not callable(getattr(a, k, None))
                }
                for k, v in attrs.items():
                    data.setdefault(k, v)
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                attrs = {
                    k: getattr(a, k)
                    for k in dir(a)
                    if not k.startswith("_") and not callable(getattr(a, k, None))
                }
                if attrs:
                    for k, v in attrs.items():
                        data.setdefault(k, v)
            except Exception:  # noqa: BLE001
                pass
    for k, v in (kwargs or {}).items():
        data[k] = v
    return data


def build_context(*args: Any, **kwargs: Any) -> Dict[str, str]:
    """Build a ``{VAR: value}`` context from loose inputs.

    Accepts flat kwargs (``camera=...``, ``CAMERA=...``), dicts, nested
    ``context``/``event``/``payload`` dicts and common aliases.
    Only variables with a non-None value are included; missing variables
    are omitted so :func:`render_string` leaves their placeholder as-is.
    """
    raw = _normalize_inputs(args, kwargs)
    flat = _flatten_nested(raw)
    ctx: Dict[str, str] = {}

    camera = _first_ci(flat, CAMERA_KEYS)
    if camera is not None:
        ctx["CAMERA"] = _to_str(camera)

    timestamp = _first_ci(flat, TIME_KEYS)
    if timestamp is not None:
        ctx["TIME"] = _to_str(timestamp)

    label = _first_ci(flat, LABEL_KEYS)
    if label is not None:
        ctx["LABEL"] = _to_str(label)

    confidence = _first_ci(flat, CONFIDENCE_KEYS)
    if confidence is not None:
        ctx["CONFIDENCE"] = _to_str(confidence)

    event_id = _first_ci(flat, EVENT_ID_KEYS)
    # Avoid treating a dict-ish "id" (e.g. object id) weirdly; only scalar.
    if event_id is not None and not isinstance(event_id, (dict, list)):
        ctx["EVENT_ID"] = _to_str(event_id)

    # Direct UPPER keys always win (explicit caller intent).
    for var in SUPPORTED_VARIABLES:
        if raw.get(var) is not None:
            ctx[var] = _to_str(raw.get(var))
        elif flat.get(var) is not None and var not in ctx:
            ctx[var] = _to_str(flat.get(var))
    return ctx


_DOUBLE_BRACE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_SINGLE_BRACE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _lookup(var: str, context: Dict[str, Any]) -> Optional[str]:
    if var in context and context[var] is not None:
        return _to_str(context[var])
    upper = var.upper()
    if upper in context and context[upper] is not None:
        return _to_str(context[upper])
    lower = {str(k).lower(): v for k, v in context.items()}
    if var.lower() in lower and lower[var.lower()] is not None:
        return _to_str(lower[var.lower()])
    return None


def render_string(template: Any, context: Any = None, **kwargs: Any) -> str:
    """Render ``{VAR}``/``{{VAR}}`` placeholders (case-insensitive).

    Only ``CAMERA``/``TIME``/``LABEL``/``CONFIDENCE``/``EVENT_ID`` are
    substituted; unknown ``{placeholders}`` are left as-is.
    """
    if template is None:
        return ""
    text = template if isinstance(template, str) else _to_str(template)
    if not text:
        return text
    ctx: Dict[str, Any] = {}
    if isinstance(context, dict):
        ctx.update(context)
    ctx.update(kwargs or {})
    # Allow build_context-style loose inputs: if caller passed event fields
    # as kwargs alongside context, merge them into canonical context.
    if args_like := build_context(context if isinstance(context, dict) else {}, kwargs or {}):
        for k, v in args_like.items():
            ctx.setdefault(k, v)
            ctx.setdefault(k.lower(), v)
            ctx.setdefault(k.title(), v)
    supported = {v.upper() for v in SUPPORTED_VARIABLES}

    def _double(m: re.Match) -> str:
        key = m.group(1)
        if key.upper() not in supported:
            return m.group(0)
        val = _lookup(key, ctx)
        if val is None:
            return m.group(0)
        return val

    def _single(m: re.Match) -> str:
        key = m.group(1)
        if key.upper() not in supported:
            return m.group(0)
        val = _lookup(key, ctx)
        if val is None:
            return m.group(0)
        return val

    rendered = _DOUBLE_BRACE_RE.sub(_double, text)
    rendered = _SINGLE_BRACE_RE.sub(_single, rendered)
    return rendered


def render_value(value: Any, context: Any = None, **kwargs: Any) -> Any:
    """Recursively render strings inside dicts/lists/tuples."""
    ctx: Dict[str, Any] = {}
    if isinstance(context, dict):
        ctx.update(context)
    ctx.update(kwargs or {})
    if isinstance(value, str):
        return render_string(value, ctx)
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            new_k = render_string(k, ctx) if isinstance(k, str) else k
            out[new_k] = render_value(v, ctx)
        return out
    if isinstance(value, (list, tuple)):
        rendered = [render_value(v, ctx) for v in value]
        return type(value)(rendered) if isinstance(value, tuple) else rendered
    return value


def render_url(url: Any, context: Any = None, **kwargs: Any) -> str:
    """Render template variables in a webhook URL."""
    ctx = build_context(context if isinstance(context, dict) else {}, kwargs or {})
    merged: Dict[str, Any] = {}
    if isinstance(context, dict):
        merged.update(context)
    merged.update(ctx)
    merged.update(kwargs or {})
    if url is None:
        return ""
    return render_string(_to_str(url), merged)


def render_subject(subject: Any, context: Any = None, **kwargs: Any) -> str:
    """Render template variables in an email subject line."""
    ctx = build_context(context if isinstance(context, dict) else {}, kwargs or {})
    merged: Dict[str, Any] = {}
    if isinstance(context, dict):
        merged.update(context)
    merged.update(ctx)
    merged.update(kwargs or {})
    if subject is None:
        return ""
    return render_string(_to_str(subject), merged)


def render_body(body: Any, context: Any = None, **kwargs: Any) -> Any:
    """Render template variables in a webhook/email body.

    Strings are rendered directly; dicts/lists are rendered recursively
    so JSON webhook payloads keep their structure.
    """
    merged: Dict[str, Any] = {}
    if isinstance(context, dict):
        merged.update(context)
    merged.update(kwargs or {})
    canonical = build_context(context if isinstance(context, dict) else {}, kwargs or {})
    for k, v in canonical.items():
        merged.setdefault(k, v)
    if body is None:
        return ""
    if isinstance(body, str):
        return render_string(body, merged)
    return render_value(body, merged)


def render_dict(payload: Any, context: Any = None, **kwargs: Any) -> Any:
    """Recursively render a webhook JSON payload dict."""
    merged: Dict[str, Any] = {}
    if isinstance(context, dict):
        merged.update(context)
    merged.update(kwargs or {})
    canonical = build_context(context if isinstance(context, dict) else {}, kwargs or {})
    for k, v in canonical.items():
        merged.setdefault(k, v)
    return render_value(payload, merged)


def render_action(
    url: Any = None,
    body: Any = None,
    subject: Any = None,
    context: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Render URL/body/subject together; returns rendered triplet + context."""
    merged_raw: Dict[str, Any] = {}
    if isinstance(context, dict):
        merged_raw.update(context)
    merged_raw.update(kwargs or {})
    # Also unwrap nested event/context dicts for convenience.
    flat = _flatten_nested(merged_raw)
    canonical = build_context(flat)
    merged: Dict[str, Any] = dict(flat)
    merged.update(canonical)
    # Provide lower/title aliases for case-insensitive templates.
    for k, v in list(canonical.items()):
        merged.setdefault(k.lower(), v)

    # Resolve inputs from loose kwargs aliases (url/target, body/payload, subject/title).
    _URL_ALIASES = ("url", "target", "uri", "endpoint", "webhook_url", "webhook", "href")
    _BODY_ALIASES = ("body", "payload", "data", "message", "text", "content", "params")
    _SUBJECT_ALIASES = ("subject", "title", "headline", "topic", "email_subject")
    if url is None:
        for a in _URL_ALIASES:
            if flat.get(a) is not None:
                url = flat.get(a)
                break
    if body is None:
        for a in _BODY_ALIASES:
            if flat.get(a) is not None:
                body = flat.get(a)
                break
    if subject is None:
        for a in _SUBJECT_ALIASES:
            if flat.get(a) is not None:
                subject = flat.get(a)
                break

    rendered_url = render_url(url, merged) if url is not None else ""
    rendered_body: Any = render_body(body, merged) if body is not None else ""
    rendered_subject = render_subject(subject, merged) if subject is not None else ""
    return {
        "ok": True,
        "success": True,
        "status": "rendered",
        "url": rendered_url,
        "rendered_url": rendered_url,
        "target": rendered_url,
        "body": rendered_body,
        "rendered_body": rendered_body,
        "payload": rendered_body,
        "rendered_payload": rendered_body,
        "subject": rendered_subject,
        "rendered_subject": rendered_subject,
        "context": {k: _to_str(v) for k, v in canonical.items()},
        "variables": list(SUPPORTED_VARIABLES),
    }


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return {
        "ok": True,
        "success": True,
        "status": "ok",
        "service": "action_templates",
        "variables": list(SUPPORTED_VARIABLES),
        "count": len(SUPPORTED_VARIABLES),
    }


class ActionTemplatesService:
    """Object-oriented facade over the module-level helpers."""

    def build_context(self, *a: Any, **k: Any) -> Dict[str, str]:
        return build_context(*a, **k)

    def render(self, *a: Any, **k: Any) -> str:
        if not a:
            return render_action(**k)  # type: ignore[return-value]
        first = a[0]
        rest = a[1:]
        if isinstance(first, dict) and not rest and not k:
            return render_dict(first)  # type: ignore[return-value]
        if isinstance(first, str):
            ctx = rest[0] if rest and isinstance(rest[0], dict) else None
            extra = dict(k)
            if rest and isinstance(rest[0], dict):
                extra = {**rest[0], **k} if len(rest) == 1 else {**k}
            elif rest:
                extra = dict(k)
            return render_string(first, ctx, **extra)
        return render_value(first, *rest, **k)  # type: ignore[return-value]

    def render_string(self, *a: Any, **k: Any) -> str:
        if not a:
            return ""
        return render_string(a[0], a[1] if len(a) > 1 else None, **k)

    def render_text(self, *a: Any, **k: Any) -> str:
        return self.render_string(*a, **k)

    def render_url(self, *a: Any, **k: Any) -> str:
        if not a:
            return render_url(k.get("url"), k)
        return render_url(a[0], a[1] if len(a) > 1 else None, **k)

    def render_body(self, *a: Any, **k: Any) -> Any:
        if not a:
            return render_body(k.get("body"), k)
        return render_body(a[0], a[1] if len(a) > 1 else None, **k)

    def render_subject(self, *a: Any, **k: Any) -> str:
        if not a:
            return render_subject(k.get("subject"), k)
        return render_subject(a[0], a[1] if len(a) > 1 else None, **k)

    def render_dict(self, *a: Any, **k: Any) -> Any:
        if not a:
            return render_dict(k.get("payload", k.get("body")), k)
        return render_dict(a[0], a[1] if len(a) > 1 else None, **k)

    render_payload = render_dict

    def render_action(self, *a: Any, **k: Any) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for item in a:
            if isinstance(item, dict):
                data.update(item)
        data.update(k)
        return render_action(
            url=data.get("url", data.get("target")),
            body=data.get("body", data.get("payload", data.get("data"))),
            subject=data.get("subject", data.get("title")),
            context=data,
        )

    def status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return status(*a, **k)


# Compatibility aliases (repo convention: many names, same behaviour).
ActionTemplateService = ActionTemplatesService
ActionVariablesService = ActionTemplatesService
ActionSubstitutionService = ActionTemplatesService
TemplatesService = ActionTemplatesService

render = render_string
render_text = render_string
render_template = render_string
render_template_string = render_string
substitute = render_string
substitute_variables = render_string
apply_templates = render_action
apply_variables = render_action
substitute_action = render_action
render_webhook = render_action
render_email = render_action
render_payload = render_dict
render_body_value = render_body
render_subject_line = render_subject

build_event_context = build_context
get_context = build_context
make_context = build_context
context_for = build_context

action_templates_service = ActionTemplatesService()
service = action_templates_service
manager = action_templates_service
templates_service = action_templates_service

__all__ = [
    "SUPPORTED_VARIABLES",
    "TEMPLATE_VARIABLES",
    "VARIABLE_NAMES",
    "VARIABLES",
    "build_context",
    "build_event_context",
    "get_context",
    "make_context",
    "context_for",
    "render_string",
    "render_text",
    "render_template",
    "render_template_string",
    "render_url",
    "render_subject",
    "render_subject_line",
    "render_body",
    "render_body_value",
    "render_value",
    "render_dict",
    "render_payload",
    "render_action",
    "substitute",
    "substitute_variables",
    "substitute_action",
    "apply_templates",
    "apply_variables",
    "render_webhook",
    "render_email",
    "status",
    "ActionTemplatesService",
    "ActionTemplateService",
    "action_templates_service",
    "service",
]
