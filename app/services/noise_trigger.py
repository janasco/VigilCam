"""Noise-level trigger for VigilCam.

Companion to :mod:`app.services.audio_detector` (which is mock-only: it
always reports ``detected=True`` with fixed confidence and never gates on
a threshold). This module provides a real threshold + cooldown trigger that
turns microphone/cam noise levels into events:

* ``level`` is normalized to a ``0-100`` scale (explicit ``level`` keys are
  used as-is; ``volume``/``rms`` keys in ``0-1`` are scaled by 100; raw
  audio payloads are scored with the same RMS formula as ``audio_detector``).
* ``evaluate`` is a pure threshold check (no state, no cooldown).
* ``check`` / ``process_level`` applies threshold + per-source cooldown and
  creates an event only when the cooldown has elapsed.

State is intentionally in-memory (thread-safe) so the feature works without
DB migrations and without ``app/main.py`` edits (the API router is
auto-registered by the ``pkgutil`` auto-loader in ``app/main.py``).

Standard library only.
"""

from __future__ import annotations

import base64
import math
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DEFAULT_THRESHOLD = 70.0
DEFAULT_COOLDOWN_SECONDS = 30.0
MIN_LEVEL = 0.0
MAX_LEVEL = 100.0

_CONFIG: Dict[str, Any] = {
    "threshold": float(DEFAULT_THRESHOLD),
    "cooldown_seconds": float(DEFAULT_COOLDOWN_SECONDS),
    "enabled": True,
}

# source key (camera/mic id or "default") -> monotonic timestamp of last event
_LAST_TRIGGER: Dict[str, float] = {}

_EVENTS: List[Dict[str, Any]] = []
_EVENTS_BY_ID: Dict[str, Dict[str, Any]] = {}

_LOCK = threading.Lock()

LEVEL_KEYS = (
    "level",
    "audio_level",
    "noise_level",
    "loudness",
    "db",
    "decibel",
    "decibels",
    "spl",
    "sound_level",
)

VOLUME_KEYS = (
    "volume",
    "rms",
    "audio_volume",
    "mic_level",
    "miclevel",
    "amplitude",
    "normalized",
    "normalized_level",
)

AUDIO_KEYS = (
    "audio_base64",
    "audio",
    "base64",
    "b64",
    "data",
    "content",
    "bytes",
    "raw",
    "sample",
    "samples",
    "clip",
    "audio_clip",
)

SOURCE_KEYS = (
    "camera_id",
    "camera",
    "cam",
    "cameraId",
    "microphone_id",
    "microphoneId",
    "mic_id",
    "micId",
    "mic",
    "audio_input_id",
    "device_id",
    "deviceId",
    "source",
)

THRESHOLD_KEYS = (
    "threshold",
    "threshold_level",
    "noise_threshold",
    "min_level",
    "trigger_level",
    "limit",
    "level_threshold",
)

COOLDOWN_KEYS = (
    "cooldown_seconds",
    "cooldown",
    "cooldown_secs",
    "cooldownSec",
    "interval",
    "suppress_seconds",
    "suppress",
)


def _now_iso() -> str:
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return datetime.utcnow().isoformat()


def _now_ts() -> float:
    try:
        return time.time()
    except Exception:  # noqa: BLE001
        return 0.0


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _clamp_level(value: float) -> float:
    return max(MIN_LEVEL, min(MAX_LEVEL, float(value)))


def _coerce_threshold(value: Any, default: float) -> float:
    v = _to_float(value)
    if v is None:
        return float(default)
    return _clamp_level(v)


def _coerce_cooldown(value: Any, default: float) -> float:
    v = _to_float(value)
    if v is None:
        return max(0.0, float(default))
    return max(0.0, v)


