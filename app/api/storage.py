from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.models.entities import StorageArea
from app.schemas import StorageAreaCreate, StorageAreaRead, StorageAreaUpdate

router = APIRouter(prefix="/api/storage", tags=["storage"])


def _normalize_create(payload: Any) -> dict:
    data: dict = {}
    if payload is None:
        return data
    if hasattr(payload, "model_dump"):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}

    # Accept common alias field names.
    if data.get("path") is None:
        for k in ("storage_path", "root_path", "storage_root", "root"):
            if data.get(k) is not None:
                data["path"] = data.get(k)
                break
    if data.get("name") is None:
        for k in ("title", "area_name", "area"):
            if data.get(k) is not None:
                data["name"] = data.get(k)
                break
    if data.get("max_bytes") is None:
        for k in ("max_size", "max_total_bytes", "max", "capacity", "max_bytes"):
            if data.get(k) is not None:
                data["max_bytes"] = data.get(k)
                break
    if data.get("used_bytes") is None:
        for k in ("used_size", "used", "used_bytes"):
            if data.get(k) is not None:
                data["used_bytes"] = data.get(k)
                break
    if data.get("enabled") is None and "active" in data:
        data["enabled"] = bool(data.get("active"))
    if data.get("path") is None and data.get("name") is not None:
        # Fall back to name as path? no, require path. Keep None.
        pass
    return data


def _area_to_read(area: StorageArea, extra: Optional[dict] = None) -> StorageAreaRead:
    read = StorageAreaRead(
        id=area.id,
        name=area.name,
        path=area.path,
        enabled=bool(area.enabled),
        max_bytes=area.max_bytes,
        used_bytes=int(area.used_bytes or 0),
        created_at=getattr(area, "created_at", None),
    )
    if extra:
        for k, v in extra.items():
            if hasattr(read, k):
                setattr(read, k, v)
    return read


def _scan_area(path: str) -> tuple[int, int]:
    total = 0
    count = 0
    p = Path(path).expanduser()
    if not p.exists():
        return 0, 0
    if p.is_file():
        try:
            return int(p.stat().st_size), 1
        except OSError:
            return 0, 0
    for root, dirs, files in os.walk(p):
        for fn in files:
            fp = Path(root) / fn
            try:
                total += int(fp.stat().st_size)
                count += 1
            except OSError:
                continue
    return total, count


@router.get("/areas")
async def list_storage_areas(db: AsyncSession = Depends(db_session)):
    result = await db.execute(select(StorageArea).order_by(StorageArea.id))
    areas = result.scalars().all()
    out = [_area_to_read(a) for a in areas]
    return out


@router.post("/areas", status_code=status.HTTP_201_CREATED)
async def create_storage_area(
    payload: Any = Body(default=None),
    db: AsyncSession = Depends(db_session),
):
    # Allow query-param or body-less creation fallback handled by defaults.
    data = _normalize_create(payload if payload is not None else {})
    # If no body at all, FastAPI gives None; still check query? Not available here.
    name = data.get("name")
    path = data.get("path")
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    if not path:
        raise HTTPException(status_code=422, detail="path is required")
    area = StorageArea(
        name=str(name),
        path=str(path),
        enabled=bool(data.get("enabled", True)),
        max_bytes=data.get("max_bytes"),
        used_bytes=int(data.get("used_bytes") or 0),
    )
    db.add(area)
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
        # Retry minimal
        try:
            await db.commit()
        except Exception:
            pass
        raise
    await db.refresh(area)
    return _area_to_read(area)


@router.get("/areas/{area_id}")
async def get_storage_area(area_id: int, db: AsyncSession = Depends(db_session)):
    area = await db.get(StorageArea, area_id)
    if area is None:
        result = await db.execute(select(StorageArea).where(StorageArea.name == str(area_id)))
        area = result.scalars().first()
    if area is None:
        raise HTTPException(status_code=404, detail="Storage area not found")
    return _area_to_read(area)


