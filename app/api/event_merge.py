"""Motion event merge/split API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
import app.services.event_merge as merge_mod
from app.services.event_merge import (
    auto_merge,
    group_events_for_merge,
    merge_event_dicts,
    merge_events,
    split_event,
    split_event_dict,
    suggest_merges,
)

router = APIRouter(tags=["event_merge"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"event_ids": [p.strip() for p in payload.split(",") if p.strip()]}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"text": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:
            data = {}
    elif isinstance(payload, (list, tuple)):
        data = {"events": list(payload)}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    # keep multi-values as CSV-joined; service coerces them
                    vals = request.query_params.getlist(k)
                    data[k] = vals[0] if len(vals) == 1 else vals
        except Exception:
            pass

    for nested_key in ("merge", "split", "payload", "options", "config",
                       "params", "data", "body", "request", "fields"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _has_inline_events(data: dict) -> bool:
    for key in ("events", "items", "sources"):
        val = data.get(key)
        if isinstance(val, list) and val:
            return True
    for key in ("event", "source", "source_event", "original", "target"):
        if isinstance(data.get(key), dict):
            return True
    return False


def _parse_ids(data: dict) -> List[int]:
    from app.services.event_merge import _coerce_id_list

    ids: List[int] = []
    for key in ("event_ids", "eventIds", "ids", "id_list", "idList",
                "event_id_list", "merge_ids", "to_merge", "sources",
                "source_ids", "inputs", "with_ids", "withIds",
                "other_ids", "secondary_ids", "merge_with"):
        if data.get(key) is not None:
            ids.extend(_coerce_id_list(data.get(key)))
    # bare event_id counts as a merge member when other ids exist
    for key in ("event_id", "eventId", "event", "primary_id", "primaryId"):
        pass
    seen = set()
    out: List[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


async def _do_merge(data: dict, db: Optional[AsyncSession]) -> dict:
    # Pure path when inline event dicts supplied (no DB needed).
    if _has_inline_events(data):
        try:
            merged = merge_events(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        merged.setdefault("success", True)
        merged.setdefault("ok", True)
        result: dict = {"success": True, "ok": True, "merged": merged,
                        "event": merged, "result": merged}
        result.update({k: v for k, v in merged.items() if k not in result})
        return result
    ids = _parse_ids(data)
    # include singular event_id as member if companions present
    from app.services.event_merge import _coerce_int

    singular = None
    for key in ("event_id", "eventId", "event"):
        v = data.get(key)
        if isinstance(v, (int, float, str)) and not isinstance(v, bool):
            singular = _coerce_int(v, None)
            break
    if singular is not None and singular not in ids:
        ids = [singular] + ids
    if len(ids) < 2:
        raise HTTPException(status_code=400,
                            detail="at least two event ids (or inline events) are required to merge")
    if db is None:
        # No DB: synthesize minimal merge from ids.
        try:
            merged = merge_events({"event_ids": ids, **data})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        merged.setdefault("success", True)
        merged.setdefault("ok", True)
        return {"success": True, "ok": True, "merged": merged,
                "event": merged, "result": merged}
    try:
        merged = await merge_mod.merge_events_db(db, {"event_ids": ids, **data})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc) or "Event not found")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return {"success": True, "ok": True, "merged": merged,
            "event": merged, "result": merged, "merged_from": merged.get("merged_from", ids)}


async def _do_split(data: dict, db: Optional[AsyncSession], path_event_id: Optional[int] = None) -> dict:
    if path_event_id is not None and data.get("event_id") is None and data.get("eventId") is None and data.get("id") is None:
        data = dict(data)
        data["event_id"] = path_event_id
    inline = None
    for key in ("event", "source", "source_event", "original", "target"):
        if isinstance(data.get(key), dict):
            inline = data.get(key)
            break
    if inline is not None or _has_inline_events(data):
        try:
            parts = split_event(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"success": True, "ok": True, "parts": parts, "events": parts,
                "split": parts, "count": len(parts)}
    from app.services.event_merge import _coerce_int

    eid = data.get("event_id", data.get("eventId", data.get("id", data.get("event"))))
    if isinstance(eid, dict):
        eid = eid.get("id", eid.get("event_id"))
    eid_int = _coerce_int(eid, None)
    if path_event_id is not None and eid_int is None:
        eid_int = path_event_id
    if eid_int is None:
        raise HTTPException(status_code=400, detail="event_id is required to split")
    data = dict(data)
    data["event_id"] = eid_int
    if db is None:
        try:
            parts = split_event(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"success": True, "ok": True, "parts": parts, "events": parts,
                "split": parts, "count": len(parts)}
    try:
        parts = await merge_mod.split_event_db(db, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError:
        raise HTTPException(status_code=404, detail="Event not found")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return {"success": True, "ok": True, "parts": parts, "events": parts,
            "split": parts, "count": len(parts)}


async def _do_suggest(data: dict, db: Optional[AsyncSession]) -> Any:
    if _has_inline_events(data):
        return suggest_merges(data)
    if db is None:
        return suggest_merges(data)
    try:
        return await merge_mod.suggest_merges_async(data, db=db)
    except Exception:
        return suggest_merges(data)


# Register merge/split/suggest endpoints across alias bases.
_MERGE_BASES = (
    "/api/events/merge",
    "/api/event-merge",
    "/api/event_merge",
    "/api/events/merge-events",
    "/api/motion/events/merge",
    "/api/motion/merge",
)
_SPLIT_BASES = (
    "/api/events/split",
    "/api/event-split",
    "/api/event_split",
    "/api/events/split-event",
    "/api/motion/events/split",
    "/api/motion/split",
)
_SUGGEST_BASES = (
    "/api/events/merge/suggestions",
    "/api/events/merge-suggestions",
    "/api/events/suggest-merge",
    "/api/event-merge/suggestions",
    "/api/event_merge/suggestions",
    "/api/motion/events/merge-suggestions",
)


# NOTE: FastAPI Depends defaults must be declared at function definition
# time, so we register routes imperatively with explicit dependencies.
from fastapi import Depends as _Depends  # noqa: E402


def _build_merge_handler(idx: int):
    async def _merge_handler(
        payload: Any = Body(default=None),
        request: Request = None,
        db: AsyncSession = _Depends(db_session),
    ):
        return await _do_merge(_normalize(payload, request), db)

    _merge_handler.__name__ = f"merge_handler_{idx}"
    return _merge_handler


def _build_split_handler(idx: int):
    async def _split_handler(
        payload: Any = Body(default=None),
        request: Request = None,
        db: AsyncSession = _Depends(db_session),
    ):
        return await _do_split(_normalize(payload, request), db)

    _split_handler.__name__ = f"split_handler_{idx}"
    return _split_handler


def _build_suggest_handlers(idx: int):
    async def _suggest_handler(
        request: Request = None,
        db: AsyncSession = _Depends(db_session),
    ):
        out = await _do_suggest(_normalize(None, request), db)
        if isinstance(out, list):
            return {"success": True, "ok": True, "suggestions": out,
                    "groups": out, "count": len(out)}
        return out

    async def _suggest_post_handler(
        payload: Any = Body(default=None),
        request: Request = None,
        db: AsyncSession = _Depends(db_session),
    ):
        out = await _do_suggest(_normalize(payload, request), db)
        if isinstance(out, list):
            return {"success": True, "ok": True, "suggestions": out,
                    "groups": out, "count": len(out)}
        return out

    _suggest_handler.__name__ = f"suggest_handler_{idx}"
    _suggest_post_handler.__name__ = f"suggest_post_handler_{idx}"
    return _suggest_handler, _suggest_post_handler


for _idx, _base in enumerate(_MERGE_BASES):
    _h = _build_merge_handler(_idx)
    router.post(_base, summary="Merge motion events")(_h)
    router.post(_base + "/", include_in_schema=False)(_h)

for _idx, _base in enumerate(_SPLIT_BASES):
    _h = _build_split_handler(_idx)
    router.post(_base, summary="Split motion event")(_h)
    router.post(_base + "/", include_in_schema=False)(_h)

for _idx, _base in enumerate(_SUGGEST_BASES):
    _gh, _ph = _build_suggest_handlers(_idx)
    router.get(_base, summary="Suggest motion event merges")(_gh)
    router.post(_base, summary="Suggest motion event merges")(_ph)


@router.post("/api/events/{event_id}/split", summary="Split a motion event")
@router.post("/api/events/{event_id}/split/", include_in_schema=False)
async def split_single_event(
    event_id: int,
    payload: Any = Body(default=None),
    request: Request = None,
    db: AsyncSession = _Depends(db_session),
):
    return await _do_split(_normalize(payload, request), db, path_event_id=int(event_id))


@router.post("/api/events/{event_id}/merge", summary="Merge a motion event with others")
@router.post("/api/events/{event_id}/merge/", include_in_schema=False)
async def merge_single_event(
    event_id: int,
    payload: Any = Body(default=None),
    request: Request = None,
    db: AsyncSession = _Depends(db_session),
):
    data = _normalize(payload, request)
    # fold path id into the merge set
    from app.services.event_merge import _coerce_id_list

    existing: List[int] = []
    for key in ("event_ids", "eventIds", "ids", "with_ids", "merge_with", "other_ids"):
        if data.get(key) is not None:
            existing.extend(_coerce_id_list(data.get(key)))
    if int(event_id) not in existing:
        existing = [int(event_id)] + existing
    data["event_ids"] = existing
    return await _do_merge(data, db)


@router.get("/api/events/{event_id}/split-preview", summary="Preview split of a motion event")
async def split_preview(
    event_id: int,
    request: Request = None,
    count: Optional[int] = Query(default=None),
    parts: Optional[int] = Query(default=None),
    db: AsyncSession = _Depends(db_session),
):
    data = _normalize(None, request)
    data.setdefault("event_id", int(event_id))
    if count is not None:
        data.setdefault("count", count)
    if parts is not None:
        data.setdefault("parts", parts)
    # Preview prefers pure computation when the row cannot be fetched.
    try:
        from app.models.entities import Event as _Event

        row = await db.get(_Event, int(event_id))
        if row is not None:
            base = {
                "id": row.id,
                "event_id": row.id,
                "camera_id": row.camera_id,
                "kind": row.kind,
                "label": row.label,
                "confidence": row.confidence,
                "payload": row.payload,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            parts_out = split_event_dict(base, count=data.get("count", data.get("parts", 2)))
            return {"success": True, "ok": True, "parts": parts_out,
                    "events": parts_out, "count": len(parts_out)}
    except HTTPException:
        raise
    except Exception:
        pass
    return await _do_split(data, None)


# Compatibility aliases.
event_merge_router = router
merge_router = router
split_router = router
motion_merge_router = router
api_router = router

__all__ = ["router"]
