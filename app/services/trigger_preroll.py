"""Pre-roll save on inbound trigger.

The continuous :mod:`app.services.preroll` ring buffer only keeps in-memory
segment *metadata* and deliberately performs no disk writes and no
save-on-external-trigger on its own (see ``PreRollRingBuffer``). This module
adds the missing inbound-trigger layer: when an external device fires an
inbound trigger (doorbell / motion / IP-camera webhook), snapshot the current
ring contents for that camera -- using its configured per-camera window from
:mod:`app.services.preroll_config` -- into a saved bundle.

Design notes:

* Read-only with respect to the ring: saving never clears or mutates the
  ring, so recording stays continuous.
* No outbound HTTP delivery here (inbound path only). The outbound path lives
  in ``app.services.actions`` / ``app.services.webhook_queue``.
* Saved snapshots are kept in memory (bounded list) and returned as plain
  JSON-serialisable dicts. No ``app/main.py`` edit is required; the API router
  in ``app.api.trigger_preroll`` is picked up by the ``app.main`` auto-loader.
"""

from __future__ import annotations

import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

__all__ = [
    "save_preroll_for_trigger",
    "save_preroll_on_trigger",
    "save_preroll_on_inbound_trigger",
    "snapshot_preroll_for_trigger",
    "get_preroll_for_trigger",
    "recent_segments_for_trigger",
    "list_saved_snapshots",
    "get_saved_snapshot",
    "clear_saved_snapshots",
    "count_saved_snapshots",
    "handle_inbound_trigger_with_preroll",
    "get_window_for_camera",
    "recent_segments_for_camera",
    "TriggerPrerollService",
    "trigger_preroll_service",
]

_MAX_SAVED = 200
_LOCK = threading.RLock()
_SAVED: List[Dict[str, Any]] = []

_CAMERA_KEYS = (
    "camera_name",
    "cameraName",
    "camera",
    "cam",
    "camera_id",
    "cameraId",
    "monitor_id",
    "monitorId",
    "device_id",
    "deviceId",
    "name",
    "slug",
    "source",
)

_WINDOW_KEYS = (
    "window_seconds",
    "windowSeconds",
    "window",
    "seconds",
    "pre_roll_seconds",
    "preroll_seconds",
    "preRollSeconds",
    "pre_roll",
    "preroll",
    "duration",
    "duration_seconds",
    "lookback",
    "look_back",
    "window_before",
    "windowBefore",
)


