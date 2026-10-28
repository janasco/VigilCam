"""INBOUND external trigger service for VigilCam.

This module handles *incoming* webhooks from external devices such as IP
cameras, video doorbells, and home-automation systems. It is the inverse of
the OUTBOUND delivery path:

* OUTBOUND (do NOT duplicate here): ``app.services.actions`` and
  ``app.services.webhook_queue`` *send* HTTP requests to third-party URLs.
* INBOUND (this module): external devices *call into* VigilCam; we validate
  an optional shared token and persist an :class:`Event` row.

No code in this module performs outbound HTTP delivery.
"""

from __future__ import annotations

import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Event

# Environment / settings keys consulted (in order) for the expected token.
TOKEN_ENV_KEYS = (
    "INBOUND_TRIGGER_TOKEN",
    "VIGILCAM_INBOUND_TRIGGER_TOKEN",
    "INBOUND_TOKEN",
    "TRIGGER_TOKEN",
    "EXTERNAL_TRIGGER_TOKEN",
    "DOORBELL_TOKEN",
    "VIGILCAM_INBOUND_TOKEN",
)

# Body/query keys that may carry the caller-supplied token.
TOKEN_FIELDS = (
    "token",
    "auth_token",
    "trigger_token",
    "inbound_token",
    "api_token",
    "api_key",
    "secret",
    "key",
    "auth",
    "password_token",
)

CAMERA_FIELDS = (
    "camera_id",
    "cameraId",
    "camera",
    "cam",
    "monitor_id",
    "monitorId",
    "monitor",
    "device_id",
    "deviceId",
    "device",
    "doorbell_id",
    "doorbellId",
    "doorbell",
    "camera_name",
    "cameraName",
    "source",
)

KIND_FIELDS = (
    "kind",
    "type",
    "event_type",
    "eventType",
    "trigger_type",
    "triggerType",
    "trigger",
    "category",
    "event",
    "alarm_type",
)

LABEL_FIELDS = (
    "label",
    "object",
    "detection",
    "event_name",
    "eventName",
    "name",
    "state",
    "subject",
    "reason",
)

CONFIDENCE_FIELDS = (
    "confidence",
    "score",
    "probability",
    "certainty",
)

TAG_FIELDS = (
    "tags",
    "tag",
    "labels",
)