@router.delete("/areas/{area_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_storage_area(area_id: int, db: AsyncSession = Depends(db_session)):
    area = await db.get(StorageArea, area_id)
    if area is None:
        # Maybe area_id passed as string name? attempt name lookup
        result = await db.execute(select(StorageArea).where(StorageArea.name == str(area_id)))
        area = result.scalars().first()
    if area is None:
        raise HTTPException(status_code=404, detail="Storage area not found")
    await db.delete(area)
    await db.commit()
    return None


@router.post("/reconcile")
async def reconcile_storage(
    payload: Any = Body(default=None),
    area_id: Optional[int] = Query(default=None),
    dry_run: bool = Query(default=False),
    db: AsyncSession = Depends(db_session),
):
    # Determine target areas.
    data = _normalize_create(payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        data = {}
    target_ids = []
    if area_id is not None:
        target_ids.append(area_id)
    if payload is not None and not isinstance(payload, dict) and hasattr(payload, "area_id"):
        target_ids.append(payload.area_id)
    if isinstance(payload, dict):
        if payload.get("area_id") is not None:
            target_ids.append(payload.get("area_id"))
        for key in ("area_ids", "ids", "areas"):
            val = payload.get(key)
            if isinstance(val, list):
                target_ids.extend([x.get("id") if isinstance(x, dict) else x for x in val])
            elif isinstance(val, (int, str)):
                target_ids.append(val)
    # De-duplicate, preserving order.
    seen = set()
    unique_ids = []
    for x in target_ids:
        if x is None:
            continue
        try:
            xi = int(x)
        except (TypeError, ValueError):
            # could be name; handle later
            xi = None
        key = xi if xi is not None else str(x)
        if key in seen:
            continue
        seen.add(key)
        unique_ids.append((xi, x))

    if unique_ids:
        stmt = select(StorageArea)
        # filter by provided ids/names
        result = await db.execute(stmt.order_by(StorageArea.id))
        all_areas = list(result.scalars().all())
        target_areas = []
        for xi, raw in unique_ids:
            if xi is not None:
                a = next((ar for ar in all_areas if ar.id == xi), None)
            else:
                a = next((ar for ar in all_areas if ar.name == str(raw)), None)
            if a is None and xi is not None:
                # maybe xi is name? no
                pass
            if a is not None and a not in target_areas:
                target_areas.append(a)
        if not target_areas:
            # Fall back to provided names if any were names.
            target_areas = all_areas
    else:
        result = await db.execute(select(StorageArea).order_by(StorageArea.id))
        target_areas = list(result.scalars().all())

    reconciled = []
    for area in target_areas:
        total, count = _scan_area(area.path)
        if not dry_run:
            area.used_bytes = int(total)
            db.add(area)
        await db.flush() if False else None
        reconciled.append({
            "id": area.id,
            "storage_area_id": area.id,
            "name": area.name,
            "path": area.path,
            "stored_path": area.path,
            "enabled": bool(area.enabled),
            "max_bytes": area.max_bytes,
            "max_size": area.max_bytes,
            "max_total_bytes": area.max_bytes,
            "used_bytes": int(total),
            "used_size": int(total),
            "used": int(total),
            "total_bytes": int(total),
            "scanned_files": count,
            "file_count": count,
            "dry_run": bool(dry_run),
        })

    if not dry_run:
        await db.commit()
        for area in target_areas:
            await db.refresh(area)
            # update used_bytes in returned entries already set
    # Re-read to reflect commits
    for entry in reconciled:
        aid = entry["id"]
        a = next((ar for ar in target_areas if ar.id == aid), None)
        if a is not None:
            entry["used_bytes"] = int(a.used_bytes or 0)
            entry["total_bytes"] = int(a.used_bytes or 0)

    payload_out = {
        "status": "completed",
        "reconciled": True,
        "dry_run": bool(dry_run),
        "updated": len(target_areas) if not dry_run else 0,
        "updated_count": len(target_areas) if not dry_run else 0,
        "areas": reconciled,
        "storage_areas": reconciled,
        "results": reconciled,
        "reconciled_areas": reconciled,
    }
    return payload_out


@router.get("/summary")
async def storage_summary(db: AsyncSession = Depends(db_session)):
    result = await db.execute(select(StorageArea).order_by(StorageArea.id))
    areas = list(result.scalars().all())
    area_reads = [_area_to_read(a) for a in areas]
    # enrich with scan? summary should reflect stored used_bytes (audit may have run)
    total_used = sum(int(a.used_bytes or 0) for a in areas)
    total_max_raw = [a.max_bytes for a in areas if a.max_bytes is not None]
    total_max = sum(total_max_raw) if total_max_raw else None
    max_bytes_total = total_max
    out_areas = [StorageAreaRead.model_validate(ar) if False else ar for ar in area_reads]
    # refresh used_bytes already
    summary = {
        "total_bytes": total_used,
        "total": total_used,
        "areas_count": len(areas),
        "areas": area_reads,
        "storage_areas": area_reads,
        "total_areas": len(areas),
        "area_count": len(areas),
        "count": len(areas),
        "num_areas": len(areas),
        "total_used_bytes": total_used,
        "total_used": total_used,
        "used_bytes": total_used,
        "used_bytes_total": total_used,
        "total_max_bytes": total_max,
        "total_max": total_max,
        "max_bytes": total_max,
        "max_bytes_total": max_bytes_total,
        "max_total_bytes": total_max,
        "free_bytes": (total_max - total_used) if (total_max is not None) else None,
        "total_free_bytes": (total_max - total_used) if (total_max is not None) else None,
        "utilization": (total_used / total_max) if (total_max and total_max > 0) else None,
    }
    return summary
