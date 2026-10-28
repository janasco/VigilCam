from __future__ import annotations

import base64
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from app.api.deps import db_session
from app.core.config import settings
from app.services.ai import placeholder_snapshot
from app.services.cameras import resolve_camera_alias

router = APIRouter(prefix="/api/voice", tags=["voice"])

TTS_BASE64_KEYS = (
    "tts_base64",
    "audio_base64",
    "audio",
    "speech_base64",
    "voice_base64",
    "base64",
    "base64_audio",
    "voice",
    "speech",
    "sound",
    "sound_base64",
    "mp3",
    "mp3_base64",
    "data",
    "content",
    "encoded",
    "b64",
)


class SpeakRequest(BaseModel):
    text: str
    voice: Optional[str] = None
    response_format: Optional[str] = None
    tts: Optional[bool] = True

    model_config = {"populate_by_name": True}


def _ai_check() -> None:
    if not settings.AI_ENABLED:
        raise HTTPException(status_code=503, detail="AI is disabled in configuration")


def _headers() -> Dict[str, str]:
    headers: Dict[str, str] = {}
    key = (settings.AI_API_KEY or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _base_url() -> str:
    return (settings.AI_BASE_URL or "").strip().rstrip("/")


def _timeout() -> float:
    try:
        return max(float(settings.AI_TIMEOUT_SECONDS or 30.0), 30.0)
    except (TypeError, ValueError):
        return 30.0


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"", "none", "null"}:
        return default
    if s in {"1", "true", "yes", "on", "y"}:
        return True
    if s in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _extract_text(data: Any, *keys: str) -> str:
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if value:
                if isinstance(value, str):
                    return value.strip()
                if isinstance(value, dict):
                    nested = _extract_text(value, *keys)
                    if nested:
                        return nested
        # OpenAI chat completion shape.
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict) and message.get("content"):
                return str(message["content"]).strip()
            if isinstance(choices[0], dict) and choices[0].get("text"):
                return str(choices[0]["text"]).strip()
    return ""


async def _stt_transcribe(audio_bytes: bytes, filename: str, content_type: str) -> str:
    url = f"{_base_url()}/audio/transcriptions"
    files = {
        "file": (filename or "audio.wav", audio_bytes, content_type or "application/octet-stream"),
    }
    data = {"model": settings.AI_STT_MODEL}
    async with httpx.AsyncClient(timeout=_timeout()) as client:
        resp = await client.post(url, headers=_headers(), files=files, data=data)
        resp.raise_for_status()
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return resp.text.strip()
    text = _extract_text(payload, "text", "transcript", "transcription", "content")
    return text


async def _vlm_answer(prompt: str, image_b64: str) -> str:
    if not image_b64:
        image_b64 = placeholder_snapshot(None)
    if not image_b64:
        raise HTTPException(status_code=500, detail="No image available for visual grounding")

    url = f"{_base_url()}/chat/completions"
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
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            },
        ],
        "max_tokens": 150,
    }
    async with httpx.AsyncClient(timeout=_timeout()) as client:
        resp = await client.post(url, headers=_headers(), json=body)
        resp.raise_for_status()
        data = resp.json()
    answer = _extract_text(data, "answer", "text", "content")
    if not answer:
        raise HTTPException(status_code=502, detail="Unexpected AI provider response")
    return answer


async def _tts_synthesize(text: str, voice: Optional[str] = None, response_format: Optional[str] = None) -> Optional[Dict[str, str]]:
    url = f"{_base_url()}/audio/speech"
    payload = {
        "model": settings.AI_TTS_MODEL,
        "input": text,
        "voice": voice or "alloy",
        "response_format": response_format or "mp3",
    }
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            resp = await client.post(url, headers=_headers(), json=payload)
            resp.raise_for_status()
            audio_bytes = resp.content
            if not audio_bytes:
                return None
            content_type = resp.headers.get("content-type") or "audio/mpeg"
            if "audio" not in content_type:
                # Some providers return JSON with base64 audio.
                try:
                    data = resp.json()
                    b64 = _extract_text(data, "audio", "base64", "data")
                    if not b64:
                        # Try common dict fields containing base64.
                        for key in ("audio_base64", "base64_audio", "audio", "data"):
                            if isinstance(data.get(key), str):
                                b64 = data[key]
                                break
                    if b64:
                        return {"base64": b64, "content_type": content_type}
                except Exception:  # noqa: BLE001
                    pass
                return None
            return {
                "base64": base64.b64encode(audio_bytes).decode("ascii"),
                "content_type": content_type,
            }
    except Exception:  # noqa: BLE001
        return None


