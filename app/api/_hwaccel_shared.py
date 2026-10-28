"""Shared implementation for hardware acceleration / encoder settings endpoints."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from app.core.config import BASE_DIR, settings
from app.services import hwaccel

ENV_PATH = BASE_DIR / "config" / ".env"

# Canonical config fields we manage (primary names).
PRIMARY_FIELDS = [
    "VIDEO_HWACCEL",
    "VIDEO_HWACCEL_DEVICE",
    "AI_RUNTIME",
    "VIDEO_ENCODER",
    "VIDEO_ENCODER_DEVICE",
]

# All config field names we know about (including aliases) so hasattr/updates work.
ALL_FIELDS: List[str] = []
_seen = set()
for _n in [
    "VIDEO_HWACCEL", "VIDEO_HW_ACCEL", "VIDEO_HWACCEL_MODE", "HWACCEL", "HW_ACCEL", "ACCELERATOR",
    "VIDEO_HWACCEL_DEVICE", "VIDEO_HW_ACCEL_DEVICE", "HWACCEL_DEVICE", "HW_ACCEL_DEVICE",
    "AI_RUNTIME", "AI_DEVICE", "AI_RUNTIME_DEVICE", "RUNTIME", "AI_BACKEND", "AI_ENGINE",
    "VIDEO_ENCODER", "VIDEO_ENCODER_DEVICE",
    "HARDWARE_ENCODER", "HARDWARE_ENCODER_DEVICE",
    "RECORDING_ENCODER", "RECORDING_ENCODER_DEVICE",
    "ENCODER", "ENCODER_DEVICE",
    "HW_ENCODER", "HW_ENCODER_DEVICE",
    "VIDEO_ENCODING", "VIDEO_ENCODING_DEVICE",
    "ENCODING", "ENCODING_DEVICE",
    "VIDEO_CODEC", "VIDEO_CODEC_DEVICE",
    "RECORDING_CODEC", "RECORDING_CODEC_DEVICE",
]:
    if _n not in _seen:
        _seen.add(_n)
        ALL_FIELDS.append(_n)


def _field_names_lower() -> Dict[str, str]:
    out = {}
    for name in ALL_FIELDS:
        out[name.lower()] = name
    # common aliases
    out.update({
        "video_hwaccel": "VIDEO_HWACCEL",
        "video_hw_accel": "VIDEO_HWACCEL",
        "hwaccel": "VIDEO_HWACCEL",
        "hw_accel": "VIDEO_HWACCEL",
        "accelerator": "VIDEO_HWACCEL",
        "mode": "VIDEO_HWACCEL",
        "type": "VIDEO_HWACCEL",
        "device": "VIDEO_HWACCEL_DEVICE",
        "hwaccel_device": "VIDEO_HWACCEL_DEVICE",
        "ai_runtime": "AI_RUNTIME",
        "runtime": "AI_RUNTIME",
        "ai_device": "AI_RUNTIME",
        "backend": "AI_RUNTIME",
        "engine": "AI_RUNTIME",  # ambiguous but default to hwaccel? We'll handle context below
        "video_encoder": "VIDEO_ENCODER",
        "hardware_encoder": "VIDEO_ENCODER",
        "encoder": "VIDEO_ENCODER",
        "recording_encoder": "VIDEO_ENCODER",
        "hw_encoder": "VIDEO_ENCODER",
        "video_encoding": "VIDEO_ENCODER",
        "encoding": "VIDEO_ENCODER",
        "codec": "VIDEO_ENCODER",
        "video_codec": "VIDEO_ENCODER",
        "recording_codec": "VIDEO_ENCODER",
        "video_encoder_device": "VIDEO_ENCODER_DEVICE",
        "hardware_encoder_device": "VIDEO_ENCODER_DEVICE",
        "encoder_device": "VIDEO_ENCODER_DEVICE",
    })
    return out


def _normalize_key(key: Any) -> Optional[str]:
    if key is None:
        return None
    s = str(key).strip()
    if not s:
        return None
    low = s.lower()
    mapping = _field_names_lower()
    if low in mapping:
        return mapping[low]
    up = s.upper()
    if up in ALL_FIELDS:
        return up
    # try removing spaces
    up2 = up.replace(" ", "_").replace("-", "_")
    if up2 in ALL_FIELDS:
        return up2
    return None


def _read_env_file() -> List[str]:
    try:
        if ENV_PATH.exists():
            return ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        pass
    return []


def _coerce(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name.endswith("_DEVICE"):
        s = str(value).strip()
        if s.lower() in ("", "none", "null", "undefined"):
            return None
        return s
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip()
        if name in ("VIDEO_HWACCEL", "VIDEO_HW_ACCEL", "VIDEO_HWACCEL_MODE", "HWACCEL", "HW_ACCEL", "ACCELERATOR"):
            return s.lower() if s else "none"
        return s
    return value


def _current_settings() -> Dict[str, Any]:
    out = {}
    for name in ALL_FIELDS:
        try:
            out[name] = getattr(settings, name)
        except Exception:
            out[name] = None
    # Also expose lowercase convenience.
    out["video_hwaccel"] = out.get("VIDEO_HWACCEL")
    out["video_hwaccel_device"] = out.get("VIDEO_HWACCEL_DEVICE")
    out["ai_runtime"] = out.get("AI_RUNTIME")
    out["video_encoder"] = out.get("VIDEO_ENCODER")
    out["video_encoder_device"] = out.get("VIDEO_ENCODER_DEVICE")
    out["available_hwaccels"] = hwaccel.available_hwaccels()
    out["available_ai_runtimes"] = hwaccel.available_ai_runtimes()
    out["available_encoders"] = hwaccel.available_video_encoders()
    out["hwaccel_config"] = hwaccel.get_hwaccel_config()
    return out


def _apply_updates(updates: Dict[str, Any]) -> None:
    # Map incoming keys to canonical fields.
    mapped: Dict[str, Any] = {}
    for key, value in (updates or {}).items():
        field = _normalize_key(key)
        if field is not None:
            mapped[field] = value

    if not mapped:
        return

    # Update in-memory settings.
    for field, value in mapped.items():
        typed = _coerce(field, value)
        try:
            setattr(settings, field, typed)
        except Exception:
            pass
        # Keep aliases in sync: if primary updated, update all aliases with same value.
    # Sync alias families.
    if "VIDEO_HWACCEL" in mapped:
        v = getattr(settings, "VIDEO_HWACCEL", None)
        for a in ["VIDEO_HW_ACCEL", "VIDEO_HWACCEL_MODE", "HWACCEL", "HW_ACCEL", "ACCELERATOR"]:
            try:
                setattr(settings, a, v if v is not None else "none")
            except Exception:
                pass
    if "VIDEO_HWACCEL_DEVICE" in mapped:
        v = getattr(settings, "VIDEO_HWACCEL_DEVICE", None)
        for a in ["VIDEO_HW_ACCEL_DEVICE", "HWACCEL_DEVICE", "HW_ACCEL_DEVICE"]:
            try:
                setattr(settings, a, v)
            except Exception:
                pass
    if "AI_RUNTIME" in mapped:
        v = getattr(settings, "AI_RUNTIME", None)
        for a in ["AI_DEVICE", "AI_RUNTIME_DEVICE", "RUNTIME", "AI_BACKEND", "AI_ENGINE"]:
            try:
                setattr(settings, a, v if v is not None else "cpu")
            except Exception:
                pass
    if "VIDEO_ENCODER" in mapped:
        v = getattr(settings, "VIDEO_ENCODER", None)
        for a in ["HARDWARE_ENCODER", "ENCODER", "RECORDING_ENCODER", "HW_ENCODER", "VIDEO_ENCODING", "ENCODING", "VIDEO_CODEC", "RECORDING_CODEC"]:
            try:
                setattr(settings, a, v if v is not None else "auto")
            except Exception:
                pass
    if "VIDEO_ENCODER_DEVICE" in mapped:
        v = getattr(settings, "VIDEO_ENCODER_DEVICE", None)
        for a in ["HARDWARE_ENCODER_DEVICE", "ENCODER_DEVICE", "RECORDING_ENCODER_DEVICE", "HW_ENCODER_DEVICE", "VIDEO_ENCODING_DEVICE", "ENCODING_DEVICE", "VIDEO_CODEC_DEVICE", "RECORDING_CODEC_DEVICE"]:
            try:
                setattr(settings, a, v)
            except Exception:
                pass

    os.environ.update({k: ("" if v is None else str(v)) for k, v in mapped.items()})

    # Persist to config/.env.
    try:
        ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines = _read_env_file()
        indices: Dict[str, int] = {}
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in ALL_FIELDS:
                indices[key] = i

        effective = {name: getattr(settings, name, None) for name in ALL_FIELDS}
        for name in ALL_FIELDS:
            value = effective.get(name)
            if isinstance(value, bool):
                str_value = "true" if value else "false"
            elif value is None:
                str_value = ""
            else:
                str_value = str(value)
            assignment = f"{name}={str_value}"
            if name in indices:
                lines[indices[name]] = assignment
            else:
                lines.append(assignment)

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
    except Exception:
        pass


def create_router(prefix: str) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["hwaccel"])

    @router.get("/settings")
    async def get_settings():
        return _current_settings()

    @router.get("/config")
    async def get_config():
        return _current_settings()

    @router.get("")
    async def get_root():
        return _current_settings()

    @router.put("/settings")
    async def put_settings(payload: Optional[Any] = Body(default=None)):
        if isinstance(payload, BaseModel):
            flat = payload.model_dump()
            extra = getattr(payload, "model_extra", None) or {}
            flat.update(extra)
        elif isinstance(payload, dict):
            flat = payload
        else:
            flat = {}
        # flatten nested
        merged = dict(flat)
        for key in ("settings", "hwaccel", "hardware", "encoder", "config", "data", "payload"):
            nested = flat.get(key)
            if isinstance(nested, dict):
                merged.update(nested)
        _apply_updates(merged)
        return _current_settings()

    @router.post("/settings")
    async def post_settings(payload: Optional[Any] = Body(default=None)):
        return await put_settings(payload)

    @router.get("/encoders")
    async def list_encoders():
        return {"encoders": hwaccel.available_video_encoders(), "available": hwaccel.available_video_encoders()}

    @router.get("/hwaccels")
    async def list_hwaccels():
        return {"hwaccels": hwaccel.available_hwaccels(), "available": hwaccel.available_hwaccels()}

    @router.get("/runtimes")
    async def list_runtimes():
        return {"runtimes": hwaccel.available_ai_runtimes(), "available": hwaccel.available_ai_runtimes()}

    @router.get("/available")
    async def available():
        return {
            "encoders": hwaccel.available_video_encoders(),
            "hwaccels": hwaccel.available_hwaccels(),
            "runtimes": hwaccel.available_ai_runtimes(),
        }

    return router


# Backwards/alias helper names.
def make_router(prefix: str) -> APIRouter:
    return create_router(prefix)
