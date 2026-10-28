from __future__ import annotations

import csv
import io
import json
import base64
from pathlib import Path
import datetime as _dt
from datetime import datetime, timezone
from typing import Any, List, Optional, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.core.config import settings
from app.models.entities import Camera, Event
from app.schemas import EventRead
from app.services.cameras import recent_events
try:
    from app.services import snapshots as snapshot_service
except Exception:  # noqa: BLE001
    snapshot_service = None
from app.services.embeddings import generate_embedding, cosine_similarity

router = APIRouter(prefix="/api/events", tags=["events"])

START_ALIASES = {
    "start", "start_time", "start_date", "from", "from_", "from_time", "begin",
    "after", "since", "min", "min_time", "lower", "lower_bound", "range_start",
    "after_time", "from_date",
}
END_ALIASES = {
    "end", "end_time", "end_date", "to", "to_time", "until", "before", "max",
    "max_time", "upper", "upper_bound", "finish", "range_end", "before_time",
    "to_date",
}
CAMERA_ALIASES = {
    "camera_id", "camera_ids", "cameras", "camera", "camera_id_list", "cam",
    "camera_name", "camera_names",
}
LABEL_ALIASES = {
    "label", "labels", "detection_label", "detection_labels", "object",
    "objects", "object_label", "class", "classes", "kind", "kinds", "type",
    "types",
}
TAG_ALIASES = {
    "tag", "tags", "event_tag", "event_tags", "tag_name", "tag_names",
    "tags[]",
}

CONFIDENCE_MIN_ALIASES = {
    "min_confidence", "confidence_min", "min_score", "score_min", "min_conf",
    "confidence_minimum", "min_confidence_score", "confidence_threshold",
    "min_probability", "probability_min",
}
CONFIDENCE_MAX_ALIASES = {
    "max_confidence", "confidence_max", "max_score", "score_max", "max_conf",
    "confidence_maximum", "max_probability", "probability_max",
}
ORDER_ALIASES = {
    "order", "sort", "sort_order", "direction", "ordering", "sort_by",
}
LIMIT_ALIASES = {
    "limit", "page_size", "per_page", "n", "count", "size", "max_results",
}
SKIP_ALIASES = {
    "skip", "offset", "start_index", "start_offset",
}


class SimilarSearchRequest(BaseModel):
    image_base64: Optional[str] = None
    image: Optional[str] = None
    limit: int = 10


class SummarizeResponse(BaseModel):
    event_id: int
    summary: str
    ai_used: bool
    model: Optional[str] = None


class EventBulkDeleteRequest(BaseModel):
    event_ids: Optional[List[Union[int, str]]] = None
    ids: Optional[List[Union[int, str]]] = None
    id: Optional[List[Union[int, str]]] = None
    id_list: Optional[List[Union[int, str]]] = None


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, bool):
        return None
    elif isinstance(value, (int, float)):
        ts = float(value)
        # Heuristic: milliseconds vs seconds.
        if abs(ts) > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except (OSError, ValueError, OverflowError):
            return None
    else:
        s = str(value).strip()
        if s == "" or s.lower() in {"none", "null", "undefined", "all"}:
            return None
        # numeric string
        # numeric string (epoch seconds or ms)
        try:
            ts = float(s)
            # guard: only treat as epoch if original looks numeric
            if s.replace(".", "", 1).replace("e", "", 1).replace("E", "", 1).replace("-", "", 1).replace("+", "", 1).isdigit():
                if abs(ts) > 1e12:
                    ts = ts / 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            pass
        s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(s2)
        except ValueError:
            # date-only
            try:
                dt = datetime.fromisoformat(s + "T00:00:00")
            except ValueError:
                return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _first_param(request: Request, names: set, declared: Optional[Any] = None) -> Optional[Any]:
    if declared is not None:
        if isinstance(declared, list):
            if len(declared) == 1:
                return declared[0]
            return declared
        return declared
    qp = request.query_params
    for name in names:
        if name in qp:
            return qp.get(name)
    # case-insensitive
    for key in qp:
        if key.lower() in {n.lower() for n in names}:
            return qp.get(key)
    return None


