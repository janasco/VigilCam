"""Plugin manifest API router — AgentDVR ``plugin.json`` parity.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from app.services import plugin_manifest as svc

router = APIRouter(tags=["plugin_manifest"])


def _manifest_to_dict(manifest: Any) -> Any:
    if manifest is None:
        return None
    if isinstance(manifest, dict):
        return dict(manifest)
    if hasattr(manifest, "model_dump"):
        try:
            return dict(manifest.model_dump())
        except Exception:
            pass
    return manifest


def _body_dict(payload: Any, request: Optional[Request] = None) -> Dict[str, Any]:
    if payload is None:
        data: Dict[str, Any] = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (list, tuple)):
        data = {"sections": list(payload)}
    elif isinstance(payload, (str, bytes, bytearray)):
        try:
            import json as _json

            text = payload.decode("utf-8", "ignore") if isinstance(payload, (bytes, bytearray)) else payload
            parsed = _json.loads(text)
            data = dict(parsed) if isinstance(parsed, dict) else {"value": parsed}
        except Exception:
            data = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {}
    # unwrap common envelopes
    for envelope in ("manifest", "plugin", "plugin_manifest", "pluginManifest", "data", "payload"):
        nested = data.get(envelope)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass
    return data


def _register(r: APIRouter, base: str) -> None:
    @r.get(base, summary="List plugin manifests")
    async def _list(request: Request):
        try:
            params = dict(request.query_params)
            plugins_dir = params.get("plugins_dir") or params.get("dir") or params.get("path")
            items = svc.discover_manifests(plugins_dir) if plugins_dir else svc.discover_manifests()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        return [_manifest_to_dict(m) for m in items]

    @r.get(base + "/", include_in_schema=False)
    async def _list_slash(request: Request):
        return await _list(request)

    @r.post(base + "/parse", summary="Parse (normalize) a plugin manifest")
    async def _parse(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _body_dict(payload, request)
        if not data:
            raise HTTPException(status_code=422, detail="manifest body is required")
        try:
            return _manifest_to_dict(svc.parse_manifest(data))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/validate", summary="Validate a plugin manifest")
    async def _validate(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _body_dict(payload, request)
        if not data:
            raise HTTPException(status_code=422, detail="manifest body is required")
        errors = svc.validate_errors(data)
        if errors:
            return {"valid": False, "errors": errors, "manifest": _manifest_to_dict(svc.parse_manifest(data))}
        return {"valid": True, "errors": [], "manifest": _manifest_to_dict(svc.parse_manifest(data))}

    @r.get(base + "/examples", summary="Builtin Barcode/Demo/Gain example manifests")
    async def _examples():
        try:
            return {k: _manifest_to_dict(v) for k, v in svc.builtin_examples().items()}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/examples/{kind}", summary="Single builtin example manifest")
    async def _example_kind(kind: str):
        examples = svc.builtin_examples()
        item = examples.get(str(kind).lower())
        if item is None:
            raise HTTPException(status_code=404, detail="unknown example: {}".format(kind))
        return _manifest_to_dict(item)

    @r.get(base + "/{name}", summary="Get a plugin manifest by name")
    async def _get(name: str, request: Request):
        params = dict(request.query_params)
        plugins_dir = params.get("plugins_dir") or params.get("dir") or params.get("path")
        try:
            item = svc.get_manifest(name, plugins_dir)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if item is None:
            # fall back to builtin examples so Barcode/Demo/Gain resolve
            # even when no plugin.json files are installed
            item = svc.builtin_examples().get(str(name).lower())
            if item is None:
                raise HTTPException(status_code=404, detail="Plugin manifest not found")
        return _manifest_to_dict(item)

    @r.get(base + "/{name}/config_schema", summary="Get a manifest config schema")
    async def _get_schema(name: str, request: Request):
        item = await _get(name, request)
        if isinstance(item, dict):
            schema = item.get("config_schema") or item.get("configSchema") or {}
            return schema
        return {}

    @r.get(base + "/{name}/custom_events", summary="Get a manifest custom events list")
    async def _get_events(name: str, request: Request):
        item = await _get(name, request)
        events: List[Any] = []
        if isinstance(item, dict):
            events = item.get("custom_events", item.get("customEvents", [])) or []
        return {"name": name, "custom_events": list(events)}

    @r.get(base + "/{name}/commands", summary="Get a manifest plugincommand list")
    async def _get_commands(name: str, request: Request):
        item = await _get(name, request)
        commands: List[Any] = []
        if isinstance(item, dict):
            commands = item.get("commands", []) or []
        return {"name": name, "commands": list(commands)}


_BASES = (
    "/api/plugin_manifests",
    "/api/v1/plugin_manifests",
    "/api/plugin-manifests",
    "/api/plugin_manifest",
    "/api/v1/plugin_manifest",
)

for _base in _BASES:
    _register(router, _base)


# Compatibility aliases.
plugin_manifest_router = router
plugin_manifests_router = router
manifest_router = router
manifests_router = router
api_router = router

__all__ = ["router"]
