"""Event auto-attach (autolink) service.

Verification (see ``app/services/event_segments.py`` +
``app/services/cameras.py`` + ``app/api/events.py``):

* ``event_segments`` linking happens **only on explicit request**:
  :func:`app.services.event_segments.link_event_to_segment` (and its async
  wrapper) called from ``app/api/event_segments.py`` endpoints such as
  ``POST /api/events/{event_id}/segments`` / ``POST /api/event-segments``,
  or via direct service calls / ``discover_segments_for_event`` queries.
* Event creation (``app.services.cameras.record_event``) never imports or
  calls ``event_segments`` -- it only persists the ``Event`` row and marks
  camera activity. ``app/api/events.py`` has no segment/link handling.

This module adds an **opt-in** auto-attach layer without touching
``app/main.py`` or ``app/templates/index.html``:

* In-memory toggle (default **disabled** to preserve explicit-only behaviour)
  with ``enable`` / ``disable`` / ``set_enabled`` / ``is_enabled`` and
  window/recording-dir configuration.
* :func:`autolink_event` / :func:`autolink_event_async` -- discover overlapping
  segments for one event and persist manual links via ``event_segments``.
* :func:`backfill` / :func:`backfill_async` -- explicit bulk attach for
  existing events (works even when the toggle is off; it is an explicit
  request).
* Best-effort hook into ``app.services.cameras.record_event`` via
  monkey-patch (installed on import). When the toggle is **enabled**, newly
  created events are auto-attached; when disabled (default) creation is
  untouched. The hook never breaks event creation -- failures are swallowed.
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
    "limit": None,
}
_STATS: Dict[str, Any] = {
    "attached_total": 0,
    "events_processed": 0,
    "last_event_id": None,
    "last_attached": 0,
    "last_run_at": None,
    "last_error": None,
}

_ORIGINAL_RECORD_EVENT: Any = None
_HOOK_INSTALLED: bool = False


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
    if s in {"1", "true", "yes", "y", "on", "enabled", "enable", "active", "auto"}:
        return True
    if s in {"0", "false", "no", "n", "off", "disabled", "disable", "inactive"}:
        return False
    return default


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
            "request", "fields", "entry", "settings", "autolink",
            "auto_attach", "event",
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
            flat.setdefault("enabled", a)
        elif isinstance(a, (int, float)) and "event_id" not in flat:
            # positional event id for attach helpers; keep for later extraction
            flat.setdefault("event_id", a)
        elif isinstance(a, str):
            s = a.strip()
            if s.lower() in {"enable", "enabled", "on", "true", "disable", "disabled", "off", "false"}:
                flat.setdefault("enabled", s)
    flat.update(kwargs or {})
    for nested_key in (
        "payload", "options", "config", "params", "data", "body",
        "request", "fields", "settings", "autolink", "auto_attach",
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


# ------------------------------------------------------------------ #
# Toggle + config
# ------------------------------------------------------------------ #

def is_enabled(*args: Any, **kwargs: Any) -> bool:
    """Return True when auto-attach on event creation is enabled."""
    flat = _normalize_kwargs(*args, **kwargs)
    if flat:
        explicit = _first_present(
            flat,
            ("enabled", "enable", "auto_attach", "autoAttach", "autolink",
             "auto_link", "active", "is_enabled", "isEnabled", "on"),
        )
        if explicit is not None and len(flat) == 1 and not isinstance(explicit, dict):
            # Called as is_enabled(enabled=...) query helper -- still report state.
            pass
    with _lock:
        return bool(_ENABLED)


def get_enabled(*args: Any, **kwargs: Any) -> bool:
    return is_enabled(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        return {
            "enabled": bool(_ENABLED),
            "auto_attach": bool(_ENABLED),
            "autolink": bool(_ENABLED),
            "active": bool(_ENABLED),
            "config": dict(_CONFIG),
            "window_before": float(_CONFIG.get("window_before", DEFAULT_WINDOW_BEFORE)),
            "window_after": float(_CONFIG.get("window_after", DEFAULT_WINDOW_AFTER)),
            "recording_dir": _CONFIG.get("recording_dir"),
            "stats": dict(_STATS),
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
            ("enabled", "enable", "auto_attach", "autoAttach", "autolink",
             "active", "is_enabled", "on", "value", "state"),
        )
        if raw is None:
            raw = True
    elif flat:
        alt = _first_present(
            flat,
            ("enabled", "enable", "auto_attach", "autoAttach", "autolink",
             "active", "is_enabled", "on", "value", "state"),
        )
        if alt is not None:
            raw = alt
    # string shorthands
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
        ("enabled", "enable", "auto_attach", "autoAttach", "autolink",
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
             "storage_root", "root", "directory", "dir", "folder"),
        )
        if rec is not None:
            _CONFIG["recording_dir"] = str(rec)
        lim = _first_present(flat, ("limit", "n", "count", "max_links", "maxLinks"))
        if lim is not None:
            _CONFIG["limit"] = _coerce_int(lim, None)
        # toggle via config payload
        tog = _first_present(
            flat,
            ("enabled", "enable", "auto_attach", "autoAttach", "autolink", "active", "on"),
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
            "attached_total": 0,
            "events_processed": 0,
            "last_event_id": None,
            "last_attached": 0,
            "last_run_at": None,
            "last_error": None,
        })
        return dict(_STATS)


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Reset toggle + config + stats (does not delete event_segments links)."""
    global _ENABLED
    flat = _normalize_kwargs(*args, **kwargs)
    with _lock:
        _ENABLED = False
        _CONFIG.update({
            "window_before": DEFAULT_WINDOW_BEFORE,
            "window_after": DEFAULT_WINDOW_AFTER,
            "recording_dir": None,
            "limit": None,
        })
        _STATS.update({
            "attached_total": 0,
            "events_processed": 0,
            "last_event_id": None,
            "last_attached": 0,
            "last_run_at": None,
            "last_error": None,
        })
        # optional: also clear links when explicitly asked
        if _coerce_bool(flat.get("clear_links", flat.get("links")), False):
            try:
                from app.services import event_segments as _seg

                _seg.clear()
            except Exception:  # noqa: BLE001
                pass
        return get_status()


