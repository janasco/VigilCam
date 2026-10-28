"""Inference budget governor for VigilCam.

No inference budget enforcement existed before this module: motion gating
(``app.services.motion`` / ``app.workers.motion_worker``) decides *whether a
frame is worth looking at*, and ``audit.AuditEventType.INFERENCE_REFUSED_BUDGET``
was defined but never emitted. Nothing capped *how much* expensive inference
(cloud / detector-adapter / genai / face / plate) may run per minute, per
camera, or concurrently.

This module adds that missing compute-budget layer, adapted from the
OpenNVR ``detect-pipeline`` tier-gating ideas (studied read-only):

* **Gate** (``gate.py``): shadow-by-default escalate/suppress decisions with
  safety rails -- ``always_analyze`` per-camera escape hatch,
  ``critical_labels`` force-escalate, ``heartbeat`` worst-case-latency bound,
  per-key ``cooldown``, stationary suppression. Recording is never gated.
* **Budget controller** (``pipeline.RegionBudgetController``): watch actual
  latency against the frame budget and spend fewer regions when over it,
  remembering the failing level (slow-start threshold) so recovery must earn
  its way back with sustained evidence. Here it watches inference latency
  against the per-call budget and sheds concurrent slots the same way.
* **Dispatcher discipline** (``dispatch.py``): global ``max_inflight`` plus a
  per-camera share so one busy camera degrades itself instead of locking out
  the fleet; best-effort drops under backpressure; once-per-track dedup so a
  repeat visit does not re-spend the budget.
* **Rollout ladder** (``ENABLEMENT.md``): ``off -> shadow -> enforce``.
  ``shadow`` (the default) computes and audits every decision but never
  refuses; ``enforce`` actually refuses; ``off`` allows everything.

Pure + injectable where it matters: ``Gate.evaluate(tracks, now)`` and
``InferenceBudgetController.observe(latency_s, ...)`` take the clock as an
argument, so every decision path is unit-tested with no I/O.
"""

from __future__ import annotations

import enum
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

MODES = ("off", "shadow", "enforce")
DEFAULT_MODE = "shadow"
DEFAULT_MAX_PER_MINUTE = 60
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_COOLDOWN_S = 30.0
DEFAULT_HEARTBEAT_S = 0.0
DEFAULT_WINDOW_S = 60.0

MIN_COOLDOWN_S = 0.0
MAX_COOLDOWN_S = 86400.0 * 7
MAX_HISTORY = 200


class TriggerPolicy(str, enum.Enum):
    """What cheap signal wakes the expensive tier. Only MOTION is evaluated
    locally; the rest are declared so the gate is authored against the
    interface, not hardcoded (mirrors OpenNVR ``gate.TriggerPolicy``)."""

    MOTION = "motion"
    SCENE_CHANGE = "scene_change"
    INTERVAL = "interval"
    FIELD_STATISTIC = "field_statistic"
    CHAINED = "chained"
    ALWAYS = "always"
    NONE = "none"


@dataclass
class GateConfig:
    """Per-scope gate policy. All rails default to the safe/measure-only posture."""

    shadow: bool = True
    trigger: TriggerPolicy = TriggerPolicy.MOTION
    interesting_labels: Optional[frozenset] = None
    critical_labels: frozenset = frozenset()
    always_analyze: bool = False
    escalate_cooldown_s: float = DEFAULT_COOLDOWN_S
    heartbeat_s: float = DEFAULT_HEARTBEAT_S
    stationary_suppress: bool = True
    min_hits: int = 1


@dataclass(frozen=True)
class GateDecision:
    track_id: Any
    label: str
    escalate: bool
    reason: str  # new_track | reactivated | critical_label | always_analyze |
    # heartbeat | refresh / stationary | cooldown | uninteresting_label | not_confirmed
    shadow: bool


@dataclass
class GateResult:
    decisions: List[GateDecision] = field(default_factory=list)
    shadow: bool = True

    @property
    def enforced(self) -> bool:
        return not self.shadow

    def to_dispatch(self) -> List[GateDecision]:
        if self.shadow:
            return []
        return [d for d in self.decisions if d.escalate]


@dataclass
class TrackView:
    id: Any = "default"
    label: str = "object"
    stationary: bool = False
    hits: int = 1