def _level_from_audio_b64(audio_b64: Optional[str]) -> Optional[float]:
    """RMS loudness of a base64 audio payload on the 0-100 scale."""
    if not audio_b64 or not isinstance(audio_b64, str):
        return None
    s = audio_b64.strip()
    if not s:
        return None
    if "," in s and s.lower().startswith("data:"):
        s = s.split(",", 1)[1].strip()
    try:
        raw = base64.b64decode(s, validate=False)
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        total = 0.0
        for b in raw:
            v = (b - 128) / 128.0
            total += v * v
        rms = (total / len(raw)) ** 0.5
        return _clamp_level(rms * 100.0)
    except Exception:  # noqa: BLE001
        return None


def _flatten(data: Any) -> Dict[str, Any]:
    if data is None:
        return {}
    if hasattr(data, "model_dump"):
        try:
            return dict(data.model_dump())
        except Exception:  # noqa: BLE001
            pass
    if isinstance(data, dict):
        out = dict(data)
        for nested_key in ("payload", "params", "data", "body", "config", "request", "options", "settings", "noise", "trigger"):
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    try:
        return dict(data)
    except Exception:  # noqa: BLE001
        return {}


def _extract_level(data: Dict[str, Any]) -> Optional[float]:
    for key in LEVEL_KEYS:
        v = _to_float(data.get(key))
        if v is not None:
            return _clamp_level(v)
    for key in VOLUME_KEYS:
        v = _to_float(data.get(key))
        if v is not None:
            if 0.0 <= v <= 1.0:
                return _clamp_level(v * 100.0)
            return _clamp_level(v)
    for key in AUDIO_KEYS:
        val = data.get(key)
        if isinstance(val, (bytes, bytearray)):
            try:
                val = base64.b64encode(bytes(val)).decode("ascii")
            except Exception:  # noqa: BLE001
                continue
        if isinstance(val, str) and val.strip():
            level = _level_from_audio_b64(val)
            if level is not None:
                return level
    return None


def _resolve_key(data: Dict[str, Any]) -> str:
    for key in SOURCE_KEYS:
        val = data.get(key)
        if val is None or isinstance(val, bool):
            continue
        s = str(val).strip()
        if s:
            return s
    return "default"


def _resolve_threshold(data: Dict[str, Any], default: float) -> float:
    for key in THRESHOLD_KEYS:
        if data.get(key) is not None:
            return _coerce_threshold(data.get(key), default)
    return float(default)


def _resolve_cooldown(data: Dict[str, Any], default: float) -> float:
    for key in COOLDOWN_KEYS:
        if data.get(key) is not None:
            return _coerce_cooldown(data.get(key), default)
    return max(0.0, float(default))


def _publish_noise_event(event: Dict[str, Any]) -> bool:
    """Best-effort fan-out over the WS event bus (mirrors bitrate_alerts)."""
    try:
        from app.services.event_bus import event_bus as _bus

        if _bus is None:
            return False
        try:
            import asyncio

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None and loop.is_running():
                loop.create_task(_bus.publish("noise_trigger", dict(event)))
                return True
            return False
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False


