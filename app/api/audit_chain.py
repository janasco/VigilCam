"""Audit chain API.

Tamper-evident hash-chain endpoints over the audit log. Auto-registered
by ``app.main`` (any module in ``app.api`` exposing ``router`` is
included), so no ``app/main.py`` edit is needed.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter(tags=["audit-chain"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
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
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    # Chain-path aliases.
    if data.get("chain_path") is None:
        for k in ("chainPath", "manifest_path", "manifest", "manifest_file",
                  "chain_file", "path", "file", "filepath", "filename",
                  "output", "out", "dest", "target", "location"):
            if data.get(k) is not None and k not in ("entry", "audit_entry"):
                if k in ("path", "file", "target", "location"):
                    val = str(data.get(k))
                    # A bare audit entry dict also arrives flattened; only
                    # adopt generic `path` when it looks like a chain file.
                    has_entryish = any(data.get(r) is not None for r in
                                       ("action", "event_type", "actor", "entry"))
                    if has_entryish and not val.endswith(".json"):
                        continue
                data["chain_path"] = data.get(k)
                break
    return data


def _chain_kwargs(data: dict) -> dict:
    kw: dict = {}
    for k in ("chain_path", "manifest_path", "manifest", "chain_file",
              "path", "file", "filepath", "filename",
              "genesis", "genesis_hash", "algorithm", "hash_algorithm",
              "save", "store", "persist", "write"):
        if k in data and data[k] is not None:
            kw[k] = data[k]
    for k in ("save", "store_chain", "persist", "write"):
        if isinstance(kw.get(k), str):
            s = kw[k].strip().lower()
            kw[k] = s not in ("0", "false", "no", "n", "off", "disabled")
    return kw


def _entries_from(data: dict) -> Any:
    for k in ("entries", "items", "logs", "rows", "events",
              "audit_entries", "audit_logs", "records", "chain_entries"):
        if data.get(k) is not None:
            return data.get(k)
    if isinstance(data.get("chain"), dict) and "entries" in data["chain"]:
        return data["chain"]
    if isinstance(data.get("chain"), list):
        return data["chain"]
    return None


async def _do_status(data: dict) -> Any:
    from app.services import audit_chain as svc

    kw = _chain_kwargs(data)
    entries = _entries_from(data)
    return svc.get_status(entries=entries, **kw)


async def _do_build(data: dict) -> Any:
    from app.services import audit_chain as svc

    kw = _chain_kwargs(data)
    entries = _entries_from(data)
    # Building via the API persists durably so the head is committed.
    kw.setdefault("save", True)
    return svc.build_chain(entries, **kw)


async def _do_verify(data: dict) -> Any:
    from app.services import audit_chain as svc

    kw = _chain_kwargs(data)
    entries = _entries_from(data)
    chain = data.get("chain") if isinstance(data.get("chain"), dict) else None
    if chain is None and isinstance(entries, dict) and "entries" in entries:
        chain = entries
        entries = None
    manifest = data.get("manifest") if isinstance(data.get("manifest"), dict) else None
    if chain is None and manifest is not None and "entries" in manifest:
        chain = manifest
    return svc.verify_chain(chain, entries=entries, **kw)


async def _do_append(data: dict) -> Any:
    from app.services import audit_chain as svc

    kw = _chain_kwargs(data)
    target: Any = None
    for k in ("entry", "audit_entry", "item", "record", "event", "audit"):
        if data.get(k) is not None:
            target = data.get(k)
            break
    if target is None:
        # Remaining keys may be the audit fields themselves.
        probe = {k: v for k, v in data.items()
                 if k not in ("chain_path", "manifest_path", "manifest",
                              "chain_file", "path", "file", "filepath",
                              "filename", "genesis", "algorithm", "save",
                              "entries", "items", "chain")}
        entryish = any(k in probe for k in ("action", "event_type", "type",
                                            "kind", "event", "actor", "details"))
        if entryish and probe:
            target = probe
    if target is None:
        entries = _entries_from(data)
        if entries is not None:
            kw2 = dict(kw)
            kw2.setdefault("save", True)
            return svc.build_chain(entries, **kw2)
        raise ValueError("append requires an audit entry")
    return svc.append_entry(target, **kw)


async def _status_all(payload: Any = Body(default=None), request: Request = None):
    return await _do_status(_normalize(payload, request))


async def _build_all(payload: Any = Body(default=None), request: Request = None):
    return await _do_build(_normalize(payload, request))


async def _verify_all(payload: Any = Body(default=None), request: Request = None):
    return await _do_verify(_normalize(payload, request))


async def _append_all(payload: Any = Body(default=None), request: Request = None):
    return await _do_append(_normalize(payload, request))


_prefixes = [
    "/api/audit/chain",
    "/api/audit-chain",
    "/api/audit_chain",
    "/api/audit/chains",
    "/api/audit-chains",
    "/api/audit_chains",
    "/api/chain/audit",
]

_status_suffixes = ["", "/", "/status", "/health", "/summary", "/report", "/state", "/info"]
_build_suffixes = ["/build", "/generate", "/create", "/scan", "/rebuild", "/commit", "/flush"]
_verify_suffixes = ["/verify", "/validate", "/check", "/inspect"]
_append_suffixes = ["/append", "/add", "/record", "/track", "/append-entry", "/add-entry"]

_registered: set = set()
_op_counter = {"n": 0}


def _add(path: str, handler, methods=("GET", "POST")):
    for m in methods:
        key = (path, m)
        if key in _registered:
            continue
        try:
            _op_counter["n"] += 1
            router.add_api_route(path, handler, methods=[m],
                                 operation_id=f"{handler.__name__}_{_op_counter['n']}")
            _registered.add(key)
        except Exception:  # noqa: BLE001
            pass


for _prefix in _prefixes:
    for _suf in _status_suffixes:
        _p = _prefix if _suf in ("", "/") else f"{_prefix}{_suf}"
        _add(_p, _status_all)
    for _suf in _build_suffixes:
        _add(f"{_prefix}{_suf}", _build_all, methods=("GET", "POST"))
    for _suf in _verify_suffixes:
        _add(f"{_prefix}{_suf}", _verify_all, methods=("GET", "POST"))
    for _suf in _append_suffixes:
        _add(f"{_prefix}{_suf}", _append_all, methods=("GET", "POST", "PUT"))

# Compatibility aliases.
audit_chain_router = router
router_audit_chain = router
chain_router = router
audit_router_chain = router

__all__ = ["router"]
