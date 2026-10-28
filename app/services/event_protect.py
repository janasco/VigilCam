"""Event-driven recording protection for flagged events.

Verified ground truth (``app/services/rec_protect.py`` +
``app/api/rec_protect.py``):

* ``protect_segment`` creates a **manual, permanent** lock record with keys
  ``path/segment/file/protected/locked/lock/reason/note/protected_at/
  created_at`` -- no ``expires_at`` / ``ttl`` / event concept.
* ``is_protected`` / ``exclude_protected`` perform no event lookup.
* The only removal paths are manual ``unprotect_segment`` / ``clear``.
* ``app.services.cameras.record_event`` never imports or calls
  ``rec_protect`` -- event creation does not protect segments.
* ``app/api/events.py`` has no segment / protection handling.

This module layers **opt-in, event-driven auto-protection** on top without
touching ``rec_protect``, ``app/main.py`` or ``app/templates/index.html``:

* :func:`is_flagged` -- alias-rich flagged-event detector (explicit flag
  fields, ``tags`` markers, or acknowledged / escalated operator state).
* :func:`auto_protect_event` -- explicit protect for one flagged event:
  discover overlapping segments via ``event_segments`` (or protect an
  explicitly supplied segment list) and lock each via ``rec_protect``.
* :func:`maybe_auto_protect_event` -- toggle-gated wrapper used by the
  ``record_event`` hook; no-op when disabled unless ``force=True``.
* :func:`backfill_flagged` / :func:`protect_flagged_events` -- explicit bulk
  protect for existing flagged events (works even when the toggle is off).
* In-memory toggle (default **disabled** to preserve manual-only behaviour)
  with ``enable`` / ``disable`` / ``set_enabled`` / ``is_enabled`` and
  window / recording-dir / reason configuration.
* Best-effort hook into ``app.services.cameras.record_event`` (installed on
  import). When the toggle is **enabled**, newly created flagged events are
  auto-protected; when disabled (default) creation is untouched.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from app.core.config import RECORDING_DIR as _DEFAULT_RECORDING_DIR  # type: ignore
except Exception:  # noqa: BLE001
    _DEFAULT_RECORDING_DIR = Path("data/recordings")  # type: ignore

DEFAULT_WINDOW_BEFORE = 30.0
DEFAULT_WINDOW_AFTER = 30.0

_lock = threading.RLock()
_ENABLED: bool = False
_CONFIG: Dict[str, Any] = {
    "window_before": DEFAULT_WINDOW_BEFORE,
    "window_after": DEFAULT_WINDOW_AFTER,
    "recording_dir": None,
    "storage_root": None,
    "limit": None,
    "reason": None,
    "protect_acknowledged": True,
    "protect_escalated": True,
}
_STATS: Dict[str, Any] = {
    "protected_total": 0,
    "events_processed": 0,
    "flagged_events": 0,
    "last_event_id": None,
    "last_protected": 0,
    "last_run_at": None,
    "last_error": None,
}

_ORIGINAL_RECORD_EVENT: Any = None
_HOOK_INSTALLED: bool = False

FLAG_TAGS = frozenset(
    {
        "flagged",
        "flag",
        "flagged_event",
        "important",
        "starred",
        "star",
        "pinned",
        "pin",
        "protect",
        "protected",
        "hold",
        "preserve",
        "preserved",
        "evidence",
        "incident",
        "case",
        "locked",
        "retain",
        "retained",
        "keep",
        "critical",
        "urgent",
        "review",
        "needs_review",
        "bookmark",
        "bookmarked",
        "favorite",
        "favourite",
    }
)

FLAG_BOOL_KEYS = (
    "flagged",
    "flag",
    "is_flagged",
    "isFlagged",
    "flagged_event",
    "flaggedEvent",
    "important",
    "is_important",
    "starred",
    "is_starred",
    "star",
    "pinned",
    "is_pinned",
    "pin",
    "protect",
    "protected",
    "should_protect",
    "auto_protect",
    "autoProtect",
    "hold",
    "preserve",
    "preserved",
    "evidence",
    "is_evidence",
    "incident",
    "is_incident",
    "case",
    "locked",
    "retain",
    "retained",
    "keep",
    "critical",
    "urgent",
    "needs_review",
    "needsReview",
    "requires_review",
    "review",
    "needs_protection",
    "bookmarked",
    "favorite",
    "favourite",
)

TAG_KEYS = (
    "tags",
    "tag",
    "labels",
    "label_list",
    "flags",
    "markers",
    "categories",
)

EVENT_ID_KEYS = (
    "event_id",
    "eventId",
    "event",
    "eventid",
    "id_event",
    "for_event",
    "camera_event_id",
    "id",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "enabled", "enable", "active", "auto", "flagged", "flag"}:
        return True
    if s in {"0", "false", "no", "n", "off", "disabled", "disable", "inactive", "none", "null", ""}:
        return False
    return default


def _coerce_bool_tri(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "enabled", "enable", "active", "flagged", "flag", "important",
             "starred", "pinned", "protect", "protected", "hold", "preserve", "evidence", "incident"}:
        return True
    if s in {"0", "false", "no", "n", "off", "disabled", "disable", "inactive"}:
        return False
    # Non-empty truthy strings like "case-42" are not flags by themselves.
    return None


def _coerce_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return default


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default


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
        for nested_key in (
            "payload", "options", "config", "params", "data", "body",
            "request", "fields", "entry", "event", "protection",
            "event_protect", "auto_protect",
        ):
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    try:
        return dict(data)
    except Exception:  # noqa: BLE001
        return {}


def _normalize_kwargs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    for a in args:
        if a is None:
            continue
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(a, bool):
            flat.setdefault("flagged", a)
        elif isinstance(a, (int, float)) and "event_id" not in flat:
            flat.setdefault("event_id", a)
        elif isinstance(a, str):
            s = a.strip()
            if s.lower() in {"enable", "enabled", "on", "true", "disable", "disabled", "off", "false"}:
                flat.setdefault("enabled", s)
    flat.update(kwargs or {})
    for nested_key in (
        "payload", "options", "config", "params", "data", "body",
        "request", "fields", "event", "protection",
    ):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    return flat


def _first_present(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _event_to_dict(ev: Any) -> Dict[str, Any]:
    if ev is None:
        return {}
    if isinstance(ev, dict):
        return dict(ev)
    if hasattr(ev, "model_dump"):
        try:
            return dict(ev.model_dump())
        except Exception:  # noqa: BLE001
            pass
    out: Dict[str, Any] = {}
    for attr in (
        "id", "event_id", "eventId", "camera_id", "cameraId", "camera",
        "camera_name", "cameraName", "kind", "label", "confidence", "payload",
        "tags", "tag", "flagged", "flag", "important", "starred", "pinned",
        "protected", "created_at", "createdAt", "timestamp", "time", "event_time",
    ):
        try:
            val = getattr(ev, attr, None)
        except Exception:  # noqa: BLE001
            val = None
        if val is not None:
            out[attr] = val
    # payload may carry flag markers
    try:
        raw_payload = getattr(ev, "payload", None)
    except Exception:  # noqa: BLE001
        raw_payload = None
    if isinstance(raw_payload, dict):
        for k, v in raw_payload.items():
            out.setdefault(k, v)
    if not out:
        try:
            return dict(ev)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return {"value": ev}
    return out


def _extract_event_id(*args: Any, **kwargs: Any) -> Optional[int]:
    flat = _normalize_kwargs(*args, **kwargs)
    for a in args:
        if isinstance(a, dict) and any(k in a for k in ("id", "event_id", "eventId")):
            cand = a.get("id", a.get("event_id", a.get("eventId")))
            v = _coerce_int(cand, None)
            if v is not None:
                return v
        elif hasattr(a, "id"):
            try:
                v = _coerce_int(getattr(a, "id", None), None)
                if v is not None:
                    return v
            except Exception:  # noqa: BLE001
                pass
    raw = _first_present(flat, EVENT_ID_KEYS)
    if isinstance(raw, dict):
        raw = raw.get("id", raw.get("event_id"))
    v = _coerce_int(raw, None)
    if v is not None:
        return v
    for a in args:
        if isinstance(a, (int, float)) and not isinstance(a, bool):
            return _coerce_int(a, None)
    return None


def _tags_to_set(value: Any) -> set:
    out: set = set()
    if value is None:
        return out
    if isinstance(value, dict):
        for key in ("tags", "tag", "values", "items", "labels", "names", "flags"):
            if value.get(key) is not None:
                out |= _tags_to_set(value.get(key))
        return out
    items: List[Any] = []
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return out
        # JSON list?
        if s.startswith("["):
            try:
                import json as _json

                parsed = _json.loads(s)
                if isinstance(parsed, list):
                    items = parsed
                else:
                    items = [parsed]
            except Exception:  # noqa: BLE001
                items = [p.strip() for p in s.split(",")]
        else:
            # split on comma/semicolon/space
            import re as _re

            items = [p for p in _re.split(r"[,;\s]+", s) if p]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    for item in items:
        if item is None:
            continue
        t = str(item).strip().lower()
        if t:
            out.add(t)
    return out


def is_flagged(event: Any = None, *args: Any, **kwargs: Any) -> bool:
    """Return True when the event is flagged for protection.

    Flagged means any of:

    * an explicit truthy flag field (``flagged`` / ``flag`` / ``important`` /
      ``starred`` / ``pinned`` / ``protect`` / ``hold`` / ``evidence`` /
      ``incident`` / ... plus camelCase aliases),
    * ``tags`` (list / CSV / JSON) containing a flag token such as
      ``flagged`` / ``important`` / ``starred`` / ``evidence`` / ``hold``,
    * acknowledged / escalated operator state (via ``event_ack`` store) when
      ``protect_acknowledged`` / ``protect_escalated`` config is on.
    """
    flat = _normalize_kwargs(*args, **kwargs)
    inline: Dict[str, Any] = {}
    if event is not None:
        if isinstance(event, dict):
            inline = dict(event)
        elif hasattr(event, "model_dump"):
            try:
                inline = dict(event.model_dump())
            except Exception:  # noqa: BLE001
                inline = _event_to_dict(event)
        elif isinstance(event, (int, float)) and not isinstance(event, bool):
            inline = {"id": _coerce_int(event, None), "event_id": _coerce_int(event, None)}
        else:
            inline = _event_to_dict(event)
    for a in args:
        if isinstance(a, dict):
            for k, v in a.items():
                inline.setdefault(k, v)
    merged: Dict[str, Any] = dict(inline)
    for k, v in flat.items():
        merged.setdefault(k, v)
    # Inline payload dict may carry flags.
    payload = merged.get("payload")
    if isinstance(payload, dict):
        for k, v in payload.items():
            merged.setdefault(k, v)
    elif isinstance(payload, str):
        try:
            import json as _json

            parsed = _json.loads(payload)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    merged.setdefault(k, v)
        except Exception:  # noqa: BLE001
            pass

    # 1. Explicit boolean-ish flag fields.
    for key in FLAG_BOOL_KEYS:
        if key in merged and merged.get(key) is not None:
            tri = _coerce_bool_tri(merged.get(key))
            if tri is True:
                return True
    # case-insensitive fallback for flag keys
    lower = {str(k).lower(): v for k, v in merged.items()}
    for key in FLAG_BOOL_KEYS:
        lk = key.lower()
        if lk in lower and lower[lk] is not None and key not in merged:
            if _coerce_bool_tri(lower[lk]) is True:
                return True

    # 2. Tags / labels markers.
    tag_vals: List[Any] = []
    for key in TAG_KEYS:
        if merged.get(key) is not None:
            tag_vals.append(merged.get(key))
    # single "flag" string tag passed as plain string?
    tags_union: set = set()
    for tv in tag_vals:
        tags_union |= _tags_to_set(tv)
    if tags_union & set(FLAG_TAGS):
        return True

    # 3. Acknowledged / escalated operator state counts as flagged.
    eid = _coerce_int(merged.get("id", merged.get("event_id", merged.get("eventId"))), None)
    if eid is None:
        eid = _extract_event_id(event, *args, **kwargs)
    with _lock:
        protect_ack = bool(_CONFIG.get("protect_acknowledged", True))
        protect_esc = bool(_CONFIG.get("protect_escalated", True))
    if eid is not None and (protect_ack or protect_esc):
        try:
            from app.services import event_ack as _ack

            if protect_ack:
                try:
                    if _ack.is_acknowledged(int(eid)):
                        return True
                except Exception:  # noqa: BLE001
                    pass
            if protect_esc:
                try:
                    if _ack.is_escalated(int(eid)):
                        return True
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    return False


def _resolve_windows(flat: Dict[str, Any]) -> tuple:
    with _lock:
        cfg_b = _CONFIG.get("window_before", DEFAULT_WINDOW_BEFORE)
        cfg_a = _CONFIG.get("window_after", DEFAULT_WINDOW_AFTER)
        cfg_dir = _CONFIG.get("recording_dir")
        cfg_lim = _CONFIG.get("limit")
        cfg_storage = _CONFIG.get("storage_root")
        cfg_reason = _CONFIG.get("reason")
    b_raw = _first_present(flat, ("window_before", "windowBefore", "before", "pre_roll", "preRoll", "preroll"))
    a_raw = _first_present(flat, ("window_after", "windowAfter", "after", "post_roll", "postRoll", "postroll"))
    both = _first_present(flat, ("window", "window_seconds", "windowSeconds", "padding", "tolerance"))
    b = _coerce_float(b_raw, None) if b_raw is not None else None
    a = _coerce_float(a_raw, None) if a_raw is not None else None
    if b is None and both is not None:
        b = _coerce_float(both, None)
    if a is None and both is not None:
        a = _coerce_float(both, None)
    if b is None:
        b = float(cfg_b)
    if a is None:
        a = float(cfg_a)
    rec = _first_present(
        flat,
        ("recording_dir", "recordingDir", "recordings_dir", "recordingsDir",
         "recording_root", "scan_path", "directory", "dir"),
    )
    if rec is None:
        rec = cfg_dir
    storage = _first_present(flat, ("storage_root", "storageRoot", "storage_dir"))
    if storage is None:
        storage = cfg_storage
    lim_raw = _first_present(flat, ("limit", "n", "count", "max_segments", "maxSegments", "size"))
    lim = _coerce_int(lim_raw, None) if lim_raw is not None else cfg_lim
    reason_raw = _first_present(flat, ("reason", "note", "notes", "label", "comment", "description", "why", "lock_reason"))
    if reason_raw is None:
        reason_raw = cfg_reason
    return float(b), float(a), rec, lim, storage, reason_raw


def _default_reason(eid: Any, inline: Optional[Dict[str, Any]] = None) -> str:
    label = None
    if isinstance(inline, dict):
        label = inline.get("label", inline.get("kind", inline.get("tag")))
    if label:
        return f"flagged-event-{eid} ({label})"
    return f"flagged-event-{eid}"


def _record_stats(event_id: Any, n_protected: int, flagged: bool, error: Any = None) -> None:
    with _lock:
        _STATS["events_processed"] = int(_STATS.get("events_processed", 0) or 0) + 1
        if flagged:
            _STATS["flagged_events"] = int(_STATS.get("flagged_events", 0) or 0) + 1
        _STATS["protected_total"] = int(_STATS.get("protected_total", 0) or 0) + int(n_protected or 0)
        _STATS["last_event_id"] = event_id
        _STATS["last_protected"] = int(n_protected or 0)
        _STATS["last_run_at"] = _now_iso()
        _STATS["last_error"] = str(error) if error is not None else None


def _extract_explicit_segments(flat: Dict[str, Any]) -> List[str]:
    for key in ("segments", "segment_paths", "segmentPaths", "paths", "files",
                "selection", "selected", "candidates", "items", "segment_list"):
        raw = flat.get(key)
        if raw is None:
            continue
        if isinstance(raw, (str, Path)):
            return [str(raw)]
        if isinstance(raw, (list, tuple, set)):
            out = [str(v) for v in raw if v is not None and str(v).strip()]
            if out:
                return out
    for key in ("segment", "segment_path", "segmentPath", "path", "file_path",
                "filePath", "filepath", "file", "recording_path", "video_path", "clip"):
        if flat.get(key) is not None:
            v = flat.get(key)
            if isinstance(v, dict):
                for k in ("segment_path", "path", "file_path", "recording_path", "file"):
                    if v.get(k):
                        return [str(v.get(k))]
                continue
            return [str(v)]
    return []


def auto_protect_event(
    *args: Any,
    event: Any = None,
    event_id: Any = None,
    db: Any = None,
    session: Any = None,
    force: Any = None,
    require_flagged: Any = None,
    only_flagged: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Protect segments overlapping one flagged event (explicit request).

    Discovers overlapping segments via ``event_segments`` (or protects an
    explicitly supplied ``segment`` / ``segments`` list) and locks each via
    ``rec_protect.protect_segment``.

    Unflagged events are skipped unless ``force=True`` (or
    ``require_flagged=False``). The enabled toggle does NOT gate this
    explicit entrypoint (see :func:`maybe_auto_protect_event` for the
    toggle-gated hook path).
    """
    flat = _normalize_kwargs(*args, **kwargs)
    inline: Optional[Dict[str, Any]] = None
    for a in args:
        if isinstance(a, dict) and any(
            k in a for k in ("camera_id", "camera_name", "created_at", "timestamp", "camera", "id", "flagged", "tags")
        ):
            inline = dict(a)
            break
        elif hasattr(a, "model_dump"):
            try:
                d = dict(a.model_dump())
                if any(k in d for k in ("camera_id", "created_at", "id", "flagged")):
                    inline = d
                    break
            except Exception:  # noqa: BLE001
                pass
        elif not isinstance(a, (int, float, str, bool, Path)) and hasattr(a, "id"):
            inline = _event_to_dict(a)
            break
    if event is not None:
        if isinstance(event, dict):
            inline = dict(event)
        elif hasattr(event, "model_dump"):
            try:
                inline = dict(event.model_dump())
            except Exception:  # noqa: BLE001
                inline = _event_to_dict(event)
        else:
            inline = _event_to_dict(event)
    for key in ("event", "source", "source_event", "original"):
        if isinstance(flat.get(key), dict):
            inline = dict(flat.get(key))
            break
    eid = event_id
    if eid is None:
        eid = _extract_event_id(*args, **kwargs)
    if eid is None and inline is not None:
        eid = _coerce_int(inline.get("id", inline.get("event_id", inline.get("eventId"))), None)
    if inline is None and eid is not None:
        inline = {"id": eid, "event_id": eid}
        for k in ("camera_id", "cameraId", "camera", "camera_name", "cameraName",
                  "created_at", "createdAt", "timestamp", "time", "event_time",
                  "flagged", "flag", "tags", "label", "kind"):
            if flat.get(k) is not None:
                inline[k] = flat.get(k)
    if inline is None:
        cand = {k: flat.get(k) for k in (
            "camera_id", "cameraId", "camera", "camera_name", "cameraName",
            "created_at", "createdAt", "timestamp", "time", "event_time",
            "flagged", "flag", "tags", "label", "kind",
        ) if flat.get(k) is not None}
        if cand and eid is not None:
            inline = {"id": eid, "event_id": eid, **cand}
    # Flag gate.
    if require_flagged is None:
        require_flagged = flat.get("require_flagged", flat.get("requireFlagged", None))
    if require_flagged is None:
        only = flat.get("only_flagged", flat.get("onlyFlagged", flat.get("flagged_only", None)))
        require_flagged = only if only is not None else True
    force_flag = _coerce_bool(force if force is not None else flat.get("force", flat.get("force_protect", False)), False)
    check_dict: Dict[str, Any] = dict(inline or {})
    for k, v in flat.items():
        check_dict.setdefault(k, v)
    flagged = is_flagged(check_dict)
    if eid is None and inline is None:
        return {
            "success": False, "ok": False, "flagged": flagged,
            "event_id": None, "discovered": 0, "protected": 0,
            "protections": [], "segments": [],
            "error": "event_id is required",
        }
    if require_flagged and _coerce_bool(require_flagged, True) and not flagged and not force_flag:
        return {
            "success": True, "ok": True, "flagged": False,
            "event_id": eid, "discovered": 0, "protected": 0,
            "already_protected": 0, "protections": [], "segments": [],
            "skipped": True, "reason": "event not flagged",
        }
    if inline is None:
        inline = {"id": eid, "event_id": eid}
    inline = dict(inline)
    inline.setdefault("id", eid)
    inline.setdefault("event_id", eid)
    before, after, rec_dir, lim, storage_root, reason_raw = _resolve_windows(flat)
    reason = str(reason_raw) if reason_raw is not None else _default_reason(eid, inline)

    # Explicit segment list wins over discovery.
    explicit = _extract_explicit_segments(flat)
    # Also accept positional string path second arg.
    if not explicit and args:
        for a in args:
            if isinstance(a, (str, Path)) and str(a).strip():
                s = str(a).strip()
                if "/" in s or s.lower().endswith((".mp4", ".mkv", ".ts", ".mov", ".m4v")):
                    explicit.append(s)
                    break
    discovered: List[Dict[str, Any]] = []
    if explicit:
        for seg_path in explicit:
            discovered.append({
                "path": seg_path, "segment_path": seg_path,
                "file_path": seg_path, "recording_path": seg_path,
                "event_id": eid,
            })
    else:
        try:
            from app.services import event_segments as _seg
        except Exception as exc:  # noqa: BLE001
            _record_stats(eid, 0, flagged, exc)
            return {"success": False, "ok": False, "flagged": flagged, "event_id": eid,
                    "discovered": 0, "protected": 0, "protections": [], "segments": [],
                    "error": str(exc)}
        discover_kwargs: Dict[str, Any] = {"window_before": before, "window_after": after}
        if rec_dir is not None:
            discover_kwargs["recording_dir"] = rec_dir
        if lim is not None:
            discover_kwargs["limit"] = lim
        for k in ("camera_id", "cameraId", "camera", "camera_name", "cameraName",
                  "created_at", "createdAt", "timestamp", "time", "event_time"):
            if k not in inline and flat.get(k) is not None:
                discover_kwargs[k] = flat.get(k)
        try:
            discovered = _seg.discover_segments_for_event(inline, **discover_kwargs) or []
        except Exception as exc:  # noqa: BLE001
            _record_stats(eid, 0, flagged, exc)
            return {"success": False, "ok": False, "flagged": flagged, "event_id": eid,
                    "discovered": 0, "protected": 0, "protections": [], "segments": [],
                    "error": str(exc)}
    if lim is not None and lim >= 0:
        discovered = discovered[: int(lim)]
    try:
        import app.services.rec_protect as _rp
    except Exception as exc:  # noqa: BLE001
        _record_stats(eid, 0, flagged, exc)
        return {"success": False, "ok": False, "flagged": flagged, "event_id": eid,
                "discovered": len(discovered), "protected": 0, "protections": [],
                "segments": discovered, "error": str(exc)}
    protections: List[Dict[str, Any]] = []
    already = 0
    errors: List[str] = []
    for seg in discovered or []:
        if isinstance(seg, dict):
            seg_path = (seg.get("segment_path") or seg.get("path") or seg.get("file_path")
                        or seg.get("recording_path") or seg.get("file"))
        else:
            seg_path = str(seg)
        if not seg_path:
            rid = seg.get("recording_id", seg.get("recordingId", seg.get("segment_id"))) if isinstance(seg, dict) else None
            if rid is None:
                errors.append("segment without path")
                continue
            seg_path = rid
        try:
            was = _rp.is_protected(str(seg_path), storage_root=storage_root)
            rec = _rp.protect_segment(str(seg_path), reason=reason, storage_root=storage_root)
            protections.append(rec)
            if was:
                already += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
    _record_stats(eid, len(protections), flagged)
    return {
        "success": True, "ok": True, "flagged": bool(flagged),
        "event_id": int(eid) if eid is not None else None,
        "discovered": len(discovered or []),
        "protected": len(protections),
        "already_protected": int(already),
        "newly_protected": len(protections) - int(already),
        "protections": protections, "segments": protections or discovered,
        "protected_paths": [str(p.get("path")) for p in protections if isinstance(p, dict)],
        "reason": reason,
        "window_before": before, "window_after": after,
        "errors": errors,
    }


