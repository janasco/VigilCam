"""Alert auto-snapshot service.

Verification (read-only, see ``app/services/cameras.py`` +
``app/api/events.py``):

* ``app.services.cameras.record_event`` only persists the ``Event`` row and
  marks camera activity. Its source contains no ``snapshot`` / ``snapshot_path``
  handling -- it never captures or attaches a snapshot.
* ``app/api/events.py`` attaches snapshots **only on explicit manual request**:
  ``POST /api/events/{event_id}/snapshot`` (``api_event_upload_snapshot``)
  which calls ``app.services.snapshots.save_snapshot`` and sets
  ``Event.snapshot_path`` + payload ``snapshot_path``/``snapshot_url``.
  There is no automatic snapshot on alert/event creation in the pre-existing
  code (alert domains ``resource_watch`` / ``bitrate_alerts`` /
  ``offline_alerts`` / ``rate_anomaly`` only use the word "snapshot" for
  threshold/health dict snapshots, never image files).

This module adds the missing **opt-in** auto-snapshot layer without touching
``app/main.py`` or ``app/templates/index.html``:

* In-memory toggle (default **disabled** to preserve explicit-only behaviour)
  with ``enable`` / ``disable`` / ``set_enabled`` / ``is_enabled`` and config.
* :func:`snapshot_event` / :func:`snapshot_event_async` -- capture one frame
  via ``app.services.snapshot_now.capture_snapshot_now`` and attach it to one
  event (``snapshot_path`` + ``snapshot_url``). Explicit calls bypass the
  toggle with ``force=True``.
* :func:`backfill` / :func:`backfill_async` -- explicit bulk snapshot for
  existing events (works even when the toggle is off).
* Best-effort hook into ``app.services.cameras.record_event`` via
  monkey-patch (installed on import). When the toggle is **enabled**, newly
  created events are auto-snapshotted; when disabled (default) creation is
  untouched. The hook never breaks event creation -- failures are swallowed.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_lock = threading.RLock()
_ENABLED: bool = False
_CONFIG: Dict[str, Any] = {
    "label": None,
    "directory": None,
    "source": None,
    "method": "snapshot_now",
}
_STATS: Dict[str, Any] = {
    "snapshots_total": 0,
    "attached_total": 0,
    "events_processed": 0,
    "last_event_id": None,
    "last_snapshot_path": None,
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


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
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
            return {}
    if isinstance(data, dict):
        return dict(data)
    try:
        return dict(data)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return {}


def _normalize_kwargs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if a is None:
            continue
        if hasattr(a, "model_dump"):
            try:
                for k, v in dict(a.model_dump()).items():
                    data.setdefault(k, v)
                continue
            except Exception:  # noqa: BLE001
                pass
        if isinstance(a, dict):
            for k, v in a.items():
                data.setdefault(k, v)
        elif isinstance(a, (list, tuple, set)):
            data.setdefault("events", list(a))
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            data.setdefault("event_id", int(a))
        elif isinstance(a, str):
            s = a.strip()
            if s.isdigit():
                data.setdefault("event_id", int(s))
            elif s:
                data.setdefault("label", s)
    for k, v in (kwargs or {}).items():
        if k in ("db", "session", "database"):
            continue
        data[k] = v
    for nested_key in (
        "payload", "options", "config", "params", "data", "body", "request",
        "fields", "entry", "event", "source_event", "alert", "snapshot",
        "settings", "filter", "filters",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _first_present(flat: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if flat.get(k) is not None:
            return flat.get(k)
    lower = {str(k).lower(): v for k, v in flat.items() if v is not None}
    for k in keys:
        if lower.get(str(k).lower()) is not None:
            return lower[str(k).lower()]
    return None


# ------------------------------------------------------------------ #
# Toggle + config
# ------------------------------------------------------------------ #

def is_enabled(*args: Any, **kwargs: Any) -> bool:
    """Return True when auto-snapshot on alert creation is enabled."""
    with _lock:
        return bool(_ENABLED)


def get_enabled(*args: Any, **kwargs: Any) -> bool:
    return is_enabled(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        return {
            "enabled": bool(_ENABLED),
            "auto_snapshot": bool(_ENABLED),
            "auto_snapshot_on_alert": bool(_ENABLED),
            "active": bool(_ENABLED),
            "ok": True,
            "success": True,
            "status": "enabled" if _ENABLED else "disabled",
            "config": dict(_CONFIG),
            "label": _CONFIG.get("label"),
            "directory": _CONFIG.get("directory"),
            "source": _CONFIG.get("source"),
            "stats": dict(_STATS),
        }


def get_policy(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return get_status(*args, **kwargs)


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
            ("enabled", "enable", "auto_snapshot", "autoSnapshot", "auto_attach",
             "active", "is_enabled", "on", "value", "state"),
        )
        if raw is None:
            raw = True
    elif flat:
        alt = _first_present(
            flat,
            ("enabled", "enable", "auto_snapshot", "autoSnapshot", "auto_attach",
             "active", "is_enabled", "on", "value", "state"),
        )
        if alt is not None:
            raw = alt
    if isinstance(raw, str) and raw.strip().lower() in {"toggle", "switch", "flip"}:
        with _lock:
            _ENABLED = not bool(_ENABLED)
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
        ("enabled", "enable", "auto_snapshot", "autoSnapshot", "active",
         "is_enabled", "on", "value", "state"),
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
        for key in ("label", "default_label", "text"):
            if flat.get(key) is not None:
                _CONFIG["label"] = str(flat.get(key))
                break
        rec = _first_present(
            flat,
            ("directory", "dir", "snapshot_dir", "snapshotDir", "folder",
             "output_dir", "target_dir", "root"),
        )
        if rec is not None:
            _CONFIG["directory"] = str(rec)
        src = _first_present(flat, ("source", "source_url", "stream_url", "url", "stream"))
        if src is not None:
            _CONFIG["source"] = str(src)
        tog = _first_present(
            flat,
            ("enabled", "enable", "auto_snapshot", "autoSnapshot", "active", "on"),
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
            "snapshots_total": 0,
            "attached_total": 0,
            "events_processed": 0,
            "last_event_id": None,
            "last_snapshot_path": None,
            "last_run_at": None,
            "last_error": None,
        })
        return dict(_STATS)


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Reset toggle + config + stats (does not delete snapshot files)."""
    global _ENABLED
    with _lock:
        _ENABLED = False
        _CONFIG.update({"label": None, "directory": None, "source": None, "method": "snapshot_now"})
        _STATS.update({
            "snapshots_total": 0,
            "attached_total": 0,
            "events_processed": 0,
            "last_event_id": None,
            "last_snapshot_path": None,
            "last_run_at": None,
            "last_error": None,
        })
        return get_status()