class Gate:
    """Stateful per-scope gate. Feed it each request's tracks; get decisions back."""

    def __init__(self, config: GateConfig | None = None) -> None:
        self.cfg = config or GateConfig()
        self._last_escalate: Dict[Any, float] = {}
        self._seen: set = set()
        self._was_stationary: Dict[Any, bool] = {}
        self._last_heartbeat: Optional[float] = None

    def _decide(self, t: TrackView, now: float, heartbeat_due: bool) -> GateDecision:
        def out(escalate: bool, reason: str) -> GateDecision:
            return GateDecision(t.id, t.label, escalate, reason, self.cfg.shadow)

        if t.hits < self.cfg.min_hits:
            return out(False, "not_confirmed")
        if self.cfg.always_analyze:
            return out(True, "always_analyze")
        if heartbeat_due:
            return out(True, "heartbeat")

        in_cooldown = (
            t.id in self._last_escalate
            and (now - self._last_escalate[t.id]) < self.cfg.escalate_cooldown_s
        )
        is_critical = t.label in self.cfg.critical_labels
        interesting = (
            self.cfg.interesting_labels is None
            or t.label in self.cfg.interesting_labels
            or is_critical
        )
        is_new = t.id not in self._seen
        reactivated = self._was_stationary.get(t.id, False) and not t.stationary

        if not in_cooldown:
            if is_critical:
                return out(True, "critical_label")
        elif is_critical:
            return out(False, "cooldown")

        if not interesting:
            return out(False, "uninteresting_label")
        if self.cfg.stationary_suppress and t.stationary:
            return out(False, "stationary")
        if in_cooldown:
            return out(False, "cooldown")
        if is_new:
            return out(True, "new_track")
        if reactivated:
            return out(True, "reactivated")
        return out(True, "refresh")

    def evaluate(self, tracks: List[TrackView], now: float) -> GateResult:
        if self._last_heartbeat is None:
            self._last_heartbeat = now
        heartbeat_due = (
            self.cfg.heartbeat_s > 0 and (now - self._last_heartbeat) >= self.cfg.heartbeat_s
        )
        decisions: List[GateDecision] = []
        for t in tracks:
            d = self._decide(t, now, heartbeat_due)
            decisions.append(d)
            if d.escalate:
                self._last_escalate[t.id] = now
            self._seen.add(t.id)
            self._was_stationary[t.id] = t.stationary
        if heartbeat_due:
            self._last_heartbeat = now
        present = {t.id for t in tracks}
        gone = self._seen - present
        for tid in gone:
            self._last_escalate.pop(tid, None)
            self._was_stationary.pop(tid, None)
        self._seen &= present
        return GateResult(decisions=decisions, shadow=self.cfg.shadow)


