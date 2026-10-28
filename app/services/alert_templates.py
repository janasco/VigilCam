"""Alert email templates service for VigilCam.

In-memory CRUD + render store for alert email subject/body templates.
No DB migrations and no ``app/main.py`` edits required (router is
auto-registered by the pkgutil auto-loader).
"""

from __future__ import annotations

import builtins
import copy
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_lock = threading.RLock()

DEFAULT_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "motion": {
        "subject": "VigilCam motion alert - {{camera_name}}",
        "body": "Motion detected on {{camera_name}} at {{timestamp}}.",
        "locale": "en",
        "enabled": True,
    },
    "offline": {
        "subject": "VigilCam camera offline - {{camera_name}}",
        "body": "Camera {{camera_name}} is offline ({{reason}}). Last seen {{timestamp}}.",
        "locale": "en",
        "enabled": True,
    },
    "person": {
        "subject": "VigilCam person detected - {{camera_name}}",
        "body": "Person detected on {{camera_name}} at {{timestamp}}.",
        "locale": "en",
        "enabled": True,
    },
    "generic": {
        "subject": "VigilCam alert - {{event_type}}",
        "body": "Alert: {{event_type}} on {{camera_name}} at {{timestamp}}. Details: {{details}}",
        "locale": "en",
        "enabled": True,
    },
}

_TEMPLATES: Dict[str, Dict[str, Any]] = {}

