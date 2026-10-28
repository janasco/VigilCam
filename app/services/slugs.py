from __future__ import annotations

import re
import unicodedata
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def slugify_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "item"


async def ensure_unique_slug(
    db: AsyncSession, model, base: str, exclude_id: Optional[int] = None
) -> str:
    slug = (base or "").strip() or "item"
    result = await db.execute(select(model).where(model.slug == slug))
    row = result.scalars().first()
    if row is None or (exclude_id is not None and row.id == exclude_id):
        return slug
    i = 2
    while True:
        cand = f"{slug}-{i}"
        result = await db.execute(select(model).where(model.slug == cand))
        row = result.scalars().first()
        if row is None or (exclude_id is not None and row.id == exclude_id):
            return cand
        i += 1


async def get_by_slug(db: AsyncSession, model, slug: str):
    try:
        cid = int(slug)
    except (TypeError, ValueError):
        cid = None
    if cid is not None:
        row = await db.get(model, cid)
        if row is not None:
            return row
    result = await db.execute(select(model).where(model.slug == slug))
    row = result.scalars().first()
    if row is not None:
        return row
    # fall back to name match where the model has one
    if hasattr(model, "name"):
        result = await db.execute(select(model).where(model.name == slug))
        return result.scalars().first()
    return None
