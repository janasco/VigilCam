from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.core import config
from app.core.config import settings

router = APIRouter(tags=["media"])


def _allowed_roots() -> list[Path]:
    roots = []
    try:
        roots.append(_recording_dir())
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.core.config import DATA_DIR as _dd

        roots.append(Path(_dd).resolve())
    except Exception:  # noqa: BLE001
        pass
    return [r for r in roots if r]


def _resolve_media_path(raw: str) -> Path:
    p = Path(raw or "")
    if not p.is_absolute():
        p = (_recording_dir() / p)
    try:
        resolved = p.resolve()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Bad path")
    for root in _allowed_roots():
        try:
            if resolved == root or root in resolved.parents:
                return resolved
        except Exception:  # noqa: BLE001
            continue
    raise HTTPException(status_code=403, detail="Path outside media storage")


@router.get("/api/media/file")
async def serve_media_file(path: str = Query(...)):
    fp = _resolve_media_path(path)
    if not fp.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    suffix = fp.suffix.lower()
    media = {
        ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
        ".mkv": "video/x-matroska", ".webm": "video/webm", ".ts": "video/mp2t",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    }.get(suffix, "application/octet-stream")
    return FileResponse(str(fp), media_type=media, filename=fp.name)


@router.delete("/api/media/file")
async def delete_media_file(path: str = Query(...)):
    fp = _resolve_media_path(path)
    if not fp.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        fp.unlink()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}")
    return {"deleted": str(fp), "ok": True}


def _recording_dir() -> Path:
    # Resolve dynamically to honor monkeypatched config/settings in tests.
    for candidate in (
        getattr(config, "RECORDING_DIR", None),
        getattr(settings, "RECORDING_DIR", None),
    ):
        if candidate is not None:
            try:
                p = Path(candidate)
                if not p.is_absolute():
                    p = (config.BASE_DIR / p).resolve() if hasattr(config, "BASE_DIR") else p.resolve()
                return p
            except Exception:  # noqa: BLE001
                pass
    try:
        return (config.BASE_DIR / "data" / "recordings").resolve()
    except Exception:  # noqa: BLE001
        return Path("data/recordings").resolve()


def _scan_dir(root: Path) -> List[dict]:
    out: List[dict] = []
    if not root.exists():
        return out
    if root.is_file():
        files = [root]
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            for fn in filenames:
                files.append(Path(dirpath) / fn)
    idx = 0
    for fp in sorted(files):
        try:
            st = fp.stat()
        except OSError:
            continue
        rel = None
        try:
            rel = str(fp.relative_to(root))
        except Exception:  # noqa: BLE001
            rel = fp.name
        name = fp.name
        size = int(st.st_size)
        mtime = float(st.st_mtime)
        ctime = float(st.st_ctime)
        modified = datetime.fromtimestamp(mtime).isoformat()
        created = datetime.fromtimestamp(ctime).isoformat()
        ext = fp.suffix.lower().lstrip(".")
        item = {
            "id": idx,
            "name": name,
            "filename": name,
            "basename": name,
            "file": name,
            "path": str(fp),
            "full_path": str(fp),
            "absolute_path": str(fp),
            "relative_path": rel,
            "size": size,
            "size_bytes": size,
            "bytes": size,
            "file_size": size,
            "modified": modified,
            "modified_at": modified,
            "mtime": mtime,
            "last_modified": modified,
            "created": created,
            "created_at": created,
            "ctime": ctime,
            "extension": ext,
            "ext": ext,
            "type": ext,
            "url": f"/api/media/file?path={fp}",
            "stored_path": str(fp),
            "file_path": str(fp),
            "download_url": str(fp),
        }
        out.append(item)
        idx += 1
    return out


@router.get("/api/media/recordings")
@router.get("/api/media/recordings/")
@router.get("/api/media/recording")
@router.get("/api/media/recording/")
async def list_media_recordings(
    path: Optional[str] = Query(default=None),
    root: Optional[str] = Query(default=None),
    dir: Optional[str] = Query(default=None),
    directory: Optional[str] = Query(default=None),
    folder: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(db_session),
):
    target = path or root or dir or directory or folder
    if target:
        root_path = Path(target)
        if not root_path.is_absolute():
            root_path = (_recording_dir().parent / root_path).resolve() if False else Path(target).resolve()
    else:
        root_path = _recording_dir()
    return _scan_dir(root_path)


@router.get("/api/media")
async def media_index():
    return {"status": "ok", "endpoints": ["/api/media/recordings"]}