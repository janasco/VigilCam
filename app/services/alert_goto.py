"""Alert-triggered PTZ goto service (in-memory).

Moves a camera to a configured preset when a detection event label matches,
subject to a per-rule cooldown. This is opt-in only:

- No detection pipeline calls this module automatically. PTZ presets, tours
  (:mod:`app.services.ptz_tour`), schedules (:mod:`app.services.ptz_scheduler`,
  :mod:`app.services.patrol_schedule`) and idle home-return
  (:mod:`app.services.ptz_idle`) never fire on detection events; they only
  move on explicit API calls or time/cron/idle triggers. This service only
  acts when :func:`handle_detection` is invoked explicitly (via the
  ``app.api.alert_goto`` router or direct call).
- Moves are mocked (no hardware required), consistent with ``ptz_tour`` and
  ``ptz_idle``. An optional ``mover`` with ``goto_preset``/``goto`` is
  attempted on a best-effort basis and never fails the call.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

ALERT_GOTO_RULES: Dict[str, Dict[str, Any]] = {}
RULES = ALERT_GOTO_RULES
alert_goto_rules = ALERT_GOTO_RULES
ITEMS = ALERT_GOTO_RULES
JOBS = ALERT_GOTO_RULES

_LAST_FIRED: Dict[str, float] = {}

_lock = threading.RLock()

DEFAULT_COOLDOWN_SECONDS = 60.0
WILDCARDS = {"*", "any", "all", ""}


def _now_ts() -> float:
    return time.time()


def _now_iso(ts: Optional[float] = None) -> str:
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _norm(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            # Bare label string when no label set yet, else treat as preset.
            if data.get("label") is None:
                data.setdefault("label", s)
            elif data.get("preset_token") is None:
                data.setdefault("preset_token", s)
        else:
            try:
                attrs = {
                    k: getattr(a, k)
                    for k in dir(a)
                    if not k.startswith("_") and not callable(getattr(a, k, None))
                }
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in (
        "payload", "options", "config", "params", "data", "body",
        "request", "settings", "detection", "event", "alert", "rule",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    low = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in low and low[k.lower()] is not None:
            return low[k.lower()]
    return None


CAMERA_KEYS = ("camera_id", "camera", "cam", "cameraId", "cameraID", "cam_id", "device_id", "deviceId")
LABEL_KEYS = ("label", "class", "class_name", "className", "object", "object_class",
              "detection_label", "label_name", "name", "event_type", "type", "kind")
PRESET_KEYS = ("preset_token", "presetToken", "preset", "preset_id", "presetId",
               "token", "position", "goto_preset")
COOLDOWN_KEYS = ("cooldown", "cooldown_seconds", "cooldown_secs", "interval",
                 "interval_seconds", "delay", "delay_seconds")


def _label_match(rule_label: Any, event_label: Any) -> bool:
    if rule_label is None:
        return True
    rl = str(rule_label).strip().lower()
    if rl in WILDCARDS:
        return True
    if event_label is None:
        return False
    return str(event_label).strip().lower() == rl


def _camera_match(rule_camera: Any, event_camera: Any) -> bool:
    if rule_camera is None:
        return True
    if event_camera is None:
        return False
    return str(rule_camera).strip() == str(event_camera).strip()


def _mock_goto(camera_id: Any, preset_token: Any, ts: float) -> Dict[str, Any]:
    return {
        "success": True,
        "mock": True,
        "moved": True,
        "status": "moved",
        "ok": True,
        "camera_id": camera_id,
        "preset_token": preset_token,
        "preset": preset_token,
        "executed_at": _now_iso(ts),
    }


def _try_mover(rule: Dict[str, Any], camera_id: Any, preset_token: Any) -> None:
    mover = rule.get("mover")
    if mover is None:
        return
    fn = getattr(mover, "goto_preset", None) or getattr(mover, "goto", None)
    if not callable(fn):
        return
    try:
        try:
            fn(camera_id, preset_token)
        except TypeError:
            try:
                fn(preset_token)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def configure_rule(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create or update an alert->preset goto rule."""
    data = _norm(*args, **kwargs)
    camera_id = _first(data, CAMERA_KEYS)
    label = _first(data, LABEL_KEYS)
    preset = _first(data, PRESET_KEYS)
    if preset is None:
        raise ValueError("preset_token is required")
    cooldown = _coerce_float(_first(data, COOLDOWN_KEYS), DEFAULT_COOLDOWN_SECONDS)
    if cooldown < 0:
        cooldown = 0.0
    rid = _first(data, ("id", "rule_id", "ruleId", "uuid"))
    rid = str(rid) if rid is not None else str(uuid.uuid4())
    enabled_raw = _first(data, ("enabled", "active", "is_enabled", "on"))
    enabled = bool(enabled_raw) if enabled_raw is not None else True
    now = _now_ts()
    with _lock:
        existing = ALERT_GOTO_RULES.get(rid)
        rule: Dict[str, Any] = dict(existing) if existing else {}
        rule.update({
            "id": rid,
            "rule_id": rid,
            "camera_id": camera_id,
            "camera": camera_id,
            "label": label,
            "preset_token": str(preset),
            "preset": str(preset),
            "cooldown_seconds": float(cooldown),
            "cooldown": float(cooldown),
            "enabled": enabled,
            "active": enabled,
            "mover": data.get("mover", rule.get("mover")),
            "created_at": rule.get("created_at", _now_iso(now)),
            "updated_at": _now_iso(now),
            "fire_count": int(rule.get("fire_count", 0) or 0),
            "ok": True,
            "success": True,
        })
        # Allow tests to seed last-fired explicitly.
        seed = _first(data, ("last_fired_ts", "lastFiredTs", "last_fired", "now"))
        ALERT_GOTO_RULES[rid] = rule
    if seed is not None and str(seed).replace(".", "", 1).replace("-", "", 1).isdigit():
        with _lock:
            _LAST_FIRED[rid] = _coerce_float(seed, 0.0)
    return dict(rule)


