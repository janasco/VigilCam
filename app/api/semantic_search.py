from __future__ import annotations

import json
from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import db_session
from app.services import semantic_search as ss

router = APIRouter(tags=["semantic_search"])


def _merge_request_data(request: Request, payload: Any = None) -> dict:
    data: dict = {}
    try:
        for k, v in request.query_params.items():
            data[k] = v
    except Exception:
        pass
    if payload is not None:
        if isinstance(payload, dict):
            data.update(payload)
        elif isinstance(payload, str):
            try:
                parsed = json.loads(payload)
                if isinstance(parsed, dict):
                    data.update(parsed)
                else:
                    data.setdefault("query", payload)
            except Exception:
                data.setdefault("query", payload)
        else:
            try:
                data.update(dict(payload))
            except Exception:
                pass
    # also try reading body if not provided
    return data


async def _read_body(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        try:
            raw = await request.body()
            if raw:
                return raw.decode("utf-8", "ignore")
        except Exception:
            pass
    return None


async def _do_search(request: Request, payload: Any = None, db=None) -> Any:
    body = payload
    if body is None:
        body = await _read_body(request)
    data = _merge_request_data(request, body)

    # If the caller is clearly indexing a document (no query-like field but
    # text/content present), handle it as an index operation for convenience.
    _query_like = None
    for _k in ("query", "q", "search", "question", "prompt", "input", "term",
               "keyword", "keywords", "image", "image_base64", "img", "vector",
               "embedding", "embeddings"):
        if _k in data and data.get(_k) is not None:
            _query_like = data.get(_k)
            break
    if _query_like is None and any(k in data and data.get(k) is not None for k in
                                   ("text", "content", "body", "document", "doc")):
        return await _do_index(request, payload if payload is not None else body)

    # in-memory docs take precedence if any exist
    try:
        if ss.count() > 0:
            return ss.search(**data)
    except Exception:
        pass

    if db is not None:
        try:
            return await ss.search_events(db, **data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    # fallback in-memory (empty)
    try:
        return ss.search(**data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def _do_index(request: Request, payload: Any = None) -> Any:
    body = payload
    if body is None:
        body = await _read_body(request)
    data = _merge_request_data(request, body)
    try:
        return ss.add_document(**data)
    except TypeError:
        try:
            return ss.add_document(data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


_PATHS = (
    "/api/semantic_search",
    "/api/semantic-search",
    "/api/semanticSearch",
    "/api/semantics",
    "/api/semantic",
    "/api/search/semantic",
    "/api/search",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def search_get(request: Request, db=Depends(db_session)):
        return await _do_search(request, None, db)

    @r.get(base + "/")
    async def search_get_slash(request: Request, db=Depends(db_session)):
        return await _do_search(request, None, db)

    @r.post(base)
    async def search_post(request: Request, payload: Any = Body(default=None), db=Depends(db_session)):
        return await _do_search(request, payload, db)

    @r.post(base + "/")
    async def search_post_slash(request: Request, payload: Any = Body(default=None), db=Depends(db_session)):
        return await _do_search(request, payload, db)

    @r.get(base + "/search")
    async def search_alias_get(request: Request, db=Depends(db_session)):
        return await _do_search(request, None, db)

    @r.post(base + "/search")
    async def search_alias_post(request: Request, payload: Any = Body(default=None), db=Depends(db_session)):
        return await _do_search(request, payload, db)

    # documents
    @r.get(base + "/documents")
    async def docs_list(request: Request):
        try:
            return ss.list_documents()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/docs")
    async def docs_list_alias(request: Request):
        return await docs_list(request)

    @r.post(base + "/documents")
    async def docs_add(request: Request, payload: Any = Body(default=None)):
        return await _do_index(request, payload)

    @r.post(base + "/index")
    async def index_endpoint(request: Request, payload: Any = Body(default=None)):
        return await _do_index(request, payload)

    @r.post(base + "/add")
    async def add_endpoint(request: Request, payload: Any = Body(default=None)):
        return await _do_index(request, payload)

    @r.post(base + "/upsert")
    async def upsert_endpoint(request: Request, payload: Any = Body(default=None)):
        return await _do_index(request, payload)

    @r.delete(base + "/documents/{doc_id}")
    async def docs_delete(doc_id: str):
        try:
            ok = ss.remove_document(doc_id)
            return {"deleted": bool(ok)}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/clear")
    async def clear_endpoint():
        try:
            return {"cleared": ss.clear()}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.post(base + "/reset")
    async def reset_endpoint():
        try:
            return {"reset": ss.reset()}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get(base + "/count")
    async def count_endpoint():
        try:
            return {"count": ss.count()}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))


for base in _PATHS:
    _register(router, base)

# prefixed alias routers
underscore_router = APIRouter(prefix="/api/semantic_search", tags=["semantic_search"])
hyphen_router = APIRouter(prefix="/api/semantic-search", tags=["semantic_search"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request, db=Depends(db_session)):
        return await _do_search(request, None, db)

    @r.get("/")
    async def slash_get(request: Request, db=Depends(db_session)):
        return await _do_search(request, None, db)

    @r.post("")
    async def root_post(request: Request, payload: Any = Body(default=None), db=Depends(db_session)):
        return await _do_search(request, payload, db)

    @r.post("/")
    async def slash_post(request: Request, payload: Any = Body(default=None), db=Depends(db_session)):
        return await _do_search(request, payload, db)

    @r.post("/index")
    async def idx(request: Request, payload: Any = Body(default=None)):
        return await _do_index(request, payload)

    @r.post("/add")
    async def addp(request: Request, payload: Any = Body(default=None)):
        return await _do_index(request, payload)

    @r.get("/documents")
    async def pd(request: Request):
        return ss.list_documents()

    @r.get("/count")
    async def pc():
        return {"count": ss.count()}

    @r.post("/clear")
    async def pcl():
        return {"cleared": ss.clear()}


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

# Compatibility aliases
semantic_search_router = underscore_router
semanticSearchRouter = underscore_router
semantic_search_api_router = underscore_router
semantic_search_hyphen_router = hyphen_router
search_router = underscore_router
search_api_router = underscore_router

__all__ = [
    "router",
    "underscore_router",
    "hyphen_router",
    "semantic_search_router",
    "semanticSearchRouter",
    "semantic_search_api_router",
    "semantic_search_hyphen_router",
    "search_router",
    "search_api_router",
]