def _all_values(request: Request, names: set, *declared_lists) -> List[str]:
    vals = []
    for lst in declared_lists:
        if lst is None:
            continue
        if isinstance(lst, list):
            vals.extend(lst)
        else:
            vals.append(lst)
    qp = request.query_params
    for key in qp:
        if key in names or key.lower() in {n.lower() for n in names}:
            vals.extend(qp.getlist(key))
    # csv from single strings
    out = []
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if not s or s.lower() in {"all", "none", "null"}:
            continue
        for part in s.split(","):
            part = part.strip()
            if part and part.lower() not in {"all", "none", "null"}:
                out.append(part)
    # de-dupe preserving
    seen = set()
    result = []
    for v in out:
        if v not in seen:
            seen.add(v)
            result.append(v)
    return result


def _camera_ids_from_values(values: List[str]) -> List[int]:
    ids = []
    for v in values:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    return ids



def _normalize_output_tags(value):
    if value is None:
        return []
    if isinstance(value, dict):
        for key in ("tags", "tag", "values", "items", "labels", "names"):
            if key in value and value.get(key) is not None:
                return _normalize_output_tags(value.get(key))
        return []
    if isinstance(value, str):
        try:
            import json
            parsed = json.loads(value)
            if isinstance(parsed, list):
                value = parsed
            else:
                return [parsed] if parsed is not None else []
        except Exception:
            value = [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, (tuple, set)):
        value = list(value)
    if not isinstance(value, list):
        if value is None:
            return []
        return [str(value)]
    out = []
    seen = set()
    for item in value:
        s = str(item).strip() if item is not None else ""
        if not s or s.lower() in {"none", "null"}:
            continue
        if s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
    return out