def get_config(source: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Return the global trigger config (per-call overrides are not persisted)."""
    with _LOCK:
        cfg = dict(_CONFIG)
    cfg.setdefault("ok", True)
    cfg.setdefault("success", True)
    cfg.setdefault("threshold_level", cfg.get("threshold"))
    cfg.setdefault("cooldown", cfg.get("cooldown_seconds"))
    cfg.setdefault("interval", cfg.get("cooldown_seconds"))
    if source is not None or kwargs:
        data = _flatten(kwargs)
        if source is not None and isinstance(source, dict):
            data.update(_flatten(source))
        cfg["source"] = _resolve_key(data)
    return cfg


def configure(data: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Persist global ``threshold`` / ``cooldown_seconds`` / ``enabled``."""
    flat = _flatten(data)
    flat.update(kwargs or {})
    # db/session compat keys are ignored.
    for k in ("db", "session", "request", "user", "current_user"):
        flat.pop(k, None)
    with _LOCK:
        for key in THRESHOLD_KEYS:
            if flat.get(key) is not None:
                _CONFIG["threshold"] = _coerce_threshold(flat[key], _CONFIG["threshold"])
                break
        for key in COOLDOWN_KEYS:
            if flat.get(key) is not None:
                _CONFIG["cooldown_seconds"] = _coerce_cooldown(flat[key], _CONFIG["cooldown_seconds"])
                break
        if flat.get("enabled") is not None:
            raw = flat.get("enabled")
            if isinstance(raw, str):
                _CONFIG["enabled"] = raw.strip().lower() not in ("0", "false", "no", "off", "disabled")
            else:
                _CONFIG["enabled"] = bool(raw)
        return get_config()


def evaluate(data: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Pure threshold evaluation (no state mutation, no cooldown).

    Returns ``{"triggered": bool, "level": float|None, "threshold": float}``.
    ``None`` level (no measurable input) never triggers.
    """
    flat = _flatten(data)
    for a in args:
        if isinstance(a, dict):
            for k, v in _flatten(a).items():
                flat.setdefault(k, v)
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            flat.setdefault("level", a)
    for k, v in (kwargs or {}).items():
        if k in ("db", "session", "request", "user", "current_user"):
            continue
        flat.setdefault(k, v)
    with _LOCK:
        default_thr = float(_CONFIG["threshold"])
        enabled = bool(_CONFIG.get("enabled", True))
    threshold = _resolve_threshold(flat, default_thr)
    level = _extract_level(flat)
    triggered = bool(enabled and level is not None and level >= threshold)
    return {
        "triggered": triggered,
        "trigger": triggered,
        "level": level,
        "noise_level": level,
        "audio_level": level,
        "threshold": float(threshold),
        "threshold_level": float(threshold),
        "enabled": enabled,
        "source": _resolve_key(flat),
        "camera_id": flat.get("camera_id", flat.get("camera")),
        "ok": True,
        "success": True,
    }


def check(data: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Threshold + cooldown trigger producing at most one event per cooldown.

    Loud input at/above ``threshold`` creates and stores an event unless the
    same source already triggered within ``cooldown_seconds`` (in which case
    the repeat is reported as suppressed, with no new event).
    """
    now = kwargs.pop("now", None)
    flat = _flatten(data)
    for a in args:
        if isinstance(a, dict):
            for k, v in _flatten(a).items():
                flat.setdefault(k, v)
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            flat.setdefault("level", a)
    for k, v in (kwargs or {}).items():
        if k in ("db", "session", "request", "user", "current_user"):
            continue
        flat.setdefault(k, v)
    with _LOCK:
        default_thr = float(_CONFIG["threshold"])
        default_cd = float(_CONFIG["cooldown_seconds"])
        enabled = bool(_CONFIG.get("enabled", True))
    threshold = _resolve_threshold(flat, default_thr)
    cooldown = _resolve_cooldown(flat, default_cd)
    level = _extract_level(flat)
    key = _resolve_key(flat)
    ts = float(now) if _to_float(now) is not None else _now_ts()

    if not enabled:
        return {
            "triggered": False,
            "trigger": False,
            "event_created": False,
            "suppressed_by_cooldown": False,
            "suppressed": False,
            "reason": "disabled",
            "level": level,
            "threshold": float(threshold),
            "cooldown_seconds": float(cooldown),
            "source": key,
            "event": None,
            "ok": True,
            "success": True,
        }
    if level is None:
        return {
            "triggered": False,
            "trigger": False,
            "event_created": False,
            "suppressed_by_cooldown": False,
            "suppressed": False,
            "reason": "no measurable level in payload",
            "level": None,
            "threshold": float(threshold),
            "cooldown_seconds": float(cooldown),
            "source": key,
            "event": None,
            "ok": True,
            "success": True,
        }
    if level < threshold:
        return {
            "triggered": False,
            "trigger": False,
            "event_created": False,
            "suppressed_by_cooldown": False,
            "suppressed": False,
            "reason": "below threshold",
            "level": float(level),
            "threshold": float(threshold),
            "cooldown_seconds": float(cooldown),
            "source": key,
            "event": None,
            "ok": True,
            "success": True,
        }

    with _LOCK:
        last = _LAST_TRIGGER.get(key)
    if last is not None and (ts - last) < cooldown:
        return {
            "triggered": True,
            "trigger": True,
            "event_created": False,
            "suppressed_by_cooldown": True,
            "suppressed": True,
            "reason": "cooldown",
            "level": float(level),
            "threshold": float(threshold),
            "cooldown_seconds": float(cooldown),
            "cooldown_remaining": max(0.0, float(cooldown - (ts - last))),
            "source": key,
            "event": None,
            "ok": True,
            "success": True,
        }

    event_id = uuid.uuid4().hex[:12]
    event: Dict[str, Any] = {
        "id": event_id,
        "event_id": event_id,
        "type": "noise",
        "event": "noise",
        "event_type": "noise_trigger",
        "label": "noise",
        "triggered": True,
        "level": float(level),
        "noise_level": float(level),
        "audio_level": float(level),
        "threshold": float(threshold),
        "threshold_level": float(threshold),
        "cooldown_seconds": float(cooldown),
        "source": key,
        "camera_id": flat.get("camera_id", flat.get("camera")),
        "microphone_id": flat.get("microphone_id", flat.get("mic_id", flat.get("mic"))),
        "timestamp": _now_iso(),
        "created_at": _now_iso(),
        "ok": True,
        "success": True,
    }
    with _LOCK:
        _LAST_TRIGGER[key] = ts
        _EVENTS.append(dict(event))
        _EVENTS_BY_ID[event_id] = dict(event)
    _publish_noise_event(event)
    return {
        "triggered": True,
        "trigger": True,
        "event_created": True,
        "suppressed_by_cooldown": False,
        "suppressed": False,
        "reason": "threshold breached",
        "level": float(level),
        "threshold": float(threshold),
        "cooldown_seconds": float(cooldown),
        "source": key,
        "event": dict(event),
        "event_id": event_id,
        "id": event_id,
        "ok": True,
        "success": True,
    }


def list_events(source: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return stored noise events, optionally filtered by source."""
    flat: Dict[str, Any] = {}
    if isinstance(source, dict):
        flat.update(_flatten(source))
    elif source is not None:
        flat["camera_id"] = source
    flat.update(_flatten(kwargs))
    want = None
    for key in SOURCE_KEYS:
        if flat.get(key) is not None:
            want = str(flat.get(key)).strip()
            break
    with _LOCK:
        items = [dict(e) for e in _EVENTS]
    if want is not None and want != "default":
        items = [e for e in items if str(e.get("source", "")) == want or str(e.get("camera_id", "")) == want]
    return items


def get_event(event_id: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return a single stored event by id, or None when not found."""
    if event_id is None:
        flat = _flatten(kwargs)
        for key in ("id", "event_id"):
            if flat.get(key) is not None:
                event_id = flat.get(key)
                break
    if event_id is None:
        return None
    with _LOCK:
        found = _EVENTS_BY_ID.get(str(event_id))
        return dict(found) if found is not None else None


def get_status(source: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Trigger status for one source: config, last trigger, event count."""
    flat: Dict[str, Any] = {}
    if isinstance(source, dict):
        flat.update(_flatten(source))
    elif source is not None:
        flat["camera_id"] = source
    flat.update(_flatten(kwargs))
    key = _resolve_key(flat)
    now = _to_float(flat.get("now"))
    ts = float(now) if now is not None else _now_ts()
    with _LOCK:
        cfg = dict(_CONFIG)
        last = _LAST_TRIGGER.get(key)
        count = len(_EVENTS)
    cooldown = _resolve_cooldown(flat, float(cfg["cooldown_seconds"]))
    in_cooldown = bool(last is not None and (ts - last) < cooldown)
    return {
        "source": key,
        "camera_id": flat.get("camera_id", flat.get("camera")),
        "enabled": bool(cfg.get("enabled", True)),
        "threshold": float(cfg.get("threshold", DEFAULT_THRESHOLD)),
        "threshold_level": float(cfg.get("threshold", DEFAULT_THRESHOLD)),
        "cooldown_seconds": float(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
        "cooldown": float(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
        "last_trigger_ts": last,
        "in_cooldown": in_cooldown,
        "cooldown_remaining": max(0.0, float(cooldown - (ts - last))) if last is not None and in_cooldown else 0.0,
        "event_count": count,
        "ok": True,
        "success": True,
    }


def get_summary() -> Dict[str, Any]:
    """Global trigger summary: config + event/source counts."""
    with _LOCK:
        cfg = dict(_CONFIG)
        count = len(_EVENTS)
        sources = sorted(_LAST_TRIGGER.keys())
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "threshold": float(cfg.get("threshold", DEFAULT_THRESHOLD)),
        "threshold_level": float(cfg.get("threshold", DEFAULT_THRESHOLD)),
        "cooldown_seconds": float(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
        "cooldown": float(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
        "event_count": count,
        "total": count,
        "sources": sources,
        "source_count": len(sources),
        "ok": True,
        "success": True,
    }


def clear(source: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Clear stored events and cooldown state (config is preserved).

    With a source given, only that source's cooldown is cleared; events are
    always cleared globally (they carry their own source for filtering).
    """
    flat: Dict[str, Any] = {}
    if isinstance(source, dict):
        flat.update(_flatten(source))
    elif source is not None and not isinstance(source, bool):
        flat["camera_id"] = source
    flat.update(_flatten(kwargs))
    key = _resolve_key(flat) if (flat or source is not None) else None
    with _LOCK:
        n = len(_EVENTS)
        _EVENTS.clear()
        _EVENTS_BY_ID.clear()
        if key is None or key == "default":
            _LAST_TRIGGER.clear()
        else:
            _LAST_TRIGGER.pop(key, None)
    return {"ok": True, "success": True, "cleared": n, "count": 0}


def reset(**kwargs: Any) -> Dict[str, Any]:
    """Clear events/cooldowns and restore default config."""
    with _LOCK:
        _CONFIG["threshold"] = float(DEFAULT_THRESHOLD)
        _CONFIG["cooldown_seconds"] = float(DEFAULT_COOLDOWN_SECONDS)
        _CONFIG["enabled"] = True
    out = clear(**kwargs)
    out.update(get_config())
    return out


class NoiseTriggerService:
    """Object-oriented facade over the module-level trigger state."""

    def evaluate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return evaluate(*args, **kwargs)

    def check(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check(*args, **kwargs)

    def process_level(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check(*args, **kwargs)

    def process(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check(*args, **kwargs)

    def handle(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check(*args, **kwargs)

    def trigger(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check(*args, **kwargs)

    def report(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check(*args, **kwargs)

    def submit(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check(*args, **kwargs)

    def get_config(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_config(*args, **kwargs)

    def configure(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return configure(*args, **kwargs)

    def list_events(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_events(*args, **kwargs)

    def get_event(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_event(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_status(*args, **kwargs)

    def get_summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_summary()

    def clear(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return clear(*args, **kwargs)

    def reset(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return reset(*args, **kwargs)


noise_trigger_service = NoiseTriggerService()
noise_service = noise_trigger_service
trigger_service = noise_trigger_service
service = noise_trigger_service
manager = noise_trigger_service

NoiseTrigger = NoiseTriggerService
NoiseService = NoiseTriggerService

# Compatibility aliases (function level).
process_level = check
process = check
handle = check
trigger = check
report = check
report_level = check
submit = check
submit_level = check
detect = check
detect_noise = check
check_level = check
check_noise = check
handle_level = check
is_triggered = evaluate
should_trigger = evaluate
above_threshold = evaluate

clear_events = clear
reset_trigger = reset
list_noise_events = list_events
get_noise_event = get_event
noise_status = get_status
noise_summary = get_summary
noise_config = get_config
configure_trigger = configure

__all__ = [
    "DEFAULT_THRESHOLD",
    "DEFAULT_COOLDOWN_SECONDS",
    "get_config",
    "configure",
    "evaluate",
    "check",
    "process_level",
    "list_events",
    "get_event",
    "get_status",
    "get_summary",
    "clear",
    "reset",
    "NoiseTriggerService",
    "noise_trigger_service",
]