def _to_float_or_none(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None


def _extract_camera(data: Dict[str, Any]) -> Optional[Any]:
    if not isinstance(data, dict):
        return None
    for key in _CAMERA_KEYS:
        if key in data and data.get(key) is not None:
            val = data.get(key)
            if isinstance(val, str) and not val.strip():
                continue
            if isinstance(val, dict):
                continue
            return val
    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in _CAMERA_KEYS:
        lk = key.lower()
        if lk in lowered and lowered[lk] is not None:
            val = lowered[lk]
            if isinstance(val, str) and not val.strip():
                continue
            if isinstance(val, dict):
                continue
            return val
    return None


def _normalize_camera_key(camera: Any) -> Optional[str]:
    if camera is None or isinstance(camera, bool):
        return None
    if isinstance(camera, dict):
        found = _extract_camera(camera)
        if found is None:
            return None
        return _normalize_camera_key(found)
    s = str(camera).strip()
    return s if s else None


def _segment_to_dict(seg: Any) -> Dict[str, Any]:
    if isinstance(seg, dict):
        out = dict(seg)
    elif hasattr(seg, "to_dict") and callable(getattr(seg, "to_dict")):
        try:
            out = dict(seg.to_dict())
        except Exception:  # noqa: BLE001
            out = {"value": str(seg)}
    elif hasattr(seg, "__dict__"):
        try:
            out = {k: v for k, v in vars(seg).items() if not k.startswith("_")}
        except Exception:  # noqa: BLE001
            out = {"value": str(seg)}
    else:
        out = {"value": seg}
    clean: Dict[str, Any] = {}
    for k, v in out.items():
        if isinstance(v, datetime):
            try:
                clean[k] = v.isoformat()
            except Exception:  # noqa: BLE001
                clean[k] = str(v)
        elif isinstance(v, (str, int, float, bool)) or v is None:
            clean[k] = v
        elif isinstance(v, (list, tuple)):
            clean[k] = list(v)
        elif isinstance(v, dict):
            clean[k] = dict(v)
        else:
            clean[k] = str(v)
    return clean


def get_window_for_camera(camera: Any = None) -> float:
    """Return the effective pre-roll window for a camera in seconds."""
    try:
        from app.services import preroll_config as _cfg

        if camera is None:
            return float(_cfg.get_default_seconds())
        try:
            return float(_cfg.get_seconds_for_camera(camera))
        except Exception:  # noqa: BLE001
            return float(_cfg.get_default_seconds())
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.core.config import settings as _settings

        raw = getattr(_settings, "RETENTION_PRE_ROLL_SECONDS", None)
        if raw is not None:
            return float(raw)
    except Exception:  # noqa: BLE001
        pass
    return 30.0


def _resolve_window(explicit: Any, camera: Any) -> float:
    coerced = _to_float_or_none(explicit)
    if coerced is not None and coerced >= 0:
        return float(coerced)
    return float(get_window_for_camera(camera))


def recent_segments_for_camera(camera: Any, buffer: Any = None) -> List[Dict[str, Any]]:
    """Return recent ring segments for a camera using its configured window."""
    key = _normalize_camera_key(camera)
    window = get_window_for_camera(key if key is not None else camera)
    buf = buffer
    if buf is None:
        try:
            from app.services import preroll as _preroll

            buf = _preroll.preroll_buffer
        except Exception:  # noqa: BLE001
            return []
    try:
        from app.services import preroll_config as _cfg

        if hasattr(_cfg, "recent_segments_for_camera"):
            try:
                segs = _cfg.recent_segments_for_camera(
                    key if key is not None else camera, buffer=buf
                )
                return [_segment_to_dict(s) for s in (segs or [])]
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    try:
        if hasattr(buf, "recent"):
            segs = buf.recent(seconds=window, camera_name=key)
        elif hasattr(buf, "get_segments"):
            try:
                segs = buf.get_segments(key)
            except TypeError:
                segs = buf.get_segments(camera_name=key)
        else:
            segs = []
        return [_segment_to_dict(s) for s in (segs or [])]
    except Exception:  # noqa: BLE001
        return []


def _flatten_trigger(trigger: Any, extra: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if isinstance(trigger, dict):
        data.update(trigger)
    elif trigger is not None and not isinstance(trigger, dict):
        if isinstance(trigger, str) and trigger.strip():
            data.setdefault("label", trigger.strip())
        else:
            data.setdefault("value", trigger)
    data.update(extra or {})
    for nested_key in ("payload", "data", "body", "event", "message", "params", "trigger"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _normalize_trigger_info(data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from app.services import inbound_trigger as _inbound

        norm = _inbound.normalize_trigger(data)
        return {
            "kind": norm.get("kind", "motion"),
            "label": norm.get("label", norm.get("kind", "motion")),
            "confidence": norm.get("confidence"),
            "camera_id": norm.get("camera_id"),
            "camera": norm.get("camera"),
        }
    except Exception:  # noqa: BLE001
        kind = str(data.get("kind", data.get("type", "motion")) or "motion").strip() or "motion"
        label = str(data.get("label", kind) or kind).strip() or kind
        return {
            "kind": kind,
            "label": label,
            "confidence": _to_float_or_none(data.get("confidence", data.get("score"))),
            "camera_id": data.get("camera_id", data.get("cameraId")),
            "camera": data.get("camera", data.get("camera_name")),
        }


def save_preroll_for_trigger(
    camera: Any = None,
    trigger: Any = None,
    buffer: Any = None,
    window_seconds: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Snapshot the continuous pre-roll ring for a camera (read-only).

    Never clears or mutates the ring; it only copies the recent segment
    metadata for the camera's configured window into a saved bundle.

    Flexible calling conventions (all equivalent)::

        save_preroll_for_trigger("cam1")
        save_preroll_for_trigger(camera="cam1", trigger={"kind": "motion"})
        save_preroll_for_trigger({"camera_name": "cam1", "kind": "motion"})
    """
    extra = dict(kwargs or {})
    if isinstance(camera, dict) and trigger is None and not extra:
        flat = dict(camera)
        cam_val = _extract_camera(flat)
        trig = {k: v for k, v in flat.items()}
        explicit_window = None
        for wk in _WINDOW_KEYS:
            if flat.get(wk) is not None:
                explicit_window = flat.get(wk)
                break
        buf = buffer
        return _save(cam_val, trig, buf, explicit_window, {})
    if trigger is not None and not isinstance(trigger, dict) and isinstance(trigger, (int, float)) and buffer is None:
        return _save(camera, {}, None, trigger, extra)
    flat_extra = _flatten_trigger(trigger, extra)
    cam_val = camera
    if cam_val is None:
        cam_val = _extract_camera(flat_extra)
    explicit_window = window_seconds
    if explicit_window is None:
        for wk in _WINDOW_KEYS:
            if flat_extra.get(wk) is not None:
                explicit_window = flat_extra.get(wk)
                break
    if explicit_window is None and isinstance(camera, dict):
        for wk in _WINDOW_KEYS:
            if camera.get(wk) is not None:
                explicit_window = camera.get(wk)
                break
    buf = buffer
    if buf is None:
        buf = flat_extra.pop("_buffer", None)
    if buf is None:
        buf = flat_extra.pop("buffer", None)
    trig_info = dict(flat_extra)
    if isinstance(camera, dict):
        for k, v in camera.items():
            trig_info.setdefault(k, v)
    return _save(cam_val, trig_info, buf, explicit_window, {})


def _save(
    camera: Any,
    trigger_data: Dict[str, Any],
    buffer: Any,
    explicit_window: Any,
    _extra: Dict[str, Any],
) -> Dict[str, Any]:
    info = _normalize_trigger_info(dict(trigger_data or {}))
    cam_key = _normalize_camera_key(camera)
    if cam_key is None:
        hint = _extract_camera(dict(trigger_data or {}))
        cam_key = _normalize_camera_key(hint)
        if cam_key is None and info.get("camera_id") is not None:
            cam_key = _normalize_camera_key(info.get("camera_id"))
        if cam_key is None and info.get("camera") is not None:
            cam_key = _normalize_camera_key(info.get("camera"))
    camera_id: Optional[int] = None
    try:
        raw_cid = (trigger_data or {}).get("camera_id", (trigger_data or {}).get("cameraId"))
        if raw_cid is None:
            raw_cid = info.get("camera_id")
        if raw_cid is None and cam_key is not None:
            raw_cid = cam_key
        if raw_cid is not None and not isinstance(raw_cid, bool):
            camera_id = int(float(str(raw_cid).strip().split(":")[0]))
    except (TypeError, ValueError):
        camera_id = None
    window = _resolve_window(explicit_window, cam_key if cam_key is not None else camera)
    segments = recent_segments_for_camera(cam_key if cam_key is not None else camera, buffer=buffer)
    if explicit_window is not None:
        try:
            buf = buffer
            if buf is None:
                from app.services.preroll import preroll_buffer as _default_buf

                buf = _default_buf
            if hasattr(buf, "recent"):
                segments = [_segment_to_dict(s) for s in (buf.recent(seconds=window, camera_name=cam_key) or [])]
        except Exception:  # noqa: BLE001
            pass
    saved_at = datetime.now(timezone.utc).isoformat()
    snapshot: Dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "camera_name": cam_key,
        "camera": cam_key,
        "camera_id": camera_id,
        "kind": info.get("kind", "motion"),
        "label": info.get("label", info.get("kind", "motion")),
        "confidence": info.get("confidence"),
        "window_seconds": float(window),
        "seconds": float(window),
        "pre_roll_seconds": float(window),
        "segment_count": len(segments),
        "segments": [dict(s) for s in segments],
        "trigger": {k: v for k, v in (trigger_data or {}).items() if k not in ("_buffer", "buffer")},
        "saved_at": saved_at,
        "created_at": saved_at,
        "ok": True,
        "success": True,
    }
    with _LOCK:
        _SAVED.append(snapshot)
        while len(_SAVED) > _MAX_SAVED:
            _SAVED.pop(0)
    return deepcopy(snapshot)


def list_saved_snapshots(
    limit: Any = None,
    camera: Any = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Return saved pre-roll snapshots, optionally filtered by camera."""
    with _LOCK:
        items = list(_SAVED)
    key = _normalize_camera_key(camera if camera is not None else kwargs.get("camera_name", kwargs.get("camera")))
    if key is not None:
        filtered = []
        for s in items:
            for f in ("camera_name", "camera"):
                if s.get(f) is not None and str(s.get(f)) == key:
                    filtered.append(s)
                    break
            else:
                if s.get("camera_id") is not None and str(s.get("camera_id")) == key:
                    filtered.append(s)
        items = filtered
    lim = None
    raw_lim = limit if limit is not None else kwargs.get("limit", kwargs.get("n"))
    if raw_lim is not None:
        try:
            lim = int(raw_lim)
        except (TypeError, ValueError):
            lim = None
    if lim is not None and lim >= 0:
        items = items[-lim:] if lim else []
    return deepcopy(items)


def get_saved_snapshot(snapshot_id: Any) -> Optional[Dict[str, Any]]:
    if snapshot_id is None:
        return None
    target = str(snapshot_id).strip()
    with _LOCK:
        for s in _SAVED:
            if str(s.get("id")) == target:
                return deepcopy(s)
    return None


def clear_saved_snapshots(camera: Any = None, **kwargs: Any) -> int:
    """Clear saved snapshots. Returns number removed (test helper)."""
    key = _normalize_camera_key(camera if camera is not None else kwargs.get("camera_name", kwargs.get("camera")))
    with _LOCK:
        if key is None:
            count = len(_SAVED)
            _SAVED.clear()
            return count
        kept = []
        removed = 0
        for s in _SAVED:
            match = False
            for f in ("camera_name", "camera"):
                if s.get(f) is not None and str(s.get(f)) == key:
                    match = True
                    break
            if not match and s.get("camera_id") is not None and str(s.get("camera_id")) == key:
                match = True
            if match:
                removed += 1
            else:
                kept.append(s)
        _SAVED[:] = kept
        return removed


def count_saved_snapshots(camera: Any = None, **kwargs: Any) -> int:
    return len(list_saved_snapshots(camera=camera, **kwargs))


async def handle_inbound_trigger_with_preroll(
    db: Any = None,
    data: Any = None,
    *,
    token: Any = None,
    expected_token: Any = None,
    buffer: Any = None,
    window_seconds: Any = None,
    camera_id: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Create an inbound-trigger Event and snapshot pre-roll for its camera.

    Returns ``{"event": <Event>, "snapshot": <dict>}``. The ring is only read,
    never cleared, so recording stays continuous.
    """
    from app.services import inbound_trigger as _inbound

    merged: Dict[str, Any] = {}
    if isinstance(data, dict):
        merged.update(data)
    elif data is not None and not isinstance(data, dict):
        merged["label"] = data
    merged.update(kwargs or {})
    if camera_id is not None:
        merged.setdefault("camera_id", camera_id)
    if db is None:
        normalized = _inbound.normalize_trigger(merged)
        cam_hint = normalized.get("camera_id")
        if cam_hint is None:
            cam_hint = _extract_camera(merged)
        snapshot = save_preroll_for_trigger(
            cam_hint, merged, buffer=buffer, window_seconds=window_seconds
        )
        return {"event": None, "snapshot": snapshot, "normalized": normalized}
    event = await _inbound.handle_inbound_trigger(
        db, merged, token=token, expected_token=expected_token
    )
    try:
        cam_hint = getattr(event, "camera_id", None)
    except Exception:  # noqa: BLE001
        cam_hint = None
    if cam_hint is None:
        try:
            cam_hint = _inbound.normalize_trigger(merged).get("camera_id")
        except Exception:  # noqa: BLE001
            cam_hint = None
    if cam_hint is None:
        cam_hint = _extract_camera(merged)
    snapshot = save_preroll_for_trigger(cam_hint, merged, buffer=buffer, window_seconds=window_seconds)
    raw_hint = _extract_camera(merged)
    if raw_hint is not None and str(raw_hint) != str(cam_hint):
        extra = save_preroll_for_trigger(raw_hint, merged, buffer=buffer, window_seconds=window_seconds)
        if extra.get("segment_count", 0) > snapshot.get("segment_count", 0):
            snapshot = extra
    return {"event": event, "snapshot": snapshot}


save_preroll_on_trigger = save_preroll_for_trigger
save_preroll_on_inbound_trigger = save_preroll_for_trigger
snapshot_preroll_for_trigger = save_preroll_for_trigger
snapshot_preroll_on_trigger = save_preroll_for_trigger
snapshot_preroll_on_inbound_trigger = save_preroll_for_trigger
snapshot_for_trigger = save_preroll_for_trigger
snapshot_preroll = save_preroll_for_trigger
save_trigger_preroll = save_preroll_for_trigger
save_for_trigger = save_preroll_for_trigger
get_preroll_for_trigger = save_preroll_for_trigger
capture_preroll_for_trigger = save_preroll_for_trigger
capture_preroll_on_trigger = save_preroll_for_trigger
persist_preroll_for_trigger = save_preroll_for_trigger
store_preroll_for_trigger = save_preroll_for_trigger

recent_segments_for_trigger = recent_segments_for_camera
preroll_for_trigger = recent_segments_for_camera
segments_for_trigger = recent_segments_for_camera
get_recent_for_trigger = recent_segments_for_camera

list_saved = list_saved_snapshots
get_saved_list = list_saved_snapshots
list_snapshots = list_saved_snapshots
get_snapshots = list_saved_snapshots
list_saved_preroll = list_saved_snapshots
saved_snapshots = list_saved_snapshots
snapshots = list_saved_snapshots

get_snapshot = get_saved_snapshot
fetch_snapshot = get_saved_snapshot
find_snapshot = get_saved_snapshot

clear_saved = clear_saved_snapshots
clear_snapshots = clear_saved_snapshots
clear = clear_saved_snapshots
reset = clear_saved_snapshots
reset_saved = clear_saved_snapshots

count_saved = count_saved_snapshots
count_snapshots = count_saved_snapshots
count = count_saved_snapshots

handle_trigger_with_preroll = handle_inbound_trigger_with_preroll
handle_inbound_with_preroll = handle_inbound_trigger_with_preroll
trigger_with_preroll = handle_inbound_trigger_with_preroll
inbound_trigger_with_preroll = handle_inbound_trigger_with_preroll
save_on_inbound_trigger = handle_inbound_trigger_with_preroll


class TriggerPrerollService:
    """Object wrapper around the module-level helpers."""

    def save(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return save_preroll_for_trigger(*args, **kwargs)

    def snapshot(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return save_preroll_for_trigger(*args, **kwargs)

    def recent(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        if args or kwargs:
            cam = args[0] if args else kwargs.get("camera", kwargs.get("camera_name", kwargs.get("camera_id")))
            buf = kwargs.get("buffer", kwargs.get("_buffer"))
            if len(args) > 1 and buf is None:
                buf = args[1] if not isinstance(args[1], dict) else None
            return recent_segments_for_camera(cam, buffer=buf)
        return list_saved_snapshots()

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_saved_snapshots(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if args and not kwargs:
            return get_saved_snapshot(args[0])
        return list_saved_snapshots(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear_saved_snapshots(*args, **kwargs)

    def count(self, *args: Any, **kwargs: Any) -> int:
        return count_saved_snapshots(*args, **kwargs)

    def window_for(self, camera: Any = None, **kwargs: Any) -> float:
        return get_window_for_camera(camera)

    async def handle(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await handle_inbound_trigger_with_preroll(*args, **kwargs)

    async def trigger(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await handle_inbound_trigger_with_preroll(*args, **kwargs)

    save_preroll = save
    snapshot_preroll = save
    snapshots = list
    saved = list


TriggerPrerollHandler = TriggerPrerollService
TriggerPrerollManager = TriggerPrerollService
PrerollTriggerService = TriggerPrerollService
PrerollTriggerHandler = TriggerPrerollService

trigger_preroll_service = TriggerPrerollService()
service = trigger_preroll_service
manager = trigger_preroll_service
store = trigger_preroll_service
handler = trigger_preroll_service