def reset(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return clear(*args, **kwargs)


def should_autolink(*args: Any, **kwargs: Any) -> bool:
    return is_enabled(*args, **kwargs)


# ------------------------------------------------------------------ #
# Event helpers
# ------------------------------------------------------------------ #

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
        "camera_name", "cameraName", "kind", "label", "confidence",
        "created_at", "createdAt", "timestamp", "time", "event_time",
    ):
        try:
            val = getattr(ev, attr, None)
        except Exception:  # noqa: BLE001
            val = None
        if val is not None:
            out[attr] = val
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
    raw = _first_present(
        flat,
        ("event_id", "eventId", "event", "id", "id_event", "for_event", "camera_event_id"),
    )
    if isinstance(raw, dict):
        raw = raw.get("id", raw.get("event_id"))
    v = _coerce_int(raw, None)
    if v is not None:
        return v
    for a in args:
        if isinstance(a, (int, float)) and not isinstance(a, bool):
            return _coerce_int(a, None)
    return None


def _resolve_windows(flat: Dict[str, Any]) -> tuple:
    with _lock:
        cfg_b = _CONFIG.get("window_before", DEFAULT_WINDOW_BEFORE)
        cfg_a = _CONFIG.get("window_after", DEFAULT_WINDOW_AFTER)
        cfg_dir = _CONFIG.get("recording_dir")
        cfg_lim = _CONFIG.get("limit")
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
         "storage_root", "root", "directory", "dir"),
    )
    if rec is None:
        rec = cfg_dir
    lim_raw = _first_present(flat, ("limit", "n", "count", "max_links", "maxLinks", "size"))
    lim = _coerce_int(lim_raw, None) if lim_raw is not None else cfg_lim
    return float(b), float(a), rec, lim


