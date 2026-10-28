"""Diagnostics bundle API.

Collects sanitized config, runtime metrics, and recent logs into a zip.
Auto-registered by ``app.main`` (any module in ``app.api`` exposing
``router`` is included), so no ``app/main.py`` edit is needed.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


def _parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


async def _build(
    *,
    db: Any = None,
    include_logs: bool = True,
    include_config: bool = True,
    include_metrics: bool = True,
    operator: Optional[str] = None,
) -> tuple[bytes, dict]:
    from app.services import diagnostics as svc

    if hasattr(svc, "build_bundle_bytes_async"):
        return await svc.build_bundle_bytes_async(
            include_logs=include_logs,
            include_config=include_config,
            include_metrics=include_metrics,
            db=db,
            operator=operator,
        )
    # Fallback for stripped-down service implementations.
    blob, manifest = svc.build_bundle_bytes(
        include_logs=include_logs,
        include_config=include_config,
        include_metrics=include_metrics,
        operator=operator,
    )
    return blob, manifest


def _zip_response(blob: bytes, filename: str = "vigilcam-diagnostics.zip") -> Response:
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _db_or_none(db: Any) -> Any:
    return db


try:
    from app.api.deps import db_session as _db_session  # noqa: E402
except Exception:  # noqa: BLE001
    _db_session = None  # type: ignore[assignment]


async def _optional_db():
    if _db_session is None:
        yield None
        return
    async for session in _db_session():
        yield session


@router.get("")
@router.get("/")
async def get_diagnostics_manifest(
    request: Request,
    db: AsyncSession = Depends(_optional_db),
    include_logs: Any = Query(default=True),
    include_config: Any = Query(default=True),
    include_metrics: Any = Query(default=True),
    format: Optional[str] = Query(default=None),  # noqa: A002 - query param name
    fmt: Optional[str] = Query(default=None),
):
    want = (format or fmt or "json").lower()
    blob, manifest = await _build(
        db=_db_or_none(db),
        include_logs=_parse_bool(include_logs, True),
        include_config=_parse_bool(include_config, True),
        include_metrics=_parse_bool(include_metrics, True),
    )
    if want == "zip":
        return _zip_response(blob)
    manifest = dict(manifest)
    manifest["download"] = "/api/diagnostics/bundle"
    return JSONResponse(manifest)


@router.post("")
@router.post("/")
async def post_diagnostics_bundle(
    request: Request,
    db: AsyncSession = Depends(_optional_db),
):
    data: dict[str, Any] = {}
    try:
        body = await request.json()
        if isinstance(body, dict):
            data.update(body)
    except Exception:  # noqa: BLE001
        pass
    # Query params also accepted.
    try:
        for k, v in request.query_params.items():
            if k not in data:
                data[k] = v
    except Exception:  # noqa: BLE001
        pass

    include_logs = _parse_bool(data.get("include_logs", True), True)
    include_config = _parse_bool(data.get("include_config", True), True)
    include_metrics = _parse_bool(data.get("include_metrics", True), True)
    operator = data.get("operator") or data.get("user")
    want = str(data.get("format") or data.get("fmt") or data.get("output") or "json").lower()

    blob, manifest = await _build(
        db=_db_or_none(db),
        include_logs=include_logs,
        include_config=include_config,
        include_metrics=include_metrics,
        operator=operator,
    )
    if want == "zip":
        return _zip_response(blob)
    manifest = dict(manifest)
    manifest["download"] = "/api/diagnostics/bundle"
    return JSONResponse(manifest)


@router.get("/bundle")
@router.get("/download")
@router.get("/zip")
async def download_diagnostics_bundle(
    request: Request,
    db: AsyncSession = Depends(_optional_db),
    include_logs: Any = Query(default=True),
    include_config: Any = Query(default=True),
    include_metrics: Any = Query(default=True),
):
    blob, _manifest = await _build(
        db=_db_or_none(db),
        include_logs=_parse_bool(include_logs, True),
        include_config=_parse_bool(include_config, True),
        include_metrics=_parse_bool(include_metrics, True),
    )
    return _zip_response(blob)


async def _bundle_endpoint(request: Request, db: AsyncSession = Depends(_optional_db)):
    return await post_diagnostics_bundle(request, db)


for _path in ("/build", "/generate", "/create", "/collect", "/manifest", "/pack"):
    try:
        router.add_api_route(_path, _bundle_endpoint, methods=["GET", "POST"])
    except Exception:  # noqa: BLE001
        pass
