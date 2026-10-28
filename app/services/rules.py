from __future__ import annotations

import datetime
import json
import re
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Event, StandingRule


class StandingRuleCompiler:
    def __init__(self) -> None:
        self.time_pattern = re.compile(r"next\s+(\d+)\s+(hour|minute|day)s?", re.IGNORECASE)
        self.color_pattern = re.compile(r"\b(red|blue|green|black|white|silver|yellow)\b", re.IGNORECASE)
        self.object_pattern = re.compile(r"\b(car|truck|person|dog|cat|package|bicycle)\b", re.IGNORECASE)

    def compile_sentence(self, sentence: str) -> Optional[dict[str, Any]]:
        time_match = self.time_pattern.search(sentence)
        color_match = self.color_pattern.search(sentence)
        object_match = self.object_pattern.search(sentence)

        if not object_match:
            return None

        duration = int(time_match.group(1)) if time_match else 24
        unit = time_match.group(2).lower() if time_match else "hours"

        now = datetime.datetime.now()
        if "minute" in unit:
            expires_at = now + datetime.timedelta(minutes=duration)
        elif "day" in unit:
            expires_at = now + datetime.timedelta(days=duration)
        else:
            expires_at = now + datetime.timedelta(hours=duration)

        attributes = {}
        if color_match:
            attributes["color"] = color_match.group(1).lower()

        return {
            "sentence": sentence,
            "target_object": object_match.group(1).lower(),
            "attributes": attributes,
            "expires_at": expires_at,
        }


def rule_matches_event(rule: StandingRule, event: Event) -> bool:
    if not rule.is_active:
        return False
    if datetime.datetime.now() > rule.expires_at:
        return False
    if rule.target_object.lower() != (event.label or "").lower():
        return False
    attrs = json.loads(rule.attributes) if rule.attributes else {}
    payload = {}
    try:
        payload = json.loads(event.payload) if event.payload else {}
    except json.JSONDecodeError:
        payload = {}
    for key, value in attrs.items():
        if payload.get(key) != value and event.label != value:
            return False
    return True


async def create_rule(db: AsyncSession, sentence: str, camera_id: int | None = None) -> StandingRule:
    compiler = StandingRuleCompiler()
    compiled = compiler.compile_sentence(sentence)
    if compiled is None:
        raise ValueError("Could not parse a target object from sentence")
    rule = StandingRule(
        sentence=compiled["sentence"],
        target_object=compiled["target_object"],
        attributes=json.dumps(compiled["attributes"]),
        camera_id=camera_id,
        expires_at=compiled["expires_at"],
        is_active=True,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


async def list_active_rules(db: AsyncSession) -> list[StandingRule]:
    now = datetime.datetime.now()
    result = await db.execute(select(StandingRule).where(StandingRule.is_active == True))
    rules = list(result.scalars().all())
    return [rule for rule in rules if rule.expires_at > now]


async def expire_rules(db: AsyncSession) -> int:
    now = datetime.datetime.now()
    result = await db.execute(select(StandingRule).where(StandingRule.is_active == True))
    count = 0
    for rule in result.scalars().all():
        if rule.expires_at <= now:
            rule.is_active = False
            count += 1
    await db.commit()
    return count