class InferenceBudgetController:
    """Shrink the inference concurrency budget when the box can't keep up.

    Adapted from OpenNVR ``RegionBudgetController``: watch actual latency
    against the per-call budget and spend fewer concurrent slots when over
    it, recovering when there is room again. The failing level is remembered
    (slow-start threshold); growth back into it must be paid for in sustained
    evidence, more each time it fails again. Pure and clock-free (caller
    supplies latencies) -- no I/O, no threads.
    """

    def __init__(
        self,
        configured: int = DEFAULT_MAX_CONCURRENT,
        budget_s: float = 5.0,
        *,
        floor: int = 1,
        over_budget: float = 1.2,
        recover_at: float = 0.6,
        settle_frames: int = 5,
        smoothing: float = 0.3,
        probe_factor: int = 4,
        max_probe_backoff: int = 8,
        probation_frames: int = 20,
    ) -> None:
        self.configured = max(0, int(configured))
        self.budget_s = float(budget_s) if float(budget_s) > 0 else 5.0
        self.floor = max(1, int(floor))
        self.over_budget = float(over_budget)
        self.recover_at = float(recover_at)
        self.settle_frames = max(1, int(settle_frames))
        self.smoothing = float(smoothing)
        self.probe_factor = max(1, int(probe_factor))
        self.max_probe_backoff = max(1, int(max_probe_backoff))
        self.probation_frames = max(1, int(probation_frames))
        self.current = self.configured
        self._ema: Optional[float] = None
        self._streak = 0
        self._ceiling: Optional[int] = None
        self._ceiling_hits = 0
        self._held = 0

    @property
    def shedding(self) -> bool:
        return self.configured > 0 and self.current < self.configured

    def observe(self, latency_s: float, units_run: int = 1) -> int:
        if self.configured <= 0 or units_run <= 0:
            return 0
        try:
            latency_s = float(latency_s)
        except (TypeError, ValueError):
            return 0
        self._ema = (
            latency_s if self._ema is None
            else (self.smoothing * latency_s + (1 - self.smoothing) * self._ema)
        )
        over = self._ema > self.budget_s * self.over_budget
        under = self._ema < self.budget_s * self.recover_at
        self._serve_probation(units_run, over)
        if over and self.current > self.floor:
            self._streak = self._streak + 1 if self._streak >= 0 else 1
        elif under and self.current < self.configured:
            self._streak = self._streak - 1 if self._streak <= 0 else -1
        else:
            self._streak = 0
            return 0
        if abs(self._streak) < self._evidence_needed():
            return 0
        self._streak = 0
        before = self.current
        if over:
            if self._ceiling is not None and before >= self._ceiling:
                self._ceiling_hits += 1
            else:
                self._ceiling_hits = 1
            self._ceiling = before if self._ceiling is None else min(self._ceiling, before)
            self._held = 0
            self.current = max(self.floor, self.current // 2)
        else:
            self.current = min(self.configured, self.current + 1)
        return self.current - before

    def _serve_probation(self, units_run: int, over: bool) -> None:
        if self._ceiling is None or self.current < self._ceiling:
            self._held = 0
            return
        if units_run < self._ceiling or over:
            return
        self._held += 1
        if self._held >= self.probation_frames:
            self._ceiling = None
            self._ceiling_hits = 0
            self._held = 0

    def _evidence_needed(self) -> int:
        if self._streak > 0:
            return self.settle_frames
        if self._ceiling is None or self.current + 1 < self._ceiling:
            return self.settle_frames
        backoff = min(self._ceiling_hits, self.max_probe_backoff)
        return self.settle_frames * self.probe_factor * max(1, backoff)


class OncePerKey:
    """Bounded memory of (key, adapter) pairs already served."""

    def __init__(self, maxlen: int = 1024) -> None:
        self._seen: "OrderedDict[tuple, None]" = OrderedDict()
        self._maxlen = max(1, int(maxlen))

    def seen(self, key: Any, adapter: str) -> bool:
        k = (key, adapter)
        if k in self._seen:
            self._seen.move_to_end(k)
            return True
        return False

    def mark(self, key: Any, adapter: str) -> None:
        self._seen[(key, adapter)] = None
        if len(self._seen) > self._maxlen:
            self._seen.popitem(last=False)


# ---------------------------------------------------------------------------
# Global governor state
# ---------------------------------------------------------------------------

_lock = threading.RLock()

_CONFIG: Dict[str, Any] = {
    "mode": DEFAULT_MODE,
    "max_per_minute": int(DEFAULT_MAX_PER_MINUTE),
    "max_concurrent": int(DEFAULT_MAX_CONCURRENT),
    "window_s": float(DEFAULT_WINDOW_S),
    "cooldown_s": float(DEFAULT_COOLDOWN_S),
    "heartbeat_s": float(DEFAULT_HEARTBEAT_S),
    "per_camera_max": None,  # None == max_concurrent - 1 (min 1), like dispatch.py
    "trigger": TriggerPolicy.MOTION.value,
    "interesting_labels": None,
    "critical_labels": [],
    "always_analyze_cameras": [],
    "budget_s": 5.0,
}

_GATES: Dict[str, Gate] = {}
_BUDGET = InferenceBudgetController(configured=int(DEFAULT_MAX_CONCURRENT))
_WINDOW: deque = deque()  # timestamps (float) of admitted calls in window
_INFLIGHT = 0
_CAM_INFLIGHT: Dict[str, int] = {}
_LAST_ALLOW: Dict[str, float] = {}
_ONCE = OncePerKey()
_ALLOWED_TOTAL = 0
_REFUSED_TOTAL = 0
_SHADOW_WOULD_REFUSE_TOTAL = 0
_HISTORY: List[Dict[str, Any]] = []


def _now_ts() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


MODE_KEYS = ("mode", "gate_mode", "enforcement", "enforce_mode")
MAX_PM_KEYS = ("max_per_minute", "max_per_min", "per_minute", "rate_limit", "max_rate",
               "budget_per_minute", "max_calls_per_minute", "max_infers_per_minute")
MAX_CONC_KEYS = ("max_concurrent", "max_inflight", "concurrency", "max_parallel",
                 "max_inference_concurrency", "budget_concurrent")
WINDOW_KEYS = ("window_s", "window_seconds", "window", "period_seconds")
COOLDOWN_KEYS = ("cooldown", "cooldown_s", "cooldown_seconds", "escalate_cooldown_s",
                 "min_interval", "interval_seconds")
HEARTBEAT_KEYS = ("heartbeat_s", "heartbeat_seconds", "heartbeat", "max_latency_s")
PER_CAM_KEYS = ("per_camera_max", "per_camera", "per_camera_share", "camera_share",
                "max_per_camera", "per_cam_max")
TRIGGER_KEYS = ("trigger", "trigger_policy", "policy")
INTERESTING_KEYS = ("interesting_labels", "interesting", "allowed_labels", "labels")
CRITICAL_KEYS = ("critical_labels", "critical", "critical_classes", "priority_labels")
ALWAYS_KEYS = ("always_analyze", "always_analyze_cameras", "always", "bypass_cameras",
               "exempt_cameras")
CAMERA_KEYS = ("camera_id", "camera", "cam", "camera_name", "device_id", "source")
ADAPTER_KEYS = ("adapter", "adapter_name", "model", "model_id", "model_name", "task")
LABEL_KEYS = ("label", "class", "class_name", "object", "category")
KEY_KEYS = ("key", "track_id", "event_id", "request_id", "id", "dedupe_key")
STATIONARY_KEYS = ("stationary", "is_stationary", "settled", "parked")
HITS_KEYS = ("hits", "hit_count", "confirmations", "detections")
NOW_KEYS = ("now", "at", "when", "timestamp", "ts", "time")
LATENCY_KEYS = ("latency_s", "latency", "latency_ms", "duration_s", "elapsed_s")


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (bytes, bytearray)):
            try:
                data.setdefault("key", bytes(a).decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if data.get("camera_id") is None and data.get("camera") is None:
                data.setdefault("camera_id", s)
            elif data.get("adapter") is None:
                data.setdefault("adapter", s)
            else:
                data.setdefault("key", s)
        elif hasattr(a, "model_dump") and callable(getattr(a, "model_dump")):
            try:
                data.update(a.model_dump())
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a)
                         if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request", "data",
                       "body", "settings", "governor", "gate"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k, v in lower.items():
        data.setdefault(k, v)
    return data


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        lk = str(k).lower()
        if lk in lower and lower[lk] is not None:
            return lower[lk]
    return None


def _norm_mode(value: Any) -> str:
    s = str(value or DEFAULT_MODE).strip().lower()
    aliases = {"on": "enforce", "enabled": "enforce", "act": "enforce",
               "off": "off", "disabled": "off", "passthrough": "off",
               "shadow": "shadow", "measure": "shadow", "audit": "shadow",
               "dry_run": "shadow", "dry-run": "shadow", "enforce": "enforce"}
    return aliases.get(s, DEFAULT_MODE if s not in MODES else s)


def _norm_str_list(value: Any) -> Optional[list]:
    if value is None:
        return None
    if isinstance(value, str):
        items = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
        return items
    if isinstance(value, (list, tuple, set)):
        return [str(x) for x in value]
    return [str(value)]