def _record_stats(event_id: Any, attached: int, error: Any = None) -> None:
    with _lock:
        _STATS["events_processed"] = int(_STATS.get("events_processed", 0) or 0) + 1
        _STATS["attached_total"] = int(_STATS.get("attached_total", 0) or 0) + int(attached or 0)
        _STATS["last_event_id"] = event_id
        _STATS["last_attached"] = int(attached or 0)
        _STATS["last_run_at"] = _now_iso()
        _STATS["last_error"] = str(error) if error is not None else None


def autolink_event(
    *args: Any,
    event: Any = None,
    event_id: Any = None,
    db: Any = None,
    session: Any = None,
    force: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Discover overlapping segments for one event and persist manual links.

    No-op (``attached == 0``) when the toggle is off unless ``force=True``.
    """
    flat = _normalize_kwargs(*args, **kwargs)
    # inline event dict positional
    inline: Optional[Dict[str, Any]] = None
    for a in args:
        if isinstance(a, dict) and any(
            k in a for k in ("camera_id", "camera_name", "created_at", "timestamp", "camera", "id")
        ):
            inline = dict(a)
            break
        elif hasattr(a, "model_dump"):
            try:
                d = dict(a.model_dump())
                if any(k in d for k in ("camera_id", "created_at", "id")):
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
                  "created_at", "createdAt", "timestamp", "time", "event_time"):
            if flat.get(k) is not None:
                inline[k] = flat.get(k)
    if inline is None:
        # also build from flat hints
        cand = {k: flat.get(k) for k in (
            "camera_id", "cameraId", "camera", "camera_name", "cameraName",
            "created_at", "createdAt", "timestamp", "time", "event_time",
        ) if flat.get(k) is not None}
        if cand and eid is not None:
            inline = {"id": eid, "event_id": eid, **cand}
    enabled = is_enabled()
    force_flag = _coerce_bool(force if force is not None else flat.get("force", flat.get("force_attach")), False)
    if not enabled and not force_flag:
        return {
            "success": True, "ok": True, "enabled": False,
            "event_id": eid, "discovered": 0, "attached": 0,
            "links": [], "segments": [],
            "skipped": True, "reason": "auto-attach disabled",
        }
    if inline is None or eid is None:
        return {
            "success": False, "ok": False, "enabled": enabled,
            "event_id": eid, "discovered": 0, "attached": 0,
            "links": [], "segments": [],
            "error": "event_id is required",
        }
    inline = dict(inline)
    inline.setdefault("id", eid)
    inline.setdefault("event_id", eid)
    before, after, rec_dir, lim = _resolve_windows(flat)
    discover_kwargs: Dict[str, Any] = {
        "window_before": before, "window_after": after,
    }
    if rec_dir is not None:
        discover_kwargs["recording_dir"] = rec_dir
    if lim is not None:
        discover_kwargs["limit"] = lim
    # pass through camera/timestamp hints that may live only in flat
    for k in ("camera_id", "cameraId", "camera", "camera_name", "cameraName",
              "created_at", "createdAt", "timestamp", "time", "event_time"):
        if k not in inline and flat.get(k) is not None:
            discover_kwargs[k] = flat.get(k)
    try:
        from app.services import event_segments as _seg
    except Exception as exc:  # noqa: BLE001
        _record_stats(eid, 0, exc)
        return {"success": False, "ok": False, "enabled": enabled, "event_id": eid,
                "discovered": 0, "attached": 0, "links": [], "error": str(exc)}
    try:
        discovered = _seg.discover_segments_for_event(inline, **discover_kwargs)
    except Exception as exc:  # noqa: BLE001
        _record_stats(eid, 0, exc)
        return {"success": False, "ok": False, "enabled": enabled, "event_id": eid,
                "discovered": 0, "attached": 0, "links": [], "error": str(exc)}
    links: List[Dict[str, Any]] = []
    for seg in discovered or []:
        seg_path = seg.get("segment_path") or seg.get("path") or seg.get("file_path") or seg.get("recording_path")
        if not seg_path:
            rid = seg.get("recording_id", seg.get("recordingId", seg.get("segment_id")))
            if rid is None:
                continue
            seg_path = rid
        try:
            link = _seg.link_event_to_segment(eid, seg_path)
            links.append(link)
        except Exception:  # noqa: BLE001
            # dedupe returns existing; any other error -> skip segment
            try:
                existing = _seg.list_links(event_id=eid)
                for e in existing:
                    if str(e.get("segment_path") or e.get("path")) == str(seg_path):
                        links.append(e)
                        break
            except Exception:  # noqa: BLE001
                pass
    _record_stats(eid, len(links))
    return {
        "success": True, "ok": True, "enabled": enabled,
        "event_id": int(eid),
        "discovered": len(discovered or []),
        "attached": len(links),
        "links": links, "segments": links,
        "window_before": before, "window_after": after,
    }


async def autolink_event_async(
    *args: Any,
    event: Any = None,
    event_id: Any = None,
    db: Any = None,
    session: Any = None,
    force: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    sess = db if db is not None else session
    flat = _normalize_kwargs(*args, **kwargs)
    # If a DB session + bare event id was given, resolve the row first so the
    # filesystem scan uses the real camera/timestamp.
    eid = event_id if event_id is not None else _extract_event_id(*args, **kwargs)
    inline: Optional[Dict[str, Any]] = None
    if event is not None:
        inline = _event_to_dict(event) if not isinstance(event, dict) else dict(event)
    else:
        for a in args:
            if isinstance(a, dict) and any(k in a for k in ("camera_id", "created_at", "timestamp", "camera")):
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
    # Fall back to sync path when no session (filesystem discovery only).
    if sess is None:
        return autolink_event(*args, event=event, event_id=event_id, force=force, **kwargs)
    enabled = is_enabled()
    force_flag = _coerce_bool(force if force is not None else flat.get("force", flat.get("force_attach")), False)
    if not enabled and not force_flag:
        return {
            "success": True, "ok": True, "enabled": False,
            "event_id": eid, "discovered": 0, "attached": 0,
            "links": [], "segments": [],
            "skipped": True, "reason": "auto-attach disabled",
        }
    if inline is None and eid is None:
        return {"success": False, "ok": False, "enabled": enabled, "event_id": None,
                "discovered": 0, "attached": 0, "links": [], "error": "event_id is required"}
    if inline is None:
        inline = {"id": eid, "event_id": eid}
        for k in ("camera_id", "camera", "camera_name", "created_at", "timestamp", "time"):
            if flat.get(k) is not None:
                inline[k] = flat.get(k)
    before, after, rec_dir, lim = _resolve_windows(flat)
    discover_kwargs: Dict[str, Any] = {"window_before": before, "window_after": after}
    if rec_dir is not None:
        discover_kwargs["recording_dir"] = rec_dir
    if lim is not None:
        discover_kwargs["limit"] = lim
    for k in ("camera_id", "cameraId", "camera", "camera_name", "cameraName",
              "created_at", "createdAt", "timestamp", "time"):
        if k not in inline and flat.get(k) is not None:
            discover_kwargs[k] = flat.get(k)
    try:
        from app.services import event_segments as _seg

        discovered = await _seg.get_segments_for_event_async(inline, db=sess, **discover_kwargs)
        if eid is None:
            eid = _coerce_int(inline.get("id", inline.get("event_id")), None)
        links: List[Dict[str, Any]] = []
        if eid is not None:
            for seg in discovered or []:
                seg_path = (seg.get("segment_path") or seg.get("path") or seg.get("file_path")
                            or seg.get("recording_path"))
                if not seg_path:
                    rid = seg.get("recording_id", seg.get("recordingId"))
                    if rid is None:
                        continue
                    seg_path = rid
                try:
                    link = await _seg.link_event_to_segment_async(int(eid), seg_path)
                    links.append(link)
                except Exception:  # noqa: BLE001
                    pass
        _record_stats(eid, len(links))
        return {"success": True, "ok": True, "enabled": enabled, "event_id": eid,
                "discovered": len(discovered or []), "attached": len(links),
                "links": links, "segments": links,
                "window_before": before, "window_after": after}
    except Exception as exc:  # noqa: BLE001
        _record_stats(eid, 0, exc)
        return {"success": False, "ok": False, "enabled": enabled, "event_id": eid,
                "discovered": 0, "attached": 0, "links": [], "error": str(exc)}


def backfill(
    *args: Any,
    db: Any = None,
    session: Any = None,
    events: Any = None,
    event_ids: Any = None,
    ids: Any = None,
    limit: Any = None,
    dry_run: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Explicitly attach segments for existing events (ignores toggle).

    Accepts either an in-memory ``events`` list (dicts/objects) or bare
    ``event_ids``. DB-backed enumeration is available via
    :func:`backfill_async`.
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
        # positional list of ids?
        for a in args:
            if isinstance(a, (list, tuple)):
                event_ids = a
                break
    dry = _coerce_bool(dry_run if dry_run is not None else flat.get("dry_run", flat.get("dryRun", flat.get("preview"))), False)
    lim = _coerce_int(limit if limit is not None else flat.get("limit", flat.get("n")), None)
    targets: List[Any] = []
    if isinstance(events, list):
        targets.extend(events)
    elif events is not None and not isinstance(events, list):
        targets = [events]
    id_list: List[int] = []
    if event_ids is not None:
        if isinstance(event_ids, (list, tuple, set)):
            iterable = list(event_ids)
        else:
            iterable = [event_ids]
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
    # bare positional ints that are not the limit
    for a in args:
        if isinstance(a, (int, float)) and not isinstance(a, bool):
            vv = _coerce_int(a, None)
            if vv is not None and vv not in id_list and (lim is None or vv != lim):
                # treat as event id only when no inline event dict present
                if not targets:
                    id_list.append(vv)
    if lim is not None and lim >= 0:
        if targets:
            targets = targets[:lim]
        if id_list:
            id_list = id_list[:lim]
    total_discovered = 0
    total_attached = 0
    per_event: List[Dict[str, Any]] = []
    # in-memory event dicts: force attach (explicit backfill)
    _passthrough = {k: v for k, v in flat.items()
                    if k not in ("events", "event_list", "items", "event",
                                 "event_ids", "ids", "eventIds", "event_id", "eventId", "id")}
    _passthrough.update(kwargs)
    for ev in targets:
        if dry:
            try:
                from app.services import event_segments as _seg

                before, after, rec_dir, _lim = _resolve_windows(flat)
                dk: Dict[str, Any] = {"window_before": before, "window_after": after}
                if rec_dir is not None:
                    dk["recording_dir"] = rec_dir
                found = _seg.discover_segments_for_event(ev if isinstance(ev, dict) else _event_to_dict(ev), **dk)
                total_discovered += len(found)
                per_event.append({"event_id": _coerce_int((ev.get("id") if isinstance(ev, dict) else None), None),
                                  "discovered": len(found), "attached": 0, "dry_run": True})
            except Exception as exc:  # noqa: BLE001
                per_event.append({"error": str(exc), "dry_run": True})
            continue
        res = autolink_event(ev, force=True, **_passthrough)
        total_discovered += int(res.get("discovered", 0) or 0)
        total_attached += int(res.get("attached", 0) or 0)
        per_event.append(res)
    for eid in id_list:
        if dry:
            per_event.append({"event_id": eid, "discovered": 0, "attached": 0, "dry_run": True})
            continue
        res = autolink_event(eid, force=True, **_passthrough)
        total_discovered += int(res.get("discovered", 0) or 0)
        total_attached += int(res.get("attached", 0) or 0)
        per_event.append(res)
    with _lock:
        _STATS["last_run_at"] = _now_iso()
    return {
        "success": True, "ok": True,
        "enabled": bool(_ENABLED),
        "dry_run": bool(dry),
        "events_processed": len(per_event),
        "events": len(per_event),
        "discovered": total_discovered,
        "attached": total_attached,
        "attached_total": total_attached,
        "per_event": per_event,
        "results": per_event,
    }


async def backfill_async(
    *args: Any,
    db: Any = None,
    session: Any = None,
    events: Any = None,
    event_ids: Any = None,
    ids: Any = None,
    limit: Any = None,
    dry_run: Any = None,
    camera_id: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    sess = db if db is not None else session
    flat = _normalize_kwargs(*args, **kwargs)
    if events is None:
        events = flat.get("events", flat.get("event_list", flat.get("items")))
    if events is None:
        single = flat.get("event")
        if isinstance(single, dict):
            events = [single]
        elif isinstance(single, list):
            events = single
    if event_ids is None:
        event_ids = flat.get("event_ids", flat.get("ids", flat.get("eventIds")))
    if ids is not None and event_ids is None:
        event_ids = ids
    dry = _coerce_bool(dry_run if dry_run is not None else flat.get("dry_run", flat.get("dryRun", flat.get("preview"))), False)
    lim = _coerce_int(limit if limit is not None else flat.get("limit", flat.get("n")), 500)
    cam_raw = camera_id if camera_id is not None else flat.get("camera_id", flat.get("cameraId", flat.get("camera")))
    cam_id = _coerce_int(cam_raw, None) if cam_raw is not None else None
    # In-memory path (no DB): delegate to sync backfill.
    if sess is None:
        return backfill(*args, events=events, event_ids=event_ids, ids=ids,
                        limit=limit, dry_run=dry, **kwargs)
    # Explicit in-memory events win over DB enumeration: attach each directly
    # (with DB session for enrichment) so callers can backfill synthetic ids
    # (e.g. tests with tmp recording_dir) without requiring DB rows.
    has_explicit_events = events is not None and (
        isinstance(events, list) and len(events) > 0 or not isinstance(events, list)
    )
    if has_explicit_events:
        seq = events if isinstance(events, list) else [events]
        if lim is not None and lim >= 0:
            seq = seq[:lim]
        total_discovered = 0
        total_attached = 0
        per_event: List[Dict[str, Any]] = []
        _extra = {k: v for k, v in flat.items()
                  if k not in ("events", "event_list", "items", "event",
                               "event_ids", "ids", "eventIds")}
        _extra.update(kwargs)
        for ev in seq:
            ev_dict = ev if isinstance(ev, dict) else _event_to_dict(ev)
            eid = _coerce_int(ev_dict.get("id", ev_dict.get("event_id")), None)
            if eid is None:
                eid = _extract_event_id(ev_dict, **flat)
            if dry:
                try:
                    from app.services import event_segments as _seg

                    before, after, rec_dir, _l = _resolve_windows(flat)
                    dk: Dict[str, Any] = {"window_before": before, "window_after": after}
                    if rec_dir is not None:
                        dk["recording_dir"] = rec_dir
                    found = await _seg.get_segments_for_event_async(ev_dict, db=sess, **dk)
                    total_discovered += len(found or [])
                    per_event.append({"event_id": eid, "discovered": len(found or []),
                                      "attached": 0, "dry_run": True})
                except Exception as exc:  # noqa: BLE001
                    per_event.append({"event_id": eid, "error": str(exc), "dry_run": True})
                continue
            res = await autolink_event_async(ev_dict, event_id=eid, db=sess, force=True, **_extra)
            total_discovered += int(res.get("discovered", 0) or 0)
            total_attached += int(res.get("attached", 0) or 0)
            per_event.append(res)
        with _lock:
            _STATS["last_run_at"] = _now_iso()
        return {"success": True, "ok": True, "enabled": bool(_ENABLED), "dry_run": bool(dry),
                "events_processed": len(per_event), "events": len(per_event),
                "discovered": total_discovered, "attached": total_attached,
                "attached_total": total_attached, "per_event": per_event, "results": per_event}
    # Explicit ids with camera/timestamp/recording hints but no full dicts:
    # build synthetic events so non-DB ids (e.g. tests) still attach.
    hint_keys = ("camera_id", "cameraId", "camera", "camera_name", "cameraName",
                 "created_at", "createdAt", "timestamp", "time", "event_time",
                 "recording_dir", "recordingDir")
    has_hints = any(flat.get(k) is not None for k in hint_keys)
    if event_ids is not None and has_hints:
        seq_ids = event_ids if isinstance(event_ids, (list, tuple, set)) else [event_ids]
        norm_ids: List[int] = []
        for v in seq_ids:
            if isinstance(v, str) and "," in v:
                for part in v.split(","):
                    vv = _coerce_int(part.strip(), None)
                    if vv is not None:
                        norm_ids.append(vv)
            else:
                vv = _coerce_int(v, None)
                if vv is not None:
                    norm_ids.append(vv)
        if lim is not None and lim >= 0:
            norm_ids = norm_ids[:lim]
        total_discovered = 0
        total_attached = 0
        per_event = []
        _extra2 = {k: v for k, v in flat.items()
                   if k not in ("events", "event_list", "items", "event",
                                "event_ids", "ids", "eventIds")}
        _extra2.update(kwargs)
        for eid in norm_ids:
            synth: Dict[str, Any] = {"id": eid, "event_id": eid}
            for k in ("camera_id", "cameraId", "camera", "camera_name", "cameraName",
                      "created_at", "createdAt", "timestamp", "time", "event_time"):
                if flat.get(k) is not None:
                    synth[k] = flat.get(k)
            if dry:
                per_event.append({"event_id": eid, "discovered": 0, "attached": 0, "dry_run": True})
                continue
            res = await autolink_event_async(synth, event_id=eid, db=sess, force=True, **_extra2)
            # Fallback: if DB-row lookup shadowed hints (row with different
            # camera/time yields zero), retry pure-filesystem sync path.
            if int(res.get("discovered", 0) or 0) == 0 and int(res.get("attached", 0) or 0) == 0:
                try:
                    sync_res = autolink_event(synth, event_id=eid, force=True, **_extra2)
                    if int(sync_res.get("attached", 0) or 0) > 0:
                        res = sync_res
                except Exception:  # noqa: BLE001
                    pass
            total_discovered += int(res.get("discovered", 0) or 0)
            total_attached += int(res.get("attached", 0) or 0)
            per_event.append(res)
        with _lock:
            _STATS["last_run_at"] = _now_iso()
        return {"success": True, "ok": True, "enabled": bool(_ENABLED), "dry_run": bool(dry),
                "events_processed": len(per_event), "events": len(per_event),
                "discovered": total_discovered, "attached": total_attached,
                "attached_total": total_attached, "per_event": per_event, "results": per_event}
    # DB enumeration of recent events.
    try:
        from sqlalchemy import select as _select  # lazy

        from app.models.entities import Event as _Event  # lazy

        stmt = _select(_Event).order_by(_Event.id.desc())
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
        # explicit id filter wins over enumeration
        wanted: Optional[set] = None
        if event_ids is not None:
            wanted = set()
            seq = event_ids if isinstance(event_ids, (list, tuple, set)) else [event_ids]
            for v in seq:
                vv = _coerce_int(v, None)
                if vv is not None:
                    wanted.add(int(vv))
            rows = [r for r in rows if int(getattr(r, "id", -1)) in wanted]
        total_discovered = 0
        total_attached = 0
        per_event: List[Dict[str, Any]] = []
        for row in rows:
            eid = int(getattr(row, "id"))
            if dry:
                try:
                    from app.services import event_segments as _seg

                    before, after, rec_dir, _l = _resolve_windows(flat)
                    dk2: Dict[str, Any] = {"window_before": before, "window_after": after}
                    if rec_dir is not None:
                        dk2["recording_dir"] = rec_dir
                    found = await _seg.get_segments_for_event_async(_event_to_dict(row), db=sess, **dk2)
                    total_discovered += len(found or [])
                    per_event.append({"event_id": eid, "discovered": len(found or []),
                                      "attached": 0, "dry_run": True})
                except Exception as exc:  # noqa: BLE001
                    per_event.append({"event_id": eid, "error": str(exc), "dry_run": True})
                continue
            res = await autolink_event_async(_event_to_dict(row), event_id=eid, db=sess,
                                             force=True, **kwargs)
            total_discovered += int(res.get("discovered", 0) or 0)
            total_attached += int(res.get("attached", 0) or 0)
            per_event.append(res)
        with _lock:
            _STATS["last_run_at"] = _now_iso()
        return {"success": True, "ok": True, "enabled": bool(_ENABLED), "dry_run": bool(dry),
                "events_processed": len(per_event), "events": len(per_event),
                "discovered": total_discovered, "attached": total_attached,
                "attached_total": total_attached, "per_event": per_event, "results": per_event}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "ok": False, "enabled": bool(_ENABLED),
                "events_processed": 0, "discovered": 0, "attached": 0,
                "per_event": [], "error": str(exc)}


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
    if orig is None or getattr(orig, "__event_autolink_wrapped__", False):
        _HOOK_INSTALLED = getattr(orig, "__event_autolink_wrapped__", False)
        # also mirror onto events re-export
        try:
            import app.services.events as _ev

            if getattr(_ev, "record_event", None) is not orig and getattr(orig, "__event_autolink_wrapped__", False):
                pass
        except Exception:  # noqa: BLE001
            pass
        return _HOOK_INSTALLED
    if not callable(orig):
        return False
    _ORIGINAL_RECORD_EVENT = orig

    async def _wrapped(*a: Any, **k: Any) -> Any:
        event = await _ORIGINAL_RECORD_EVENT(*a, **k)
        try:
            if is_enabled():
                db_arg = None
                if a:
                    db_arg = a[0]
                if db_arg is None:
                    db_arg = k.get("db", k.get("session"))
                try:
                    await autolink_event_async(event, db=db_arg, force=False)
                except Exception:  # noqa: BLE001
                    # Fallback: sync filesystem-only attach (explicit event dict).
                    try:
                        autolink_event(_event_to_dict(event), force=False)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
        return event

    _wrapped.__event_autolink_wrapped__ = True  # type: ignore[attr-defined]
    try:
        _wrapped.__name__ = getattr(orig, "__name__", "record_event")
        _wrapped.__doc__ = getattr(orig, "__doc__", None)
    except Exception:  # noqa: BLE001
        pass
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

        if _ORIGINAL_RECORD_EVENT is not None:
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


# Install on import so the auto-loader activates auto-attach without main.py edits.
try:
    _ensure_hook_installed()
except Exception:  # noqa: BLE001
    pass


class EventAutolinkService:
    """Object-oriented facade over the autolink store."""

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

    def autolink(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return autolink_event(*a, **k)

    def attach(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return autolink_event(*a, **k)

    def backfill(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return backfill(*a, **k)

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
    auto_attach = autolink_event
    attach_event = autolink_event
    autolink_event = autolink_event
    maybe_autolink = autolink_event
    maybe_auto_attach = autolink_event
    process_event = autolink_event
    handle_event = autolink_event
    on_event_created = autolink_event
    try_autolink = autolink_event
    run_backfill = backfill
    backfill_events = backfill
    backfill_all = backfill
    reset = reset
    install = install_hook


event_autolink_service = EventAutolinkService()
autolink_service = event_autolink_service
auto_attach_service = event_autolink_service
service = event_autolink_service
manager = event_autolink_service


# Compatibility aliases (function level).
get_enabled_alias = get_enabled
enabled_alias = is_enabled
get_state = get_status
fetch_status = get_status
describe = get_status
state = get_status
set_state = set_enabled
update = set_config
setup = set_config
attach = autolink_event
auto_attach = autolink_event
attach_event = autolink_event
auto_attach_for_event = autolink_event
autolink = autolink_event
maybe_autolink = autolink_event
maybe_auto_attach = autolink_event
process_event = autolink_event
handle_event = autolink_event
handle_event_creation = autolink_event
on_event_created = autolink_event
try_autolink = autolink_event
try_autolink_for_event = autolink_event
run_backfill = backfill
backfill_events = backfill
backfill_all = backfill

__all__ = [
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
    "should_autolink",
    "autolink_event",
    "autolink_event_async",
    "backfill",
    "backfill_async",
    "install_hook",
    "uninstall_hook",
    "ensure_hook_installed",
    "EventAutolinkService",
    "event_autolink_service",
]