async def auto_protect_event_async(
    *args: Any,
    event: Any = None,
    event_id: Any = None,
    db: Any = None,
    session: Any = None,
    force: Any = None,
    require_flagged: Any = None,
    only_flagged: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    sess = db if db is not None else session
    flat = _normalize_kwargs(*args, **kwargs)
    eid = event_id if event_id is not None else _extract_event_id(*args, **kwargs)
    inline: Optional[Dict[str, Any]] = None
    if event is not None:
        inline = _event_to_dict(event) if not isinstance(event, dict) else dict(event)
    else:
        for a in args:
            if isinstance(a, dict) and any(k in a for k in ("camera_id", "created_at", "timestamp", "camera", "flagged", "tags")):
                inline = dict(a)
                break
    if inline is None and eid is not None and sess is not None:
        try:
            from app.models.entities import Event as _Event  # lazy

            row = await sess.get(_Event, int(eid))
            if row is not None:
                inline = _event_to_dict(row)
        except Exception:  # noqa: BLE001
            inline = None
    if sess is None:
        return auto_protect_event(*args, event=event, event_id=event_id, force=force,
                                  require_flagged=require_flagged, only_flagged=only_flagged, **kwargs)
    if inline is None and eid is None:
        return {"success": False, "ok": False, "flagged": False, "event_id": None,
                "discovered": 0, "protected": 0, "protections": [], "error": "event_id is required"}
    if inline is None:
        inline = {"id": eid, "event_id": eid}
        for k in ("camera_id", "camera", "camera_name", "created_at", "timestamp", "time", "flagged", "tags"):
            if flat.get(k) is not None:
                inline[k] = flat.get(k)
    # Flag gate (same as sync).
    if require_flagged is None:
        require_flagged = flat.get("require_flagged", flat.get("requireFlagged", None))
    if require_flagged is None:
        only = only_flagged if only_flagged is not None else flat.get("only_flagged", flat.get("onlyFlagged", None))
        require_flagged = only if only is not None else True
    force_flag = _coerce_bool(force if force is not None else flat.get("force", False), False)
    check_dict = dict(inline or {})
    for k, v in flat.items():
        check_dict.setdefault(k, v)
    flagged = is_flagged(check_dict)
    if require_flagged and _coerce_bool(require_flagged, True) and not flagged and not force_flag:
        return {"success": True, "ok": True, "flagged": False, "event_id": eid,
                "discovered": 0, "protected": 0, "protections": [], "skipped": True,
                "reason": "event not flagged"}
    before, after, rec_dir, lim, storage_root, reason_raw = _resolve_windows(flat)
    reason = str(reason_raw) if reason_raw is not None else _default_reason(eid, inline)
    explicit = _extract_explicit_segments(flat)
    discovered: List[Dict[str, Any]] = []
    if explicit:
        for seg_path in explicit:
            discovered.append({"path": seg_path, "segment_path": seg_path, "event_id": eid})
    else:
        try:
            from app.services import event_segments as _seg

            dk: Dict[str, Any] = {"window_before": before, "window_after": after}
            if rec_dir is not None:
                dk["recording_dir"] = rec_dir
            if lim is not None:
                dk["limit"] = lim
            discovered = await _seg.get_segments_for_event_async(inline, db=sess, **dk) or []
        except Exception:  # noqa: BLE001
            discovered = []
            try:
                from app.services import event_segments as _seg2

                discovered = _seg2.discover_segments_for_event(inline, **{
                    "window_before": before, "window_after": after, **({"recording_dir": rec_dir} if rec_dir else {}),
                }) or []
            except Exception:  # noqa: BLE001
                discovered = []
    if lim is not None and lim >= 0:
        discovered = discovered[: int(lim)]
    try:
        import app.services.rec_protect as _rp
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "ok": False, "flagged": flagged, "event_id": eid,
                "discovered": len(discovered), "protected": 0, "error": str(exc)}
    protections: List[Dict[str, Any]] = []
    already = 0
    for seg in discovered or []:
        seg_path = (seg.get("segment_path") or seg.get("path") or seg.get("file_path")
                    or seg.get("recording_path")) if isinstance(seg, dict) else str(seg)
        if not seg_path:
            continue
        try:
            was = _rp.is_protected(str(seg_path), storage_root=storage_root)
            rec = _rp.protect_segment(str(seg_path), reason=reason, storage_root=storage_root)
            protections.append(rec)
            if was:
                already += 1
        except Exception:  # noqa: BLE001
            pass
    _record_stats(eid, len(protections), flagged)
    return {"success": True, "ok": True, "flagged": bool(flagged), "event_id": eid,
            "discovered": len(discovered or []), "protected": len(protections),
            "already_protected": int(already), "protections": protections,
            "segments": protections or discovered,
            "protected_paths": [str(p.get("path")) for p in protections if isinstance(p, dict)],
            "reason": reason}