def _per_camera_cap() -> int:
    with _lock:
        cap = _CONFIG.get("per_camera_max")
        if cap is not None:
            try:
                return max(1, int(cap))
            except (TypeError, ValueError):
                pass
        return max(1, int(_CONFIG.get("max_concurrent", DEFAULT_MAX_CONCURRENT)) - 1)


def _gate_for(camera: str) -> Gate:
    with _lock:
        mode = str(_CONFIG.get("mode", DEFAULT_MODE))
        shadow = mode != "enforce"  # shadow + off both non-enforcing; off short-circuits earlier
        if mode == "shadow":
            shadow = True
        trigger_raw = str(_CONFIG.get("trigger", TriggerPolicy.MOTION.value))
        try:
            trigger = TriggerPolicy(trigger_raw)
        except ValueError:
            trigger = TriggerPolicy.MOTION
        interesting = _CONFIG.get("interesting_labels")
        critical = _CONFIG.get("critical_labels") or []
        always_list = _CONFIG.get("always_analyze_cameras") or []
        cfg = GateConfig(
            shadow=shadow,
            trigger=trigger,
            interesting_labels=frozenset(interesting) if interesting else None,
            critical_labels=frozenset(critical),
            always_analyze=bool(camera in set(always_list)),
            escalate_cooldown_s=float(_CONFIG.get("cooldown_s", DEFAULT_COOLDOWN_S)),
            heartbeat_s=float(_CONFIG.get("heartbeat_s", DEFAULT_HEARTBEAT_S)),
        )
        g = _GATES.get(camera)
        if g is None:
            g = Gate(cfg)
            _GATES[camera] = g
        else:
            g.cfg = cfg
        return g


def _prune_window(now: float, window_s: float) -> None:
    cutoff = now - window_s
    while _WINDOW and _WINDOW[0] <= cutoff:
        _WINDOW.popleft()


def _audit_refused(camera: str, adapter: str, reason: str, shadow: bool) -> None:
    try:
        from app.services import audit as _audit

        et = getattr(_audit.AuditEventType, "INFERENCE_REFUSED_BUDGET", None)
        store = getattr(_audit, "audit_store", None) or getattr(_audit, "_default_store", None)
        if et is None or store is None:
            return
        store.emit(et, camera_id=camera, adapter=adapter,
                   details={"reason": reason, "shadow": bool(shadow)})
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def get_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        cfg = dict(_CONFIG)
        interesting = cfg.get("interesting_labels")
        critical = cfg.get("critical_labels") or []
        always_l = cfg.get("always_analyze_cameras") or []
    out: Dict[str, Any] = {
        "ok": True, "success": True,
        "mode": cfg.get("mode", DEFAULT_MODE),
        "shadow": bool(cfg.get("mode") != "enforce"),
        "enforcing": bool(cfg.get("mode") == "enforce"),
        "max_per_minute": int(cfg.get("max_per_minute", DEFAULT_MAX_PER_MINUTE)),
        "max_concurrent": int(cfg.get("max_concurrent", DEFAULT_MAX_CONCURRENT)),
        "per_camera_max": _per_camera_cap(),
        "window_s": float(cfg.get("window_s", DEFAULT_WINDOW_S)),
        "cooldown_s": float(cfg.get("cooldown_s", DEFAULT_COOLDOWN_S)),
        "heartbeat_s": float(cfg.get("heartbeat_s", DEFAULT_HEARTBEAT_S)),
        "trigger": cfg.get("trigger", TriggerPolicy.MOTION.value),
        "interesting_labels": list(interesting) if interesting else None,
        "critical_labels": list(critical),
        "always_analyze_cameras": list(always_l),
        "budget_s": float(cfg.get("budget_s", 5.0)),
        "current_concurrent": int(_BUDGET.current),
        "configured_concurrent": int(_BUDGET.configured),
    }
    out["max_per_min"] = out["max_per_minute"]
    out["rate_limit"] = out["max_per_minute"]
    return out


