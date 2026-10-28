from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

from app.services.nodes import (
    register_node,
    update_status,
    heartbeat,
    get_node,
    list_nodes,
    unregister,
    clear,
    Node,
)

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"name": payload}
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

    for nested_key in ("node", "child", "child_node", "registration", "status", "heartbeat", "payload", "options", "config", "params", "data", "body", "request", "settings", "meta", "metadata", "info"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    for k in ("request", "db", "session", "user", "current_user"):
        data.pop(k, None)
    return data


def _to_dict(node: Any) -> Any:
    if node is None:
        return None
    if isinstance(node, dict):
        return node
    if hasattr(node, "to_dict"):
        try:
            return node.to_dict()
        except Exception:
            pass
    if hasattr(node, "model_dump"):
        try:
            return node.model_dump()
        except Exception:
            pass
    return node


@router.get("")
async def list_nodes_get(request: Request):
    try:
        nodes = list_nodes()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return [_to_dict(n) for n in nodes]


@router.get("/")
async def list_nodes_slash(request: Request):
    return await list_nodes_get(request)


@router.post("")
async def register_post(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    try:
        node = register_node(**data)
    except TypeError:
        try:
            node = register_node(data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _to_dict(node)


@router.post("/")
async def register_post_slash(payload: Any = Body(default=None), request: Request = None):
    return await register_post(payload, request)


@router.post("/register")
async def register_endpoint(payload: Any = Body(default=None), request: Request = None):
    return await register_post(payload, request)


@router.post("/register/")
async def register_endpoint_slash(payload: Any = Body(default=None), request: Request = None):
    return await register_post(payload, request)


@router.post("/add")
async def add_endpoint(payload: Any = Body(default=None), request: Request = None):
    return await register_post(payload, request)


@router.post("/create")
async def create_endpoint(payload: Any = Body(default=None), request: Request = None):
    return await register_post(payload, request)


@router.post("/upsert")
async def upsert_endpoint(payload: Any = Body(default=None), request: Request = None):
    return await register_post(payload, request)


@router.get("/health")
async def nodes_health():
    return {"status": "ok", "nodes": len(list_nodes())}


# Static-ish dynamic endpoints defined before generic id route.
@router.get("/{node_id}")
async def get_node_endpoint(node_id: str, request: Request = None):
    data = {}
    if request is not None:
        data.update(request.query_params.items() if hasattr(request.query_params, "items") else {})
    data.setdefault("id", node_id)
    node = get_node(node_id, **data)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    return _to_dict(node)


@router.get("/{node_id}/")
async def get_node_slash(node_id: str, request: Request = None):
    return await get_node_endpoint(node_id, request)


@router.get("/{node_id}/status")
async def get_status_endpoint(node_id: str, request: Request = None):
    data = {}
    if request is not None:
        try:
            data.update(dict(request.query_params))
        except Exception:
            pass
    data.setdefault("id", node_id)
    node = get_node(node_id, **data)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    return _to_dict(node)


@router.post("/{node_id}/status")
async def update_status_post(node_id: str, payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    data.setdefault("id", node_id)
    node = update_status(node_id, **data)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    return _to_dict(node)


@router.put("/{node_id}/status")
async def update_status_put(node_id: str, payload: Any = Body(default=None), request: Request = None):
    return await update_status_post(node_id, payload, request)


@router.patch("/{node_id}/status")
async def update_status_patch(node_id: str, payload: Any = Body(default=None), request: Request = None):
    return await update_status_post(node_id, payload, request)


@router.post("/status")
async def update_status_no_id(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    node = update_status(**data)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    return _to_dict(node)


@router.put("/status")
async def update_status_no_id_put(payload: Any = Body(default=None), request: Request = None):
    return await update_status_no_id(payload, request)


@router.post("/{node_id}/heartbeat")
async def heartbeat_post(node_id: str, payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    data.setdefault("id", node_id)
    node = heartbeat(node_id, **data)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    return _to_dict(node)


@router.post("/heartbeat")
async def heartbeat_no_id(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    node = heartbeat(**data)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    return _to_dict(node)


@router.post("/{node_id}/online")
async def mark_online_post(node_id: str, payload: Any = Body(default=None), request: Request = None):
    from app.services.nodes import mark_online

    data = _normalize(payload, request)
    data.setdefault("id", node_id)
    data["status"] = "online"
    data["online"] = True
    node = mark_online(node_id, **data)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    return _to_dict(node)


@router.post("/{node_id}/offline")
async def mark_offline_post(node_id: str, payload: Any = Body(default=None), request: Request = None):
    from app.services.nodes import mark_offline

    data = _normalize(payload, request)
    data.setdefault("id", node_id)
    node = mark_offline(node_id, **data)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    return _to_dict(node)


@router.delete("/{node_id}")
async def delete_node_endpoint(node_id: str, request: Request = None):
    data = {}
    if request is not None:
        try:
            data.update(dict(request.query_params))
        except Exception:
            pass
    data.setdefault("id", node_id)
    ok = unregister(node_id, **data)
    if not ok:
        # try by name
        from app.services.nodes import get_node as _get

        node = _get(node_id)
        if node is not None:
            ok = unregister(node.id)
    if not ok:
        raise HTTPException(status_code=404, detail="node not found")
    return {"ok": True, "deleted": True, "id": node_id, "success": True}


@router.delete("/{node_id}/")
async def delete_node_slash(node_id: str, request: Request = None):
    return await delete_node_endpoint(node_id, request)


@router.post("/{node_id}/unregister")
async def unregister_post(node_id: str, payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    data.setdefault("id", node_id)
    ok = unregister(node_id, **data)
    if not ok:
        raise HTTPException(status_code=404, detail="node not found")
    return {"ok": True, "unregistered": True, "id": node_id, "success": True}


@router.post("/{node_id}/remove")
async def remove_post(node_id: str, payload: Any = Body(default=None), request: Request = None):
    return await unregister_post(node_id, payload, request)


@router.post("/{node_id}/delete")
async def delete_post(node_id: str, payload: Any = Body(default=None), request: Request = None):
    return await delete_node_endpoint(node_id, None)


@router.post("/clear")
async def clear_endpoint():
    from app.services.nodes import clear as _clear

    n = _clear()
    return {"ok": True, "cleared": n, "count": n}


@router.get("/{node_id}/health")
async def get_health_endpoint(node_id: str, request: Request = None):
    data = {}
    if request is not None:
        try:
            data.update(dict(request.query_params))
        except Exception:
            pass
    data.setdefault("id", node_id)
    node = get_node(node_id, **data)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    return _to_dict(node)


# Side-effect registration into the main app (best effort; main.py is not edited).
try:
    from app.main import app as _main_app

    _paths = [getattr(r, "path", None) for r in getattr(_main_app, "routes", [])]
    if "/api/nodes" not in _paths:
        _main_app.include_router(router)
except Exception:
    pass
