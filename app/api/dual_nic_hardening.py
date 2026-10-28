from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

from app.services import dual_nic_hardening as svc


def _svc():
    return svc


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"value": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"data": payload.decode("utf-8", "ignore")}
        except Exception:
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "status", "hardening", "nic", "nics", "interfaces", "firewall"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


def _to_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "status"):
        try:
            return obj.status()
        except Exception:
            pass
    return obj


def _call(name: str, data: dict) -> Any:
    fn = getattr(svc, name, None)
    # try service instance
    try:
        inst = getattr(svc, "dual_nic_hardening_service", None) or getattr(svc, "service", None)
        if inst is not None and hasattr(inst, name):
            fn = getattr(inst, name)
    except Exception:
        pass
    if callable(fn):
        try:
            return fn(**data)
        except TypeError:
            try:
                return fn(data)
            except Exception:
                try:
                    return fn()
                except Exception as exc:
                    raise exc
        except Exception as exc:
            raise exc
    # fallback to status
    try:
        return svc.status(**data)
    except Exception:
        return svc.status()


router = APIRouter(tags=["dual_nic_hardening"])

_BASES = (
    "/api/dual_nic_hardening",
    "/api/dual-nic-hardening",
    "/api/dualNicHardening",
    "/api/dualnic_hardening",
    "/api/dual_nic",
    "/api/dual-nic",
    "/api/nic_hardening",
    "/api/nic-hardening",
    "/api/network_hardening",
    "/api/network-hardening",
    "/api/network/dual-nic",
    "/api/network/dual_nic",
    "/api/network/dual-nic-hardening",
    "/api/network/dual_nic_hardening",
    "/api/network/dualnic_hardening",
    "/api/dual_nic/hardening",
    "/api/dual-nic/hardening",
    "/api/nic/hardening",
    "/api/hardening",
    "/api/hardening/dual_nic",
    "/api/hardening/dual-nic",
    "/api/system/dual_nic_hardening",
    "/api/system/network/hardening",
    "/api/v1/dual_nic_hardening",
    "/api/v1/dual-nic-hardening",
    "/api/v1/network/hardening",
    "/api/v1/dual_nic/hardening",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def get_root(request: Request):
        data = _normalize(None, request)
        try:
            return _to_dict(_call("status", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/")
    async def get_root_slash(request: Request):
        return await get_root(request)

    @r.get(base + "/status")
    async def get_status(request: Request):
        data = _normalize(None, request)
        try:
            return _to_dict(_call("status", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/state")
    async def get_state(request: Request):
        return await get_status(request)

    @r.get(base + "/info")
    async def get_info(request: Request):
        return await get_status(request)

    @r.get(base + "/config")
    async def get_config(request: Request):
        return await get_status(request)

    @r.get(base + "/health")
    async def health():
        try:
            return {"ok": True, "status": "ok"}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/interfaces")
    async def get_interfaces(request: Request):
        data = _normalize(None, request)
        try:
            ifaces = svc.detect_interfaces(**data)
            return {"interfaces": ifaces, "nics": ifaces, "ok": True}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/nics")
    async def get_nics(request: Request):
        return await get_interfaces(request)

    @r.get(base + "/rules")
    async def get_rules(request: Request):
        data = _normalize(None, request)
        try:
            rules = svc.dual_nic_hardening_service.firewall_rules(**data)
            return {"rules": rules, "firewall_rules": rules, "ok": True}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base)
    async def post_root(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        # decide action by payload hints
        action = None
        for k in ("action", "operation", "op", "mode", "command"):
            if data.get(k) is not None:
                action = str(data.get(k)).lower()
                break
        try:
            if action in {"status", "get", "info", "state", "check", "verify", "validate"}:
                return _to_dict(_call(action, data))
            if action in {"apply", "harden", "enable", "configure", "config", "update", "set", "run", "execute"}:
                return _to_dict(_call("apply" if action in {"harden", "enable", "run", "execute"} else ("configure" if action in {"config", "configure", "update", "set"} else action), data))
            if action in {"reset", "clear", "remove", "disable"}:
                return _to_dict(_call(action, data))
            if action in {"detect", "detect_interfaces", "interfaces", "nics"}:
                ifaces = svc.detect_interfaces(**data)
                return {"interfaces": ifaces, "nics": ifaces, "ok": True}
            # default apply if hardened flag present else status
            if any(k in data for k in ("hardened", "enabled", "active", "applied", "isolated", "management_interface", "camera_interface")):
                return _to_dict(_call("apply" if data.get("hardened", data.get("enabled", data.get("applied"))) else "configure", data))
            return _to_dict(_call("status", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/")
    async def post_root_slash(payload: Any = Body(default=None), request: Request = None):
        return await post_root(payload, request)

    @r.post(base + "/status")
    async def post_status(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            return _to_dict(_call("status", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/apply")
    async def post_apply(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            return _to_dict(_call("apply", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/apply/")
    async def post_apply_slash(payload: Any = Body(default=None), request: Request = None):
        return await post_apply(payload, request)

    @r.post(base + "/harden")
    async def post_harden(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            return _to_dict(_call("apply", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/configure")
    async def post_configure(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            return _to_dict(_call("configure", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/config")
    async def post_config(payload: Any = Body(default=None), request: Request = None):
        return await post_configure(payload, request)

    @r.post(base + "/update")
    async def post_update(payload: Any = Body(default=None), request: Request = None):
        return await post_configure(payload, request)

    @r.post(base + "/set")
    async def post_set(payload: Any = Body(default=None), request: Request = None):
        return await post_configure(payload, request)

    @r.post(base + "/check")
    async def post_check(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            return _to_dict(_call("check", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/verify")
    async def post_verify(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            return _to_dict(_call("verify", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/validate")
    async def post_validate(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            return _to_dict(_call("validate", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/reset")
    async def post_reset(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            return _to_dict(_call("reset", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/clear")
    async def post_clear(payload: Any = Body(default=None), request: Request = None):
        return await post_reset(payload, request)

    @r.post(base + "/disable")
    async def post_disable(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            return _to_dict(_call("disable", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/enable")
    async def post_enable(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            return _to_dict(_call("apply", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/detect")
    async def post_detect(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            ifaces = svc.detect_interfaces(**data)
            return {"interfaces": ifaces, "nics": ifaces, "ok": True}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/interfaces")
    async def post_interfaces(payload: Any = Body(default=None), request: Request = None):
        return await post_detect(payload, request)

    @r.post(base + "/rules")
    async def post_rules(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            if "rules" in data or "firewall_rules" in data or "rule" in data:
                return _to_dict(svc.dual_nic_hardening_service.set_rules(**data))
            return _to_dict(_call("status", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/{item_id}")
    async def get_item(item_id: str, request: Request):
        data = _normalize(None, request)
        data.setdefault("id", item_id)
        try:
            return _to_dict(_call("status", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/{item_id}")
    async def post_item(item_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        data.setdefault("id", item_id)
        try:
            return _to_dict(_call("apply", data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))


for _base in _BASES:
    _register(router, _base)


# Also expose underscore/hyphen router aliases
underscore_router = router
hyphen_router = router
push_router = router