def configure(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    global _BUDGET
    data = _normalize_inputs(*args, **kwargs)
    with _lock:
        mode_raw = _first(data, MODE_KEYS)
        if mode_raw is not None:
            _CONFIG["mode"] = _norm_mode(mode_raw)
        # enabled/disabled convenience maps to enforce/shadow (never data-lossy off
        # unless explicitly asked via mode=off).
        if "enabled" in data and data["enabled"] is not None and mode_raw is None:
            try:
                _CONFIG["mode"] = "enforce" if bool(data["enabled"]) else "shadow"
            except Exception:  # noqa: BLE001
                pass
        v = _first(data, MAX_PM_KEYS)
        if v is not None:
            iv = _to_int(v)
            if iv is not None:
                _CONFIG["max_per_minute"] = max(0, iv)
        v = _first(data, MAX_CONC_KEYS)
        if v is not None:
            iv = _to_int(v)
            if iv is not None:
                _CONFIG["max_concurrent"] = max(0, iv)
                _BUDGET.configured = max(0, iv)
                _BUDGET.current = min(_BUDGET.current, max(0, iv)) if iv > 0 else _BUDGET.current
                if _BUDGET.current <= 0 and iv > 0:
                    _BUDGET.current = iv
        v = _first(data, WINDOW_KEYS)
        if v is not None:
            fv = _to_float(v)
            if fv is not None and fv > 0:
                _CONFIG["window_s"] = float(fv)
        v = _first(data, COOLDOWN_KEYS)
        if v is not None:
            fv = _to_float(v)
            if fv is not None:
                _CONFIG["cooldown_s"] = max(MIN_COOLDOWN_S, min(MAX_COOLDOWN_S, float(fv)))
        v = _first(data, HEARTBEAT_KEYS)
        if v is not None:
            fv = _to_float(v)
            if fv is not None:
                _CONFIG["heartbeat_s"] = max(0.0, float(fv))
        v = _first(data, PER_CAM_KEYS)
        if v is not None:
            iv = _to_int(v)
            _CONFIG["per_camera_max"] = max(1, iv) if iv is not None and iv > 0 else None
        v = _first(data, TRIGGER_KEYS)
        if v is not None:
            s = str(v).strip().lower()
            try:
                _CONFIG["trigger"] = TriggerPolicy(s).value
            except ValueError:
                pass
        v = _first(data, INTERESTING_KEYS)
        if v is not None:
            if isinstance(v, str) and v.strip().lower() in ("none", "null", "all", ""):
                _CONFIG["interesting_labels"] = None
            else:
                _CONFIG["interesting_labels"] = _norm_str_list(v)
        v = _first(data, CRITICAL_KEYS)
        if v is not None:
            _CONFIG["critical_labels"] = _norm_str_list(v) or []
        v = _first(data, ALWAYS_KEYS)
        if v is not None:
            _CONFIG["always_analyze_cameras"] = _norm_str_list(v) or []
        v = _first(data, ("budget_s", "budget_seconds", "latency_budget_s"))
        if v is not None:
            fv = _to_float(v)
            if fv is not None and fv > 0:
                _CONFIG["budget_s"] = float(fv)
                _BUDGET.budget_s = float(fv)
    return get_config()


def set_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return configure(*args, **kwargs)


def update_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return configure(*args, **kwargs)


def enable(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return configure({"mode": "enforce"})


def disable(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    # disable() parks in shadow (measure-only), never silently unbounded,
    # unless the caller explicitly asked for mode=off.
    if str(data.get("mode", "")).strip().lower() == "off":
        return configure({"mode": "off"})
    return configure({"mode": "shadow"})


def reset(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    global _INFLIGHT, _ALLOWED_TOTAL, _REFUSED_TOTAL, _SHADOW_WOULD_REFUSE_TOTAL, _ONCE
    data = _normalize_inputs(*args, **kwargs)
    camera = _first(data, CAMERA_KEYS)
    with _lock:
        if camera is not None and str(camera).strip():
            key = str(camera).strip()
            _GATES.pop(key, None)
            _CAM_INFLIGHT.pop(key, None)
            return {"ok": True, "success": True, "reset": True, "camera_id": key}
        _GATES.clear()
        _WINDOW.clear()
        _INFLIGHT = 0
        _CAM_INFLIGHT.clear()
        _LAST_ALLOW.clear()
        _ONCE = OncePerKey()
        _ALLOWED_TOTAL = 0
        _REFUSED_TOTAL = 0
        _SHADOW_WOULD_REFUSE_TOTAL = 0
        _HISTORY.clear()
        _BUDGET.current = _BUDGET.configured
        _BUDGET._ema = None
        _BUDGET._streak = 0
        _BUDGET._ceiling = None
        _BUDGET._ceiling_hits = 0
        _BUDGET._held = 0
        return {"ok": True, "success": True, "reset": True, "cleared": True}


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return reset(*args, **kwargs)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _resolve_call(data: Dict[str, Any]) -> Dict[str, Any]:
    camera = _first(data, CAMERA_KEYS)
    camera = str(camera).strip() if camera is not None and str(camera).strip() else "default"
    adapter = _first(data, ADAPTER_KEYS)
    adapter = str(adapter).strip() if adapter is not None and str(adapter).strip() else "default"
    label = _first(data, LABEL_KEYS)
    label = str(label).strip() if label is not None and str(label).strip() else "object"
    key = _first(data, KEY_KEYS)
    if key is None:
        key = f"{camera}:{adapter}:{label}"
    key = str(key)
    stationary = bool(_first(data, STATIONARY_KEYS) or False)
    hits_raw = _first(data, HITS_KEYS)
    try:
        hits = int(hits_raw) if hits_raw is not None else 1
    except (TypeError, ValueError):
        hits = 1
    now_raw = _first(data, NOW_KEYS)
    now = _to_float(now_raw) if now_raw is not None else None
    if now is None:
        now = _now_ts()
    return {"camera": camera, "adapter": adapter, "label": label, "key": key,
            "stationary": stationary, "hits": hits, "now": float(now)}


def evaluate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Pure admission check: would this inference call be allowed right now?

    No state is mutated. ``mode=off`` always allows; ``mode=shadow`` reports
    what *would* happen (``allowed=True`` always, ``would_refuse`` when the
    budget says no); ``mode=enforce`` reports the real verdict.
    """
    data = _normalize_inputs(*args, **kwargs)
    call = _resolve_call(data)
    camera, adapter, label = call["camera"], call["adapter"], call["label"]
    now = call["now"]
    with _lock:
        mode = str(_CONFIG.get("mode", DEFAULT_MODE))
        max_pm = int(_CONFIG.get("max_per_minute", DEFAULT_MAX_PER_MINUTE))
        window_s = float(_CONFIG.get("window_s", DEFAULT_WINDOW_S))
        max_conc = int(_BUDGET.current)
        per_cam = _per_camera_cap()
    if mode == "off":
        return {"ok": True, "success": True, "allowed": True, "refused": False,
                "would_refuse": False, "enforced": False, "shadow": False,
                "mode": mode, "reason": "disabled",
                "camera_id": camera, "adapter": adapter, "label": label, "key": call["key"]}
    gate = _gate_for(camera)
    track = TrackView(id=call["key"], label=label,
                      stationary=call["stationary"], hits=call["hits"])
    # Gate state mutation on a read-only evaluate would corrupt cooldown
    # accounting; snapshot the decision without recording it.
    with _lock:
        saved_seen = set(gate._seen)
        saved_last = dict(gate._last_escalate)
        saved_stat = dict(gate._was_stationary)
        saved_hb = gate._last_heartbeat
    try:
        gres = gate.evaluate([track], now)
    finally:
        with _lock:
            gate._seen = saved_seen
            gate._last_escalate = saved_last
            gate._was_stationary = saved_stat
            gate._last_heartbeat = saved_hb
    gdec = gres.decisions[0] if gres.decisions else None
    gate_allows = bool(gdec.escalate) if gdec else True
    gate_reason = gdec.reason if gdec else "refresh"

    with _lock:
        _prune_window(now, window_s)
        rate_full = max_pm > 0 and len(_WINDOW) >= max_pm
        conc_full = max_conc > 0 and _INFLIGHT >= max_conc
        cam_full = max_conc > 0 and _CAM_INFLIGHT.get(camera, 0) >= per_cam
        once_dup = _ONCE.seen(call["key"], adapter)
    reasons: List[str] = []
    if not gate_allows:
        reasons.append(f"gate:{gate_reason}")
    if once_dup:
        reasons.append("duplicate")
    if rate_full:
        reasons.append("rate_budget")
    if conc_full:
        reasons.append("concurrency_budget")
    if cam_full:
        reasons.append("camera_share")
    would_refuse = bool(reasons)
    reason = reasons[0] if reasons else (f"gate:{gate_reason}" if gdec else "ok")
    if mode == "shadow":
        return {"ok": True, "success": True, "allowed": True, "refused": False,
                "would_refuse": would_refuse, "enforced": False, "shadow": True,
                "mode": mode, "reason": reason, "gate_reason": gate_reason,
                "camera_id": camera, "adapter": adapter, "label": label, "key": call["key"]}
    # enforce
    allowed = not would_refuse
    return {"ok": True, "success": True, "allowed": allowed, "refused": not allowed,
            "would_refuse": not allowed, "enforced": True, "shadow": False,
            "mode": mode, "reason": reason if not allowed else (f"gate:{gate_reason}" if gdec else "ok"),
            "gate_reason": gate_reason,
            "camera_id": camera, "adapter": adapter, "label": label, "key": call["key"]}


def should_allow(*args: Any, **kwargs: Any) -> bool:
    try:
        return bool(evaluate(*args, **kwargs).get("allowed", False))
    except Exception:  # noqa: BLE001
        return True


def is_throttled(*args: Any, **kwargs: Any) -> bool:
    try:
        ev = evaluate(*args, **kwargs)
        return bool(ev.get("would_refuse", ev.get("refused", False)))
    except Exception:  # noqa: BLE001
        return False


def check(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return evaluate(*args, **kwargs)


def check_and_acquire(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Atomically check the budget and, if allowed, admit the call.

    On admission the sliding-window slot is consumed, the concurrency
    counters are bumped, the gate cooldown is recorded, and the once-per-key
    filter is marked. On refusal (enforce mode) the refusal counters bump and
    a best-effort ``inference.refused_budget`` audit event is emitted.
    In shadow mode the call is always admitted but ``would_refuse`` records
    what enforcement would have done.
    """
    global _INFLIGHT, _ALLOWED_TOTAL, _REFUSED_TOTAL, _SHADOW_WOULD_REFUSE_TOTAL
    data = _normalize_inputs(*args, **kwargs)
    call = _resolve_call(data)
    camera, adapter, label, key, now = (
        call["camera"], call["adapter"], call["label"], call["key"], call["now"])
    need_audit = False
    with _lock:
        mode = str(_CONFIG.get("mode", DEFAULT_MODE))
    if mode == "off":
        return {"ok": True, "success": True, "allowed": True, "refused": False,
                "would_refuse": False, "enforced": False, "shadow": False,
                "mode": mode, "reason": "disabled",
                "camera_id": camera, "adapter": adapter, "label": label, "key": key}
    gate = _gate_for(camera)
    track = TrackView(id=key, label=label,
                      stationary=call["stationary"], hits=call["hits"])
    with _lock:
        gres = gate.evaluate([track], now)
        gdec = gres.decisions[0] if gres.decisions else None
        gate_allows = bool(gdec.escalate) if gdec else True
        gate_reason = gdec.reason if gdec else "refresh"
        max_pm = int(_CONFIG.get("max_per_minute", DEFAULT_MAX_PER_MINUTE))
        window_s = float(_CONFIG.get("window_s", DEFAULT_WINDOW_S))
        max_conc = int(_BUDGET.current)
        per_cam = _per_camera_cap()
        _prune_window(now, window_s)
        rate_full = max_pm > 0 and len(_WINDOW) >= max_pm
        conc_full = max_conc > 0 and _INFLIGHT >= max_conc
        cam_full = max_conc > 0 and _CAM_INFLIGHT.get(camera, 0) >= per_cam
        once_dup = _ONCE.seen(key, adapter)
        reasons: List[str] = []
        if not gate_allows:
            reasons.append(f"gate:{gate_reason}")
        if once_dup:
            reasons.append("duplicate")
        if rate_full:
            reasons.append("rate_budget")
        if conc_full:
            reasons.append("concurrency_budget")
        if cam_full:
            reasons.append("camera_share")
        would_refuse = bool(reasons)
        reason = reasons[0] if reasons else f"gate:{gate_reason}"
        shadow = mode == "shadow"
        if shadow:
            # Admit, but audit what enforcement would have done.
            _WINDOW.append(now)
            _INFLIGHT += 1
            _CAM_INFLIGHT[camera] = _CAM_INFLIGHT.get(camera, 0) + 1
            _ALLOWED_TOTAL += 1
            if would_refuse:
                _SHADOW_WOULD_REFUSE_TOTAL += 1
            _record_history(True, camera, adapter, label, key, reason, shadow, now)
            return {"ok": True, "success": True, "allowed": True, "refused": False,
                    "would_refuse": would_refuse, "enforced": False, "shadow": True,
                    "mode": mode, "reason": reason, "gate_reason": gate_reason,
                    "camera_id": camera, "adapter": adapter, "label": label, "key": key}
        # enforce
        if would_refuse:
            _REFUSED_TOTAL += 1
            _record_history(False, camera, adapter, label, key, reason, False, now)
            need_audit = True
        else:
            _WINDOW.append(now)
            _INFLIGHT += 1
            _CAM_INFLIGHT[camera] = _CAM_INFLIGHT.get(camera, 0) + 1
            _LAST_ALLOW[key] = now
            _ONCE.mark(key, adapter)
            _ALLOWED_TOTAL += 1
            _record_history(True, camera, adapter, label, key, reason, False, now)
            return {"ok": True, "success": True, "allowed": True, "refused": False,
                    "would_refuse": False, "enforced": True, "shadow": False,
                    "mode": mode, "reason": reason, "gate_reason": gate_reason,
                    "camera_id": camera, "adapter": adapter, "label": label, "key": key}
    if need_audit:
        _audit_refused(camera, adapter, reason, False)
    return {"ok": True, "success": True, "allowed": False, "refused": True,
            "would_refuse": True, "enforced": True, "shadow": False,
            "mode": mode, "reason": reason, "gate_reason": gate_reason,
            "camera_id": camera, "adapter": adapter, "label": label, "key": key}


def release(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Release one concurrency slot previously admitted by ``check_and_acquire``."""
    global _INFLIGHT
    data = _normalize_inputs(*args, **kwargs)
    call = _resolve_call(data)
    camera = call["camera"]
    with _lock:
        if _INFLIGHT > 0:
            _INFLIGHT -= 1
        left = _CAM_INFLIGHT.get(camera, 0) - 1
        if left > 0:
            _CAM_INFLIGHT[camera] = left
        else:
            _CAM_INFLIGHT.pop(camera, None)
        return {"ok": True, "success": True, "released": True,
                "inflight": int(_INFLIGHT),
                "camera_inflight": int(_CAM_INFLIGHT.get(camera, 0)),
                "camera_id": camera, "key": call["key"]}


def observe(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Feed one inference latency sample to the adaptive budget controller.

    Returns the applied delta (0 = no change) plus the current budget.
    Idle samples (``units_run <= 0``) are not evidence either way.
    """
    data = _normalize_inputs(*args, **kwargs)
    lat = _first(data, LATENCY_KEYS)
    latency_s: Optional[float] = None
    if lat is not None:
        fv = _to_float(lat)
        if fv is not None:
            # Heuristic: values >= 1000 are almost certainly milliseconds.
            lk = None
            for k in LATENCY_KEYS:
                if k in data and data.get(k) is not None:
                    lk = k
                    break
            if lk in ("latency_ms",) or (fv >= 1000 and lk != "latency_s"):
                latency_s = fv / 1000.0
            else:
                latency_s = fv
    units_raw = _first(data, ("units_run", "regions_run", "units", "count", "n"))
    units = _to_int(units_raw) if units_raw is not None else 1
    if units is None:
        units = 1
    if latency_s is None:
        return {"ok": True, "success": True, "delta": 0, "current": int(_BUDGET.current),
                "configured": int(_BUDGET.configured), "shedding": bool(_BUDGET.shedding),
                "detail": "no latency sample"}
    with _lock:
        delta = _BUDGET.observe(float(latency_s), int(units))
        return {"ok": True, "success": True, "delta": int(delta),
                "current": int(_BUDGET.current), "configured": int(_BUDGET.configured),
                "shedding": bool(_BUDGET.shedding),
                "ceiling": _BUDGET._ceiling, "ema_s": _BUDGET._ema}


def _record_history(allowed: bool, camera: str, adapter: str, label: str,
                    key: str, reason: str, shadow: bool, now: float) -> None:
    _HISTORY.append({"allowed": bool(allowed), "refused": not allowed,
                     "camera_id": camera, "adapter": adapter, "label": label,
                     "key": key, "reason": reason, "shadow": bool(shadow),
                     "at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                     "ts": float(now)})
    while len(_HISTORY) > MAX_HISTORY:
        _HISTORY.pop(0)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    camera = _first(data, CAMERA_KEYS)
    camera = str(camera).strip() if camera is not None and str(camera).strip() else None
    with _lock:
        now = _now_ts()
        window_s = float(_CONFIG.get("window_s", DEFAULT_WINDOW_S))
        _prune_window(now, window_s)
        out: Dict[str, Any] = {
            "ok": True, "success": True,
            "mode": str(_CONFIG.get("mode", DEFAULT_MODE)),
            "enforcing": bool(str(_CONFIG.get("mode")) == "enforce"),
            "shadow": bool(str(_CONFIG.get("mode")) != "enforce"),
            "inflight": int(_INFLIGHT),
            "window_used": len(_WINDOW),
            "window_size_s": float(window_s),
            "max_per_minute": int(_CONFIG.get("max_per_minute", DEFAULT_MAX_PER_MINUTE)),
            "max_concurrent": int(_BUDGET.current),
            "configured_concurrent": int(_BUDGET.configured),
            "shedding": bool(_BUDGET.shedding),
            "per_camera_max": _per_camera_cap(),
            "per_camera_inflight": dict(_CAM_INFLIGHT),
            "allowed_total": int(_ALLOWED_TOTAL),
            "refused_total": int(_REFUSED_TOTAL),
            "shadow_would_refuse_total": int(_SHADOW_WOULD_REFUSE_TOTAL),
            "at": _now_iso(),
        }
        if camera is not None:
            out["camera_id"] = camera
            out["camera_inflight"] = int(_CAM_INFLIGHT.get(camera, 0))
    return out


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    status = get_status(*args, **kwargs)
    cfg = get_config()
    with _lock:
        hist = list(reversed(_HISTORY[-20:]))
    return {"ok": True, "success": True, "config": cfg, "status": status,
            "recent": hist, "recent_count": len(hist)}


def get_metrics(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        return {"ok": True, "success": True,
                "allowed_total": int(_ALLOWED_TOTAL),
                "refused_total": int(_REFUSED_TOTAL),
                "shadow_would_refuse_total": int(_SHADOW_WOULD_REFUSE_TOTAL),
                "inflight": int(_INFLIGHT),
                "window_used": len(_WINDOW),
                "current_concurrent": int(_BUDGET.current),
                "configured_concurrent": int(_BUDGET.configured),
                "shedding": bool(_BUDGET.shedding),
                "ceiling": _BUDGET._ceiling}


def get_history(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    data = _normalize_inputs(*args, **kwargs)
    limit = _to_int(data.get("limit", data.get("n", data.get("count"))))
    with _lock:
        hist = list(reversed(_HISTORY))
    if limit is not None and limit >= 0:
        hist = hist[: int(limit)]
    return hist


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------

class InferGovernorService:
    """Object-oriented facade over the module-level governor state."""

    def get_config(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_config(*a, **k)

    def configure(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return configure(*a, **k)

    def set_config(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return configure(*a, **k)

    def update_config(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return configure(*a, **k)

    def enable(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return enable(*a, **k)

    def disable(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return disable(*a, **k)

    def evaluate(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return evaluate(*a, **k)

    def check(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return check(*a, **k)

    def should_allow(self, *a: Any, **k: Any) -> bool:
        return should_allow(*a, **k)

    def is_throttled(self, *a: Any, **k: Any) -> bool:
        return is_throttled(*a, **k)

    def check_and_acquire(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return check_and_acquire(*a, **k)

    def acquire(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return check_and_acquire(*a, **k)

    def try_acquire(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return check_and_acquire(*a, **k)

    def release(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return release(*a, **k)

    def observe(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return observe(*a, **k)

    def get_status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_status(*a, **k)

    def get_summary(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_summary(*a, **k)

    def get_metrics(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_metrics(*a, **k)

    def get_history(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return get_history(*a, **k)

    def reset(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return reset(*a, **k)

    def clear(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return clear(*a, **k)


InferenceGovernorService = InferGovernorService
InferGovernor = InferGovernorService
GovernorService = InferGovernorService
InferenceGovernor = InferGovernorService

infer_governor_service = InferGovernorService()
governor_service = infer_governor_service
service = infer_governor_service
manager = infer_governor_service
governor = infer_governor_service


def get_service() -> InferGovernorService:
    return infer_governor_service


def create_service() -> InferGovernorService:
    return InferGovernorService()


# Flexible aliases.
allow = check_and_acquire
acquire = check_and_acquire
try_acquire = check_and_acquire
gate = check_and_acquire
admit = check_and_acquire
status = get_status
state = get_status
summary = get_summary
metrics = get_metrics
history = get_history
config = get_config
reset_all = reset
clear_all = clear


__all__ = [
    "TriggerPolicy", "GateConfig", "GateDecision", "GateResult", "TrackView",
    "Gate", "InferenceBudgetController", "OncePerKey",
    "MODES", "DEFAULT_MODE", "DEFAULT_MAX_PER_MINUTE", "DEFAULT_MAX_CONCURRENT",
    "get_config", "configure", "set_config", "update_config", "enable", "disable",
    "evaluate", "check", "should_allow", "is_throttled",
    "check_and_acquire", "allow", "acquire", "try_acquire", "gate", "admit",
    "release", "observe",
    "get_status", "status", "get_summary", "summary",
    "get_metrics", "metrics", "get_history", "history",
    "reset", "clear",
    "InferGovernorService", "infer_governor_service",
]
