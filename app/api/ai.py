from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import settings
from app.services.ai import placeholder_snapshot

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


class VLMQueryRequest(BaseModel):
    prompt: str
    camera_id: Optional[int] = None
    image_base64: Optional[str] = None


class VLMQueryResponse(BaseModel):
    prompt: str
    answer: str
    camera_id: Optional[int]
    model: str


@router.get("/status")
async def ai_status():
    return {
        "enabled": settings.AI_ENABLED,
        "base_url": settings.AI_BASE_URL,
        "model": settings.AI_VLM_MODEL,
    }


@router.post("/query", response_model=VLMQueryResponse)
async def vlm_query(payload: VLMQueryRequest):
    if not settings.AI_ENABLED:
        raise HTTPException(status_code=503, detail="AI is disabled in configuration")

    import httpx

    image_b64 = payload.image_base64 or placeholder_snapshot(payload.camera_id)
    if not image_b64:
        raise HTTPException(status_code=500, detail="No image available for visual grounding")

    headers = {"Authorization": f"Bearer {settings.AI_API_KEY}"}
    body = {
        "model": settings.AI_VLM_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are VigilCam AI. Answer concisely in 1-2 spoken sentences using only the provided image.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": payload.prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            },
        ],
        "max_tokens": 150,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.AI_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{settings.AI_BASE_URL.rstrip('/')}/chat/completions",
                headers=headers,
                json=body,
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI provider request failed: {exc}")

    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise HTTPException(status_code=502, detail="Unexpected AI provider response")

    return VLMQueryResponse(
        prompt=payload.prompt,
        answer=answer,
        camera_id=payload.camera_id,
        model=settings.AI_VLM_MODEL,
    )
