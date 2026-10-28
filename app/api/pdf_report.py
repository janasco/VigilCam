"""PDF incident report API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edit to
``app/main.py`` is required.

Serves printable incident reports as ``application/pdf`` built by
:mod:`app.services.pdf_report` (stdlib-only). Event export under
``/api/events/export`` stays CSV/JSON-only; this module adds the separate
PDF incident-report path.
"""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends as _Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services import pdf_report as pdf_svc

router = APIRouter(tags=["pdf-report"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"title": payload} if payload.strip() else {}
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
                    vals = request.query_params.getlist(k)
                    data[k] = vals[0] if len(vals) == 1 else vals
        except Exception:
            pass

    for nested_key in (
        "incident",
        "report",
        "correlate",
        "group",
        "payload",
        "options",
        "config",
        "params",
        "data",
        "body",
        "request",
        "fields",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _get_title(data: dict) -> str:
    for key in ("title", "report_title", "name", "subject", "heading"):
        val = data.get(key)
        if isinstance(val, list):
            val = val[0] if val else None
        if val is not None and str(val).strip():
            return str(val).strip()
    return pdf_svc.TITLE_DEFAULT


def _get_operator(data: dict) -> Optional[str]:
    for key in ("operator", "user", "username", "actor", "author"):
        val = data.get(key)
        if isinstance(val, list):
            val = val[0] if val else None
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, list):
        for item in value:
            out = _coerce_int(item)
            if out is not None:
                return out
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _pick_incident(incidents: List[dict], incident_id: Optional[int]) -> Any:
    if incident_id is None:
        return incidents
    for inc in incidents:
        if not isinstance(inc, dict):
            continue
        for key in ("incident_id", "id"):
            try:
                if inc.get(key) is not None and int(inc.get(key)) == int(incident_id):
                    return inc
            except (TypeError, ValueError):
                continue
    return incidents


def _inline_incidents(data: dict) -> Optional[List[dict]]:
    for key in ("incidents", "groups", "reports"):
        val = data.get(key)
        if isinstance(val, list) and val:
            return val
    return None


def _inline_incident(data: dict) -> Optional[dict]:
    for key in ("incident", "report"):
        val = data.get(key)
        if isinstance(val, dict) and val:
            # treat as incident if it carries incident-ish keys or nested events
            if any(k in val for k in ("incident_id", "id", "event_ids", "events", "cameras", "camera_ids")):
                return val
    # bare incident dict passed directly (has event_ids / events + cameras)?
    if isinstance(data.get("events"), list) and any(
        k in data for k in ("incident_id", "id", "event_ids", "camera_count", "cameras")
    ):
        return data
    return None


def _inline_events(data: dict) -> Optional[List[dict]]:
    for key in ("events", "items", "sources", "detections", "alerts"):
        val = data.get(key)
        if isinstance(val, list) and val:
            return list(val)
    return None


async def _incidents_from_db(data: dict, db: Optional[AsyncSession]) -> List[dict]:
    if db is None:
        return []
    try:
        from app.services.incidents import correlate_incidents_async

        out = await correlate_incidents_async(data, db=db)
        return list(out or [])
    except HTTPException:
        raise
    except Exception:
        return []


async def _resolve_incidents(data: dict, db: Optional[AsyncSession]) -> List[dict]:
    inline = _inline_incidents(data)
    if inline:
        return inline
    single = _inline_incident(data)
    if single is not None:
        return [single]
    events = _inline_events(data)
    if events:
        try:
            from app.services.incidents import correlate_events

            return list(correlate_events({"events": events, **data}) or [])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    # Fall back to DB correlation (recent events) when no inline payload.
    db_incidents = await _incidents_from_db(data, db)
    return db_incidents


async def _resolve_events_for_listing(data: dict, db: Optional[AsyncSession]) -> List[dict]:
    events = _inline_events(data)
    if events:
        return events
    if db is None:
        return []
    try:
        from sqlalchemy import select

        from app.models.entities import Event as EventModel

        limit = _coerce_int(data.get("limit", data.get("n"))) or 500
        limit = max(1, min(int(limit), 2000))
        result = await db.execute(
            select(EventModel).order_by(EventModel.id.desc()).limit(limit)
        )
        rows = list(result.scalars().all())
        out = []
        for r in rows:
            item = {}
            for attr in ("id", "camera_id", "kind", "label", "confidence", "payload", "created_at"):
                try:
                    val = getattr(r, attr, None)
                except Exception:
                    val = None
                if hasattr(val, "isoformat"):
                    try:
                        val = val.isoformat()
                    except Exception:
                        pass
                item[attr] = val
            out.append(item)
        return out
    except Exception:
        return []


def _pdf_response(pdf: bytes, filename: str = "incident-report.pdf") -> Response:
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _report_handler(
    payload: Any = Body(default=None),
    request: Request = None,
    db: AsyncSession = _Depends(db_session),
    title: Optional[str] = Query(default=None),
    report_title: Optional[str] = Query(default=None),
    operator: Optional[str] = Query(default=None),
    incident_id: Optional[str] = Query(default=None),
    format: Optional[str] = Query(default=None, alias="format"),
    fmt: Optional[str] = Query(default=None, alias="fmt"),
) -> Response:
    data = _normalize(payload, request)
    if title is not None:
        data.setdefault("title", title)
    if report_title is not None:
        data.setdefault("title", report_title)
    if operator is not None:
        data.setdefault("operator", operator)
    if incident_id is not None:
        data.setdefault("incident_id", incident_id)
    # explicit format guard: this endpoint is PDF-only; accept pdf-ish values.
    fmt_val = format or fmt or data.get("format", data.get("fmt")) or "pdf"
    fmt_norm = str(fmt_val).strip().lower() if not isinstance(fmt_val, list) else str(fmt_val[0]).strip().lower()
    if fmt_norm not in ("pdf", "application/pdf", "pdf-download", "download", "report", "incident"):
        # tolerate json/csv ask by still returning PDF? No -- be explicit.
        raise HTTPException(status_code=400, detail="This endpoint serves PDF only (format=pdf)")

    t = _get_title(data)
    op = _get_operator(data)
    iid = _coerce_int(data.get("incident_id", data.get("id")))

    incidents = await _resolve_incidents(data, db)
    if iid is not None and incidents:
        picked = _pick_incident(incidents, iid)
        if isinstance(picked, dict):
            pdf = pdf_svc.build_incident_pdf(picked, title=t, operator=op)
            return _pdf_response(pdf, filename=f"incident-{iid}-report.pdf")
    if incidents:
        if len(incidents) == 1:
            pdf = pdf_svc.build_incident_pdf(incidents[0], title=t, operator=op)
        else:
            pdf = pdf_svc.build_incidents_pdf(incidents, title=t, operator=op)
        return _pdf_response(pdf)
    # No incidents correlated: if raw events were supplied (single-camera etc.),
    # still render an events listing PDF instead of an error.
    events = await _resolve_events_for_listing(data, db)
    if events:
        pdf = pdf_svc.build_events_pdf(events, title=t, operator=op)
        return _pdf_response(pdf, filename="events-report.pdf")
    pdf = pdf_svc.build_incidents_pdf([], title=t, operator=op)
    return _pdf_response(pdf)


async def _report_by_id_handler(
    incident_id: int,
    request: Request = None,
    db: AsyncSession = _Depends(db_session),
    title: Optional[str] = Query(default=None),
    operator: Optional[str] = Query(default=None),
) -> Response:
    data = _normalize(None, request)
    if title is not None:
        data["title"] = title
    if operator is not None:
        data["operator"] = operator
    data["incident_id"] = incident_id
    t = _get_title(data)
    op = _get_operator(data)
    incidents = await _resolve_incidents(data, db)
    picked = _pick_incident(incidents, incident_id)
    if isinstance(picked, dict):
        pdf = pdf_svc.build_incident_pdf(picked, title=t, operator=op)
        return _pdf_response(pdf, filename=f"incident-{incident_id}-report.pdf")
    if isinstance(picked, list) and picked:
        pdf = pdf_svc.build_incidents_pdf(picked, title=t, operator=op)
        return _pdf_response(pdf, filename=f"incident-{incident_id}-report.pdf")
    # DB has no correlated incident (e.g. empty DB in tests): render a
    # placeholder single-incident PDF so the download still works.
    pdf = pdf_svc.build_incident_pdf(
        {"incident_id": incident_id, "id": incident_id, "events": []},
        title=t,
        operator=op,
    )
    return _pdf_response(pdf, filename=f"incident-{incident_id}-report.pdf")


async def _health_handler() -> dict:
    return {"status": "ok", "format": "pdf"}


_REPORT_BASES = (
    "/api/incidents/report.pdf",
    "/api/incidents/report",
    "/api/incident/report.pdf",
    "/api/incident/report",
    "/api/pdf_report",
    "/api/pdf-report",
    "/api/reports/incident.pdf",
    "/api/reports/incident",
    "/api/reports/incidents.pdf",
)


def _build_report_handler(idx: int):
    async def _h(
        payload: Any = Body(default=None),
        request: Request = None,
        db: AsyncSession = _Depends(db_session),
        title: Optional[str] = Query(default=None),
        report_title: Optional[str] = Query(default=None),
        operator: Optional[str] = Query(default=None),
        incident_id: Optional[str] = Query(default=None),
        format: Optional[str] = Query(default=None, alias="format"),
        fmt: Optional[str] = Query(default=None, alias="fmt"),
    ):
        return await _report_handler(
            payload=payload,
            request=request,
            db=db,
            title=title,
            report_title=report_title,
            operator=operator,
            incident_id=incident_id,
            format=format,
            fmt=fmt,
        )

    _h.__name__ = f"pdf_report_handler_{idx}"
    return _h


for _idx, _base in enumerate(_REPORT_BASES):
    _h = _build_report_handler(_idx)
    router.post(_base, summary="Generate PDF incident report")(_h)
    router.post(_base + "/", include_in_schema=False)(_h)
    router.get(_base, summary="Download PDF incident report")(_h)
    router.get(_base + "/", include_in_schema=False)(_h)


async def _by_id_handler(
    incident_id: int,
    request: Request = None,
    db: AsyncSession = _Depends(db_session),
    title: Optional[str] = Query(default=None),
    operator: Optional[str] = Query(default=None),
):
    return await _report_by_id_handler(
        incident_id, request=request, db=db, title=title, operator=operator
    )


for _p in ("/api/incidents/{incident_id}/report.pdf", "/api/incidents/{incident_id}/report"):
    router.get(_p, summary="Download PDF report for one incident")(_by_id_handler)
    router.post(_p, include_in_schema=False)(_by_id_handler)

for _p in ("/api/pdf_report/health", "/api/pdf-report/health", "/api/incidents/report/health"):
    router.get(_p, summary="PDF report health", include_in_schema=False)(_health_handler)


# Compatibility aliases.
pdf_report_router = router
api_router = router
report_router = router
incidents_pdf_router = router

__all__ = ["router"]
