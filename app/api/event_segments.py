"""Event-to-segment linker API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.event_segments as seg_mod
from app.services.event_segments import (
    delete_links_for_event,
    discover_segments_for_event,
    get_events_for_segment,
    get_link,
    get_segments_for_event,
    link_event_to_segment,
    list_links,
    unlink_event_segment,
)

router = APIRouter(tags=["event_segments"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"segment": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"segment": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, (list, tuple)):
        data = {"segments": list(payload)}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {"segment": str(payload)}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    vals = request.query_params.getlist(k)
                    data[k] = vals[0] if len(vals) == 1 else vals
        except Exception:  # noqa: BLE001
            pass
    for nested_key in ("link", "segment", "payload", "options", "config",
                        "params", "data", "body", "request", "fields", "event"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _coerce_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _require_event_id(event_id: Any) -> int:
    eid = _coerce_int_or_none(event_id)
    if eid is None:
        raise HTTPException(status_code=400, detail="event_id is required")
    return eid


def _link_or_404(link: Optional[dict], link_id: Any = None) -> dict:
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found")
    return link


async def _do_list(event_id: int, data: dict, db: Any) -> List[dict]:
    eid = _require_event_id(event_id)
    # Prefer DB-backed async discovery when a session is available.
    if db is not None:
        try:
            out = await seg_mod.get_segments_for_event_async(eid, db=db, **data)
            return out
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            pass
    # Sync fallback: inline event / camera / timestamp hints or manual links.
    inline: Optional[dict] = None
    for key in ("event", "source", "source_event"):
        if isinstance(data.get(key), dict):
            inline = dict(data.get(key))
            break
    if inline is not None:
        inline.setdefault("id", eid)
        inline.setdefault("event_id", eid)
        try:
            return discover_segments_for_event(inline, **data)
        except Exception:  # noqa: BLE001
            pass
        try:
            return get_segments_for_event(inline, **data)
        except Exception:  # noqa: BLE001
            pass
    has_hints = any(
        data.get(k) is not None
        for k in (
            "camera_id", "cameraId", "camera", "camera_name", "cameraName",
            "created_at", "createdAt", "timestamp", "time", "event_time",
            "recording_dir", "recordingDir", "recordings_dir", "storage_root",
            "window_before", "window_after", "window", "pre_roll", "post_roll",
        )
    )
    if has_hints:
        synth: dict = {"id": eid, "event_id": eid}
        for k in (
            "camera_id", "cameraId", "camera", "camera_name", "cameraName",
            "created_at", "createdAt", "timestamp", "time", "event_time",
            "start", "start_time", "recording_dir",
        ):
            if data.get(k) is not None:
                synth[k] = data.get(k)
        try:
            found = discover_segments_for_event(synth, **data)
        except Exception:  # noqa: BLE001
            found = []
        manual = [m for m in list_links(event_id=eid)]
        seen = {str(d.get("path") or d.get("segment_path")) for d in found}
        for m in manual:
            if str(m.get("segment_path") or m.get("path")) not in seen:
                found.append(m)
        return found
    # Manual-links path (works with no DB and no hints).
    manual = list_links(event_id=eid)
    if manual:
        return manual
    # Last resort: try DB-backed discovery already attempted; if the event
    # row exists but has no overlapping segments, return [] (not 404).
    if db is not None:
        try:
            row = await seg_mod._fetch_event(db, int(eid))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            row = None
        if row is not None:
            return []
    # No event row, no hints, no manual links -> 404 for unknown event.
    raise HTTPException(status_code=404, detail="Event not found")


async def _do_link(event_id: int, data: dict, db: Any) -> dict:
    eid = _require_event_id(event_id)
    data = dict(data)
    data["event_id"] = eid
    # Validate segment presence for a clean 400.
    seg_present = any(
        data.get(k) is not None
        for k in (
            "segment", "segment_path", "segmentPath", "path", "file_path",
            "filePath", "file", "recording_path", "recordingPath",
            "recording", "clip", "video", "src", "source", "source_path",
            "recording_id", "recordingId", "segment_id", "segmentId",
        )
    )
    if not seg_present:
        raise HTTPException(status_code=400, detail="segment (segment_path/recording_id) is required")
    try:
        if db is not None:
            link = await seg_mod.link_event_to_segment_async(eid, db=db, **data)
        else:
            link = link_event_to_segment(eid, **data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    out = dict(link)
    out.setdefault("success", True)
    out.setdefault("ok", True)
    out.setdefault("linked", True)
    return out


def _register_event_routes(r: APIRouter, base: str) -> None:
    from fastapi import Depends as _Depends  # noqa: E402

    from app.api.deps import db_session as _db_session  # noqa: E402

    async def _list(
        event_id: int,
        request: Request,
        db: Any = _Depends(_db_session),
    ):
        return await _do_list(event_id, _normalize(None, request), db)

    async def _create(
        event_id: int,
        payload: Any = Body(default=None),
        request: Request = None,  # type: ignore[assignment]
        db: Any = _Depends(_db_session),
    ):
        return await _do_link(event_id, _normalize(payload, request), db)

    async def _delete_all(
        event_id: int,
        db: Any = _Depends(_db_session),
    ):
        eid = _require_event_id(event_id)
        n = delete_links_for_event(eid)
        return {"success": True, "ok": True, "deleted_count": n, "event_id": eid}

    async def _delete_one(
        event_id: int,
        link_id: int,
        db: Any = _Depends(_db_session),
    ):
        eid = _require_event_id(event_id)
        link = get_link(link_id)
        if link is None:
            raise HTTPException(status_code=404, detail="Link not found")
        try:
            if int(link.get("event_id")) != int(eid):
                raise HTTPException(status_code=404, detail="Link not found")
        except HTTPException:
            raise
        except (TypeError, ValueError):
            pass
        ok = unlink_event_segment(link_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Link not found")
        return {"success": True, "ok": True, "deleted": [int(link_id)], "id": int(link_id)}

    async def _get_one(
        event_id: int,
        link_id: int,
        db: Any = _Depends(_db_session),
    ):
        return _get_scoped(event_id, link_id)

    _list.__name__ = f"list_event_segments_{abs(hash(base)) % 10_000_000}"
    _create.__name__ = f"create_event_segment_link_{abs(hash(base)) % 10_000_000}"
    _delete_all.__name__ = f"delete_event_segments_{abs(hash(base)) % 10_000_000}"
    _delete_one.__name__ = f"delete_event_segment_{abs(hash(base)) % 10_000_000}"
    _get_one.__name__ = f"get_event_segment_{abs(hash(base)) % 10_000_000}"

    r.get(base, summary="List segments linked to event")(_list)
    r.get(base + "/", include_in_schema=False)(_list)
    r.post(base, status_code=status.HTTP_200_OK, summary="Link segment to event")(_create)
    r.post(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)(_create)
    r.delete(base, summary="Unlink all segments from event")(_delete_all)
    r.get(base + "/{link_id}", summary="Get single event-segment link")(_get_one)
    r.delete(base + "/{link_id}", summary="Unlink single segment from event")(_delete_one)


def _get_scoped(event_id: int, link_id: int) -> dict:
    eid = _require_event_id(event_id)
    nid = _coerce_int_or_none(link_id)
    if nid is None:
        raise HTTPException(status_code=400, detail="link_id is required")
    link = get_link(nid)
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found")
    try:
        if int(link.get("event_id")) != int(eid):
            raise HTTPException(status_code=404, detail="Link not found")
    except HTTPException:
        raise
    except (TypeError, ValueError):
        pass
    return link


def _scoped_get_factory():
    from fastapi import Depends as _Depends  # noqa: E402

    from app.api.deps import db_session as _db_session  # noqa: E402

    async def _get(event_id: int, link_id: int, db: Any = _Depends(_db_session)):
        return _get_scoped(event_id, link_id)

    _get.__name__ = f"get_event_segment_link_{id(_get) % 10_000_000}"
    return _get


_EVENT_BASES = (
    "/api/events/{event_id}/segments",
    "/api/events/{event_id}/segment",
    "/api/events/{event_id}/recordings",
    "/api/events/{event_id}/clips",
)
for _base in _EVENT_BASES:
    _register_event_routes(router, _base)


def _register_top_level(r: APIRouter, base: str) -> None:
    from fastapi import Depends as _Depends  # noqa: E402

    from app.api.deps import db_session as _db_session  # noqa: E402

    async def _list_all(
        request: Request,
        event_id: Optional[int] = Query(default=None),
        segment: Optional[str] = Query(default=None),
        db: Any = _Depends(_db_session),
    ):
        data = _normalize(None, request)
        eid = event_id
        if eid is None:
            try:
                for alias in ("event_id", "eventId", "event"):
                    if alias in request.query_params:
                        eid = _coerce_int_or_none(request.query_params.get(alias))
                        break
            except Exception:  # noqa: BLE001
                pass
        if eid is not None:
            return await _do_list(int(eid), data, db)
        # No event scope: filter manual links (optionally by segment), or
        # reverse-lookup events for a segment.
        seg = segment
        if seg is None:
            for alias in ("segment", "segment_path", "path", "recording_path"):
                if alias in request.query_params:
                    seg = request.query_params.get(alias)
                    break
        if seg is not None:
            if db is not None:
                try:
                    return await seg_mod.get_events_for_segment_async(seg, db=db, **data)
                except Exception:  # noqa: BLE001
                    pass
            return get_events_for_segment(seg, **data)
        return list_links(**data)

    async def _create_top(
        payload: Any = Body(default=None),
        request: Request = None,  # type: ignore[assignment]
        db: Any = _Depends(_db_session),
    ):
        data = _normalize(payload, request)
        raw_eid = data.get("event_id", data.get("eventId", data.get("event")))
        if isinstance(raw_eid, dict):
            raw_eid = raw_eid.get("id", raw_eid.get("event_id"))
        eid = _coerce_int_or_none(raw_eid)
        if eid is None:
            raise HTTPException(status_code=400, detail="event_id is required")
        return await _do_link(int(eid), data, db)

    async def _get_top(link_id: int, db: Any = _Depends(_db_session)):
        nid = _coerce_int_or_none(link_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="link_id is required")
        return _link_or_404(get_link(nid), nid)

    async def _delete_top(link_id: int, db: Any = _Depends(_db_session)):
        nid = _coerce_int_or_none(link_id)
        if nid is None:
            raise HTTPException(status_code=400, detail="link_id is required")
        ok = unlink_event_segment(nid)
        if not ok:
            raise HTTPException(status_code=404, detail="Link not found")
        return {"success": True, "ok": True, "deleted": [int(nid)], "id": int(nid)}

    async def _discover_top(
        payload: Any = Body(default=None),
        request: Request = None,  # type: ignore[assignment]
        db: Any = _Depends(_db_session),
    ):
        data = _normalize(payload, request)
        raw_eid = data.get("event_id", data.get("eventId", data.get("event")))
        if isinstance(raw_eid, dict):
            raw_eid = raw_eid.get("id", raw_eid.get("event_id"))
        eid = _coerce_int_or_none(raw_eid)
        inline: Optional[dict] = None
        for key in ("event", "source", "source_event"):
            if isinstance(data.get(key), dict):
                inline = dict(data.get(key))
                break
        if eid is None and inline is None:
            # Pure discovery from inline camera/timestamp without event id.
            if any(data.get(k) is not None for k in ("camera_id", "camera", "camera_name", "created_at", "timestamp", "time")):
                try:
                    return discover_segments_for_event(data)
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(status_code=400, detail=str(exc))
            raise HTTPException(status_code=400, detail="event_id is required")
        if eid is not None:
            return await _do_list(int(eid), data, db)
        assert inline is not None
        inline.setdefault("id", 0)
        return discover_segments_for_event(inline, **data)

    _list_all.__name__ = f"list_event_segments_top_{abs(hash(base)) % 10_000_000}"
    _create_top.__name__ = f"create_event_segment_top_{abs(hash(base)) % 10_000_000}"
    _get_top.__name__ = f"get_event_segment_top_{abs(hash(base)) % 10_000_000}"
    _delete_top.__name__ = f"delete_event_segment_top_{abs(hash(base)) % 10_000_000}"
    _discover_top.__name__ = f"discover_event_segments_top_{abs(hash(base)) % 10_000_000}"

    r.get(base, summary="List event-segment links")(_list_all)
    r.get(base + "/", include_in_schema=False)(_list_all)
    r.post(base, status_code=status.HTTP_200_OK, summary="Create event-segment link")(_create_top)
    r.post(base + "/", status_code=status.HTTP_200_OK, include_in_schema=False)(_create_top)
    r.post(base + "/discover", summary="Discover segments for event")(_discover_top)
    r.post(base + "/link", status_code=status.HTTP_200_OK, include_in_schema=False)(_create_top)
    r.get(base + "/{link_id}", summary="Get event-segment link")(_get_top)
    r.delete(base + "/{link_id}", summary="Delete event-segment link")(_delete_top)


_TOP_BASES = (
    "/api/event-segments",
    "/api/event_segments",
    "/api/eventsegments",
    "/api/segments/links",
)
_seen_top: set = set()
for _base in _TOP_BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen_top:
        continue
    _seen_top.add(_norm)
    _register_top_level(router, _base)


# Compatibility aliases.
event_segments_router = router
segments_router = router
event_segment_router = router
api_router = router

__all__ = ["router"]