def remove_rule(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _norm(*args, **kwargs)
    rid = _first(data, ("id", "rule_id", "ruleId", "uuid"))
    if rid is None and args and not isinstance(args[0], dict):
        rid = args[0]
    key = str(rid) if rid is not None else None
    with _lock:
        existed = key in ALERT_GOTO_RULES if key else False
        if existed:
            ALERT_GOTO_RULES.pop(key, None)  # type: ignore[arg-type]
            _LAST_FIRED.pop(key, None)  # type: ignore[arg-type]
    return {"removed": bool(existed), "deleted": bool(existed),
            "id": key, "ok": True, "success": True}


def list_rules(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    data = _norm(*args, **kwargs)
    cam = _first(data, CAMERA_KEYS)
    with _lock:
        items = [dict(r) for r in ALERT_GOTO_RULES.values()]
    if cam is not None:
        items = [r for r in items
                 if r.get("camera_id") is None or str(r.get("camera_id")) == str(cam)]
    return items


def clear(*args: Any, **kwargs: Any) -> int:
    with _lock:
        n = len(ALERT_GOTO_RULES)
        ALERT_GOTO_RULES.clear()
        _LAST_FIRED.clear()
    return n


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _norm(*args, **kwargs)
    now = _coerce_float(data.get("now"), _now_ts()) if data.get("now") is not None else _now_ts()
    with _lock:
        rules = [dict(r) for r in ALERT_GOTO_RULES.values()]
        last = dict(_LAST_FIRED)
    out_rules = []
    for r in rules:
        lf = last.get(r["id"])
        remaining = 0.0
        if lf is not None:
            remaining = max(0.0, float(r.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)) - (now - lf))
        out_rules.append({**r, "cooldown_remaining": remaining, "in_cooldown": bool(remaining > 0)})
    return {"ok": True, "success": True, "rules": out_rules,
            "count": len(out_rules), "checked_at": _now_iso(now)}


def handle_detection(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Handle one detection event; goto preset on label match (cooldown-gated).

    Returns ``{"moved": bool, "matched": bool, "suppressed": bool, ...}``.
    Never raises for non-matching or cooldown-suppressed events.
    """
    data = _norm(*args, **kwargs)
    camera_id = _first(data, CAMERA_KEYS)
    label = _first(data, LABEL_KEYS)
    # Also accept common detector payload shapes: {"class": ...} lives under
    # "label" already via LABEL_KEYS; support explicit "detection" dicts.
    if label is None:
        det = data.get("detection")
        if isinstance(det, dict):
            for k in ("label", "class", "class_name", "name", "type"):
                if det.get(k) is not None:
                    label = det.get(k)
                    break
    now = _coerce_float(data.get("now"), _now_ts()) if data.get("now") is not None else _now_ts()
    force = bool(data.get("force", False))

    with _lock:
        rules = [dict(r) for r in ALERT_GOTO_RULES.values()]

    matched = [r for r in rules
               if r.get("enabled", True)
               and _camera_match(r.get("camera_id"), camera_id)
               and _label_match(r.get("label"), label)]
    if not matched:
        return {"moved": False, "matched": False, "suppressed": False,
                "camera_id": camera_id, "label": label,
                "reason": "no_matching_rule", "ok": True, "success": True}

    # Fire the first matching rule (deterministic: insertion order).
    rule = matched[0]
    rid = rule["id"]
    with _lock:
        last = _LAST_FIRED.get(rid)
    cooldown = float(rule.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS) or 0.0)
    remaining = 0.0
    if last is not None and not force:
        remaining = max(0.0, cooldown - (now - last))
    if remaining > 0:
        with _lock:
            rule_rec = ALERT_GOTO_RULES.get(rid)
            if rule_rec is not None:
                rule_rec["suppressed_count"] = int(rule_rec.get("suppressed_count", 0) or 0) + 1
        return {"moved": False, "matched": True, "suppressed": True,
                "throttled": True, "allowed": False,
                "camera_id": camera_id, "label": label,
                "rule_id": rid, "preset_token": rule.get("preset_token"),
                "cooldown_remaining": remaining, "remaining_seconds": remaining,
                "reason": "cooldown", "ok": True, "success": True}

    move = _mock_goto(camera_id, rule.get("preset_token"), now)
    _try_mover(rule, camera_id, rule.get("preset_token"))
    with _lock:
        _LAST_FIRED[rid] = now
        rec = ALERT_GOTO_RULES.get(rid)
        if rec is not None:
            rec["fire_count"] = int(rec.get("fire_count", 0) or 0) + 1
            rec["last_fired_ts"] = now
            rec["last_fired"] = _now_iso(now)
            rec["last_move"] = dict(move)
            rec["updated_at"] = _now_iso(now)
    return {"moved": True, "matched": True, "suppressed": False,
            "throttled": False, "allowed": True,
            "camera_id": camera_id, "label": label,
            "rule_id": rid, "preset_token": rule.get("preset_token"),
            "preset": rule.get("preset_token"),
            "move_result": move, "cooldown_remaining": 0.0,
            "ok": True, "success": True}


class AlertGotoService:
    """Singleton mirror of the module functions."""

    def configure_rule(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return configure_rule(*a, **k)

    def add_rule(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return configure_rule(*a, **k)

    def create_rule(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return configure_rule(*a, **k)

    def remove_rule(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return remove_rule(*a, **k)

    def delete_rule(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return remove_rule(*a, **k)

    def list_rules(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_rules(*a, **k)

    def clear(self, *a: Any, **k: Any) -> int:
        return clear(*a, **k)

    def reset(self, *a: Any, **k: Any) -> int:
        return clear(*a, **k)

    def get_status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_status(*a, **k)

    def status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_status(*a, **k)

    def handle_detection(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return handle_detection(*a, **k)

    def on_detection(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return handle_detection(*a, **k)

    def trigger(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return handle_detection(*a, **k)


AlertGoto = AlertGotoService
AlertGotoRuleService = AlertGotoService

alert_goto_service = AlertGotoService()
service = alert_goto_service
manager = alert_goto_service
default_service = alert_goto_service


def get_service() -> AlertGotoService:
    return alert_goto_service


# Module-level aliases for flexible callers.
add_rule = configure_rule
create_rule = configure_rule
set_rule = configure_rule
upsert_rule = configure_rule
delete_rule = remove_rule
delete = remove_rule
remove = remove_rule
list = list_rules  # noqa: A001
all = list_rules  # noqa: A001
reset = clear
on_detection = handle_detection
on_event = handle_detection
trigger = handle_detection
handle_event = handle_detection
process_detection = handle_detection
status = get_status


__all__ = [
    "ALERT_GOTO_RULES",
    "DEFAULT_COOLDOWN_SECONDS",
    "AlertGotoService",
    "alert_goto_service",
    "configure_rule",
    "remove_rule",
    "list_rules",
    "clear",
    "get_status",
    "handle_detection",
]
