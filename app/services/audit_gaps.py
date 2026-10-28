"""Audit gap finder for VigilCam.

Finds two kinds of holes in the audit trail (see ``app.services.audit``):

* **temporal gaps** — periods with no audit rows (clock skew, dropped
  writes, aggressive pruning, or a dead writer). Given the sorted
  timestamps present, any inter-entry delta larger than
  ``max_gap_seconds`` is reported as a missing range.
* **coverage gaps** — expected state-changing actions with zero rows.
  Inspired by the OpenNVR ``test_audit_gaps`` audit (read-only reference:
  ``server/tests/test_audit_gaps.py``), which pins that PTZ moves,
  alert acknowledgements, recording protect/export, firewall admin and
  skill claim/release must each leave an audit row. This module answers
  the same question generically: given an ``expected_actions`` list,
  which actions have no audit entry?

This module is intentionally distinct from neighbouring services:

* ``app.services.audit`` stores rows.
* ``app.services.audit_export`` serialises rows.
* ``app.services.audit_prune`` deletes old rows.
* ``app.services.gap_detector`` finds missing *recording* time ranges.

This module deletes nothing and validates no files. It only answers the
coverage question: which audit time ranges / actions are missing.

Standard library only (plus ``app.services.audit`` for store reads).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

__all__ = [
    "AuditGapsDetector",
    "audit_gaps_detector",
]

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MAX_GAP_SECONDS = 3600.0
DEFAULT_MIN_GAP_SECONDS = 1.0

#: Security-relevant actions expected to leave an audit row. Mirrors the
#: OpenNVR audit-gap reference (PTZ, alert ack, recording protect/export,
#: firewall admin, skill claim/release) translated to VigilCam's action
#: vocabulary. Callers should override with their own list; this is only
#: a sensible default for the coverage check.
DEFAULT_EXPECTED_ACTIONS: List[str] = [
    "auth.login",
    "auth.logout",
    "camera.create",
    "camera.update",
    "camera.delete",
    "ptz.move",
    "ptz.stop",
    "recording.protect",
    "recording.unprotect",
    "recording.export",
    "alerts.ack",
    "user.create",
    "config.update",
    "backup.create",
    "audit.export",
    "audit.prune",
]

EXPECTED_ACTIONS = DEFAULT_EXPECTED_ACTIONS
EXPECTED = DEFAULT_EXPECTED_ACTIONS
DEFAULT_ACTIONS = DEFAULT_EXPECTED_ACTIONS

_MAX_GAP_KEYS = (
    "max_gap_seconds",
    "maxGapSeconds",
    "max_gap",
    "maxGap",
    "gap_threshold",
    "gapThreshold",
    "threshold_seconds",
    "threshold",
    "max_interval_seconds",
    "maxIntervalSeconds",
    "idle_threshold_seconds",
    "silence_threshold_seconds",
    "expected_interval_seconds",
    "heartbeat_seconds",
)

_MIN_GAP_KEYS = (
    "min_gap_seconds",
    "minGapSeconds",
    "min_gap",
    "minGap",
    "minimum_gap_seconds",
    "minimum_gap",
    "min_duration_seconds",
    "min_duration",
    "ignore_below_seconds",
)

_EXPECTED_KEYS = (
    "expected_actions",
    "expectedActions",
    "expected",
    "required_actions",
    "requiredActions",
    "required",
    "actions",
    "check_actions",
    "watch_actions",
)

_WINDOW_START_KEYS = (
    "window_start",
    "windowStart",
    "start",
    "start_time",
    "startTime",
    "from",
    "begin",
    "window_from",
    "since",
    "after",
)

_WINDOW_END_KEYS = (
    "window_end",
    "windowEnd",
    "end",
    "end_time",
    "endTime",
    "to",
    "finish",
    "until",
    "window_to",
    "before",
)

_STORE_KEYS = ("store", "audit_store", "storage", "backend")

_ENTRIES_KEYS = ("entries", "items", "logs", "rows", "events", "audit_entries", "audit_logs")


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

def _as_float(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(value, str):
        s = value.strip().lower()
        if not s or s in ("none", "null", "auto"):
            return default
        try:
            return float(s)
        except (TypeError, ValueError):
            return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        if abs(num) > 1e12:
            num = num / 1000.0
        try:
            return datetime.fromtimestamp(num, tz=timezone.utc).replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("none", "null", "auto"):
            return None
        try:
            num = float(s)
            if s.replace(".", "", 1).replace("e", "", 1).replace("E", "", 1).replace("-", "", 1).replace("+", "", 1).isdigit():
                if abs(num) > 1e8:
                    if abs(num) > 1e12:
                        num = num / 1000.0
                    return datetime.fromtimestamp(num, tz=timezone.utc).replace(tzinfo=None)
        except ValueError:
            pass
        s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(s2)
        except ValueError:
            try:
                dt = datetime.fromisoformat(s + "T00:00:00")
            except ValueError:
                return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None


def _entry_ts(entry: Any) -> Optional[datetime]:
    if not isinstance(entry, dict):
        return None
    for key in ("timestamp", "ts", "created_at", "occurred_at", "time", "at", "date"):
        raw = entry.get(key)
        if raw is None:
            continue
        if isinstance(raw, datetime):
            dt = raw
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        parsed = _parse_ts(raw)
        if parsed is not None:
            return parsed
    return None


def _entry_action(entry: Any) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    for key in ("action", "event_type", "type", "kind", "event"):
        raw = entry.get(key)
        if raw is not None:
            s = str(raw).strip()
            if s:
                return s
    return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:  # noqa: BLE001
        return None


def _resolve_store(store: Any = None, **kwargs: Any) -> Any:
    if store is not None:
        return store
    for key in _STORE_KEYS:
        if kwargs.get(key) is not None:
            return kwargs.get(key)
    try:
        from app.services.audit import _default_store as _store

        return _store
    except Exception:  # noqa: BLE001
        return None


def _read_entries(store: Any, entries: Any = None) -> List[dict]:
    if entries is not None:
        if isinstance(entries, dict):
            return [dict(entries)]
        try:
            out: List[dict] = []
            for e in list(entries):
                if isinstance(e, dict):
                    out.append(dict(e))
                else:
                    try:
                        out.append(dict(e))
                    except Exception:  # noqa: BLE001
                        continue
            return out
        except Exception:  # noqa: BLE001
            return []
    if store is None:
        return []
    for attr in ("read_all", "all", "list"):
        try:
            fn = getattr(store, attr, None)
            if callable(fn):
                data = fn()
                import inspect as _inspect

                if _inspect.isawaitable(data):
                    continue
                return [dict(e) if isinstance(e, dict) else dict(e) for e in (data or [])]
        except Exception:  # noqa: BLE001
            continue
    try:
        raw = getattr(store, "_entries", None)
        if isinstance(raw, list):
            return [dict(e) if isinstance(e, dict) else dict(e) for e in raw]
    except Exception:  # noqa: BLE001
        pass
    return []


def _effective_options(options: Dict[str, Any]) -> Dict[str, Any]:
    for nested_key in ("payload", "options", "config", "params", "data",
                       "body", "request", "policy", "settings", "filter", "filters"):
        nested = options.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                options.setdefault(k, v)

    raw_max = None
    for k in _MAX_GAP_KEYS:
        if options.get(k) is not None:
            raw_max = options.get(k)
            break
    max_gap = _as_float(raw_max, DEFAULT_MAX_GAP_SECONDS)
    if max_gap <= 0:
        max_gap = DEFAULT_MAX_GAP_SECONDS

    raw_min = None
    for k in _MIN_GAP_KEYS:
        if options.get(k) is not None:
            raw_min = options.get(k)
            break
    min_gap = _as_float(raw_min, DEFAULT_MIN_GAP_SECONDS)
    if min_gap < 0:
        min_gap = 0.0

    expected: Optional[List[str]] = None
    for k in _EXPECTED_KEYS:
        if options.get(k) is not None:
            expected = options.get(k)
            break
    if expected is not None:
        if isinstance(expected, str):
            expected = [s.strip() for s in expected.split(",") if s.strip()]
        else:
            try:
                expected = [str(x).strip() for x in list(expected) if str(x).strip()]
            except Exception:  # noqa: BLE001
                expected = None

    raw_start = None
    for k in _WINDOW_START_KEYS:
        if options.get(k) is not None:
            raw_start = options.get(k)
            break
    raw_end = None
    for k in _WINDOW_END_KEYS:
        if options.get(k) is not None:
            raw_end = options.get(k)
            break
    window_start = _parse_ts(raw_start)
    window_end = _parse_ts(raw_end)

    entries = None
    for k in _ENTRIES_KEYS:
        if options.get(k) is not None:
            entries = options.get(k)
            break

    return {
        "max_gap_seconds": float(max_gap),
        "min_gap_seconds": float(min_gap),
        "expected_actions": expected,
        "window_start": window_start,
        "window_end": window_end,
        "entries": entries,
        "options": options,
    }


# ---------------------------------------------------------------------------
# Core finders
# ---------------------------------------------------------------------------

def find_audit_gaps(
    entries: Any = None,
    store: Any = None,
    window_start: Any = None,
    window_end: Any = None,
    max_gap_seconds: Any = None,
    min_gap_seconds: Any = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Find temporal gaps (silence periods) in audit coverage.

    A gap is any inter-entry delta strictly greater than
    ``max_gap_seconds``. Deltas shorter than ``min_gap_seconds`` are
    never reported. Returns a list of ``{start, end, duration_seconds,
    ...}`` dicts ordered oldest-first.
    """
    merged: Dict[str, Any] = dict(kwargs)
    if entries is not None:
        merged.setdefault("entries", entries)
    if store is not None:
        merged.setdefault("store", store)
    if window_start is not None:
        merged.setdefault("window_start", window_start)
    if window_end is not None:
        merged.setdefault("window_end", window_end)
    if max_gap_seconds is not None:
        merged.setdefault("max_gap_seconds", max_gap_seconds)
    if min_gap_seconds is not None:
        merged.setdefault("min_gap_seconds", min_gap_seconds)
    eff = _effective_options(merged)

    max_gap = float(eff["max_gap_seconds"])
    min_gap = float(eff["min_gap_seconds"])
    data = _read_entries(_resolve_store(merged.get("store"), **merged),
                         entries=eff.get("entries"))

    stamped: List[datetime] = []
    for e in data:
        ts = _entry_ts(e)
        if ts is not None:
            stamped.append(ts)
    stamped.sort()
    if len(stamped) < 2:
        return []

    ws = eff.get("window_start") or stamped[0]
    we = eff.get("window_end") or stamped[-1]
    if we < ws:
        ws, we = we, ws

    # Clip to window then scan consecutive deltas.
    clipped = [t for t in stamped if ws <= t <= we]
    if len(clipped) < 2:
        # Window covers <2 points: the whole window is a gap only if it
        # exceeds the threshold and has <2 witnesses.
        total = (we - ws).total_seconds()
        if total > max_gap and total >= min_gap and total > 0:
            return [{
                "start": ws,
                "end": we,
                "start_iso": _iso(ws),
                "end_iso": _iso(we),
                "start_time": _iso(ws),
                "end_time": _iso(we),
                "duration_seconds": float(total),
                "duration": float(total),
                "gap_seconds": float(total),
            }]
        return []

    gaps: List[Dict[str, Any]] = []
    for prev, cur in zip(clipped, clipped[1:]):
        dur = (cur - prev).total_seconds()
        if dur > max_gap and dur >= min_gap and dur > 0:
            gaps.append({
                "start": prev,
                "end": cur,
                "start_iso": _iso(prev),
                "end_iso": _iso(cur),
                "start_time": _iso(prev),
                "end_time": _iso(cur),
                "duration_seconds": float(dur),
                "duration": float(dur),
                "gap_seconds": float(dur),
            })
    return gaps