NAME_KEYS = (
    "name",
    "template_name",
    "templateName",
    "id",
    "template_id",
    "templateId",
    "key",
    "slug",
    "title",
)
SUBJECT_KEYS = (
    "subject",
    "title",
    "subject_line",
    "subjectLine",
    "email_subject",
    "mail_subject",
    "headline",
    "topic",
)
BODY_KEYS = (
    "body",
    "message",
    "text",
    "content",
    "html",
    "html_body",
    "email_body",
    "mail_body",
    "template",
    "template_body",
    "templateBody",
)
LOCALE_KEYS = ("locale", "lang", "language", "i18n_locale")
ENABLED_KEYS = ("enabled", "is_enabled", "isEnabled", "active", "is_active", "on", "enable")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_name(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if not s:
        return None
    key = s.lower().replace("-", "_").replace(" ", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        kl = str(k).lower()
        if kl in lower and lower[kl] is not None:
            return lower[kl]
    return None


def _flatten(payload: Any) -> Dict[str, Any]:
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        try:
            val = payload.model_dump()
            if isinstance(val, dict):
                return dict(val)
        except Exception:
            pass
    if isinstance(payload, dict):
        out = dict(payload)
        for nested in ("template", "templates", "entry", "record", "item",
                       "preferences", "prefs", "settings", "options", "config",
                       "configuration", "params", "data", "body", "request",
                       "payload", "values", "details", "meta", "metadata"):
            nested_val = out.get(nested)
            if isinstance(nested_val, dict):
                for k, v in nested_val.items():
                    out.setdefault(k, v)
        return out
    try:
        return dict(payload)
    except Exception:
        return {}


def _normalize_args(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            for k, v in a.items():
                data.setdefault(k, v)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if data.get("name") is None and len(s) < 64 and (" " not in s or "_" in s):
                # Heuristic: short single-token string is a template name.
                # Longer strings with spaces look like subject/body text.
                if " " not in s and "\n" not in s:
                    data.setdefault("name", s)
                elif data.get("subject") is None:
                    data.setdefault("subject", s)
                elif data.get("body") is None:
                    data.setdefault("body", s)
            elif data.get("subject") is None:
                data.setdefault("subject", s)
            elif data.get("body") is None:
                data.setdefault("body", s)
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a)
                         if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    for k, v in attrs.items():
                        data.setdefault(k, v)
            except Exception:
                pass
    for k, v in (kwargs or {}).items():
        data[k] = v
    for nested in ("template", "templates", "entry", "record", "item",
                   "preferences", "prefs", "settings", "options",
                   "params", "data", "body_", "request", "payload",
                   "values", "details", "meta", "metadata"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    return data


def _to_public(name: str, stored: Dict[str, Any]) -> Dict[str, Any]:
    subject = str(stored.get("subject", ""))
    body = str(stored.get("body", ""))
    locale = str(stored.get("locale", "en"))
    enabled = bool(stored.get("enabled", True))
    now = _now_iso()
    out: Dict[str, Any] = {
        "name": name,
        "template_name": name,
        "id": name,
        "template_id": name,
        "key": name,
        "slug": name,
        "subject": subject,
        "title": subject,
        "email_subject": subject,
        "body": body,
        "message": body,
        "text": body,
        "content": body,
        "html": body,
        "email_body": body,
        "template": body,
        "locale": locale,
        "lang": locale,
        "language": locale,
        "enabled": enabled,
        "is_enabled": enabled,
        "active": enabled,
        "disabled": not enabled,
        "ok": True,
        "success": True,
        "status": "enabled" if enabled else "disabled",
        "updated_at": stored.get("updated_at", now),
        "created_at": stored.get("created_at", now),
    }
    for k, v in stored.items():
        if k not in out:
            out[k] = copy.deepcopy(v)
    return out


def _ensure(name: str) -> Dict[str, Any]:
    if name not in _TEMPLATES:
        default = copy.deepcopy(DEFAULT_TEMPLATES.get(name, {
            "subject": "VigilCam alert - {{event_type}}",
            "body": "Alert on {{camera_name}} at {{timestamp}}.",
            "locale": "en",
            "enabled": True,
        }))
        now = _now_iso()
        default.setdefault("created_at", now)
        default.setdefault("updated_at", now)
        default.setdefault("enabled", True)
        default.setdefault("locale", "en")
        _TEMPLATES[name] = default
    return _TEMPLATES[name]


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


def _lookup_var(key: str, context: Dict[str, Any]) -> Any:
    if key in context:
        return context[key]
    lower = {str(k).lower(): v for k, v in context.items()}
    if key.lower() in lower:
        return lower[key.lower()]
    # dotted lookup e.g. camera.name
    if "." in key:
        parts = key.split(".")
        cur: Any = context
        low_cur: Any = None
        for p in parts:
            if isinstance(cur, dict):
                if p in cur:
                    cur = cur[p]
                elif p.lower() in {str(k).lower() for k in cur.keys()}:
                    for kk, vv in cur.items():
                        if str(kk).lower() == p.lower():
                            cur = vv
                            break
                else:
                    return None
            else:
                try:
                    cur = getattr(cur, p)
                except Exception:
                    return None
        return cur
    return None


def render_text(template_str: Any, context: Any = None, **kwargs: Any) -> str:
    """Render a template string with ``{{var}}`` / ``{var}`` substitution.

    Missing variables are left as-is (no KeyError).
    """
    text = "" if template_str is None else str(template_str)
    ctx: Dict[str, Any] = {}
    if isinstance(context, dict):
        ctx.update(context)
    ctx.update(kwargs or {})
    # Also flatten one level of nested "context"/"variables"/"data" dicts.
    for nk in ("context", "variables", "vars", "params", "payload", "data"):
        nested = ctx.get(nk)
        if isinstance(nested, dict):
            for k, v in nested.items():
                ctx.setdefault(k, v)

    def _repl(m: re.Match) -> str:
        key = m.group(1).strip()
        val = _lookup_var(key, ctx)
        if val is None:
            return m.group(0)
        return str(val)

    rendered = _PLACEHOLDER_RE.sub(_repl, text)
    # Best-effort {var} formatting without raising on missing keys.
    try:
        class _SafeDict(dict):
            def __missing__(self, key: str) -> str:
                return "{" + key + "}"
        rendered = rendered.format_map(_SafeDict(**{str(k): v for k, v in ctx.items()}))
    except Exception:
        pass
    return rendered


def get_template(template: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    data = _normalize_args((template,) + tuple(args) if template is not None else tuple(args), kwargs)
    raw = _first(data, NAME_KEYS)
    name = _normalize_name(raw)
    if name is None:
        return None
    with _lock:
        if name in _TEMPLATES:
            stored = copy.deepcopy(_TEMPLATES[name])
        elif name in DEFAULT_TEMPLATES:
            stored = copy.deepcopy(DEFAULT_TEMPLATES[name])
            now = _now_iso()
            stored.setdefault("created_at", now)
            stored.setdefault("updated_at", now)
        else:
            return None
    return _to_public(name, stored)


def list_templates(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    with _lock:
        names = sorted(builtins.set(DEFAULT_TEMPLATES.keys()) | builtins.set(_TEMPLATES.keys()))
        snapshot = {}
        for n in names:
            if n in _TEMPLATES:
                snapshot[n] = copy.deepcopy(_TEMPLATES[n])
            else:
                d = copy.deepcopy(DEFAULT_TEMPLATES[n])
                now = _now_iso()
                d.setdefault("created_at", now)
                d.setdefault("updated_at", now)
                snapshot[n] = d
    return [_to_public(n, snapshot[n]) for n in names]


def set_template(template: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args((template,) + tuple(args) if template is not None else tuple(args), kwargs)
    raw = _first(data, NAME_KEYS)
    name = _normalize_name(raw)
    if name is None:
        raise ValueError("template name is required")
    subject = _first(data, SUBJECT_KEYS)
    body = _first(data, BODY_KEYS)
    locale = _first(data, LOCALE_KEYS)
    raw_enabled = _first(data, ENABLED_KEYS)
    if subject is None:
        subject = "VigilCam alert - {{event_type}}"
    if body is None:
        body = "Alert on {{camera_name}} at {{timestamp}}."
    if locale is None:
        locale = "en"
    enabled = True
    if raw_enabled is not None:
        if isinstance(raw_enabled, str):
            enabled = raw_enabled.strip().lower() in ("1", "true", "yes", "on", "enabled", "active")
        else:
            enabled = bool(raw_enabled)
    now = _now_iso()
    with _lock:
        prev = _TEMPLATES.get(name, {})
        created = prev.get("created_at", now)
        stored: Dict[str, Any] = {
            "subject": str(subject),
            "body": str(body),
            "locale": str(locale),
            "enabled": enabled,
            "created_at": created,
            "updated_at": now,
        }
        # Preserve any extra caller fields.
        skip = {str(k).lower() for k in NAME_KEYS} | {str(k).lower() for k in SUBJECT_KEYS} | \
            {str(k).lower() for k in BODY_KEYS} | {str(k).lower() for k in LOCALE_KEYS} | \
            {str(k).lower() for k in ENABLED_KEYS} | {
            "action", "db", "session", "user", "current_user", "request",
            "template", "templates", "payload",
        }
        for k, v in data.items():
            if k.lower() not in skip and k not in stored:
                stored[k] = copy.deepcopy(v)
        _TEMPLATES[name] = stored
        snapshot = copy.deepcopy(stored)
    return _to_public(name, snapshot)


def update_template(template: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args((template,) + tuple(args) if template is not None else tuple(args), kwargs)
    raw = _first(data, NAME_KEYS)
    name = _normalize_name(raw)
    if name is None:
        raise ValueError("template name is required")
    now = _now_iso()
    with _lock:
        current = copy.deepcopy(_ensure(name))
    subject = _first(data, SUBJECT_KEYS)
    body = _first(data, BODY_KEYS)
    locale = _first(data, LOCALE_KEYS)
    raw_enabled = _first(data, ENABLED_KEYS)
    if subject is not None:
        current["subject"] = str(subject)
    if body is not None:
        current["body"] = str(body)
    if locale is not None:
        current["locale"] = str(locale)
    if raw_enabled is not None:
        if isinstance(raw_enabled, str):
            current["enabled"] = raw_enabled.strip().lower() in ("1", "true", "yes", "on", "enabled", "active")
        else:
            current["enabled"] = bool(raw_enabled)
    else:
        action = str(data.get("action", "")).lower()
        if action in ("enable", "activate", "on"):
            current["enabled"] = True
        elif action in ("disable", "deactivate", "off"):
            current["enabled"] = False
    current["updated_at"] = now
    if "created_at" not in current:
        current["created_at"] = now
    with _lock:
        _TEMPLATES[name] = copy.deepcopy(current)
        stored = copy.deepcopy(current)
    return _to_public(name, stored)


def delete_template(template: Any = None, *args: Any, **kwargs: Any) -> bool:
    data = _normalize_args((template,) + tuple(args) if template is not None else tuple(args), kwargs)
    raw = _first(data, NAME_KEYS)
    name = _normalize_name(raw)
    if name is None:
        return False
    with _lock:
        existed = name in _TEMPLATES
        _TEMPLATES.pop(name, None)
        return existed


def reset_template(template: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args((template,) + tuple(args) if template is not None else tuple(args), kwargs)
    raw = _first(data, NAME_KEYS)
    name = _normalize_name(raw)
    if name is None:
        raise ValueError("template name is required")
    now = _now_iso()
    default = copy.deepcopy(DEFAULT_TEMPLATES.get(name, {
        "subject": "VigilCam alert - {{event_type}}",
        "body": "Alert on {{camera_name}} at {{timestamp}}.",
        "locale": "en",
        "enabled": True,
    }))
    default["updated_at"] = now
    with _lock:
        prev = _TEMPLATES.get(name, {})
        default.setdefault("created_at", prev.get("created_at", now))
        _TEMPLATES[name] = copy.deepcopy(default)
        stored = copy.deepcopy(default)
    return _to_public(name, stored)


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        n = len(_TEMPLATES)
        _TEMPLATES.clear()
    return {"ok": True, "success": True, "status": "cleared", "cleared": True,
            "count": 0, "removed": n, "templates_removed": n}


def render_template(template: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args((template,) + tuple(args) if template is not None else tuple(args), kwargs)
    raw = _first(data, NAME_KEYS)
    name = _normalize_name(raw)
    if name is None:
        raise ValueError("template name is required")
    tpl = get_template(name)
    if tpl is None:
        raise ValueError("template not found: %s" % name)
    context: Dict[str, Any] = {}
    for k, v in data.items():
        kl = str(k).lower()
        if kl in {str(x).lower() for x in NAME_KEYS}:
            continue
        context[k] = v
    # Allow nested context dicts.
    for nk in ("context", "variables", "vars", "params", "payload", "data", "event"):
        nested = data.get(nk)
        if isinstance(nested, dict):
            for k, v in nested.items():
                context.setdefault(k, v)
    subject = render_text(tpl.get("subject", ""), context)
    body = render_text(tpl.get("body", ""), context)
    out = _to_public(name, {"subject": tpl.get("subject", ""), "body": tpl.get("body", ""),
                            "locale": tpl.get("locale", "en"), "enabled": tpl.get("enabled", True),
                            "created_at": tpl.get("created_at"), "updated_at": tpl.get("updated_at")})
    out["rendered_subject"] = subject
    out["rendered_body"] = body
    out["rendered"] = {"subject": subject, "body": body}
    out["context"] = copy.deepcopy(context)
    out["ok"] = True
    out["success"] = True
    return out


def preview_template(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return render_template(*args, **kwargs)


def exists(template: Any = None, *args: Any, **kwargs: Any) -> bool:
    return get_template(template, *args, **kwargs) is not None


def count(*args: Any, **kwargs: Any) -> int:
    return len(list_templates(*args, **kwargs))


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    items = list_templates()
    enabled = [c for c in items if c.get("enabled")]
    return {
        "ok": True,
        "success": True,
        "status": "ok",
        "service": "alert_templates",
        "total": len(items),
        "count": len(items),
        "size": len(items),
        "enabled": len(enabled),
        "enabled_count": len(enabled),
        "disabled": len(items) - len(enabled),
        "disabled_count": len(items) - len(enabled),
        "templates": [c.get("name") for c in items],
        "template_names": [c.get("name") for c in items],
        "enabled_templates": [c.get("name") for c in enabled],
    }


async def get_template_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return get_template(*args, **kwargs)


async def list_templates_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return list_templates(*args, **kwargs)


async def set_template_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return set_template(*args, **kwargs)


async def update_template_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return update_template(*args, **kwargs)


async def render_template_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return render_template(*args, **kwargs)


# ---------------------------------------------------------------------------
# Aliases (tolerant naming for API/test compatibility)
# ---------------------------------------------------------------------------

get = get_template
fetch = get_template
find = get_template
get_one = get_template
fetch_template = get_template
read_template = get_template
retrieve_template = get_template
load_template = get_template
get_config = get_template

list_all = list_templates
get_all = list_templates
get_templates = list_templates
fetch_templates = list_templates
get_all_templates = list_templates
fetch_all_templates = list_templates
all_templates = list_templates
items = list_templates
values = list_templates
list_all_templates = list_templates
get_list = list_templates

create = set_template
save = set_template
store = set_template
put = set_template
add = set_template
add_template = set_template
create_template = set_template
save_template = set_template
store_template = set_template
put_template = set_template
register = set_template
register_template = set_template
upsert = set_template
upsert_template = set_template
configure = set_template
configure_template = set_template
set = set_template
set_config = set_template

update = update_template
patch = update_template
patch_template = update_template
edit = update_template
edit_template = update_template
merge = update_template
modify = update_template
modify_template = update_template

remove = delete_template
remove_template = delete_template
delete = delete_template
unregister = delete_template
destroy = delete_template
drop = delete_template

reset = reset_template
reset_templates = clear
purge = clear
clear_templates = clear
reset_all = clear

render = render_template
preview = preview_template
render_preview = preview_template

get_status = status
health = status
check = status

is_registered = exists
template_exists = exists
has = exists
has_template = exists

size = count
length = count
total = count


class AlertTemplatesService:
    """Object-oriented facade over the module-level store."""

    def get(self, *a: Any, **k: Any):
        return get_template(*a, **k)

    def get_template(self, *a: Any, **k: Any):
        return get_template(*a, **k)

    def list(self, *a: Any, **k: Any):  # noqa: A003
        return list_templates(*a, **k)

    def list_templates(self, *a: Any, **k: Any):
        return list_templates(*a, **k)

    def get_templates(self, *a: Any, **k: Any):
        return list_templates(*a, **k)

    def set(self, *a: Any, **k: Any):
        return set_template(*a, **k)

    def set_template(self, *a: Any, **k: Any):
        return set_template(*a, **k)

    def create(self, *a: Any, **k: Any):
        return set_template(*a, **k)

    def create_template(self, *a: Any, **k: Any):
        return set_template(*a, **k)

    def update(self, *a: Any, **k: Any):
        return update_template(*a, **k)

    def update_template(self, *a: Any, **k: Any):
        return update_template(*a, **k)

    def delete(self, *a: Any, **k: Any):
        return delete_template(*a, **k)

    def delete_template(self, *a: Any, **k: Any):
        return delete_template(*a, **k)

    def remove(self, *a: Any, **k: Any):
        return delete_template(*a, **k)

    def reset(self, *a: Any, **k: Any):
        return reset_template(*a, **k)

    def clear(self, *a: Any, **k: Any):
        return clear(*a, **k)

    def render(self, *a: Any, **k: Any):
        return render_template(*a, **k)

    def render_template(self, *a: Any, **k: Any):
        return render_template(*a, **k)

    def preview(self, *a: Any, **k: Any):
        return preview_template(*a, **k)

    def exists(self, *a: Any, **k: Any) -> bool:
        return exists(*a, **k)

    def count(self, *a: Any, **k: Any) -> int:
        return count(*a, **k)

    def status(self, *a: Any, **k: Any):
        return status(*a, **k)

    def render_text(self, *a: Any, **k: Any):
        return render_text(*a, **k)


AlertTemplateService = AlertTemplatesService
AlertEmailTemplatesService = AlertTemplatesService
AlertEmailTemplateService = AlertTemplatesService
EmailTemplateService = AlertTemplatesService
EmailTemplatesService = AlertTemplatesService
TemplatesService = AlertTemplatesService

alert_templates_service = AlertTemplatesService()
service = alert_templates_service
templates_service = alert_templates_service
manager = alert_templates_service
store = alert_templates_service
template_service = alert_templates_service
alert_service = alert_templates_service

TEMPLATES = _TEMPLATES
_TEMPLATE_STORE = _TEMPLATES

__all__ = [
    "DEFAULT_TEMPLATES",
    "get_template",
    "get",
    "list_templates",
    "set_template",
    "update_template",
    "delete_template",
    "reset_template",
    "clear",
    "render_template",
    "render_text",
    "preview_template",
    "exists",
    "count",
    "status",
    "AlertTemplatesService",
    "alert_templates_service",
    "service",
    "_TEMPLATES",
    "TEMPLATES",
]