def maybe_auto_protect_event(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Toggle-gated auto-protect (hook path).

    No-op when disabled unless ``force=True``; otherwise delegates to
    :func:`auto_protect_event` (which still skips unflagged events).
    """
    flat = _normalize_kwargs(*args, **kwargs)
    force = flat.get("force", flat.get("force_protect", None))
    force_flag = _coerce_bool(force, False) if force is not None else False
    if not is_enabled() and not force_flag:
        eid = _extract_event_id(*args, **kwargs)
        return {"success": True, "ok": True, "enabled": False, "flagged": None,
                "event_id": eid, "discovered": 0, "protected": 0,
                "protections": [], "segments": [], "skipped": True,
                "reason": "auto-protect disabled"}
    out = auto_protect_event(*args, **kwargs)
    out.setdefault("enabled", True)
    return out


async def maybe_auto_protect_event_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    flat = _normalize_kwargs(*args, **kwargs)
    force = flat.get("force", flat.get("force_protect", None))
    force_flag = _coerce_bool(force, False) if force is not None else False
    if not is_enabled() and not force_flag:
        eid = _extract_event_id(*args, **kwargs)
        return {"success": True, "ok": True, "enabled": False, "event_id": eid,
                "discovered": 0, "protected": 0, "protections": [], "skipped": True,
                "reason": "auto-protect disabled"}
    return await auto_protect_event_async(*args, db=db, session=session, **kwargs)


def backfill_flagged(
    *args: Any,
    db: Any = None,
    session: Any = None,
    events: Any = None,
    event_ids: Any = None,
    ids: Any = None,
    limit: Any = None,
    dry_run: Any = None,
    only_flagged: Any = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Explicit bulk protect for flagged events (ignores toggle).

    Accepts in-memory ``events`` dicts and/or bare ``event_ids`` (with
    camera / timestamp / flag hints in kwargs). ``only_flagged=True``
    (default) skips unflagged events; ``force=True`` protects regardless.
    ``dry_run=True`` discovers without locking.
    """
    flat = _normalize_kwargs(*args, **kwargs)
    if events is None:
        events = flat.get("events", flat.get("event_list", flat.get("items")))
    if events is None:
        single = flat.get("event")
        if isinstance(single, dict):
            events = [single]
    if event_ids is None:
        event_ids = flat.get("event_ids", flat.get("ids", flat.get("eventIds")))
    if ids is not None and event_ids is None:
        event_ids = ids
    if event_ids is None and args:
        for a in args:
            if isinstance(a, (list, tuple)):
                event_ids = a
                break
    dry = _coerce_bool(dry_run if dry_run is not None else flat.get("dry_run", flat.get("dryRun", flat.get("preview"))), False)
    lim = _coerce_int(limit if limit is not None else flat.get("limit", flat.get("n")), None)
    if only_flagged is None:
        only_flagged = flat.get("only_flagged", flat.get("onlyFlagged", True))
    force_flag = _coerce_bool(flat.get("force", flat.get("force_protect", False)), False)
    targets: List[Any] = []
    if isinstance(events, list):
        targets.extend(events)
    elif events is not None and not isinstance(events, list):
        targets = [events]
    id_list: List[int] = []
    if event_ids is not None:
        iterable = list(event_ids) if isinstance(event_ids, (list, tuple, set)) else [event_ids]
        for v in iterable:
            if isinstance(v, str) and "," in v:
                for part in v.split(","):
                    vv = _coerce_int(part.strip(), None)
                    if vv is not None:
                        id_list.append(vv)
            else:
                vv = _coerce_int(v, None)
                if vv is not None:
                    id_list.append(vv)
    for a in args:
        if isinstance(a, (int, float)) and not isinstance(a, bool):
            vv = _coerce_int(a, None)
            if vv is not None and vv not in id_list and (lim is None or vv != lim):
                if not targets:
                    id_list.append(vv)
    if lim is not None and lim >= 0:
        if targets:
            targets = targets[:lim]
        if id_list:
            id_list = id_list[:lim]
    _passthrough = {k: v for k, v in flat.items()
                    if k not in ("events", "event_list", "items", "event",
                                 "event_ids", "ids", "eventIds", "event_id", "eventId", "id")}
    _passthrough.update(kwargs)
    total_discovered = 0
    total_protected = 0
    total_flagged = 0
    total_skipped = 0
    per_event: List[Dict[str, Any]] = []
    # Dict targets: honour flagged filter via auto_protect_event itself.
    synth_hints = {k: flat.get(k) for k in (
        "camera_id", "cameraId", "camera", "camera_name", "cameraName",
        "created_at", "createdAt", "timestamp", "time", "event_time",
        "recording_dir", "recordingDir", "storage_root", "reason",
        "flagged", "flag", "tags", "window_before", "window_after",
    ) if flat.get(k) is not None}
    for ev in targets:
        ev_dict = ev if isinstance(ev, dict) else _event_to_dict(ev)
        merged_hints = dict(synth_hints)
        merged_hints.update(_passthrough)
        if dry:
            flagged = is_flagged(ev_dict, **merged_hints)
            if _coerce_bool(only_flagged, True) and not flagged and not force_flag:
                total_skipped += 1
                per_event.append({"event_id": _coerce_int(ev_dict.get("id", ev_dict.get("event_id")), None),
                                  "flagged": False, "discovered": 0, "protected": 0, "skipped": True, "dry_run": True})
                continue
            try:
                from app.services import event_segments as _seg

                before, after, rec_dir, _lim, _sr, _rs = _resolve_windows(merged_hints)
                dk: Dict[str, Any] = {"window_before": before, "window_after": after}
                if rec_dir is not None:
                    dk["recording_dir"] = rec_dir
                found = _seg.discover_segments_for_event(ev_dict, **dk)
                total_discovered += len(found or [])
                total_flagged += 1 if flagged else 0
                per_event.append({"event_id": _coerce_int(ev_dict.get("id", ev_dict.get("event_id")), None),
                                  "flagged": flagged, "discovered": len(found or []),
                                  "protected": 0, "dry_run": True})
            except Exception as exc:  # noqa: BLE001
                per_event.append({"error": str(exc), "dry_run": True})
            continue
        res = auto_protect_event(ev_dict, force=force_flag if force_flag else None,
                                 require_flagged=only_flagged, **merged_hints)
        total_discovered += int(res.get("discovered", 0) or 0)
        total_protected += int(res.get("protected", 0) or 0)
        if res.get("flagged"):
            total_flagged += 1
        if res.get("skipped"):
            total_skipped += 1
        per_event.append(res)
    for eid in id_list:
        synth: Dict[str, Any] = {"id": eid, "event_id": eid}
        for k, v in synth_hints.items():
            synth.setdefault(k, v)
        if dry:
            flagged = is_flagged(synth)
            if _coerce_bool(only_flagged, True) and not flagged and not force_flag:
                total_skipped += 1
                per_event.append({"event_id": eid, "flagged": False, "discovered": 0,
                                  "protected": 0, "skipped": True, "dry_run": True})
                continue
            per_event.append({"event_id": eid, "flagged": flagged, "discovered": 0,
                              "protected": 0, "dry_run": True})
            continue
        res = auto_protect_event(synth, force=force_flag if force_flag else None,
                                 require_flagged=only_flagged, **synth_hints, **_passthrough)
        total_discovered += int(res.get("discovered", 0) or 0)
        total_protected += int(res.get("protected", 0) or 0)
        if res.get("flagged"):
            total_flagged += 1
        if res.get("skipped"):
            total_skipped += 1
        per_event.append(res)
    with _lock:
        _STATS["last_run_at"] = _now_iso()
    return {
        "success": True, "ok": True,
        "enabled": bool(_ENABLED),
        "dry_run": bool(dry),
        "only_flagged": bool(_coerce_bool(only_flagged, True)),
        "events_processed": len(per_event),
        "events": len(per_event),
        "flagged": total_flagged,
        "flagged_count": total_flagged,
        "discovered": total_discovered,
        "protected": total_protected,
        "protected_total": total_protected,
        "protected_count": total_protected,
        "skipped": total_skipped,
        "skipped_count": total_skipped,
        "per_event": per_event,
        "results": per_event,
    }


async def backfill_flagged_async(
    *args: Any, db: Any = None, session: Any = None, **kwargs: Any,
) -> Dict[str, Any]:
    sess = db if db is not None else session
    if sess is None:
        return backfill_flagged(*args, **kwargs)
    flat = _normalize_kwargs(*args, **kwargs)
    events = flat.get("events", flat.get("event_list", flat.get("items")))
    single = flat.get("event")
    if events is None and isinstance(single, dict):
        events = [single]
    event_ids = flat.get("event_ids", flat.get("ids", flat.get("eventIds")))
    dry = _coerce_bool(flat.get("dry_run", flat.get("dryRun", False)), False)
    lim = _coerce_int(flat.get("limit", flat.get("n")), 500)
    only_flagged = flat.get("only_flagged", flat.get("onlyFlagged", True))
    force_flag = _coerce_bool(flat.get("force", False), False)
    # Explicit in-memory events win over DB enumeration.
    has_explicit = events is not None and (isinstance(events, list) and len(events) > 0 or not isinstance(events, list))
    if has_explicit:
        seq = events if isinstance(events, list) else [events]
        if lim is not None and lim >= 0:
            seq = seq[:lim]
        per_event: List[Dict[str, Any]] = []
        total_discovered = 0
        total_protected = 0
        total_flagged = 0
        for ev in seq:
            ev_dict = ev if isinstance(ev, dict) else _event_to_dict(ev)
            eid = _coerce_int(ev_dict.get("id", ev_dict.get("event_id")), None)
            if dry:
                flagged = is_flagged(ev_dict)
                per_event.append({"event_id": eid, "flagged": flagged, "discovered": 0, "protected": 0, "dry_run": True})
                if flagged:
                    total_flagged += 1
                continue
            res = await auto_protect_event_async(ev_dict, event_id=eid, db=sess,
                                                 force=force_flag if force_flag else None,
                                                 require_flagged=only_flagged, **kwargs)
            total_discovered += int(res.get("discovered", 0) or 0)
            total_protected += int(res.get("protected", 0) or 0)
            if res.get("flagged"):
                total_flagged += 1
            per_event.append(res)
        return {"success": True, "ok": True, "enabled": bool(_ENABLED), "dry_run": bool(dry),
                "events_processed": len(per_event), "flagged": total_flagged,
                "discovered": total_discovered, "protected": total_protected,
                "per_event": per_event, "results": per_event}
    # DB enumeration path: recent events, filter to flagged.
    try:
        from sqlalchemy import select as _select  # lazy

        from app.models.entities import Event as _Event  # lazy

        stmt = _select(_Event).order_by(_Event.id.desc())
        cam_raw = flat.get("camera_id", flat.get("cameraId", flat.get("camera")))
        try:
            cam_id = _coerce_int(cam_raw, None) if cam_raw is not None else None
        except Exception:  # noqa: BLE001
            cam_id = None
        if cam_id is not None:
            try:
                stmt = stmt.where(_Event.camera_id == int(cam_id))
            except (TypeError, ValueError):
                pass
        qlim = lim if (lim is not None and lim >= 0) else 500
        stmt = stmt.limit(int(qlim) + 1 if int(qlim) >= 0 else 500)
        result = await sess.execute(stmt)
        rows = list(result.scalars().all())
        if lim is not None and lim >= 0:
            rows = rows[:lim]
        wanted: Optional[set] = None
        if event_ids is not None:
            wanted = set()
            seq_ids = event_ids if isinstance(event_ids, (list, tuple, set)) else [event_ids]
            for v in seq_ids:
                vv = _coerce_int(v, None)
                if vv is not None:
                    wanted.add(int(vv))
            rows = [r for r in rows if int(getattr(r, "id", -1)) in wanted]
        per_event = []
        total_discovered = 0
        total_protected = 0
        total_flagged = 0
        for row in rows:
            eid = int(getattr(row, "id"))
            ev_dict = _event_to_dict(row)
            # Merge caller-supplied flag hints (e.g. tags) so callers can force-flag DB rows.
            for k in ("flagged", "flag", "tags", "tag", "important", "starred", "pinned"):
                if flat.get(k) is not None:
                    ev_dict.setdefault(k, flat.get(k))
            if dry:
                flagged = is_flagged(ev_dict)
                per_event.append({"event_id": eid, "flagged": flagged, "discovered": 0, "protected": 0, "dry_run": True})
                if flagged:
                    total_flagged += 1
                continue
            res = await auto_protect_event_async(ev_dict, event_id=eid, db=sess,
                                                 force=force_flag if force_flag else None,
                                                 require_flagged=only_flagged, **kwargs)
            total_discovered += int(res.get("discovered", 0) or 0)
            total_protected += int(res.get("protected", 0) or 0)
            if res.get("flagged"):
                total_flagged += 1
            per_event.append(res)
        return {"success": True, "ok": True, "enabled": bool(_ENABLED), "dry_run": bool(dry),
                "events_processed": len(per_event), "flagged": total_flagged,
                "discovered": total_discovered, "protected": total_protected,
                "per_event": per_event, "results": per_event}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "ok": False, "enabled": bool(_ENABLED),
                "events_processed": 0, "discovered": 0, "protected": 0,
                "per_event": [], "error": str(exc)}


