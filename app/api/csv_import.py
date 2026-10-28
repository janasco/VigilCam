"""CSV camera import/export REST endpoints.

No ``app/main.py`` changes are required: the application auto-loads any
``app.api`` module exposing ``router``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session

router = APIRouter(tags=["csv-import"])

_BASES = (
    "/api/csv-import",
    "/api/csv_import",
)


class CsvImportRequest(BaseModel):
    csv_text: Optional[str] = None
    csv: Optional[str] = None
    content: Optional[str] = None
    text: Optional[str] = None
    data: Optional[Any] = None
    rows: Optional[List[Any]] = None
    cameras: Optional[List[Any]] = None
    skip_invalid: Optional[bool] = True
    update_existing: Optional[bool] = False

    model_config = {"extra": "allow"}


def _to_camera_dict(camera: Any) -> Dict[str, Any]:
    if isinstance(camera, dict):
        return dict(camera)
    try:
        from app.schemas import CameraRead

        return CameraRead.model_validate(camera, from_attributes=True).model_dump(mode="json")
    except Exception:
        pass
    data: Dict[str, Any] = {}
    for key in ("id", "name", "kind", "source", "onvif_host", "ip_address", "onvif_port"):
        try:
            data[key] = getattr(camera, key, None)
        except Exception:
            data[key] = None
    return data


def _coerce_csv_text(payload: Any, request: Request | None = None) -> Any:
    from app.services import csv_import as svc

    if payload is None and request is not None:
        try:
            qp = dict(request.query_params)
        except Exception:
            qp = {}
        for key in ("csv_text", "csv", "content", "text", "data"):
            if qp.get(key):
                return qp[key]
        if qp.get("name") and qp.get("source"):
            return [qp]
        return []
    if hasattr(payload, "model_dump"):
        try:
            payload = payload.model_dump()
        except Exception:
            pass
    if isinstance(payload, dict):
        for key in ("csv_text", "csv", "content", "text", "body"):
            if isinstance(payload.get(key), str) and payload[key].strip():
                return payload[key]
        for key in ("rows", "cameras", "items", "entries", "records", "data"):
            nested = payload.get(key)
            if isinstance(nested, list) and nested:
                return nested
            if isinstance(nested, str) and nested.strip():
                return nested
        # Extra keys may themselves be the CSV text.
        extra = {k: v for k, v in payload.items()
                 if k not in {"csv_text", "csv", "content", "text", "data", "rows",
                              "cameras", "skip_invalid", "update_existing", "skipInvalid",
                              "updateExisting"}}
        if len(extra) == 1:
            only = next(iter(extra.values()))
            if isinstance(only, (str, list)):
                return only
        # Single row passed directly.
        if payload.get("name") and payload.get("source"):
            return [payload]
        if not extra and not payload.get("rows") and not payload.get("data"):
            return []
        return payload
    return payload


def _flags(payload: Any) -> tuple[bool, bool]:
    skip_invalid = True
    update_existing = False
    if isinstance(payload, dict):
        if payload.get("skip_invalid") is not None:
            skip_invalid = bool(payload.get("skip_invalid"))
        elif payload.get("skipInvalid") is not None:
            skip_invalid = bool(payload.get("skipInvalid"))
        if payload.get("update_existing") is not None:
            update_existing = bool(payload.get("update_existing"))
        elif payload.get("updateExisting") is not None:
            update_existing = bool(payload.get("updateExisting"))
    elif hasattr(payload, "model_dump"):
        try:
            d = payload.model_dump()
            return _flags(d)
        except Exception:
            pass
    else:
        try:
            d = getattr(payload, "__dict__", None)
            if isinstance(d, dict):
                return _flags(d)
        except Exception:
            pass
    return skip_invalid, update_existing


async def _do_import(db: AsyncSession, payload: Any) -> Dict[str, Any]:
    from app.services import csv_import as svc

    skip_invalid, update_existing = _flags(payload if isinstance(payload, dict) else {})
    # When payload is a pydantic model, extract flags from it.
    if not isinstance(payload, dict) and hasattr(payload, "model_dump"):
        try:
            skip_invalid, update_existing = _flags(payload.model_dump())
        except Exception:
            pass
    rows_payload = _coerce_csv_text(payload)
    result = await svc.import_csv_db(
        db, rows_payload, skip_invalid=skip_invalid, update_existing=update_existing,
    )
    created = result.get("imported", [])
    return {
        "imported": len(created),
        "count": result.get("count", len(created)),
        "total": result.get("total", len(created)),
        "cameras": created,
        "created": created,
        "errors": result.get("errors", []),
        "total_errors": result.get("total_errors", len(result.get("errors", []))),
    }


async def _do_preview(db: AsyncSession, payload: Any) -> List[Dict[str, Any]]:
    from app.services import csv_import as svc

    rows_payload = _coerce_csv_text(payload)
    return await svc.preview_csv_db(db, rows_payload)


async def _do_parse(payload: Any) -> Dict[str, Any]:
    from app.services import csv_import as svc

    rows_payload = _coerce_csv_text(payload)
    raw = svc.extract_rows(rows_payload)
    normalized = [svc.normalize_row(r) for r in raw]
    results = [
        {"index": i, "valid": svc.validate_row(r), "row": r}
        for i, r in enumerate(normalized)
    ]
    valid = sum(1 for r in results if r["valid"])
    return {
        "count": len(normalized),
        "valid": valid,
        "invalid": len(normalized) - valid,
        "rows": normalized,
        "results": results,
    }


def _register(r: APIRouter, base: str) -> None:
    @r.get(base + "/template", response_class=PlainTextResponse)
    async def get_template():
        from app.services import csv_import as svc

        return svc.build_template_csv()

    @r.get(base + "/sample", response_class=PlainTextResponse)
    async def get_sample():
        from app.services import csv_import as svc

        return svc.build_template_csv()

    @r.get(base + "/example", response_class=PlainTextResponse)
    async def get_example():
        from app.services import csv_import as svc

        return svc.build_template_csv()

    @r.get(base + "/export", response_class=PlainTextResponse)
    async def export_csv(db: AsyncSession = Depends(db_session)):
        from app.services import csv_import as svc

        return await svc.export_cameras_csv_db(db)

    @r.post(base + "/parse")
    async def parse_csv_endpoint(request: Request, payload: Any = Body(default=None)):
        body = payload
        if body is None:
            try:
                raw = await request.body()
                body = raw.decode("utf-8", "ignore") if raw else []
            except Exception:
                body = []
        return await _do_parse(body)

    @r.post(base + "/validate")
    async def validate_csv_endpoint(request: Request, payload: Any = Body(default=None)):
        body = payload
        if body is None:
            try:
                raw = await request.body()
                body = raw.decode("utf-8", "ignore") if raw else []
            except Exception:
                body = []
        parsed = await _do_parse(body)
        return {
            "count": parsed["count"],
            "valid": parsed["valid"],
            "invalid": parsed["invalid"],
            "results": parsed["results"],
        }

    @r.post(base + "/preview")
    async def preview_csv_endpoint(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        body = payload
        if body is None:
            try:
                raw = await request.body()
                body = raw.decode("utf-8", "ignore") if raw else []
            except Exception:
                body = []
        return await _do_preview(db, body)

    @r.post(base + "/import")
    async def import_csv_endpoint(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        body = payload
        if body is None:
            try:
                raw = await request.body()
                ctype = request.headers.get("content-type", "")
                if "text/csv" in ctype or "text/plain" in ctype:
                    body = raw.decode("utf-8", "ignore") if raw else ""
                else:
                    body = raw.decode("utf-8", "ignore") if raw else []
            except Exception:
                body = []
        return await _do_import(db, body)

    @r.post(base)
    async def import_csv_root(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        body = payload
        if body is None:
            try:
                raw = await request.body()
                body = raw.decode("utf-8", "ignore") if raw else []
            except Exception:
                body = []
        return await _do_import(db, body)


for _base in _BASES:
    _register(router, _base)


@router.post("/api/csv-import/import-file")
@router.post("/api/csv_import/import-file")
async def import_csv_file(
    file: UploadFile = File(...),
    update_existing: bool = Query(default=False),
    db: AsyncSession = Depends(db_session),
):
    from fastapi import HTTPException

    from app.services import csv_import as svc

    try:
        raw = await file.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read upload: {exc}")
    try:
        text = raw.decode("utf-8-sig", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Uploaded file is not valid CSV")
    if not text.strip():
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    return await _do_import(db, {"csv_text": text, "update_existing": update_existing})


# Compatibility aliases for varied import styles.
csv_import_router = router
csvImportRouter = router
camera_csv_router = router
camera_csv_import_router = router

__all__ = [
    "router",
    "csv_import_router",
    "csvImportRouter",
    "camera_csv_router",
    "camera_csv_import_router",
    "CsvImportRequest",
]