def _event_to_dict(event: Event) -> dict:
    _tags = None
    try:
        _tags = _normalize_output_tags(event.tags)
    except Exception:
        _tags = event.tags
    return {
        "id": event.id,
        "camera_id": event.camera_id,
        "kind": event.kind,
        "label": event.label,
        "confidence": event.confidence,
        "payload": event.payload,
        "tags": _tags,
        "tag": _tags,
        "snapshot_path": getattr(event, "snapshot_path", None),
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def _extract_image_base64(payload: Optional[str]) -> Optional[str]:
    if not payload:
        return None
    s = payload.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                for key in ("image_base64", "image", "snapshot"):
                    if isinstance(obj.get(key), str):
                        return obj[key]
        except Exception:
            pass
    # Could be raw base64.
    if len(s) > 64 and "=" in s or len(s) > 128:
        try:
            base64.b64decode(s, validate=False)
            return s
        except Exception:
            return None
    return None


def _parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s.lower() in {"none", "null", "undefined", "all"}:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _parse_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    s = str(value).strip()
    if not s or s.lower() in {"none", "null", "undefined", "all"}:
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


async def _fetch_all_events(db: AsyncSession) -> List[Event]:
    result = await db.execute(select(Event).order_by(Event.id.desc()))
    return list(result.scalars().all())


async def _filtered_events(
    request: Request,
    db: AsyncSession,
    *,
    apply_paging: bool = True,
    declared: Optional[dict] = None,
) -> List[Event]:
    declared = declared or {}
    events = await _fetch_all_events(db)

    # Time range
    start_value = None
    for _v in [
        declared.get("start"), declared.get("start_time"), declared.get("start_date"),
        declared.get("from_"), declared.get("from_time"), declared.get("begin"),
        declared.get("after"), declared.get("since"), declared.get("min_time"),
        declared.get("lower_bound"),
    ]:
        if _v is not None:
            start_value = _v
            break
    start_value = _first_param(request, START_ALIASES, start_value)
    if start_value is None:
        for key in request.query_params:
            if key in START_ALIASES or key.lower() in {n.lower() for n in START_ALIASES}:
                start_value = request.query_params.get(key)
                break

    end_value = None
    for _v in [
        declared.get("end"), declared.get("end_time"), declared.get("end_date"),
        declared.get("to"), declared.get("to_time"), declared.get("until"),
        declared.get("before"), declared.get("max_time"), declared.get("upper_bound"),
    ]:
        if _v is not None:
            end_value = _v
            break
    end_value = _first_param(request, END_ALIASES, end_value)
    if end_value is None:
        for key in request.query_params:
            if key in END_ALIASES or key.lower() in {n.lower() for n in END_ALIASES}:
                end_value = request.query_params.get(key)
                break

    start_dt = _parse_dt(start_value)
    end_dt = _parse_dt(end_value)

    # Cameras
    camera_values = _all_values(
        request,
        CAMERA_ALIASES,
        declared.get("camera_id"),
        declared.get("camera_ids"),
        declared.get("cameras"),
        declared.get("camera"),
    )
    camera_id_set = set(_camera_ids_from_values(camera_values))

    # Labels
    label_values = _all_values(request, LABEL_ALIASES)
    label_set = {v.lower() for v in label_values}

    # Tags (event tags)
    tag_values = _all_values(request, TAG_ALIASES)
    tag_set = {v.lower() for v in tag_values}
    # support declared explicit
    for key in ("tag", "tags", "event_tag", "event_tags"):
        if declared.get(key) is not None:
            tag_values = tag_values + ([declared.get(key)] if not isinstance(declared.get(key), list) else declared.get(key))
    tag_set = {str(v).strip().lower() for v in tag_values if str(v).strip() and str(v).strip().lower() not in {"all", "none", "null"}}

    # Confidence
    min_conf_value = _first_param(request, CONFIDENCE_MIN_ALIASES)
    if min_conf_value is None and "min_confidence" in (declared or {}):
        min_conf_value = declared.get("min_confidence")
    min_conf = _parse_float(min_conf_value)

    max_conf_value = _first_param(request, CONFIDENCE_MAX_ALIASES)
    max_conf = _parse_float(max_conf_value)

    # Filter
    filtered = []
    for ev in events:
        ca = ev.created_at
        if ca is not None and (start_dt is not None or end_dt is not None):
            compare = ca
            if compare.tzinfo is not None:
                compare = compare.astimezone(timezone.utc).replace(tzinfo=None)
            if start_dt is not None and compare < start_dt:
                continue
            if end_dt is not None and compare > end_dt:
                continue
        # if timestamps missing but dt filter present, skip? keep
        if camera_id_set and (ev.camera_id not in camera_id_set):
            continue
        if label_set and (ev.label is None or str(ev.label).lower() not in label_set):
            continue
        if tag_set:
            ev_tags = [str(t).lower() for t in _normalize_output_tags(getattr(ev, "tags", None))]
            if not any(t in ev_tags for t in tag_set):
                continue
        if min_conf is not None:
            if ev.confidence is None or ev.confidence < min_conf:
                continue
        if max_conf is not None:
            if ev.confidence is None or ev.confidence > max_conf:
                continue
        filtered.append(ev)

    # Ordering
    order_raw = _first_param(request, ORDER_ALIASES)
    if order_raw is None:
        order_raw = declared.get("order")
    order_norm = (str(order_raw) if order_raw is not None else "").lower().strip()
    if order_norm in {"asc", "ascending", "chronological", "oldest"}:
        filtered.sort(key=lambda e: (e.created_at or datetime.min, e.id or 0))
    elif order_norm in {"desc", "descending", "newest", "latest"}:
        filtered.sort(key=lambda e: (e.created_at or datetime.min, e.id or 0), reverse=True)
    else:
        # default newest first to mirror prior behavior
        filtered.sort(key=lambda e: (e.created_at or datetime.min, e.id or 0), reverse=True)

    if not apply_paging:
        return filtered

    # Paging
    limit_raw = _first_param(request, LIMIT_ALIASES)
    if limit_raw is None:
        limit_raw = declared.get("limit")
    skip_raw = _first_param(request, SKIP_ALIASES)
    if skip_raw is None:
        skip_raw = declared.get("skip")
        if skip_raw is None:
            skip_raw = declared.get("offset")

    try:
        limit = int(limit_raw) if limit_raw is not None else 1000
    except (TypeError, ValueError):
        s = str(limit_raw).strip().lower()
        if s in {"all", "none", "unlimited"}:
            limit = None
        else:
            limit = 1000
    if limit is not None and limit < 0:
        limit = 0

    try:
        skip = int(skip_raw) if skip_raw is not None else 0
    except (TypeError, ValueError):
        skip = 0
    if skip < 0:
        skip = 0

    # declared offset fallback
    if (declared.get("offset") is not None and skip_raw is None):
        try:
            skip = int(declared.get("offset"))
        except (TypeError, ValueError):
            pass

    if skip:
        filtered = filtered[skip:]
    if limit is not None:
        filtered = filtered[:limit]

    return filtered


def _bulk_ids_from_request(request: Request, body: Optional[EventBulkDeleteRequest]) -> List[int]:
    ids = []
    if body is not None:
        for attr in ("event_ids", "ids", "id", "id_list"):
            val = getattr(body, attr, None)
            if val:
                ids.extend(val)
    qp = request.query_params
    for key in ("ids", "id", "event_ids", "event_id"):
        if key in qp:
            ids.extend(qp.getlist(key))
            # csv
    # parse
    parsed = []
    seen = set()
    for v in ids:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv not in seen:
            seen.add(iv)
            parsed.append(iv)
    # csv split for qp stored single strings already? _all? simpler:
    for key in ("ids", "id", "event_ids", "event_id"):
        if key in qp:
            for part in str(qp.get(key)).split(","):
                part = part.strip()
                if part:
                    try:
                        iv = int(part)
                        if iv not in seen:
                            seen.add(iv)
                            parsed.append(iv)
                    except ValueError:
                        pass
    return parsed


@router.get("/tags")
@router.get("/tag")
async def api_events_list_tags(db: AsyncSession = Depends(db_session)):
    events = await _fetch_all_events(db)
    tags = set()
    for ev in events:
        for t in _normalize_output_tags(getattr(ev, "tags", None)):
            tags.add(t)
    return sorted(tags)


@router.get("", response_model=List[EventRead])
@router.get("/", response_model=List[EventRead], include_in_schema=False)
async def api_events_list(request: Request, db: AsyncSession = Depends(db_session)):
    return await _filtered_events(request, db, apply_paging=True)


@router.get("/timeline", response_model=List[EventRead])
async def api_events_timeline(
    request: Request,
    # Time range (multiple aliases accepted via request.query_params fallback too)
    start: Optional[str] = Query(None, alias="start"),
    start_time: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    from_: Optional[str] = Query(None, alias="from"),
    from_time: Optional[str] = Query(None),
    begin: Optional[str] = Query(None),
    after: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    min_time: Optional[str] = Query(None),
    lower_bound: Optional[str] = Query(None),
    end: Optional[str] = Query(None, alias="end"),
    end_time: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    to: Optional[str] = Query(None, alias="to"),
    to_time: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    before: Optional[str] = Query(None),
    max_time: Optional[str] = Query(None),
    upper_bound: Optional[str] = Query(None),
    # Camera filters
    camera_id: Optional[Union[int, List[int]]] = Query(None),
    camera_ids: Optional[str] = Query(None),
    cameras: Optional[str] = Query(None),
    camera: Optional[str] = Query(None),
    camera_name: Optional[str] = Query(None),
    # Label filters
    label: Optional[str] = Query(None),
    labels: Optional[str] = Query(None),
    # Confidence
    min_confidence: Optional[Union[float, str]] = Query(None),
    # Paging
    limit: int = Query(1000, ge=1, le=100000),
    skip: int = Query(0, ge=0),
    offset: int = Query(0, ge=0),
    order: Optional[str] = Query(None),
    db: AsyncSession = Depends(db_session),
):
    declared = {
        "start": start, "start_time": start_time, "start_date": start_date,
        "from_": from_, "from_time": from_time, "begin": begin, "after": after,
        "since": since, "min_time": min_time, "lower_bound": lower_bound,
        "end": end, "end_time": end_time, "end_date": end_date, "to": to,
        "to_time": to_time, "until": until, "before": before, "max_time": max_time,
        "upper_bound": upper_bound, "camera_id": camera_id, "camera_ids": camera_ids,
        "cameras": cameras, "camera": camera, "camera_name": camera_name,
        "label": label, "labels": labels, "min_confidence": min_confidence,
        "limit": limit, "skip": skip, "offset": offset, "order": order,
    }
    # Limit default coercion: if request didn't send limit, _filtered_events will default via declared.
    return await _filtered_events(request, db, apply_paging=True, declared=declared)


@router.get("/export")
async def api_events_export(
    request: Request,
    format: Optional[str] = Query("json", alias="format"),
    fmt: Optional[str] = Query(None, alias="fmt"),
    db: AsyncSession = Depends(db_session),
):
    fmt_val = format or fmt or request.query_params.get("format") or request.query_params.get("fmt") or "json"
    fmt_norm = str(fmt_val).strip().lower()
    if fmt_norm in {"csv", "text/csv"}:
        fmt_norm = "csv"
    elif fmt_norm in {"json", "application/json"}:
        fmt_norm = "json"
    else:
        # allow other aliases
        if "csv" in fmt_norm:
            fmt_norm = "csv"
        else:
            fmt_norm = "json"

    events = await _filtered_events(request, db, apply_paging=False)

    if fmt_norm == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "camera_id", "kind", "label", "confidence", "created_at", "payload"])
        for ev in events:
            writer.writerow([
                ev.id,
                ev.camera_id,
                ev.kind,
                ev.label,
                ev.confidence,
                ev.created_at.isoformat() if ev.created_at else "",
                ev.payload or "",
            ])
        content = buf.getvalue()
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=events.csv"},
        )

    out = [_event_to_dict(ev) for ev in events]
    return JSONResponse(content=out)


