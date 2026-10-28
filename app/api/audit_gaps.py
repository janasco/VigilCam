"""Audit gaps API router.

Exposes :mod:`app.services.audit_gaps` over HTTP under a broad set of
alias prefixes so clients can discover missing audit time ranges and
missing (un-audited) actions at any reasonable path.

Auto-registered by ``app.main`` (it imports every ``app.api`` module and
includes ``router``), so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(tags=["audit-gaps"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
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
                       "body", "request", "filter", "filters", "policy", "settings"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    # Coerce numerics that arrive as strings.
    for key in (
        "max_gap_seconds", "maxGapSeconds", "max_gap", "gap_threshold",
        "threshold_seconds", "threshold", "min_gap_seconds", "minGapSeconds",
        "min_gap", "window_start", "window_end",
    ):
        if data.get(key) is not None and isinstance(data.get(key), str):
            s = str(data.get(key)).strip()
            if key.startswith(("window", "start", "end", "since", "before", "until")):
                continue
            try:
                data[key] = float(s) if "." in s else int(s)
            except (TypeError, ValueError):
                pass
    # Comma-separated expected actions arriving as one string.
    for key in ("expected_actions", "expectedActions", "expected",
                "required_actions", "requiredActions", "required", "actions"):
        val = data.get(key)
        if isinstance(val, str) and "," in val:
            data[key] = [s.strip() for s in val.split(",") if s.strip()]
    return data


async def _do_detect(data: Any) -> Any:
    from app.services import audit_gaps as svc

    kwargs = dict(data or {})
    return svc.detect_audit_gaps(**kwargs)


async def _do_coverage(data: Any) -> Any:
    from app.services import audit_gaps as svc

    kwargs = dict(data or {})
    return svc.check_action_coverage(**kwargs)


async def _do_status(data: Any) -> Any:
    from app.services import audit_gaps as svc

    kwargs = dict(data or {})
    rep = svc.detect_audit_gaps(**kwargs)
    cov = rep.get("coverage") or {}
    return {
        "status": rep.get("status"),
        "state": rep.get("status"),
        "ok": rep.get("ok"),
        "success": True,
        "healthy": rep.get("healthy"),
        "has_gaps": rep.get("has_gaps"),
        "gaps_found": rep.get("has_gaps"),
        "total_gaps": rep.get("total_gaps"),
        "gap_count": rep.get("gap_count"),
        "missing_actions": rep.get("missing_actions"),
        "missing_count": cov.get("missing_count", 0),
        "coverage_pct": rep.get("coverage_pct"),
        "total_entries": rep.get("total_entries"),
    }


async def _detect_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    max_gap_seconds: Optional[float] = Query(default=None),
    min_gap_seconds: Optional[float] = Query(default=None),
    window_start: Optional[str] = Query(default=None),
    window_end: Optional[str] = Query(default=None),
    expected_actions: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if max_gap_seconds is not None and data.get("max_gap_seconds") is None:
        data["max_gap_seconds"] = max_gap_seconds
    if min_gap_seconds is not None and data.get("min_gap_seconds") is None:
        data["min_gap_seconds"] = min_gap_seconds
    if window_start is not None and data.get("window_start") is None:
        data["window_start"] = window_start
    if window_end is not None and data.get("window_end") is None:
        data["window_end"] = window_end
    if expected_actions is not None and data.get("expected_actions") is None:
        data["expected_actions"] = [s.strip() for s in str(expected_actions).split(",") if s.strip()]
    return await _do_detect(data)


async def _coverage_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
    expected_actions: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if expected_actions is not None and data.get("expected_actions") is None:
        data["expected_actions"] = [s.strip() for s in str(expected_actions).split(",") if s.strip()]
    return await _do_coverage(data)


async def _status_all(
    payload: Any = Body(default=None),
    request: Request = None,  # type: ignore[assignment]
):
    return await _do_status(_normalize(payload, request))


_prefixes = [
    "/api/audit/gaps",
    "/api/audit/gap",
    "/api/audit-gaps",
    "/api/audit-gap",
    "/api/audit_gaps",
    "/api/audit_gap",
    "/api/audits/gaps",
    "/api/audit/coverage/gaps",
    "/api/audit/coverage-gaps",
    "/api/audit/coverage_gaps",
    "/api/gaps/audit",
]

_suffixes = [
    "",
    "/",
    "/status",
    "/summary",
    "/report",
    "/detect",
    "/detection",
    "/scan",
    "/check",
    "/analyze",
    "/analysis",
    "/coverage",
]

_coverage_suffixes = [
    "/coverage",
    "/coverage/report",
    "/coverage/check",
    "/missing",
    "/missing-actions",
    "/missing_actions",
    "/uncovered",
    "/expected",
]

_registered: set = set()
_op_counter = {"n": 0}


def _add(path: str, handler, methods=("GET", "POST")):
    for m in methods:
        key = (path, m)
        if key in _registered:
            continue
        try:
            _op_counter["n"] += 1
            router.add_api_route(
                path, handler, methods=[m],
                operation_id=f"{handler.__name__}_{_op_counter['n']}",
            )
            _registered.add(key)
        except Exception:  # noqa: BLE001
            pass


for _prefix in _prefixes:
    for _suf in _suffixes:
        _p = _prefix if _suf in ("", "/") else f"{_prefix}{_suf}"
        _add(_p, _detect_all)
    _add(f"{_prefix}/health", _status_all)
    for _suf in _coverage_suffixes:
        _add(f"{_prefix}{_suf}", _coverage_all)

# Bare coverage aliases mirroring the detect prefixes.
for _p in (
    "/api/audit/coverage",
    "/api/audit/coverage/",
    "/api/audit-coverage",
    "/api/audit_coverage",
    "/api/audit/missing-actions",
    "/api/audit/missing_actions",
):
    _add(_p, _coverage_all)


# Compatibility aliases.
audit_gaps_router = router
router_audit_gaps = router
gaps_router = router
audit_router = router
audit_coverage_router = router
coverage_router = router
api_router = router

__all__ = ["router"]
