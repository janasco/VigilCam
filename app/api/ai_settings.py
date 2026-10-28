from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import BASE_DIR, settings

router = APIRouter(prefix="/api/ai", tags=["ai_settings"])

ENV_PATH = BASE_DIR / "config" / ".env"
FIELDS = [
    "AI_ENABLED",
    "AI_BASE_URL",
    "AI_API_KEY",
    "AI_VLM_MODEL",
    "AI_STT_MODEL",
    "AI_TTS_MODEL",
]


class AISettingsPayload(BaseModel):
    AI_ENABLED: Optional[bool] = None
    AI_BASE_URL: Optional[str] = None
    AI_API_KEY: Optional[str] = None
    AI_VLM_MODEL: Optional[str] = None
    AI_STT_MODEL: Optional[str] = None
    AI_TTS_MODEL: Optional[str] = None

    # Convenience aliases (lowercase) accepted on input.
    ai_enabled: Optional[bool] = Field(default=None, alias="ai_enabled")
    ai_base_url: Optional[str] = Field(default=None, alias="ai_base_url")
    ai_api_key: Optional[str] = Field(default=None, alias="ai_api_key")
    ai_vlm_model: Optional[str] = Field(default=None, alias="ai_vlm_model")
    ai_stt_model: Optional[str] = Field(default=None, alias="ai_stt_model")
    ai_tts_model: Optional[str] = Field(default=None, alias="ai_tts_model")

    model_config = {"populate_by_name": True}


class AITestPayload(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None


def _mask_api_key(key: Optional[str]) -> str:
    key = key or ""
    if not key:
        return ""
    if len(key) <= 4:
        return "*" * len(key)
    # Show only a short suffix; keep most of the key masked.
    suffix = key[-4:]
    return ("*" * 8) + suffix


def _read_env_file() -> list[str]:
    try:
        if ENV_PATH.exists():
            return ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        pass
    return []


def _is_masked(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return True
    if set(v) <= {"*"}:
        return True
    # Incoming value equals current mask or contains only mask characters + same suffix.
    current_mask = _mask_api_key(getattr(settings, "AI_API_KEY", ""))
    if v == current_mask:
        return True
    # If it looks like our mask pattern (>=4 stars), treat as masked.
    if v.startswith("********") or v.count("*") >= 4:
        return True
    return False


def _coerce(name: str, value: Any) -> Any:
    if name == "AI_ENABLED":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    return str(value)


def _current_settings() -> Dict[str, Any]:
    return {name: getattr(settings, name) for name in FIELDS}


def _apply_updates(updates: Dict[str, Any]) -> None:
    # Update in-memory settings + os.environ.
    for name, value in updates.items():
        typed = _coerce(name, value)
        setattr(settings, name, typed)
        os.environ[name] = "true" if (name == "AI_ENABLED" and typed is True) else (
            "false" if (name == "AI_ENABLED" and typed is False) else str(typed)
        )

    # Persist ALL six fields (current effective values) to config/.env.
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = _read_env_file()
    # Track indices of existing AI keys.
    indices: Dict[str, int] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in FIELDS:
            indices[key] = i

    effective = _current_settings()
    for name in FIELDS:
        value = effective[name]
        if name == "AI_ENABLED":
            str_value = "true" if bool(value) else "false"
        else:
            str_value = str(value)
        assignment = f"{name}={str_value}"
        if name in indices:
            lines[indices[name]] = assignment
        else:
            lines.append(assignment)

    # Atomic write.
    tmp = ENV_PATH.with_suffix(".env.tmp")
    content = "\n".join(lines).rstrip("\n") + "\n"
    tmp.write_text(content, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, ENV_PATH)
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass


def _to_response(values: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(values)
    out["AI_API_KEY"] = _mask_api_key(out.get("AI_API_KEY"))
    return out


@router.get("/settings")
async def get_ai_settings():
    return _to_response(_current_settings())


@router.put("/settings")
async def put_ai_settings(payload: AISettingsPayload):
    # Collect updates from both uppercase and lowercase input.
    incoming: Dict[str, Any] = {}
    for upper, lower_attr in [
        ("AI_ENABLED", "ai_enabled"),
        ("AI_BASE_URL", "ai_base_url"),
        ("AI_API_KEY", "ai_api_key"),
        ("AI_VLM_MODEL", "ai_vlm_model"),
        ("AI_STT_MODEL", "ai_stt_model"),
        ("AI_TTS_MODEL", "ai_tts_model"),
    ]:
        v = getattr(payload, upper, None)
        if v is None:
            v = getattr(payload, lower_attr, None)
        if v is not None and upper != "AI_API_KEY":
            incoming[upper] = v
        if v is not None and upper == "AI_API_KEY":
            if _is_masked(str(v)):
                # Keep existing key.
                pass
            else:
                incoming[upper] = v

    # Handle direct uppercase fields if present via payload dict (already).
    # Also capture any explicitly provided False for AI_ENABLED: pydantic optional bool
    # None default; False is a value, but `is not None` above covers False. Good.

    if not incoming:
        # Nothing to update; still return masked current.
        return _to_response(_current_settings())

    _apply_updates(incoming)
    return _to_response(_current_settings())


@router.post("/test")
async def test_ai_provider(payload: Optional[AITestPayload] = None):
    payload = payload or AITestPayload()
    base_url = (payload.base_url or settings.AI_BASE_URL or "").strip().rstrip("/")
    api_key = payload.api_key or settings.AI_API_KEY or ""
    if _is_masked(str(api_key)):
        api_key = settings.AI_API_KEY or ""

    if not base_url:
        raise HTTPException(status_code=400, detail="base_url is required")

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    candidates = [f"{base_url}/models", base_url]
    start = time.monotonic()
    last_error = None
    status_code = None
    ok = False
    detail = ""
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        for url in candidates:
            try:
                resp = await client.get(url, headers=headers)
                status_code = resp.status_code
                if resp.status_code < 400:
                    ok = True
                    detail = f"Reachable ({resp.status_code})"
                    break
                detail = f"HTTP {resp.status_code}"
            except Exception as exc:  # network/timeout
                last_error = str(exc)
                detail = f"Request failed: {exc}"
        # If none reachable, maybe try HEAD on base
        if not ok and last_error is None and status_code is None:
            try:
                resp = await client.head(base_url, headers=headers)
                status_code = resp.status_code
                ok = resp.status_code < 400
                detail = f"HTTP {resp.status_code}"
            except Exception as exc:
                detail = f"Request failed: {exc}"
    latency_ms = round((time.monotonic() - start) * 1000, 1)

    # If api_key masked handling already done.
    if not ok and last_error:
        detail = last_error

    return {
        "ok": ok,
        "base_url": base_url,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "detail": detail,
    }