@router.post("/{event_id}/tags")
@router.post("/{event_id}/tag")
async def api_event_add_tags(
    event_id: int,
    append: bool = Query(False),
    payload: Any = Body(default=None),
    tag: Optional[Any] = Query(None, alias="tag"),
    tags: Optional[Any] = Query(None, alias="tags"),
    db: AsyncSession = Depends(db_session),
):
    ev = await db.get(Event, event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Event not found")

    incoming_source = payload
    mode = None
    if isinstance(payload, dict):
        for key in ("tags", "tag", "values", "items", "labels", "names"):
            if key in payload and payload.get(key) is not None:
                incoming_source = payload.get(key)
                break
        mode = payload.get("mode") or payload.get("action") or payload.get("operation")
    if append or (isinstance(mode, str) and mode.lower() in {"append", "add", "merge"}):
        # merge with existing
        incoming = _normalize_output_tags(incoming_source)
        if tags is not None:
            incoming = _normalize_output_tags([*incoming, *_coerce_query_tags(tags)])
        if tag is not None:
            incoming = _normalize_output_tags([*incoming, *_coerce_query_tags(tag)])
        current = _normalize_output_tags(getattr(ev, "tags", None))
        merged = current + [t for t in incoming if t.lower() not in {x.lower() for x in current}]
        ev.tags = merged
    else:
        # If payload/query only provides mode and no tags, do nothing beyond coerce.
        incoming = _normalize_output_tags(incoming_source)
        if tags is not None:
            # combine explicit query tags
            qtags = _normalize_output_tags(_coerce_query_tags(tags))
            incoming = _normalize_output_tags([*incoming, *qtags])
        if tag is not None:
            qtags = _normalize_output_tags(_coerce_query_tags(tag))
            incoming = _normalize_output_tags([*incoming, *qtags])
        # If both payload and query absent but endpoint called, clear? keep existing? 
        # If no tags specified at all, retain existing tags (no-op) rather than wipe.
        if incoming == [] and payload is None and tag is None and tags is None:
            incoming = _normalize_output_tags(getattr(ev, "tags", None))
        ev.tags = incoming
    await db.commit()
    await db.refresh(ev)
    return _event_to_dict(ev)


def _coerce_query_tags(value):
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        try:
            import json
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [p.strip() for p in value.split(",") if p.strip()]
    return [value]


@router.delete("/{event_id}/tags")
@router.delete("/{event_id}/tag")
async def api_event_delete_tags(event_id: int, db: AsyncSession = Depends(db_session)):
    ev = await db.get(Event, event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Event not found")
    ev.tags = []
    await db.commit()
    await db.refresh(ev)
    return _event_to_dict(ev)


# Single delete
@router.delete("/{event_id}")
async def api_event_delete_one(event_id: int, db: AsyncSession = Depends(db_session)):
    ev = await db.get(Event, event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Event not found")
    await db.delete(ev)
    await db.commit()
    return {"success": True, "deleted": [event_id], "not_found": []}


# Bulk delete (no ids => delete current filtered set)
@router.delete("", include_in_schema=False)
@router.delete("/", include_in_schema=False)
@router.post("/bulk-delete", include_in_schema=False)
@router.post("/bulk_delete", include_in_schema=False)
@router.post("/delete", include_in_schema=False)
@router.post("/archive", include_in_schema=False)
async def api_events_bulk_delete(
    request: Request,
    body: Optional[EventBulkDeleteRequest] = Body(None),
    db: AsyncSession = Depends(db_session),
):
    ids = _bulk_ids_from_request(request, body)

    if not ids:
        # delete filtered set
        filtered = await _filtered_events(request, db, apply_paging=False)
        ids = [e.id for e in filtered]

    deleted = []
    not_found = []
    for eid in ids:
        ev = await db.get(Event, eid)
        if ev is None:
            not_found.append(eid)
            continue
        await db.delete(ev)
        deleted.append(eid)
    await db.commit()

    return JSONResponse(
        content={
            "success": True,
            "deleted_events": deleted,
            "deleted": deleted,
            "not_found_events": not_found,
            "not_found": not_found,
        }
    )


@router.post("/{event_id}/summarize", response_model=SummarizeResponse)
async def api_events_summarize(event_id: int, db: AsyncSession = Depends(db_session)):
    event = await db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    camera_name = None
    if event.camera_id is not None:
        cam = await db.get(Camera, event.camera_id)
        if cam:
            camera_name = cam.name

    created = event.created_at.isoformat() if event.created_at else None

    ai_used = False
    summary = None
    model = settings.AI_VLM_MODEL if settings.AI_ENABLED else None

    if settings.AI_ENABLED and settings.AI_BASE_URL:
        try:
            import httpx

            prompt = (
                f"Summarize this security camera event in one short sentence. "
                f"Event id {event.id}, type {event.kind}, label {event.label}, "
                f"confidence {event.confidence}, camera {camera_name or event.camera_id}, "
                f"time {created}, payload {event.payload}."
            )
            headers = {}
            if settings.AI_API_KEY:
                headers["Authorization"] = f"Bearer {settings.AI_API_KEY}"
            body = {
                "model": settings.AI_VLM_MODEL,
                "messages": [
                    {"role": "system", "content": "You are VigilCam. Summarize concisely."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 120,
            }
            timeout = min(float(settings.AI_TIMEOUT_SECONDS or 30.0), 15.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{settings.AI_BASE_URL.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
            answer = data["choices"][0]["message"]["content"]
            if isinstance(answer, str) and answer.strip():
                summary = answer.strip()
                ai_used = True
        except Exception:
            summary = None

    if not summary:
        conf_text = f" (confidence {event.confidence:.2f})" if event.confidence is not None else ""
        summary = (
            f"Mock summary: {event.label} event #{event.id} "
            f"{'on camera ' + camera_name if camera_name else ''}".strip()
            + f" at {created}" + conf_text + "."
        ).replace("  ", " ")

    return SummarizeResponse(
        event_id=event.id,
        summary=summary,
        ai_used=ai_used,
        model=model if ai_used else None,
    )


@router.post("/{event_id}/snapshot")
async def api_event_upload_snapshot(
    event_id: int,
    payload: Any = Body(default=None),
    db: AsyncSession = Depends(db_session),
):
    ev = await db.get(Event, event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Event not found")

    image_base64 = None
    if isinstance(payload, dict):
        for key in ("image_base64", "image", "snapshot", "snapshot_base64",
                    "data", "base64", "content", "file"):
            if payload.get(key):
                image_base64 = payload.get(key)
                break
    elif isinstance(payload, str):
        image_base64 = payload

    if not image_base64:
        raise HTTPException(status_code=400, detail="image_base64 is required")

    saver = None
    try:
        from app.services import snapshots as _snapshots

        saver = getattr(_snapshots, "save_snapshot", None)
    except Exception:  # noqa: BLE001
        saver = None
    if saver is None:
        raise HTTPException(status_code=500, detail="snapshot service unavailable")

    try:
        saved_path = saver(image_base64, event_id=event_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid image: {exc}")

    ev.snapshot_path = str(saved_path)

    # Attach snapshot path to event payload.
    try:
        raw = ev.payload
        obj = None
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    obj = parsed
                else:
                    obj = {"payload": raw}
            except Exception:  # noqa: BLE001
                obj = {"payload": raw}
        else:
            obj = {}
        obj["snapshot_path"] = str(saved_path)
        obj["snapshot_url"] = f"/api/events/{event_id}/snapshot"
        ev.payload = json.dumps(obj)
    except Exception:  # noqa: BLE001
        pass

    await db.commit()
    await db.refresh(ev)
    return {
        "success": True,
        "status": "ok",
        "snapshot_path": str(saved_path),
        "path": str(saved_path),
        "file_path": str(saved_path),
        "url": f"/api/events/{event_id}/snapshot",
        "event": _event_to_dict(ev),
    }


@router.get("/{event_id}/snapshot")
async def api_event_get_snapshot(
    event_id: int,
    db: AsyncSession = Depends(db_session),
):
    ev = await db.get(Event, event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Event not found")

    resolved = None
    try:
        from app.services import snapshots as _snapshots

        resolved = _snapshots.resolve_snapshot_path(getattr(ev, "snapshot_path", None))
    except Exception:  # noqa: BLE001
        p = getattr(ev, "snapshot_path", None)
        if p:
            pp = Path(str(p))
            if pp.is_file():
                resolved = pp

    if resolved is not None:
        resolved = Path(resolved)
        if resolved.is_file():
            suffix = resolved.suffix.lower()
            media = "image/png" if suffix == ".png" else "image/jpeg"
            return FileResponse(str(resolved), media_type=media)

    # Placeholder (1x1 PNG).
    import base64 as _b64

    placeholder = _b64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
        "x8AAwMCAO+X2Z0AAAAASUVORK5CYII="
    )
    return Response(content=placeholder, media_type="image/png")


@router.post("/search/similar")
async def api_events_search_similar(
    payload: SimilarSearchRequest = Body(...),
    limit: Optional[int] = Query(None),
    db: AsyncSession = Depends(db_session),
):
    image_base64 = payload.image_base64 or payload.image
    if not image_base64:
        raise HTTPException(status_code=400, detail="image_base64 is required")

    try:
        query_vector = generate_embedding(image_base64=image_base64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image: {exc}")

    effective_limit = limit if limit is not None else (payload.limit or 10)
    if effective_limit < 1:
        effective_limit = 1
    if effective_limit > 500:
        effective_limit = 500

    result = await db.execute(select(Event).order_by(Event.id.desc()).limit(1000))
    events = list(result.scalars().all())

    # Score events that carry an image payload; fall back to recent order.
    scored = []
    any_vector = False
    for ev in events:
        img = _extract_image_base64(ev.payload)
        vec = None
        if img is not None:
            try:
                vec = generate_embedding(image_base64=img)
                any_vector = True
            except Exception:
                vec = None
        if vec is not None:
            score = cosine_similarity(query_vector, vec)
            scored.append((score, ev))
        else:
            scored.append((None, ev))

    if any_vector:
        scored.sort(key=lambda x: (x[0] is not None, x[0] if x[0] is not None else -1.0), reverse=True)
        top = scored[:effective_limit]
    else:
        top = [(1.0, ev) for _, ev in scored[:effective_limit]]

    out = []
    for score, ev in top:
        item = _event_to_dict(ev)
        item["similarity"] = float(score) if score is not None else 1.0
        out.append(item)
    return out
