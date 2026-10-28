"""Full system configuration export/import REST endpoints.

Exports the whole operator-facing system configuration (cameras, groups,
layouts, standing rules, relay settings, storage areas, PTZ schedules,
linked monitor triggers, camera capabilities/permissions, settings
snapshot) as one JSON document, and restores it idempotently.

No ``app/main.py`` changes are required: the application auto-loads any
``app.api`` module exposing ``router``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services import config_io as config_io_service_module
from app.services.config_io import ConfigIOError

router = APIRouter(tags=["config_io"])

_BASES = (
    "/api/config-io",
    "/api/config_io",
    "/api/system/config",
    "/api/config",
)


def _slug(base: str) -> str:
    return base.strip("/").replace("/", "_").replace("-", "_")


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            data = dict(parsed) if isinstance(parsed, dict) else {"sections": parsed}
        except Exception:  # noqa: BLE001
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    for nested in ("options", "config", "params", "data", "body", "payload", "envelope"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    return data


def _coerce_sections(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, list):
        out = [str(v).strip() for v in value if str(v).strip()]
        return out or None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(v).strip() for v in parsed if str(v).strip()] or None
            except Exception:  # noqa: BLE001
                pass
        return [s.strip() for s in text.split(",") if s.strip()] or None
    return None


def _coerce_bool(value: Any, default: bool = True) -> bool:
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


async def _do_export(
    db: AsyncSession,
    sections: Any = None,
    include_settings: Any = True,
) -> Dict[str, Any]:
    try:
        return await config_io_service_module.export_config_db(
            db,
            sections=_coerce_sections(sections),
            include_settings=_coerce_bool(include_settings, True),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Config export failed: {exc}")


async def _do_import(
    db: AsyncSession,
    payload: Any,
    sections: Any = None,
    mode: Any = "merge",
    update_existing: Any = True,
    skip_invalid: Any = True,
) -> Dict[str, Any]:
    extracted = config_io_service_module.extract_config(payload)
    section_filter = _coerce_sections(sections)
    if section_filter is not None:
        extracted = {k: v for k, v in extracted.items() if k in section_filter}
    if not extracted or not any(isinstance(v, list) and v for v in extracted.values()):
        # Distinguish "no sections at all" (400) from "all rows invalid".
        known = [k for k in extracted if k in config_io_service_module._ALL_KNOWN_SECTIONS]
        if not known and not extracted:
            raise HTTPException(status_code=400, detail="No config sections found in import payload")
        if not any(isinstance(v, list) and v for v in extracted.values()):
            raise HTTPException(status_code=400, detail="No config entries found in import payload")
    mode_s = str(mode or "merge").strip().lower()
    if mode_s not in ("merge", "replace"):
        raise HTTPException(status_code=400, detail="mode must be 'merge' or 'replace'")
    try:
        result = await config_io_service_module.import_config_db(
            db,
            payload,
            sections=section_filter,
            mode=mode_s,
            update_existing=_coerce_bool(update_existing, True),
            skip_invalid=_coerce_bool(skip_invalid, True),
        )
    except ConfigIOError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Config import failed: {exc}")
    if result.get("count", 0) == 0 and result.get("errors"):
        raise HTTPException(
            status_code=422,
            detail={"message": "No valid config entries to import", "errors": result.get("errors", [])},
        )
    return result


def _register(r: APIRouter, base: str) -> None:
    slug = _slug(base)

    @r.get(base, operation_id=f"export_config_{slug}")
    async def export_config_root(
        db: AsyncSession = Depends(db_session),
        sections: Optional[str] = Query(default=None),
        include_settings: bool = Query(default=True),
    ):
        return await _do_export(db, sections, include_settings)

    @r.get(base + "/", operation_id=f"export_config_slash_{slug}")
    async def export_config_root_slash(
        db: AsyncSession = Depends(db_session),
        sections: Optional[str] = Query(default=None),
        include_settings: bool = Query(default=True),
    ):
        return await _do_export(db, sections, include_settings)

    @r.get(base + "/export", operation_id=f"export_config_doc_{slug}")
    async def export_config_doc(
        db: AsyncSession = Depends(db_session),
        sections: Optional[str] = Query(default=None),
        include_settings: bool = Query(default=True),
    ):
        return await _do_export(db, sections, include_settings)

    @r.get(base + "/export/", operation_id=f"export_config_doc_slash_{slug}")
    async def export_config_doc_slash(
        db: AsyncSession = Depends(db_session),
        sections: Optional[str] = Query(default=None),
        include_settings: bool = Query(default=True),
    ):
        return await _do_export(db, sections, include_settings)

    @r.get(base + "/download", operation_id=f"download_config_{slug}")
    async def download_config(
        db: AsyncSession = Depends(db_session),
        sections: Optional[str] = Query(default=None),
        include_settings: bool = Query(default=True),
    ):
        envelope = await _do_export(db, sections, include_settings)
        body = json.dumps(envelope, indent=2, default=str).encode("utf-8")
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="vigilcam-config.json"'},
        )

    @r.get(base + "/sections", operation_id=f"config_sections_{slug}")
    async def list_sections():
        return {
            "sections": list(config_io_service_module.CONFIG_SECTIONS),
            "optional_sections": list(config_io_service_module.OPTIONAL_SECTIONS),
            "version": config_io_service_module.CONFIG_IO_VERSION,
            "kind": config_io_service_module.CONFIG_KIND,
        }

    @r.post(base + "/validate", operation_id=f"validate_config_{slug}")
    async def validate_config(request: Request, payload: Any = Body(default=None)):
        body = _normalize(payload, request)
        # An envelope passed straight through keeps its shape for extract.
        candidate: Any = body if body else payload
        sections = config_io_service_module.extract_config(candidate)
        results = [
            {"section": name, "count": len(rows) if isinstance(rows, list) else 0}
            for name, rows in sections.items()
        ]
        valid = config_io_service_module.validate_config(candidate)
        known = sum(1 for n in sections if n in config_io_service_module._ALL_KNOWN_SECTIONS)
        unknown = sorted(n for n in sections if n not in config_io_service_module._ALL_KNOWN_SECTIONS)
        return {
            "valid": bool(valid),
            "count": sum(res["count"] for res in results),
            "sections": {res["section"]: res["count"] for res in results},
            "known_sections": known,
            "unknown_sections": unknown,
            "results": results,
        }

    async def _import_common(
        request: Request,
        payload: Any,
        sections: Optional[str],
        mode: str,
        update_existing: bool,
        skip_invalid: bool,
        db: AsyncSession,
    ) -> Dict[str, Any]:
        data = _normalize(payload, request)
        candidate: Any = data if data else payload
        # Query-string overrides win when the body does not specify them.
        eff_sections = data.get("sections") if isinstance(data, dict) and data.get("sections") is not None else sections
        eff_mode = data.get("mode", mode)
        eff_update = data.get("update_existing", data.get("updateExisting", update_existing))
        eff_skip = data.get("skip_invalid", data.get("skipInvalid", skip_invalid))
        return await _do_import(db, candidate, eff_sections, eff_mode, eff_update, eff_skip)

    @r.post(base + "/import", operation_id=f"import_config_{slug}")
    async def import_config(
        request: Request,
        payload: Any = Body(default=None),
        sections: Optional[str] = Query(default=None),
        mode: str = Query(default="merge"),
        update_existing: bool = Query(default=True),
        skip_invalid: bool = Query(default=True),
        db: AsyncSession = Depends(db_session),
    ):
        return await _import_common(request, payload, sections, mode, update_existing, skip_invalid, db)

    @r.post(base + "/import/", operation_id=f"import_config_slash_{slug}")
    async def import_config_slash(
        request: Request,
        payload: Any = Body(default=None),
        sections: Optional[str] = Query(default=None),
        mode: str = Query(default="merge"),
        update_existing: bool = Query(default=True),
        skip_invalid: bool = Query(default=True),
        db: AsyncSession = Depends(db_session),
    ):
        return await _import_common(request, payload, sections, mode, update_existing, skip_invalid, db)

    @r.post(base, operation_id=f"import_config_root_{slug}")
    async def import_config_root(
        request: Request,
        payload: Any = Body(default=None),
        sections: Optional[str] = Query(default=None),
        mode: str = Query(default="merge"),
        update_existing: bool = Query(default=True),
        skip_invalid: bool = Query(default=True),
        db: AsyncSession = Depends(db_session),
    ):
        return await _import_common(request, payload, sections, mode, update_existing, skip_invalid, db)

    @r.post(base + "/", operation_id=f"import_config_root_slash_{slug}")
    async def import_config_root_slash(
        request: Request,
        payload: Any = Body(default=None),
        sections: Optional[str] = Query(default=None),
        mode: str = Query(default="merge"),
        update_existing: bool = Query(default=True),
        skip_invalid: bool = Query(default=True),
        db: AsyncSession = Depends(db_session),
    ):
        return await _import_common(request, payload, sections, mode, update_existing, skip_invalid, db)

    @r.post(base + "/upload", operation_id=f"upload_config_{slug}")
    async def upload_config(
        file: UploadFile = File(...),
        sections: Optional[str] = Query(default=None),
        mode: str = Query(default="merge"),
        update_existing: bool = Query(default=True),
        skip_invalid: bool = Query(default=True),
        db: AsyncSession = Depends(db_session),
    ):
        try:
            raw = await file.read()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Could not read upload: {exc}")
        if not raw:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        try:
            payload = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Uploaded file is not valid JSON")
        return await _do_import(db, payload, sections, mode, update_existing, skip_invalid)

    @r.post(base + "/import-file", operation_id=f"import_config_file_{slug}")
    async def import_config_file(
        file: UploadFile = File(...),
        sections: Optional[str] = Query(default=None),
        mode: str = Query(default="merge"),
        update_existing: bool = Query(default=True),
        skip_invalid: bool = Query(default=True),
        db: AsyncSession = Depends(db_session),
    ):
        return await upload_config(
            file=file,
            sections=sections,
            mode=mode,
            update_existing=update_existing,
            skip_invalid=skip_invalid,
            db=db,
        )


for _base in _BASES:
    _register(router, _base)


# Compatibility aliases for varied import styles.
config_io_router = router
config_router = router
system_config_router = router
systemconfig_router = router
full_config_router = router
router_config_io = router

__all__ = [
    "router",
    "config_io_router",
    "config_router",
    "system_config_router",
    "systemconfig_router",
    "full_config_router",
    "router_config_io",
]
