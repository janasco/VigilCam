from __future__ import annotations

import base64
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import db_session
from app.core import config
from app.core.config import settings

router = APIRouter(tags=["floorplans"])


def _floorplan_dir() -> Path:
    for candidate in (
        getattr(settings, "DATA_DIR", None),
        getattr(config, "DATA_DIR", None),
    ):
        if candidate is not None:
            try:
                base = Path(candidate)
                if not base.is_absolute():
                    base = (config.BASE_DIR / base).resolve() if hasattr(config, "BASE_DIR") else base.resolve()
                d = base / "floorplans"
                d.mkdir(parents=True, exist_ok=True)
                return d
            except Exception:  # noqa: BLE001
                pass
    try:
        d = (config.BASE_DIR / "data" / "floorplans").resolve()
    except Exception:  # noqa: BLE001
        d = Path("data/floorplans").resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_filename(name: Optional[str]) -> str:
    if not name:
        name = "floorplan"
    name = os.path.basename(str(name))
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name:
        name = "floorplan"
    return name


def _save_bytes(data: bytes, filename: Optional[str]) -> dict:
    d = _floorplan_dir()
    safe = _safe_filename(filename)
    stem = Path(safe).stem
    suffix = Path(safe).suffix
    target = d / safe
    if target.exists():
        safe = f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"
        target = d / safe
    target.write_bytes(data)
    return {
        "id": target.stem,
        "name": safe,
        "filename": safe,
        "path": str(target),
        "full_path": str(target),
        "absolute_path": str(target),
        "stored_path": str(target),
        "file_path": str(target),
        "location": str(target),
        "size": len(data),
        "size_bytes": len(data),
        "bytes": len(data),
        "created_at": datetime.utcnow().isoformat(),
        "url": f"/data/floorplans/{safe}",
        "download_url": f"/data/floorplans/{safe}",
    }


def _list_floorplans() -> List[dict]:
    d = _floorplan_dir()
    out: List[dict] = []
    if not d.exists():
        return out
    for fp in sorted(d.iterdir()):
        if not fp.is_file():
            continue
        try:
            st = fp.stat()
        except OSError:
            continue
        out.append({
            "id": fp.stem,
            "name": fp.name,
            "filename": fp.name,
            "path": str(fp),
            "full_path": str(fp),
            "size": int(st.st_size),
            "size_bytes": int(st.st_size),
            "created_at": datetime.fromtimestamp(st.st_ctime).isoformat(),
            "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
            "url": f"/data/floorplans/{fp.name}",
        })
    return out


async def _process_upload(request: Request) -> dict:
    content_type = (request.headers.get("content-type") or "").lower()

    # multipart
    if "multipart/form-data" in content_type:
        form = await request.form()
        file_field = None
        for key in ("file", "image", "floorplan", "upload", "data", "content"):
            if key in form:
                val = form[key]
                if hasattr(val, "read"):
                    file_field = val
                    break
        if file_field is None:
            for val in form.values():
                if hasattr(val, "read"):
                    file_field = val
                    break
        if file_field is not None:
            data = await file_field.read()
            if not data:
                raise HTTPException(status_code=400, detail="empty file")
            meta = _save_bytes(data, getattr(file_field, "filename", None))
            return {"status": "uploaded", "ok": True, "success": True, **meta, "floorplan": meta, "file": meta}

    # raw body
    body = await request.body()
    filename = "floorplan.png"
    # query params
    qp = dict(request.query_params)
    if qp.get("filename"):
        filename = qp["filename"]

    # try JSON
    parsed = None
    if body:
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            parsed = None

    if isinstance(parsed, dict):
        # filename
        for k in ("filename", "file_name", "name"):
            if parsed.get(k):
                filename = parsed.get(k)
                break
        # base64
        b64_candidate = None
        for k in ("file", "data", "content", "base64", "b64", "image", "image_base64",
                  "file_base64", "payload", "bytes", "encoded", "file_data", "file_content",
                  "content_base64", "upload", "body"):
            if parsed.get(k) is not None:
                b64_candidate = parsed.get(k)
                break
        raw_bytes = None
        if isinstance(b64_candidate, str):
            s = b64_candidate.strip()
            if "," in s and s.lower().startswith("data:"):
                s = s.split(",", 1)[1]
            try:
                raw_bytes = base64.b64decode(s, validate=False)
            except Exception:  # noqa: BLE001
                try:
                    raw_bytes = base64.b64decode(s + "==", validate=False)
                except Exception:  # noqa: BLE001
                    raw_bytes = None
        elif isinstance(b64_candidate, (bytes, bytearray)):
            raw_bytes = bytes(b64_candidate)

        # file path
        if raw_bytes is None:
            for k in ("file_path", "path", "source", "source_path", "input_path", "local_path", "src", "from_path"):
                if parsed.get(k):
                    try:
                        p = Path(str(parsed.get(k)))
                        if p.exists() and p.is_file():
                            raw_bytes = p.read_bytes()
                            filename = filename or p.name
                            break
                    except Exception:  # noqa: BLE001
                        pass

        if raw_bytes:
            meta = _save_bytes(raw_bytes, filename)
            return {"status": "uploaded", "ok": True, "success": True, **meta, "floorplan": meta, "file": meta}

    # If content-type is image/*, treat raw body as image
    if body and content_type.startswith("image/"):
        meta = _save_bytes(body, filename)
        return {"status": "uploaded", "ok": True, "success": True, **meta, "floorplan": meta, "file": meta}

    # Try reading as form even if content-type not multipart (fallback)
    try:
        form = await request.form()
        file_field = None
        for val in form.values():
            if hasattr(val, "read"):
                file_field = val
                break
        if file_field is not None:
            data = await file_field.read()
            if data:
                meta = _save_bytes(data, getattr(file_field, "filename", None))
                return {"status": "uploaded", "ok": True, "success": True, **meta, "floorplan": meta, "file": meta}
    except Exception:  # noqa: BLE001
        pass

    raise HTTPException(status_code=400, detail="image file is required")


# Upload endpoints
_UPLOAD_PATHS = [
    "/api/floorplans",
    "/api/floorplan",
    "/api/floor-plans",
    "/api/floorplans/upload",
    "/api/floorplan/upload",
    "/api/floor-plans/upload",
    "/api/floorplans/upload-image",
    "/api/floor-plans/upload-image",
    "/api/layouts/floorplan",
    "/api/layouts/floorplans",
    "/api/media/floorplans",
    "/api/media/floorplan",
]


def _register_upload(path):
    async def handler(request: Request):
        return await _process_upload(request)

    router.post(path)(handler)


for p in _UPLOAD_PATHS:
    _register_upload(p)

# List endpoints
_LIST_PATHS = [
    "/api/floorplans",
    "/api/floorplan",
    "/api/floor-plans",
    "/api/layouts/floorplan",
    "/api/layouts/floorplans",
    "/api/media/floorplans",
    "/api/media/floorplan",
]


def _register_list(path):
    async def handler():
        return _list_floorplans()

    router.get(path)(handler)


for p in _LIST_PATHS:
    _register_list(p)