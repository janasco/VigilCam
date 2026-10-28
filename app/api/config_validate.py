"""Config dry-run validation REST endpoints (no writes).

Frigate analogy: ``POST /api/config/save`` validates with
``FrigateConfig.parse`` before writing and returns ``400`` on failure;
bulk ``PUT /api/config`` rolls the file back when validation fails.
This router offers the same safety as a standalone dry-run: clients POST a
candidate system-config envelope (or flat section map, JSON string, YAML
text) and receive per-location errors/warnings without any database or
filesystem mutation.

Paths intentionally avoid the existing ``app.api.config_io`` bases
(``/api/config-io``, ``/api/config_io``, ``/api/system/config``,
``/api/config``) so the pkgutil auto-loader in ``app/main.py`` can
register both routers without collisions. All handlers here are pure:
no ``db_session`` dependency is used.

No ``app/main.py`` changes are required: the application auto-loads any
``app.api`` module exposing ``router``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Query, Request

from app.services import config_validate as svc

router = APIRouter(tags=["config_validate"])

_BASES = (
    "/api/config-validate",
    "/api/config_validate",
)


def _normalize(payload: Any, request: Request | None = None) -> Any:
    if payload is None:
        data: Any = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, str):
        text = payload.strip()
        if not text:
            data = {}
        else:
            try:
                data = json.loads(text)
            except Exception:  # noqa: BLE001
                data = {"raw": payload}
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = json.loads(bytes(payload).decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            data = {"raw": bytes(payload).decode("utf-8", "ignore")}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {"value": payload}
    if request is not None and isinstance(data, dict):
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    if isinstance(data, dict):
        for nested in ("options", "params", "data", "body", "payload", "envelope"):
            nested_val = data.get(nested)
            if isinstance(nested_val, dict):
                for k, v in nested_val.items():
                    data.setdefault(k, v)
    return data


def _coerce_sections(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, list):
        out = [str(v).strip() for v in value if str(v).strip()]
        return out or None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(v).strip() for v in parsed if str(v).strip()] or None
            except Exception:  # noqa: BLE001
                pass
        return [s.strip() for s in text.split(",") if s.strip()] or None
    return None


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "strict"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _candidate(payload: Any, request: Request | None = None) -> Any:
    data = _normalize(payload, request)
    if isinstance(data, dict):
        # A wrapped envelope keeps its shape for the service extractor.
        if "sections" in data or any(k in svc.ALL_KNOWN_SECTIONS for k in data):
            return data
        for key in ("raw", "yaml", "yml", "text", "content", "config_text"):
            if isinstance(data.get(key), str) and data.get(key, "").strip():
                return data
        # Single camera posted directly stays as-is.
        if isinstance(data.get("name"), str) and isinstance(data.get("source"), str):
            return data
        # Query-only call with no body.
        if not data:
            return payload if payload not in (None, {}) else data
        return data
    return data if data else payload


def _do_validate(
    payload: Any,
    request: Request | None = None,
    sections: Any = None,
    strict: Any = False,
    check_references: Any = True,
) -> Dict[str, Any]:
    candidate = _candidate(payload, request)
    section_filter = _coerce_sections(sections)
    if isinstance(candidate, dict) and section_filter is None:
        maybe = candidate.get("sections_filter", candidate.get("sectionsFilter"))
        if maybe is None:
            maybe = candidate.get("only", candidate.get("include"))
        section_filter = _coerce_sections(maybe)
    strict_flag = candidate.get("strict", strict) if isinstance(candidate, dict) else strict
    refs = candidate.get("check_references", check_references) if isinstance(candidate, dict) else check_references
    if isinstance(candidate, dict):
        for alias in ("checkReferences", "verify_refs", "verifyRefs"):
            if candidate.get(alias) is not None and refs is check_references:
                refs = candidate.get(alias)
    report = svc.validate_config_dry_run(
        candidate,
        sections=section_filter,
        strict=_coerce_bool(strict_flag, False),
        check_references=_coerce_bool(refs, True) if isinstance(refs, str) else bool(refs),
    )
    return report


def _register(r: APIRouter, base: str) -> None:
    slug = base.strip("/").replace("/", "_").replace("-", "_")

    async def _validate_handler(
        request: Request,
        payload: Any = Body(default=None),
        sections: Optional[str] = Query(default=None),
        strict: bool = Query(default=False),
        check_references: bool = Query(default=True),
    ):
        return _do_validate(payload, request, sections, strict, check_references)

    async def _validate_query_handler(
        request: Request,
        sections: Optional[str] = Query(default=None),
        strict: bool = Query(default=False),
        check_references: bool = Query(default=True),
    ):
        return _do_validate(None, request, sections, strict, check_references)

    for suffix, methods in (
        ("/validate", ["POST"]),
        ("/validate/", ["POST"]),
        ("/dry-run", ["POST"]),
        ("/dry-run/", ["POST"]),
        ("/check", ["POST"]),
        ("/check/", ["POST"]),
        ("", ["POST"]),
        ("/", ["POST"]),
    ):
        try:
            r.add_api_route(
                base + suffix,
                _validate_handler,
                methods=methods,
                operation_id=f"validate_config_dry_run_{slug}{suffix.replace('/', '_').replace('-', '_') or '_root'}",
            )
        except Exception:  # noqa: BLE001
            pass

    async def _health_handler():
        return {
            "status": "ok",
            "dry_run": True,
            "writes": False,
            "version": svc.CONFIG_VALIDATE_VERSION,
            "kind": svc.CONFIG_VALIDATE_KIND,
        }

    async def _sections_handler():
        return {
            "sections": list(svc.KNOWN_SECTIONS),
            "optional_sections": list(svc.OPTIONAL_SECTIONS),
            "all_sections": list(svc.ALL_KNOWN_SECTIONS),
            "version": svc.CONFIG_VALIDATE_VERSION,
            "kind": svc.CONFIG_VALIDATE_KIND,
            "dry_run": True,
            "writes": False,
        }

    for suffix, handler, op in (
        ("/health", _health_handler, f"config_validate_health_{slug}"),
        ("/sections", _sections_handler, f"config_validate_sections_{slug}"),
    ):
        try:
            r.add_api_route(base + suffix, handler, methods=["GET"], operation_id=op)
        except Exception:  # noqa: BLE001
            pass
        try:
            r.add_api_route(base + suffix + "/", handler, methods=["GET"], operation_id=op + "_slash")
        except Exception:  # noqa: BLE001
            pass


for _base in _BASES:
    _register(router, _base)


# Compatibility aliases for varied import styles.
config_validate_router = router
router_config_validate = router
validate_router = router
dry_run_router = router
config_validator_router = router
configvalidation_router = router

__all__ = [
    "router",
    "config_validate_router",
    "router_config_validate",
    "validate_router",
    "dry_run_router",
    "config_validator_router",
    "configvalidation_router",
]