def _with_tts_fields(result: Dict[str, Any], tts: Optional[Dict[str, str]]) -> Dict[str, Any]:
    if not tts or not tts.get("base64"):
        return result
    b64 = tts["base64"]
    for key in TTS_BASE64_KEYS:
        result[key] = b64
    result["content_type"] = tts.get("content_type", "audio/mpeg")
    result["mime_type"] = result["content_type"]
    return result


@router.post("/transcribe")
async def transcribe_voice(
    audio: UploadFile = File(...),
    camera_alias: Optional[str] = Form(None),
    camera: Optional[str] = Form(None),
    alias: Optional[str] = Form(None),
    camera_name: Optional[str] = Form(None),
    camera_id: Optional[int] = Form(None),
    camera_alias_q: Optional[str] = Query(None, alias="camera_alias"),
    camera_q: Optional[str] = Query(None, alias="camera"),
    alias_q: Optional[str] = Query(None, alias="alias"),
    camera_name_q: Optional[str] = Query(None, alias="camera_name"),
    camera_id_q: Optional[int] = Query(None, alias="camera_id"),
    tts: Optional[str] = Form(None),
    tts_q: Optional[str] = Query(None, alias="tts"),
    generate_tts: Optional[str] = Query(None, alias="generate_tts"),
    db: AsyncSession = Depends(db_session),
):
    _ai_check()

    alias_value = camera_alias or camera or alias or camera_name or camera_alias_q or camera_q or alias_q or camera_name_q
    resolved_camera_id = camera_id if camera_id is not None else camera_id_q

    camera_obj = None
    try:
        camera_obj = await resolve_camera_alias(db, alias_value if alias_value is not None else resolved_camera_id)
    except Exception:  # noqa: BLE001
        camera_obj = None
    if camera_obj is not None:
        resolved_camera_id = camera_obj.id

    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="audio file is required")

    try:
        transcript = await _stt_transcribe(audio_bytes, audio.filename or "audio.wav", audio.content_type or "application/octet-stream")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"STT provider request failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"STT provider request failed: {exc}")

    prompt = transcript.strip() if transcript.strip() else "Describe what you see on this camera."
    try:
        image_b64 = placeholder_snapshot(resolved_camera_id)
        answer = await _vlm_answer(prompt, image_b64)
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"VLM provider request failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"VLM provider request failed: {exc}")

    result: Dict[str, Any] = {
        "text": answer,
        "answer": answer,
        "transcript": transcript,
        "transcription": transcript,
        "prompt": prompt,
        "camera_id": resolved_camera_id,
        "model": settings.AI_VLM_MODEL,
        "stt_model": settings.AI_STT_MODEL,
    }
    if camera_obj is not None:
        result["camera_alias"] = camera_obj.name
        result["camera_name"] = camera_obj.name

    should_tts = _as_bool(
        tts if tts is not None else (tts_q if tts_q is not None else generate_tts),
        default=True,
    )
    if should_tts and (settings.AI_TTS_MODEL or "").strip():
        voice_name = None
        tts_result = await _tts_synthesize(answer)
        result = _with_tts_fields(result, tts_result)

    return result


@router.post("/speak")
async def speak_voice(payload: SpeakRequest):
    _ai_check()
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    if not _as_bool(payload.tts, default=True):
        # Still allow skipping but endpoint's purpose is TTS.
        pass

    if not (settings.AI_TTS_MODEL or "").strip():
        raise HTTPException(status_code=503, detail="TTS model is not configured")

    tts_result = await _tts_synthesize(text, payload.voice, payload.response_format)
    if not tts_result or not tts_result.get("base64"):
        raise HTTPException(status_code=502, detail="TTS provider request failed")

    result: Dict[str, Any] = {
        "text": text,
        "audio_base64": tts_result["base64"],
        "content_type": tts_result.get("content_type", "audio/mpeg"),
        "mime_type": tts_result.get("content_type", "audio/mpeg"),
        "model": settings.AI_TTS_MODEL,
    }
    for key in TTS_BASE64_KEYS:
        result[key] = tts_result["base64"]
    return result


@router.post("/speak/form")
async def speak_voice_form(
    text: str = Form(...),
    voice: Optional[str] = Form(None),
    response_format: Optional[str] = Form(None),
):
    return await speak_voice(SpeakRequest(text=text, voice=voice, response_format=response_format))
