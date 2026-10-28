"""Evidence manifest API.

Auto-registered by ``app.main`` (any module in ``app.api`` exposing
``router`` is included), so no ``app/main.py`` edit is needed.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter(tags=["evidence-manifest"])


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
    # Manifest path aliases.
    if data.get("manifest_path") is None:
        for k in ("manifest", "manifest_file", "manifestPath", "path", "file",
                  "filepath", "filename", "output", "out", "dest", "target",
                  "location"):
            if data.get(k) is not None and k not in ("root", "directory"):
                # Only adopt generic `path` when it points at a manifest-ish
                # target (json suffix) or when no root-ish key is present.
                if k in ("path", "file", "target", "location"):
                    has_root = any(data.get(r) is not None for r in
                                   ("root", "directory", "storage_root", "recordings_dir"))
                    val = str(data.get(k))
                    if has_root and not val.endswith(".json"):
                        continue
                data["manifest_path"] = data.get(k)
                break
    return data


def _root_kwargs(data: dict) -> dict:
    kw: dict = {}
    for k in ("root", "directory", "storage_root", "recordings_dir",
              "recordings_path", "base_dir", "scan_path", "folder",
              "extensions", "exts", "suffixes", "video_extensions",
              "reason", "flush_reason", "db_uuid", "dir_uuid",
              "verify_files", "check_files", "with_files", "no_files"):
        if k in data and data[k] is not None:
            kw[k] = data[k]
    # String booleans from query params.
    for k in ("verify_files", "check_files", "with_files", "no_files", "save"):
        if isinstance(kw.get(k), str):
            s = kw[k].strip().lower()
            kw[k] = s not in ("0", "false", "no", "n", "off", "disabled")
    return kw


async def _do_status(data: dict) -> Any:
    from app.services import evidence_manifest as svc

    mp = data.get("manifest_path")
    kw = _root_kwargs(data)
    return svc.get_status(mp, **kw)


async def _do_build(data: dict) -> Any:
    from app.services import evidence_manifest as svc

    mp = data.get("manifest_path")
    kw = _root_kwargs(data)
    for k in ("save", "store", "persist", "write"):
        if k in data and data[k] is not None:
            kw[k] = data[k]
    # Building persists durably by default through the API so the chain
    # head is committed like a Moonfire flush.
    kw.setdefault("save", True)
    return svc.build_manifest(manifest_path=mp, **kw)


async def _do_verify(data: dict) -> Any:
    from app.services import evidence_manifest as svc

    mp = data.get("manifest_path")
    kw = _root_kwargs(data)
    manifest = data.get("manifest") if isinstance(data.get("manifest"), dict) else None
    if manifest is None and isinstance(data.get("entries"), list):
        manifest = {"entries": data.get("entries"), "head": data.get("head")}
    return svc.verify_manifest(manifest, manifest_path=mp, **kw)


async def _do_append(data: dict) -> Any:
    from app.services import evidence_manifest as svc

    mp = data.get("manifest_path")
    kw = _root_kwargs(data)
    target: Any = None
    for k in ("target_file", "target", "file", "filepath", "filename",
              "source", "path", "rel"):
        if data.get(k) is not None and k != "manifest_path":
            # Disambiguate: `manifest_path` already consumed; a remaining
            # `path` that ends in .json is the manifest, otherwise the file.
            if k in ("path", "target") and str(data.get(k)).endswith(".json") \
                    and data.get("file") is None and data.get("filepath") is None:
                continue
            target = data.get(k)
            break
    if target is None:
        # Fall back to building (scan) when no single file was given.
        return await _do_build(data)
    return svc.append_entry(target, manifest_path=mp, **kw)


async def _status_all(payload: Any = Body(default=None), request: Request = None):
    return await _do_status(_normalize(payload, request))


async def _build_all(payload: Any = Body(default=None), request: Request = None):
    return await _do_build(_normalize(payload, request))


async def _verify_all(payload: Any = Body(default=None), request: Request = None):
    return await _do_verify(_normalize(payload, request))


async def _append_all(payload: Any = Body(default=None), request: Request = None):
    return await _do_append(_normalize(payload, request))


_prefixes = [
    "/api/evidence-manifest",
    "/api/evidence_manifest",
    "/api/evidence/manifest",
    "/api/manifest/evidence",
    "/api/evidence-manifests",
    "/api/evidence_manifests",
]

_status_suffixes = ["", "/", "/status", "/health", "/summary", "/report", "/state", "/info"]
_build_suffixes = ["/build", "/generate", "/create", "/scan", "/rebuild", "/commit", "/flush"]
_verify_suffixes = ["/verify", "/validate", "/check", "/inspect"]
_append_suffixes = ["/append", "/add", "/record", "/track", "/append-file", "/add-file"]

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
evidence_manifest_router = router
router_evidence_manifest = router
manifest_router = router
evidence_router = router

__all__ = ["router"]