def get_expected_token(explicit: Any = None) -> Optional[str]:
    """Return the configured inbound token, or ``None`` when open access."""
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    for key in TOKEN_ENV_KEYS:
        val = os.getenv(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    try:
        from app.core.config import settings as _settings

        for key in TOKEN_ENV_KEYS:
            for attr in (key, key.lower()):
                val = getattr(_settings, attr, None)
                if val is not None and str(val).strip():
                    return str(val).strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def validate_token(provided: Any = None, expected: Any = None) -> bool:
    """Return True when the caller is authorized.

    When no expected token is configured (the default), any caller —
    including a missing token — is accepted. When an expected token is
    configured, the provided value must match exactly.
    """
    exp = get_expected_token(explicit=expected)
    if exp is None or str(exp) == "":
        return True
    if provided is None:
        return False
    prov = str(provided)
    if isinstance(prov, bool):
        return False
    prov = prov.strip()
    if prov.lower().startswith("bearer "):
        prov = prov[7:].strip()
    if not prov:
        return False
    return hmac.compare_digest(prov, str(exp))


def extract_token(data: Any) -> Optional[str]:
    """Pull a caller-supplied token out of a mapping, if present."""
    if not isinstance(data, dict):
        return None
    for field in TOKEN_FIELDS:
        if field in data and data.get(field) is not None:
            val = data.get(field)
            if isinstance(val, (str, int, float)) and str(val).strip():
                return str(val).strip()
    lowered = {str(k).lower(): v for k, v in data.items()}
    for field in TOKEN_FIELDS:
        if field.lower() in lowered and lowered[field.lower()] is not None:
            val = lowered[field.lower()]
            if isinstance(val, (str, int, float)) and str(val).strip():
                return str(val).strip()
    return None


def _first_present(mapping: Dict[str, Any], fields: tuple) -> Any:
    for field in fields:
        if field in mapping and mapping.get(field) is not None:
            val = mapping.get(field)
            if isinstance(val, str) and val.strip() == "":
                continue
            return val
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for field in fields:
        key = field.lower()
        if key in lowered and lowered[key] is not None:
            val = lowered[key]
            if isinstance(val, str) and val.strip() == "":
                continue
            return val
    return None


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_camera_id(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (list, tuple, set)):
        vals = list(value)
        if len(vals) == 1:
            return _coerce_camera_id(vals[0])
        return None
    if isinstance(value, dict):
        for key in ("id", "camera_id", "monitor_id", "device_id", "value"):
            if value.get(key) is not None:
                coerced = _coerce_camera_id(value.get(key))
                if coerced is not None:
                    return coerced
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(float(s.split(":")[0].strip()))
    except (TypeError, ValueError):
        return None


def _coerce_tags(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                value = parsed
            else:
                return [str(parsed).strip()] if str(parsed).strip() else []
        except Exception:  # noqa: BLE001
            value = [p.strip() for p in s.split(",") if p.strip()]
    if isinstance(value, (tuple, set)):
        value = list(value)
    if not isinstance(value, list):
        return [str(value).strip()] if str(value).strip() else []
    out: List[str] = []
    seen = set()
    for item in value:
        s = str(item).strip() if item is not None else ""
        if not s or s.lower() in {"none", "null"}:
            continue
        if s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
    return out


def normalize_trigger(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Normalize a flexible inbound payload into Event fields.

    Accepts a dict positionally and/or keyword arguments. Never raises for
    missing fields; fills ``kind``/``label`` defaults instead.
    """
    data: Dict[str, Any] = {}
    for arg in args:
        if isinstance(arg, dict):
            data.update(arg)
        elif isinstance(arg, str) and arg.strip():
            data.setdefault("label", arg.strip())
    data.update(kwargs or {})

    for nested_key in ("payload", "data", "body", "event", "message", "params"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    camera_raw = _first_present(data, CAMERA_FIELDS)
    camera_id = _coerce_camera_id(camera_raw)

    kind_raw = _first_present(data, KIND_FIELDS)
    kind = str(kind_raw).strip().lower().replace(" ", "_") if kind_raw is not None else "motion"
    if not kind or kind in {"null", "none", "undefined"}:
        kind = "motion"
    if kind in {"doorbell_press", "doorbell-pressed", "doorbellpush", "ring", "ding_dong"}:
        kind = "doorbell"

    label_raw = _first_present(data, LABEL_FIELDS)
    if label_raw is not None:
        label = str(label_raw).strip()
    else:
        label = "doorbell" if kind == "doorbell" else kind
    if not label:
        label = kind

    confidence = _coerce_float(_first_present(data, CONFIDENCE_FIELDS))
    tags = _coerce_tags(_first_present(data, TAG_FIELDS))

    try:
        payload_str = json.dumps(data, default=str)
    except Exception:  # noqa: BLE001
        payload_str = str(data)

    return {
        "camera_id": camera_id,
        "camera": camera_raw,
        "kind": kind,
        "label": label,
        "confidence": confidence,
        "tags": tags,
        "payload": payload_str,
        "raw": dict(data),
    }


async def resolve_camera_id(db: AsyncSession, identifier: Any) -> Optional[int]:
    """Resolve a camera identifier to a DB id, or ``None`` if unknown."""
    direct = _coerce_camera_id(identifier)
    if direct is not None:
        try:
            from app.models.entities import Camera

            cam = await db.get(Camera, direct)
            if cam is not None:
                return int(direct)
            # Keep numeric ids even when the camera row is missing so the
            # Event still records which device triggered.
            return int(direct)
        except Exception:  # noqa: BLE001
            return int(direct)
    if identifier is None:
        return None
    s = str(identifier).strip()
    if not s:
        return None
    try:
        from app.services.cameras import _resolve_camera_alias

        cam = await _resolve_camera_alias(db, s)
        if cam is not None and getattr(cam, "id", None) is not None:
            return int(cam.id)
    except Exception:  # noqa: BLE001
        pass
    return None


async def handle_inbound_trigger(
    db: AsyncSession,
    data: Any = None,
    *,
    token: Any = None,
    expected_token: Any = None,
    camera_id: Any = None,
    kind: Any = None,
    label: Any = None,
    confidence: Any = None,
    tags: Any = None,
    **kwargs: Any,
) -> Event:
    """Validate the optional token and persist an :class:`Event` row.

    Raises:
        PermissionError: when a token is configured but the caller did not
            supply a matching one.
    """
    merged: Dict[str, Any] = {}
    if isinstance(data, dict):
        merged.update(data)
    elif data is not None and not isinstance(data, dict):
        merged["label"] = data
    merged.update(kwargs or {})
    if camera_id is not None:
        merged.setdefault("camera_id", camera_id)
    if kind is not None:
        merged.setdefault("kind", kind)
    if label is not None:
        merged.setdefault("label", label)
    if confidence is not None:
        merged.setdefault("confidence", confidence)
    if tags is not None:
        merged.setdefault("tags", tags)

    provided = token
    if provided is None:
        provided = extract_token(merged)
    if not validate_token(provided, expected=expected_token):
        raise PermissionError("Invalid or missing trigger token")

    normalized = normalize_trigger(merged)

    resolved_id = normalized.get("camera_id")
    camera_hint = merged.get("camera_id", merged.get("camera", merged.get("device_id")))
    if resolved_id is None and camera_hint is not None:
        resolved_id = await resolve_camera_id(db, camera_hint)

    event = Event(
        camera_id=resolved_id,
        kind=normalized["kind"],
        label=normalized["label"],
        confidence=normalized["confidence"],
        payload=normalized["payload"],
        tags=normalized["tags"] or None,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    if resolved_id is not None:
        try:
            from app.services.activity import mark_camera_activity_db

            await mark_camera_activity_db(
                db, resolved_id, event.created_at or datetime.now(timezone.utc), motion=False
            )
        except Exception:  # noqa: BLE001
            pass
    return event


def event_to_dict(event: Event) -> Dict[str, Any]:
    tags = getattr(event, "tags", None)
    if isinstance(tags, str):
        try:
            parsed = json.loads(tags)
            tags = parsed if isinstance(parsed, list) else [parsed]
        except Exception:  # noqa: BLE001
            tags = [p.strip() for p in tags.split(",") if p.strip()]
    return {
        "id": getattr(event, "id", None),
        "camera_id": getattr(event, "camera_id", None),
        "kind": getattr(event, "kind", None),
        "label": getattr(event, "label", None),
        "confidence": getattr(event, "confidence", None),
        "payload": getattr(event, "payload", None),
        "tags": list(tags) if isinstance(tags, list) else [],
        "created_at": event.created_at.isoformat() if getattr(event, "created_at", None) else None,
    }


# ---------------------------------------------------------------------------
# Aliases (naming conveniences only; all stay on the INBOUND path).
# ---------------------------------------------------------------------------
verify_token = validate_token
check_token = validate_token
is_valid_token = validate_token
token_valid = validate_token
authenticate = validate_token
authenticate_token = validate_token
check_trigger_token = validate_token
verify_trigger_token = validate_token

get_token = get_expected_token
expected_token = get_expected_token
get_configured_token = get_expected_token
configured_token = get_expected_token

normalize_payload = normalize_trigger
normalize_inbound = normalize_trigger
normalize_inbound_trigger = normalize_trigger
parse_trigger = normalize_trigger
parse_inbound_trigger = normalize_trigger
normalize_trigger_payload = normalize_trigger

handle_trigger = handle_inbound_trigger
receive_trigger = handle_inbound_trigger
receive_inbound_trigger = handle_inbound_trigger
ingest_trigger = handle_inbound_trigger
ingest_inbound_trigger = handle_inbound_trigger
process_trigger = handle_inbound_trigger
process_inbound = handle_inbound_trigger
process_inbound_trigger = handle_inbound_trigger
create_event_from_trigger = handle_inbound_trigger
create_inbound_event = handle_inbound_trigger
record_trigger_event = handle_inbound_trigger
record_inbound_event = handle_inbound_trigger
trigger_event = handle_inbound_trigger

InboundTriggerService_handle = handle_inbound_trigger


class InboundTriggerService:
    """Thin object wrapper around the module-level helpers."""

    def __init__(self, expected_token: Any = None) -> None:
        self.expected_token = expected_token

    def validate(self, provided: Any = None, expected: Any = None) -> bool:
        return validate_token(
            provided, expected=expected if expected is not None else self.expected_token
        )

    def normalize(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return normalize_trigger(*args, **kwargs)

    async def handle(self, db: AsyncSession, *args: Any, **kwargs: Any) -> Event:
        kwargs.setdefault("expected_token", self.expected_token)
        return await handle_inbound_trigger(db, *args, **kwargs)

    async def receive(self, db: AsyncSession, *args: Any, **kwargs: Any) -> Event:
        return await self.handle(db, *args, **kwargs)

    async def process(self, db: AsyncSession, *args: Any, **kwargs: Any) -> Event:
        return await self.handle(db, *args, **kwargs)

    async def trigger(self, db: AsyncSession, *args: Any, **kwargs: Any) -> Event:
        return await self.handle(db, *args, **kwargs)


InboundTriggerHandler = InboundTriggerService
InboundTriggerManager = InboundTriggerService
TriggerService = InboundTriggerService
TriggerHandler = InboundTriggerService
DoorbellService = InboundTriggerService

inbound_trigger_service = InboundTriggerService()
trigger_service = inbound_trigger_service
service = inbound_trigger_service


__all__ = [
    "get_expected_token",
    "validate_token",
    "extract_token",
    "normalize_trigger",
    "resolve_camera_id",
    "handle_inbound_trigger",
    "event_to_dict",
    "InboundTriggerService",
    "inbound_trigger_service",
]
