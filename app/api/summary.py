from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session

router = APIRouter(prefix="/api/summary", tags=["summary"])


@router.get("/dashboard")
async def get_dashboard(db: AsyncSession = Depends(db_session)):
    from app.services.summary import get_dashboard_summary

    return await get_dashboard_summary(db)
