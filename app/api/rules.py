from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.schemas import StandingRuleCreate, StandingRuleRead
from app.services.rules import create_rule, expire_rules, list_active_rules

router = APIRouter(prefix="/api/rules", tags=["rules"])


@router.post("/standing", response_model=StandingRuleRead, status_code=status.HTTP_201_CREATED)
async def create_standing_rule_endpoint(
    payload: StandingRuleCreate,
    db: AsyncSession = Depends(db_session),
):
    camera_id = payload.camera_id
    try:
        if camera_id is not None:
            camera_id = int(camera_id)
    except (TypeError, ValueError):
        pass
    try:
        rule = await create_rule(db, payload.sentence, camera_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    try:
        from app.services.event_bus import event_bus

        await event_bus.publish(
            "standing_rule_created",
            {
                "rule_id": rule.id,
                "sentence": rule.sentence,
                "target_object": rule.target_object,
                "camera_id": rule.camera_id,
                "expires_at": rule.expires_at,
                "is_active": rule.is_active,
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return rule


@router.get("/standing", response_model=List[StandingRuleRead])
async def list_standing_rules_endpoint(db: AsyncSession = Depends(db_session)):
    return await list_active_rules(db)


@router.post("/standing/expire")
async def expire_standing_rules_endpoint(db: AsyncSession = Depends(db_session)):
    count = await expire_rules(db)
    return {"expired": count, "count": count, "expired_count": count}


@router.delete("/standing/{rule_id}")
async def delete_standing_rule_endpoint(rule_id: int, db: AsyncSession = Depends(db_session)):
    from app.models.entities import StandingRule

    rule = await db.get(StandingRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    await db.delete(rule)
    await db.commit()
    return {"deleted": rule_id, "ok": True}