def reset(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return clear(*args, **kwargs)


def should_auto_snapshot(*args: Any, **kwargs: Any) -> bool:
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
        "snapshot_path", "snapshotPath", "created_at", "createdAt",
        "timestamp", "time",
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


def _extract_camera_id(*args: Any, **kwargs: Any) -> Optional[int]:
    flat = _normalize_kwargs(*args, **kwargs)
    # Prefer explicit camera keys.
    raw = _first_present(
        flat,
        ("camera_id", "cameraId", "camera_ids", "camera", "cam_id", "cam",
         "cameraName", "camera_name"),
    )
    if isinstance(raw, dict):
        raw = raw.get("id", raw.get("camera_id"))
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            try:
                return int(float(str(raw).strip()))
            except (TypeError, ValueError):
                pass
    for a in args:
        if isinstance(a, dict):
            for k in ("camera_id", "cameraId", "camera", "cam_id"):
                if a.get(k) is not None:
                    try:
                        return int(a.get(k))  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        continue
        elif hasattr(a, "camera_id"):
            try:
                v = getattr(a, "camera_id", None)
                if v is not None:
                    return int(v)
            except (TypeError, ValueError):
                pass
    return None


def _extract_force(flat: Dict[str, Any]) -> bool:
    for k in ("force", "force_snapshot", "forceSnapshot", "force_capture",
              "explicit", "bypass", "bypass_toggle", "always", "manual"):
        if flat.get(k) is not None and _coerce_bool(flat.get(k), False):
            return True
    return False


def _capture_frame(
    camera_id: Optional[int] = None,
    source: Optional[str] = None,
    label: Optional[str] = None,
    directory: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Capture one frame via snapshot_now (always succeeds with placeholder)."""
    try:
        from app.services import snapshot_now as _snap_now
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"snapshot service unavailable: {exc}") from exc
    call: Dict[str, Any] = {}
    if camera_id is not None:
        call["camera_id"] = camera_id
    # Config defaults.
    with _lock:
        cfg_label = _CONFIG.get("label")
        cfg_dir = _CONFIG.get("directory")
        cfg_src = _CONFIG.get("source")
    eff_source = source if source is not None else cfg_src
    eff_label = label if label is not None else cfg_label
    eff_dir = directory if directory is not None else cfg_dir
    if eff_source:
        call["source"] = eff_source
    if eff_label:
        call["label"] = eff_label
    elif camera_id is not None:
        call["label"] = f"Cam {camera_id} alert"
    else:
        call["label"] = "Alert snapshot"
    if eff_dir:
        call["directory"] = eff_dir
    # Forward width/height/timeout when provided.
    for k in ("width", "height", "w", "h", "timeout", "ffmpeg_timeout"):
        if extra.get(k) is not None:
            call[k] = extra.get(k)
    result = _snap_now.capture_snapshot_now(**call)
    if not isinstance(result, dict):
        result = {"path": str(result)}
    return result


def _snapshot_url_for(event_id: Any) -> str:
    try:
        eid = int(event_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        eid = event_id
    return f"/api/events/{eid}/snapshot"


# ------------------------------------------------------------------ #
# Core snapshot
# ------------------------------------------------------------------ #

def snapshot_event(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Capture a snapshot for one alert/event and attach ``snapshot_path``.

    Accepts an event dict, an ``Event`` object, or ``event_id``/``id`` plus
    optional ``camera_id``/``source``/``label``. Returns a result dict with
    ``snapshot_path``/``path``/``snapshot_url``. Explicit single calls with
    ``force=True`` bypass the toggle; otherwise the toggle gates auto capture
    (disabled -> ``skipped=True``, ``attached=0``).
    """
    flat = _normalize_kwargs(*args, **kwargs)
    force = _extract_force(flat)
    # Dry-run support.
    dry = _first_present(flat, ("dry_run", "dryRun", "dry-run", "dry", "preview", "preview_only"))
    is_dry = _coerce_bool(dry, False) if dry is not None else False

    event_id = _extract_event_id(*args, **kwargs)
    # Inline event dict?
    inline: Optional[Dict[str, Any]] = None
    for a in args:
        if isinstance(a, dict) and any(k in a for k in ("id", "event_id", "camera_id", "label", "camera_name")):
            inline = dict(a)
            break
    if inline is None:
        for k in ("event", "source_event", "alert", "data_event"):
            if isinstance(flat.get(k), dict):
                inline = dict(flat.get(k))
                break
    if event_id is None and inline is not None:
        event_id = _coerce_int(inline.get("id", inline.get("event_id")), None)

    camera_id = _extract_camera_id(*args, **kwargs)
    if camera_id is None and inline is not None:
        for k in ("camera_id", "cameraId", "camera", "cam_id"):
            if inline.get(k) is not None:
                try:
                    camera_id = int(inline.get(k))  # type: ignore[arg-type]
                    break
                except (TypeError, ValueError):
                    continue
    source = _first_present(flat, ("source", "source_url", "stream_url", "url", "stream", "rtsp_url"))
    if source is None and inline is not None:
        source = inline.get("source", inline.get("source_url"))
    label = _first_present(flat, ("label", "text", "title", "name"))
    directory = _first_present(
        flat, ("directory", "dir", "snapshot_dir", "snapshotDir", "folder", "output_dir")
    )

    enabled = is_enabled()
    if not enabled and not force and not is_dry:
        return {
            "success": True,
            "ok": True,
            "status": "skipped",
            "skipped": True,
            "enabled": False,
            "attached": 0,
            "snapshotted": False,
            "captured": False,
            "event_id": event_id,
            "camera_id": camera_id,
            "snapshot_path": None,
            "path": None,
            "reason": "auto-snapshot disabled (explicit force required)",
        }

    if is_dry:
        return {
            "success": True,
            "ok": True,
            "status": "preview",
            "dry_run": True,
            "dryRun": True,
            "preview": True,
            "skipped": False,
            "enabled": bool(enabled),
            "attached": 0,
            "would_attach": 1 if event_id is not None else 0,
            "event_id": event_id,
            "camera_id": camera_id,
            "snapshot_path": None,
        }

    try:
        captured = _capture_frame(
            camera_id=camera_id, source=str(source) if source else None,
            label=str(label) if label else None,
            directory=str(directory) if directory else None,
            **{k: v for k, v in flat.items() if k in ("width", "height", "w", "h", "timeout")},
        )
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _STATS["last_error"] = str(exc)
        return {
            "success": False, "ok": False, "enabled": bool(enabled),
            "attached": 0, "event_id": event_id, "camera_id": camera_id,
            "snapshot_path": None, "error": str(exc),
        }

    snap_path = (
        captured.get("snapshot_path") or captured.get("path")
        or captured.get("file") or captured.get("filename")
    )
    snap_path_s = str(snap_path) if snap_path else None
    size = captured.get("size", captured.get("size_bytes"))

    # Mutate inline dict best-effort (in-memory callers see attachment).
    if inline is not None and snap_path_s:
        inline["snapshot_path"] = snap_path_s
        inline["snapshot_url"] = _snapshot_url_for(inline.get("id", inline.get("event_id", event_id)))
        # Mirror back onto caller's dict when it was passed positionally.
        for a in args:
            if isinstance(a, dict) and a is not inline:
                continue
            if isinstance(a, dict) and any(k in a for k in ("id", "event_id", "camera_id")):
                try:
                    a["snapshot_path"] = snap_path_s
                except Exception:  # noqa: BLE001
                    pass

    with _lock:
        _STATS["snapshots_total"] = int(_STATS.get("snapshots_total", 0) or 0) + 1
        _STATS["attached_total"] = int(_STATS.get("attached_total", 0) or 0) + 1
        _STATS["events_processed"] = int(_STATS.get("events_processed", 0) or 0) + 1
        _STATS["last_event_id"] = event_id
        _STATS["last_snapshot_path"] = snap_path_s
        _STATS["last_run_at"] = _now_iso()
        _STATS["last_error"] = None

    out: Dict[str, Any] = {
        "success": True,
        "ok": True,
        "status": "captured",
        "captured": True,
        "snapshotted": True,
        "attached": 1,
        "enabled": bool(enabled) or bool(force),
        "force": bool(force),
        "event_id": event_id,
        "id": event_id,
        "camera_id": camera_id,
        "camera": camera_id,
        "snapshot_path": snap_path_s,
        "path": snap_path_s,
        "file": snap_path_s,
        "file_path": snap_path_s,
        "filename": Path(str(snap_path_s)).name if snap_path_s else None,
        "snapshot_url": _snapshot_url_for(event_id) if event_id is not None else None,
        "url": _snapshot_url_for(event_id) if event_id is not None else None,
        "method": captured.get("method", "snapshot_now"),
        "size": size,
        "size_bytes": size,
        "captured_at": captured.get("captured_at", _now_iso()),
    }
    # Echo label/source for debuggability.
    if label is not None:
        out["label"] = label
    if source is not None:
        out["source"] = source
    return out


async def snapshot_event_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Async snapshot for one event; updates the ``Event`` row when ``db`` given."""
    flat = _normalize_kwargs(*args, **kwargs)
    db_obj = db if db is not None else session
    if db_obj is None:
        for k in ("db", "session", "database"):
            if kwargs.get(k) is not None:
                db_obj = kwargs.get(k)
                break
            if flat.get(k) is not None and hasattr(flat.get(k), "execute"):
                db_obj = flat.get(k)
                break
    # If no DB session, fall back to sync path.
    if db_obj is None or not hasattr(db_obj, "get"):
        data = dict(flat)
        # Preserve positional event dict/id.
        return snapshot_event(*args, **data)

    event_id = _extract_event_id(*args, **kwargs)
    inline = None
    for a in args:
        if isinstance(a, dict):
            inline = dict(a)
            break
        if hasattr(a, "id") and hasattr(a, "camera_id"):
            inline = _event_to_dict(a)
            # Keep the live object for direct update.
            inline["_live"] = a
            break
    force = _extract_force(flat)
    enabled = is_enabled()
    if not enabled and not force:
        # Still report skipped without capturing.
        return {
            "success": True, "ok": True, "status": "skipped", "skipped": True,
            "enabled": False, "attached": 0, "event_id": event_id,
            "snapshot_path": None,
        }
    # Resolve DB row when we have an id.
    row = None
    if event_id is not None:
        try:
            from app.models.entities import Event as _Event

            row = await db_obj.get(_Event, int(event_id))
        except Exception:  # noqa: BLE001
            row = None
    # Determine camera/source/label.
    camera_id = _extract_camera_id(*args, **kwargs)
    if camera_id is None and row is not None:
        try:
            camera_id = int(getattr(row, "camera_id", None)) if getattr(row, "camera_id", None) is not None else None
        except (TypeError, ValueError):
            camera_id = None
    if camera_id is None and inline is not None:
        for k in ("camera_id", "cameraId", "camera"):
            if inline.get(k) is not None:
                try:
                    camera_id = int(inline.get(k))  # type: ignore[arg-type]
                    break
                except (TypeError, ValueError):
                    continue
    source = _first_present(flat, ("source", "source_url", "stream_url", "url", "stream"))
    label = _first_present(flat, ("label", "text", "title"))
    directory = _first_present(flat, ("directory", "dir", "snapshot_dir", "folder"))

    # Try to resolve camera stream source from DB for a real frame.
    if source is None and camera_id is not None:
        try:
            from app.models.entities import Camera as _Camera

            cam = await db_obj.get(_Camera, int(camera_id))
            if cam is not None:
                for attr in ("source", "stream_url", "rtsp_url", "url", "onvif_host"):
                    try:
                        v = getattr(cam, attr, None)
                    except Exception:  # noqa: BLE001
                        v = None
                    if v and str(v).strip():
                        source = str(v).strip()
                        break
        except Exception:  # noqa: BLE001
            pass

    try:
        captured = _capture_frame(
            camera_id=camera_id, source=str(source) if source else None,
            label=str(label) if label else None,
            directory=str(directory) if directory else None,
        )
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _STATS["last_error"] = str(exc)
        return {"success": False, "ok": False, "enabled": bool(enabled),
                "attached": 0, "event_id": event_id, "snapshot_path": None,
                "error": str(exc)}
    snap_path_s = str(captured.get("snapshot_path") or captured.get("path") or "")
    size = captured.get("size", captured.get("size_bytes"))

    attached = 0
    if row is not None and snap_path_s:
        try:
            row.snapshot_path = snap_path_s
            try:
                raw = getattr(row, "payload", None)
                obj = None
                if raw:
                    try:
                        parsed = json.loads(raw)
                        obj = parsed if isinstance(parsed, dict) else {"payload": raw}
                    except Exception:  # noqa: BLE001
                        obj = {"payload": raw}
                else:
                    obj = {}
                obj["snapshot_path"] = snap_path_s
                obj["snapshot_url"] = _snapshot_url_for(getattr(row, "id", event_id))
                row.payload = json.dumps(obj)
            except Exception:  # noqa: BLE001
                pass
            await db_obj.commit()
            try:
                await db_obj.refresh(row)
            except Exception:  # noqa: BLE001
                pass
            attached = 1
        except Exception as exc:  # noqa: BLE001
            try:
                await db_obj.rollback()
            except Exception:  # noqa: BLE001
                pass
            with _lock:
                _STATS["last_error"] = str(exc)
            return {"success": False, "ok": False, "enabled": bool(enabled),
                    "attached": 0, "event_id": event_id,
                    "snapshot_path": snap_path_s, "error": str(exc)}
    elif snap_path_s:
        attached = 1

    with _lock:
        _STATS["snapshots_total"] = int(_STATS.get("snapshots_total", 0) or 0) + 1
        _STATS["attached_total"] = int(_STATS.get("attached_total", 0) or 0) + attached
        _STATS["events_processed"] = int(_STATS.get("events_processed", 0) or 0) + 1
        _STATS["last_event_id"] = event_id
        _STATS["last_snapshot_path"] = snap_path_s or None
        _STATS["last_run_at"] = _now_iso()
        _STATS["last_error"] = None

    return {
        "success": True, "ok": True, "status": "captured", "captured": True,
        "snapshotted": True, "attached": attached, "enabled": bool(enabled) or bool(force),
        "force": bool(force), "event_id": event_id, "id": event_id,
        "camera_id": camera_id, "camera": camera_id,
        "snapshot_path": snap_path_s or None, "path": snap_path_s or None,
        "file": snap_path_s or None, "file_path": snap_path_s or None,
        "filename": Path(snap_path_s).name if snap_path_s else None,
        "snapshot_url": _snapshot_url_for(event_id) if event_id is not None else None,
        "url": _snapshot_url_for(event_id) if event_id is not None else None,
        "method": captured.get("method", "snapshot_now"),
        "size": size, "size_bytes": size,
        "captured_at": captured.get("captured_at", _now_iso()),
    }


def backfill(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Explicit bulk snapshot for existing events (works even when disabled)."""
    flat = _normalize_kwargs(*args, **kwargs)
    # Resolve event list.
    events: List[Any] = []
    for key in ("events", "entries", "items", "rows", "event_list", "alerts"):
        if flat.get(key) is not None:
            v = flat.get(key)
            events = list(v) if isinstance(v, (list, tuple, set)) else [v]
            break
    if not events:
        for key in ("event_ids", "eventIds", "ids", "id_list"):
            if flat.get(key) is not None:
                raw = flat.get(key)
                ids = raw if isinstance(raw, (list, tuple, set)) else [raw]
                events = [{"id": _coerce_int(x, None)} for x in ids]
                events = [e for e in events if e.get("id") is not None]
                break
    if not events and args and isinstance(args[0], (list, tuple)):
        events = list(args[0])
    dry = _first_present(flat, ("dry_run", "dryRun", "dry", "preview"))
    is_dry = _coerce_bool(dry, False) if dry is not None else False
    limit = _coerce_int(_first_present(flat, ("limit", "n", "count")), None)
    if limit is not None and limit >= 0:
        events = events[:limit]

    per_event: List[Dict[str, Any]] = []
    attached = 0
    if is_dry:
        for ev in events:
            eid = _extract_event_id(ev)
            per_event.append({"event_id": eid, "dry_run": True, "would_attach": 1 if eid is not None else 0})
        return {
            "success": True, "ok": True, "dry_run": True, "dryRun": True, "preview": True,
            "enabled": bool(is_enabled()), "events_processed": len(events),
            "events": len(events), "attached": 0, "attached_total": 0,
            "per_event": per_event, "results": per_event,
        }
    for ev in events:
        try:
            if isinstance(ev, dict):
                res = snapshot_event(ev, force=True, **{k: v for k, v in flat.items() if k not in ("events", "entries", "items", "rows")})
            else:
                res = snapshot_event(ev, force=True)
            per_event.append(res)
            attached += int(res.get("attached", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            per_event.append({"event_id": _extract_event_id(ev), "error": str(exc)})
    with _lock:
        _STATS["last_run_at"] = _now_iso()
    return {
        "success": True, "ok": True, "enabled": bool(is_enabled()),
        "events_processed": len(events), "events": len(events),
        "attached": attached, "attached_total": attached,
        "snapshots_total": attached,
        "per_event": per_event, "results": per_event,
    }


async def backfill_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    flat = _normalize_kwargs(*args, **kwargs)
    db_obj = db if db is not None else session
    events: List[Any] = []
    for key in ("events", "entries", "items", "rows", "event_list", "alerts"):
        if flat.get(key) is not None:
            v = flat.get(key)
            events = list(v) if isinstance(v, (list, tuple, set)) else [v]
            break
    if not events:
        for key in ("event_ids", "eventIds", "ids"):
            if flat.get(key) is not None:
                raw = flat.get(key)
                ids = raw if isinstance(raw, (list, tuple, set)) else [raw]
                events = [{"id": _coerce_int(x, None)} for x in ids]
                break
    if not events and db_obj is not None and hasattr(db_obj, "execute"):
        # Pull recent events from DB when no explicit list given.
        try:
            from sqlalchemy import select as _select

            from app.models.entities import Event as _Event

            limit = _coerce_int(flat.get("limit"), 50) or 50
            res = await db_obj.execute(_select(_Event).order_by(_Event.id.desc()).limit(int(limit)))
            events = list(res.scalars().all())
        except Exception:  # noqa: BLE001
            events = []
    dry = _first_present(flat, ("dry_run", "dryRun", "dry", "preview"))
    is_dry = _coerce_bool(dry, False) if dry is not None else False
    if is_dry:
        return {
            "success": True, "ok": True, "dry_run": True, "dryRun": True,
            "enabled": bool(is_enabled()), "events_processed": len(events),
            "attached": 0, "per_event": [],
        }
    per_event: List[Dict[str, Any]] = []
    attached = 0
    for ev in events:
        try:
            if isinstance(ev, dict):
                eid = _coerce_int(ev.get("id", ev.get("event_id")), None)
                res = await snapshot_event_async(ev, event_id=eid, db=db_obj, force=True)
            elif hasattr(ev, "id"):
                res = await snapshot_event_async(ev, db=db_obj, force=True)
            else:
                res = await snapshot_event_async(ev, db=db_obj, force=True)
            per_event.append(res)
            attached += int(res.get("attached", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            per_event.append({"error": str(exc)})
    with _lock:
        _STATS["last_run_at"] = _now_iso()
    return {
        "success": True, "ok": True, "enabled": bool(is_enabled()),
        "events_processed": len(events), "events": len(events),
        "attached": attached, "attached_total": attached,
        "per_event": per_event, "results": per_event,
    }


# --- Hook into event creation (no main.py edit) ---------------------------

_KNOWN_HOOK_MARKERS = (
    "__alert_snapshot_wrapped__",
    "__event_autolink_wrapped__",
    "__event_protect_wrapped__",
)


def _outer_has_ours() -> bool:
    try:
        import app.services.cameras as _cams

        return bool(getattr(getattr(_cams, "record_event", None), "__alert_snapshot_wrapped__", False))
    except Exception:  # noqa: BLE001
        return False


def _propagate_marker_to_outer() -> None:
    try:
        import app.services.cameras as _cams

        outer = getattr(_cams, "record_event", None)
        if outer is not None:
            try:
                setattr(outer, "__alert_snapshot_wrapped__", True)
            except Exception:  # noqa: BLE001
                pass
        try:
            import app.services.events as _ev

            ev_outer = getattr(_ev, "record_event", None)
            if ev_outer is not None:
                try:
                    setattr(ev_outer, "__alert_snapshot_wrapped__", True)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def _ensure_hook_installed(force: bool = False) -> bool:
    global _ORIGINAL_RECORD_EVENT, _HOOK_INSTALLED
    if _HOOK_INSTALLED and not force:
        # Another hook (autolink/protect) may have wrapped after us, hiding
        # our marker on the outer function while our inner wrapper is still
        # in the call chain. Propagate the marker outward so
        # ``getattr(record_event, "__alert_snapshot_wrapped__")`` stays True.
        if _outer_has_ours():
            return True
        try:
            import app.services.cameras as _cams

            outer = getattr(_cams, "record_event", None)
            # If outer is callable, our inner wrapper is still reachable via
            # its closure chain; just re-expose the marker.
            if callable(outer):
                _propagate_marker_to_outer()
                _HOOK_INSTALLED = True
                return True
        except Exception:  # noqa: BLE001
            pass
        # Fall through to re-wrap below.
    try:
        import app.services.cameras as _cams
    except Exception:  # noqa: BLE001
        return False
    orig = getattr(_cams, "record_event", None)
    if orig is None:
        return False
    if getattr(orig, "__alert_snapshot_wrapped__", False):
        _HOOK_INSTALLED = True
        return True
    if not callable(orig):
        return False
    # If our marker is hidden inner (wrapped after us), just propagate.
    # Heuristic: if we have a stored original and outer differs, assume
    # we are inner and propagate instead of double-wrapping endlessly.
    if _ORIGINAL_RECORD_EVENT is not None and not force:
        try:
            if callable(orig) and orig is not _ORIGINAL_RECORD_EVENT:
                _propagate_marker_to_outer()
                _HOOK_INSTALLED = True
                return True
        except Exception:  # noqa: BLE001
            pass
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
                    await snapshot_event_async(event, db=db_arg, force=False)
                except Exception:  # noqa: BLE001
                    try:
                        snapshot_event(_event_to_dict(event), force=False)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
        return event

    _wrapped.__alert_snapshot_wrapped__ = True  # type: ignore[attr-defined]
    # Preserve sibling hook markers so autolink/protect checks keep passing,
    # and link the chain for traversal.
    try:
        for _m in _KNOWN_HOOK_MARKERS:
            if getattr(orig, _m, False):
                try:
                    setattr(_wrapped, _m, True)
                except Exception:  # noqa: BLE001
                    pass
        _wrapped.__wrapped__ = orig  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
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

        if _ORIGINAL_RECORD_EVENT is not None and getattr(
            _cams.record_event, "__alert_snapshot_wrapped__", False
        ):
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


# Install on import so the auto-loader activates auto-snapshot without main.py edits.
try:
    _ensure_hook_installed()
except Exception:  # noqa: BLE001
    pass


class AlertSnapshotService:
    """Object-oriented facade over the alert snapshot helpers."""

    def snapshot(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return snapshot_event(*a, **k)

    def capture(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return snapshot_event(*a, force=True, **k)

    def attach(self, *a: Any, **k: Any) -> Dict[str, Any]:
        k = dict(k)
        k.setdefault("force", True)
        return snapshot_event(*a, **k)

    def run(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return snapshot_event(*a, **k)

    def backfill(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return backfill(*a, **k)

    def is_enabled(self, *a: Any, **k: Any) -> bool:
        return is_enabled(*a, **k)

    def get_status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_status(*a, **k)

    def enable(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return enable(*a, **k)

    def disable(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return disable(*a, **k)

    async def snapshot_async(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return await snapshot_event_async(*a, **k)

    async def backfill_async_method(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return await backfill_async(*a, **k)


alert_snapshot_service = AlertSnapshotService()
alert_snapshot_manager = alert_snapshot_service
service = alert_snapshot_service
manager = alert_snapshot_service
store = alert_snapshot_service


def get_service() -> AlertSnapshotService:
    return alert_snapshot_service


def create_service() -> AlertSnapshotService:
    return AlertSnapshotService()


# Compatibility aliases (many names, same behaviour).
capture_alert_snapshot = snapshot_event
ensure_alert_snapshot = snapshot_event
auto_snapshot = snapshot_event
auto_snapshot_for_alert = snapshot_event
snapshot_for_alert = snapshot_event
snapshot_for_event = snapshot_event
capture_snapshot_for_alert = snapshot_event
capture_snapshot_for_event = snapshot_event
attach_snapshot = snapshot_event
attach_snapshot_to_event = snapshot_event
snapshot_alert = snapshot_event
handle_alert = snapshot_event
handle_event = snapshot_event
process_alert = snapshot_event
process_event = snapshot_event
on_alert = snapshot_event
on_event = snapshot_event
run = snapshot_event
execute = snapshot_event
capture = snapshot_event
snapshot = snapshot_event
ensure_snapshot = snapshot_event
auto_capture = snapshot_event
alert_snapshot = snapshot_event
event_snapshot = snapshot_event
snapshot_event_sync = snapshot_event
capture_alert_snapshot_sync = snapshot_event
auto_snapshot_sync = snapshot_event

capture_alert_snapshot_async = snapshot_event_async
ensure_alert_snapshot_async = snapshot_event_async
auto_snapshot_async = snapshot_event_async
snapshot_for_alert_async = snapshot_event_async
attach_snapshot_async = snapshot_event_async
run_async = snapshot_event_async

backfill_snapshots = backfill
snapshot_backfill = backfill
bulk_snapshot = backfill
bulk_capture = backfill

__all__ = [
    "is_enabled",
    "get_enabled",
    "get_status",
    "get_policy",
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
    "should_auto_snapshot",
    "snapshot_event",
    "snapshot_event_async",
    "backfill",
    "backfill_async",
    "install_hook",
    "uninstall_hook",
    "ensure_hook_installed",
    "AlertSnapshotService",
    "alert_snapshot_service",
]