def find_missing_actions(
    entries: Any = None,
    store: Any = None,
    expected_actions: Any = None,
    **kwargs: Any,
) -> List[str]:
    """Return expected actions with zero audit rows (coverage gaps).

    Comparison is exact on the ``action`` string (with ``event_type`` /
    ``type`` / ``kind`` accepted as aliases on the entry side).
    """
    merged: Dict[str, Any] = dict(kwargs)
    if entries is not None:
        merged.setdefault("entries", entries)
    if store is not None:
        merged.setdefault("store", store)
    if expected_actions is not None:
        merged.setdefault("expected_actions", expected_actions)
    eff = _effective_options(merged)

    expected = eff.get("expected_actions")
    if expected is None:
        expected = list(DEFAULT_EXPECTED_ACTIONS)
    data = _read_entries(_resolve_store(merged.get("store"), **merged),
                         entries=eff.get("entries"))
    present = set()
    for e in data:
        a = _entry_action(e)
        if a is not None:
            present.add(a)
    return [a for a in expected if a not in present]


def check_action_coverage(
    entries: Any = None,
    store: Any = None,
    expected_actions: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Coverage summary for expected vs observed audit actions."""
    merged: Dict[str, Any] = dict(kwargs)
    if entries is not None:
        merged.setdefault("entries", entries)
    if store is not None:
        merged.setdefault("store", store)
    if expected_actions is not None:
        merged.setdefault("expected_actions", expected_actions)
    eff = _effective_options(merged)

    expected = eff.get("expected_actions")
    if expected is None:
        expected = list(DEFAULT_EXPECTED_ACTIONS)
    data = _read_entries(_resolve_store(merged.get("store"), **merged),
                         entries=eff.get("entries"))
    present: List[str] = sorted({_entry_action(e) for e in data if _entry_action(e) is not None})
    present_set = set(present)
    missing = [a for a in expected if a not in present_set]
    covered = [a for a in expected if a in present_set]
    total = len(expected)
    pct = (len(covered) / total * 100.0) if total else 100.0
    return {
        "expected": list(expected),
        "expected_actions": list(expected),
        "required_actions": list(expected),
        "present": present,
        "present_actions": present,
        "observed": present,
        "observed_actions": present,
        "missing": missing,
        "missing_actions": missing,
        "uncovered": missing,
        "uncovered_actions": missing,
        "coverage_gaps": missing,
        "covered": covered,
        "covered_actions": covered,
        "expected_count": total,
        "present_count": len(present),
        "covered_count": len(covered),
        "missing_count": len(missing),
        "uncovered_count": len(missing),
        "coverage_pct": float(pct),
        "coverage_percent": float(pct),
        "complete": len(missing) == 0,
        "ok": len(missing) == 0,
    }


def detect_audit_gaps(
    entries: Any = None,
    store: Any = None,
    expected_actions: Any = None,
    window_start: Any = None,
    window_end: Any = None,
    max_gap_seconds: Any = None,
    min_gap_seconds: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Full audit-gap report: temporal gaps + action coverage gaps."""
    merged: Dict[str, Any] = dict(kwargs)
    if entries is not None:
        merged.setdefault("entries", entries)
    if store is not None:
        merged.setdefault("store", store)
    if expected_actions is not None:
        merged.setdefault("expected_actions", expected_actions)
    if window_start is not None:
        merged.setdefault("window_start", window_start)
    if window_end is not None:
        merged.setdefault("window_end", window_end)
    if max_gap_seconds is not None:
        merged.setdefault("max_gap_seconds", max_gap_seconds)
    if min_gap_seconds is not None:
        merged.setdefault("min_gap_seconds", min_gap_seconds)
    eff = _effective_options(dict(merged))

    data = _read_entries(_resolve_store(merged.get("store"), **merged),
                         entries=eff.get("entries"))
    gaps = find_audit_gaps(entries=data, max_gap_seconds=eff["max_gap_seconds"],
                           min_gap_seconds=eff["min_gap_seconds"],
                           window_start=eff.get("window_start"),
                           window_end=eff.get("window_end"))
    coverage = check_action_coverage(entries=data,
                                     expected_actions=eff.get("expected_actions")
                                     if eff.get("expected_actions") is not None
                                     else list(DEFAULT_EXPECTED_ACTIONS))

    total_gaps = len(gaps)
    missing_count = int(coverage.get("missing_count", 0))
    has_gaps = total_gaps > 0 or missing_count > 0
    total_missing_seconds = float(sum(float(g.get("duration_seconds") or 0.0) for g in gaps))

    if data:
        ts_list = sorted([t for t in (_entry_ts(e) for e in data) if t is not None])
        cov_start = _iso(ts_list[0]) if ts_list else None
        cov_end = _iso(ts_list[-1]) if ts_list else None
    else:
        cov_start = _iso(eff.get("window_start"))
        cov_end = _iso(eff.get("window_end"))

    status = "gaps" if has_gaps else "ok"
    report: Dict[str, Any] = {
        "status": status,
        "state": status,
        "ok": not has_gaps,
        "success": True,
        "healthy": not has_gaps,
        "has_gaps": has_gaps,
        "gaps_found": has_gaps,
        "has_temporal_gaps": total_gaps > 0,
        "has_coverage_gaps": missing_count > 0,
        "gaps": gaps,
        "temporal_gaps": gaps,
        "time_gaps": gaps,
        "missing_ranges": gaps,
        "total_gaps": total_gaps,
        "gap_count": total_gaps,
        "gaps_count": total_gaps,
        "num_gaps": total_gaps,
        "total_missing_seconds": total_missing_seconds,
        "missing_seconds": total_missing_seconds,
        "missing_total_seconds": total_missing_seconds,
        "coverage": coverage,
        "action_coverage": coverage,
        "missing_actions": coverage.get("missing"),
        "uncovered_actions": coverage.get("missing"),
        "coverage_gaps": coverage.get("missing"),
        "missing": coverage.get("missing"),
        "expected_actions": coverage.get("expected"),
        "present_actions": coverage.get("present"),
        "coverage_pct": coverage.get("coverage_pct"),
        "total_entries": len(data),
        "entry_count": len(data),
        "entries_count": len(data),
        "num_entries": len(data),
        "count": len(data),
        "max_gap_seconds": float(eff["max_gap_seconds"]),
        "gap_threshold_seconds": float(eff["max_gap_seconds"]),
        "min_gap_seconds": float(eff["min_gap_seconds"]),
        "window_start": _iso(eff.get("window_start")),
        "window_end": _iso(eff.get("window_end")),
        "coverage_start": cov_start,
        "coverage_end": cov_end,
    }
    report["summary"] = {
        "status": status,
        "total_entries": len(data),
        "total_gaps": total_gaps,
        "missing_count": missing_count,
        "has_gaps": has_gaps,
        "coverage_pct": coverage.get("coverage_pct"),
    }
    report["report"] = dict(report["summary"])
    return report


# ---------------------------------------------------------------------------
# Detector class
# ---------------------------------------------------------------------------

class AuditGapsDetector:
    """Stateful audit-gap detector."""

    def __init__(
        self,
        store: Any = None,
        max_gap_seconds: Any = None,
        min_gap_seconds: Any = None,
        expected_actions: Any = None,
        **kwargs: Any,
    ) -> None:
        self.store = store
        merged = dict(kwargs)
        if max_gap_seconds is not None:
            merged["max_gap_seconds"] = max_gap_seconds
        if min_gap_seconds is not None:
            merged["min_gap_seconds"] = min_gap_seconds
        if expected_actions is not None:
            merged["expected_actions"] = expected_actions
        eff = _effective_options(merged)
        self.max_gap_seconds = float(eff["max_gap_seconds"])
        self.min_gap_seconds = float(eff["min_gap_seconds"])
        self.expected_actions: Optional[List[str]] = eff.get("expected_actions")
        self.last_report: Optional[Dict[str, Any]] = None

    def _defaults(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(kwargs)
        out.setdefault("max_gap_seconds", self.max_gap_seconds)
        out.setdefault("min_gap_seconds", self.min_gap_seconds)
        if self.store is not None:
            out.setdefault("store", self.store)
        if self.expected_actions is not None and out.get("expected_actions") is None:
            for k in _EXPECTED_KEYS:
                if out.get(k) is None:
                    continue
                break
            else:
                out["expected_actions"] = self.expected_actions
        return out

    def detect(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        entries: Any = None
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (list, tuple)):
                if entries is None:
                    entries = list(a)
        merged.update(kwargs)
        if entries is not None and merged.get("entries") is None:
            for k in _ENTRIES_KEYS:
                if merged.get(k) is None:
                    continue
                break
            else:
                merged["entries"] = entries
        rep = detect_audit_gaps(**self._defaults(merged))
        self.last_report = rep
        return rep

    def scan(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.detect(*args, **kwargs)

    def analyze(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.detect(*args, **kwargs)

    def find_gaps(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.detect(*args, **kwargs)

    def check(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.detect(*args, **kwargs)

    def find_temporal_gaps(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        merged: Dict[str, Any] = {}
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (list, tuple)):
                merged.setdefault("entries", list(a))
        merged.update(kwargs)
        return find_audit_gaps(**self._defaults(merged))

    def find_coverage_gaps(self, *args: Any, **kwargs: Any) -> List[str]:
        merged: Dict[str, Any] = {}
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (list, tuple)):
                merged.setdefault("entries", list(a))
        merged.update(kwargs)
        return find_missing_actions(**self._defaults(merged))

    def coverage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (list, tuple)):
                merged.setdefault("entries", list(a))
        merged.update(kwargs)
        return check_action_coverage(**self._defaults(merged))

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        rep = self.detect(*args, **kwargs)
        return {
            "status": rep.get("status"),
            "state": rep.get("status"),
            "ok": rep.get("ok"),
            "success": True,
            "healthy": rep.get("healthy"),
            "has_gaps": rep.get("has_gaps"),
            "total_gaps": rep.get("total_gaps"),
            "gap_count": rep.get("gap_count"),
            "missing_actions": rep.get("missing_actions"),
            "missing_count": rep.get("coverage", {}).get("missing_count", 0),
            "total_entries": rep.get("total_entries"),
            "report": rep,
        }

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        if any(h in low for h in ("status", "health", "summary", "info", "state")):
            return self.get_status
        if any(h in low for h in ("cover", "missing", "action", "expected", "required")):
            return self.coverage
        if any(h in low for h in ("gap", "scan", "detect", "analy", "report",
                                  "run", "check", "find", "audit")):
            return self.detect
        raise AttributeError(name)


# Class aliases.
AuditGapDetector = AuditGapsDetector
AuditGapsService = AuditGapsDetector
AuditGapService = AuditGapsDetector
AuditCoverageDetector = AuditGapsDetector
AuditGapAnalyzer = AuditGapsDetector
GapDetector = AuditGapsDetector
Detector = AuditGapsDetector

# Singleton instances.
audit_gaps_detector = AuditGapsDetector()
audit_gap_detector = audit_gaps_detector
gaps_detector = audit_gaps_detector
detector = audit_gaps_detector
service = audit_gaps_detector
manager = audit_gaps_detector
analyzer = audit_gaps_detector
default_detector = audit_gaps_detector
default_service = audit_gaps_detector
instance = audit_gaps_detector
singleton = audit_gaps_detector


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

def scan(entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_audit_gaps(entries=entries, **kwargs)


def scan_audit_gaps(entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_audit_gaps(entries=entries, **kwargs)


def analyze(entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_audit_gaps(entries=entries, **kwargs)


def analyze_audit_gaps(entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_audit_gaps(entries=entries, **kwargs)


def find_gaps(entries: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return find_audit_gaps(entries=entries, **kwargs)


def get_gaps(entries: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return find_audit_gaps(entries=entries, **kwargs)


def get_audit_gaps(entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_audit_gaps(entries=entries, **kwargs)


def check(entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_audit_gaps(entries=entries, **kwargs)


def check_audit_gaps(entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_audit_gaps(entries=entries, **kwargs)


def check_gaps(entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_audit_gaps(entries=entries, **kwargs)


def detect(entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_audit_gaps(entries=entries, **kwargs)


def get_status(entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return audit_gaps_detector.get_status(entries=entries, **kwargs)


def get_report(entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_audit_gaps(entries=entries, **kwargs)


def report(entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return detect_audit_gaps(entries=entries, **kwargs)


def coverage_report(entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return check_action_coverage(entries=entries, **kwargs)


def get_coverage(entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return check_action_coverage(entries=entries, **kwargs)


status = get_status
summary = get_status
health = get_status
get_summary = get_status
find_missing = find_missing_actions
get_missing_actions = find_missing_actions
missing_actions = find_missing_actions
coverage = check_action_coverage