# ------------------------------------------------------------------ #
# Toggle + config
# ------------------------------------------------------------------ #

def is_enabled(*args: Any, **kwargs: Any) -> bool:
    with _lock:
        return bool(_ENABLED)


def get_enabled(*args: Any, **kwargs: Any) -> bool:
    return is_enabled(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        return {
            "enabled": bool(_ENABLED),
            "auto_protect": bool(_ENABLED),
            "autoprotect": bool(_ENABLED),
            "active": bool(_ENABLED),
            "config": dict(_CONFIG),
            "window_before": float(_CONFIG.get("window_before", DEFAULT_WINDOW_BEFORE)),
            "window_after": float(_CONFIG.get("window_after", DEFAULT_WINDOW_AFTER)),
            "recording_dir": _CONFIG.get("recording_dir"),
            "storage_root": _CONFIG.get("storage_root"),
            "limit": _CONFIG.get("limit"),
            "stats": dict(_STATS),
            "ok": True,
            "success": True,
        }


def get_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        return dict(_CONFIG)


def get_settings(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return get_status(*args, **kwargs)


def set_enabled(value: Any = True, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    global _ENABLED
    flat = _normalize_kwargs(*args, **kwargs)
    raw = value
    if isinstance(value, dict):
        flat2 = _flatten(value)
        for k, v in flat2.items():
            flat.setdefault(k, v)
        raw = _first_present(
            flat2,
            ("enabled", "enable", "auto_protect", "autoProtect", "autoprotect",
             "active", "is_enabled", "on", "value", "state"),
        )
        if raw is None:
            raw = True
    elif flat:
        alt = _first_present(
            flat,
            ("enabled", "enable", "auto_protect", "autoProtect", "autoprotect",
             "active", "is_enabled", "on", "value", "state"),
        )
        if alt is not None:
            raw = alt
    if isinstance(raw, str) and raw.strip().lower() in {"toggle", "switch", "flip"}:
        with _lock:
            new_val = not bool(_ENABLED)
            _ENABLED = new_val
            return get_status()
    new_val = _coerce_bool(raw, True)
    with _lock:
        _ENABLED = bool(new_val)
        return get_status()


def enable(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return set_enabled(True, *args, **kwargs)


def disable(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return set_enabled(False, *args, **kwargs)


def toggle(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    global _ENABLED
    flat = _normalize_kwargs(*args, **kwargs)
    explicit = _first_present(
        flat,
        ("enabled", "enable", "auto_protect", "autoProtect", "autoprotect",
         "active", "is_enabled", "on", "value", "state"),
    )
    if explicit is not None:
        return set_enabled(explicit)
    with _lock:
        _ENABLED = not bool(_ENABLED)
        return get_status()


def set_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    global _ENABLED
    flat = _normalize_kwargs(*args, **kwargs)
    with _lock:
        if "window_before" in flat or "windowBefore" in flat or "before" in flat:
            raw = _first_present(flat, ("window_before", "windowBefore", "before", "pre_roll", "preroll"))
            v = _coerce_float(raw, None)
            if v is not None and v >= 0:
                _CONFIG["window_before"] = float(v)
        if "window_after" in flat or "windowAfter" in flat or "after" in flat:
            raw = _first_present(flat, ("window_after", "windowAfter", "after", "post_roll", "postroll"))
            v = _coerce_float(raw, None)
            if v is not None and v >= 0:
                _CONFIG["window_after"] = float(v)
        both = _first_present(flat, ("window", "window_seconds", "windowSeconds", "padding", "tolerance"))
        if both is not None:
            v = _coerce_float(both, None)
            if v is not None and v >= 0:
                _CONFIG["window_before"] = float(v)
                _CONFIG["window_after"] = float(v)
        rec = _first_present(
            flat,
            ("recording_dir", "recordingDir", "recordings_dir", "recordingsDir",
             "recording_root", "storage_root", "root", "directory", "dir", "folder"),
        )
        if rec is not None:
            _CONFIG["recording_dir"] = str(rec)
        stor = _first_present(flat, ("storage_root", "storageRoot", "storage_dir"))
        if stor is not None:
            _CONFIG["storage_root"] = str(stor)
        lim = _first_present(flat, ("limit", "n", "count", "max_segments", "maxSegments"))
        if lim is not None:
            _CONFIG["limit"] = _coerce_int(lim, None)
        rsn = _first_present(flat, ("reason", "default_reason", "reason_template"))
        if rsn is not None:
            _CONFIG["reason"] = str(rsn)
        for key in ("protect_acknowledged", "protectAcknowledged", "protect_acked", "ack_protect"):
            if flat.get(key) is not None:
                _CONFIG["protect_acknowledged"] = _coerce_bool(flat.get(key), True)
                break
        for key in ("protect_escalated", "protectEscalated", "escalated_protect"):
            if flat.get(key) is not None:
                _CONFIG["protect_escalated"] = _coerce_bool(flat.get(key), True)
                break
        tog = _first_present(
            flat,
            ("enabled", "enable", "auto_protect", "autoProtect", "autoprotect", "active", "on"),
        )
        if tog is not None:
            _ENABLED = _coerce_bool(tog, bool(_ENABLED))
        return get_status()


def configure(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return set_config(*args, **kwargs)


def update_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return set_config(*args, **kwargs)


def get_stats(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        return dict(_STATS)


def reset_stats(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        _STATS.update({
            "protected_total": 0,
            "events_processed": 0,
            "flagged_events": 0,
            "last_event_id": None,
            "last_protected": 0,
            "last_run_at": None,
            "last_error": None,
        })
        return dict(_STATS)


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Reset toggle + config + stats (does not unlock rec_protect locks)."""
    global _ENABLED
    flat = _normalize_kwargs(*args, **kwargs)
    with _lock:
        _ENABLED = False
        _CONFIG.update({
            "window_before": DEFAULT_WINDOW_BEFORE,
            "window_after": DEFAULT_WINDOW_AFTER,
            "recording_dir": None,
            "storage_root": None,
            "limit": None,
            "reason": None,
            "protect_acknowledged": True,
            "protect_escalated": True,
        })
        _STATS.update({
            "protected_total": 0,
            "events_processed": 0,
            "flagged_events": 0,
            "last_event_id": None,
            "last_protected": 0,
            "last_run_at": None,
            "last_error": None,
        })
        if _coerce_bool(flat.get("clear_locks", flat.get("clear_protections", False)), False):
            try:
                from app.services import rec_protect as _rp

                _rp.clear()
            except Exception:  # noqa: BLE001
                pass
        return get_status()


def reset(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return clear(*args, **kwargs)


def should_auto_protect(*args: Any, **kwargs: Any) -> bool:
    return is_enabled(*args, **kwargs)


# Thin rec_protect pass-throughs (flagged-aware status).
def is_protected(path: Any, storage_root: Any = None, **kwargs: Any) -> bool:
    try:
        import app.services.rec_protect as _rp

        root = storage_root if storage_root is not None else kwargs.get("storage_root", kwargs.get("storageRoot"))
        return bool(_rp.is_protected(path, storage_root=root))
    except Exception:  # noqa: BLE001
        return False


def status_for(path: Any, storage_root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    try:
        import app.services.rec_protect as _rp

        root = storage_root if storage_root is not None else kwargs.get("storage_root", kwargs.get("storageRoot"))
        return dict(_rp.status_for(path, storage_root=root))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "success": False, "protected": False, "path": str(path), "error": str(exc)}


def list_protected(storage_root: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    try:
        import app.services.rec_protect as _rp

        root = storage_root if storage_root is not None else kwargs.get("storage_root", kwargs.get("storageRoot"))
        return list(_rp.list_protected(storage_root=root))
    except Exception:  # noqa: BLE001
        return []


# --- Hook into event creation (no main.py edit) ---------------------------

def _ensure_hook_installed(force: bool = False) -> bool:
    global _ORIGINAL_RECORD_EVENT, _HOOK_INSTALLED
    if _HOOK_INSTALLED and not force:
        return True
    try:
        import app.services.cameras as _cams
    except Exception:  # noqa: BLE001
        return False
    orig = getattr(_cams, "record_event", None)
    if orig is None or getattr(orig, "__event_protect_wrapped__", False):
        _HOOK_INSTALLED = getattr(orig, "__event_protect_wrapped__", False)
        return _HOOK_INSTALLED
    if not callable(orig):
        return False
    _ORIGINAL_RECORD_EVENT = orig

    async def _wrapped(*a: Any, **k: Any) -> Any:
        event = await _ORIGINAL_RECORD_EVENT(*a, **k)
        try:
            if is_enabled():
                try:
                    flagged = is_flagged(event)
                except Exception:  # noqa: BLE001
                    flagged = False
                if flagged:
                    db_arg = None
                    if a:
                        db_arg = a[0]
                    if db_arg is None:
                        db_arg = k.get("db", k.get("session"))
                    try:
                        await auto_protect_event_async(event, db=db_arg, force=False)
                    except Exception:  # noqa: BLE001
                        try:
                            auto_protect_event(_event_to_dict(event), force=False)
                        except Exception:  # noqa: BLE001
                            pass
        except Exception:  # noqa: BLE001
            pass
        return event

    _wrapped.__event_protect_wrapped__ = True  # type: ignore[attr-defined]
    # Chain-friendly: preserve autolink marker when both hooks are installed.
    try:
        if getattr(orig, "__event_autolink_wrapped__", False):
            _wrapped.__event_autolink_wrapped__ = True  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    try:
        _wrapped.__name__ = getattr(orig, "__name__", "record_event")
        _wrapped.__doc__ = getattr(orig, "__doc__", None)
    except Exception:  # noqa: BLE001
        pass
    # If autolink already wrapped record_event, wrap the wrapper so both fire.
    _cams.record_event = _wrapped  # type: ignore[method-assign]
    try:
        import app.services.events as _ev

        _ev.record_event = _wrapped  # type: ignore[method-assign]
    except Exception:  # noqa: BLE001
        pass
    _HOOK_INSTALLED = True
    return True


def install_hook(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    ok = _ensure_hook_installed()
    return {"success": bool(ok), "ok": bool(ok), "installed": bool(ok), **get_status()}


def uninstall_hook(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    global _HOOK_INSTALLED, _ORIGINAL_RECORD_EVENT
    try:
        import app.services.cameras as _cams

        if _ORIGINAL_RECORD_EVENT is not None and getattr(_cams.record_event, "__event_protect_wrapped__", False):
            # Only restore when our wrapper is on top; if autolink wrapped us,
            # leave the chain intact (best-effort).
            _cams.record_event = _ORIGINAL_RECORD_EVENT  # type: ignore[method-assign]
            try:
                import app.services.events as _ev

                _ev.record_event = _ORIGINAL_RECORD_EVENT  # type: ignore[method-assign]
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    _HOOK_INSTALLED = False
    return {"success": True, "ok": True, "installed": False, **get_status()}


def ensure_hook_installed(*args: Any, **kwargs: Any) -> bool:
    return _ensure_hook_installed()


# Install on import so the auto-loader activates auto-protect without main.py edits.
try:
    _ensure_hook_installed()
except Exception:  # noqa: BLE001
    pass


class EventProtectService:
    """Object-oriented facade over the event-protect store."""

    def is_flagged(self, *a: Any, **k: Any) -> bool:
        return is_flagged(*a, **k)

    def check(self, *a: Any, **k: Any) -> bool:
        return is_flagged(*a, **k)

    def is_enabled(self, *a: Any, **k: Any) -> bool:
        return is_enabled(*a, **k)

    def status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_status(*a, **k)

    def get_status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_status(*a, **k)

    def enable(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return enable(*a, **k)

    def disable(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return disable(*a, **k)

    def toggle(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return toggle(*a, **k)

    def set_enabled(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return set_enabled(*a, **k)

    def configure(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return set_config(*a, **k)

    def protect(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return auto_protect_event(*a, **k)

    def auto_protect(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return auto_protect_event(*a, **k)

    def maybe_protect(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return maybe_auto_protect_event(*a, **k)

    def backfill(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return backfill_flagged(*a, **k)

    def clear(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return clear(*a, **k)

    # aliases
    get_enabled = is_enabled
    enabled = is_enabled
    get_config = get_config
    config = get_config
    settings = get_settings
    stats = get_stats
    set_config = set_config
    update_config = update_config
    auto_protect_event = auto_protect_event
    protect_event = auto_protect_event
    protect_event_segments = auto_protect_event
    maybe_auto_protect = maybe_auto_protect_event
    maybe_auto_protect_event = maybe_auto_protect_event
    process_event = maybe_auto_protect_event
    handle_event = maybe_auto_protect_event
    on_event_created = maybe_auto_protect_event
    protect_flagged = backfill_flagged
    backfill_flagged = backfill_flagged
    reset = reset
    install = install_hook


event_protect_service = EventProtectService()
protect_service = event_protect_service
service = event_protect_service
manager = event_protect_service
auto_protect_service = event_protect_service


# Compatibility aliases (function level).
flagged = is_flagged
check_flagged = is_flagged
is_event_flagged = is_flagged
is_flagged_event = is_flagged
check = is_flagged
flag_check = is_flagged
protect_event = auto_protect_event
auto_protect = auto_protect_event
auto_protect_for_event = auto_protect_event
protect_for_event = auto_protect_event
protect_event_segments = auto_protect_event
protect_segments_for_event = auto_protect_event
auto_protect_segments = auto_protect_event
protect_flagged_event = auto_protect_event
auto_protect_flagged_event = auto_protect_event
maybe_protect = maybe_auto_protect_event
maybe_auto_protect = maybe_auto_protect_event
maybe_protect_event = maybe_auto_protect_event
handle_event = maybe_auto_protect_event
process_event = maybe_auto_protect_event
on_event_created = maybe_auto_protect_event
try_auto_protect = maybe_auto_protect_event
protect_flagged_events = backfill_flagged
protect_flagged = backfill_flagged
backfill = backfill_flagged
backfill_events = backfill_flagged
backfill_all = backfill_flagged
run_backfill = backfill_flagged

__all__ = [
    "is_flagged",
    "auto_protect_event",
    "auto_protect_event_async",
    "maybe_auto_protect_event",
    "maybe_auto_protect_event_async",
    "backfill_flagged",
    "backfill_flagged_async",
    "protect_flagged_events",
    "is_enabled",
    "get_enabled",
    "get_status",
    "get_config",
    "get_settings",
    "set_enabled",
    "enable",
    "disable",
    "toggle",
    "set_config",
    "configure",
    "update_config",
    "get_stats",
    "reset_stats",
    "clear",
    "reset",
    "should_auto_protect",
    "is_protected",
    "status_for",
    "list_protected",
    "install_hook",
    "uninstall_hook",
    "ensure_hook_installed",
    "EventProtectService",
    "event_protect_service",
    "FLAG_TAGS",
]
