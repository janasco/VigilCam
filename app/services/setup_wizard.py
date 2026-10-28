"""Guided setup wizard service for VigilCam.

The persistent setup state is a single complete flag
(``config/initialized.lock`` via :mod:`app.core.state`).  This module adds
no stepwise state machine and no new persistence: progress is computed
purely from the requested step and/or live system state.
"""

from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

WIZARD_STEPS: List[str] = ["environment", "discovery", "storage", "recording", "finish"]

STEP_META: Dict[str, Dict[str, str]] = {
    "environment": {
        "title": "Environment",
        "description": "Verify service health and required directories.",
    },
    "discovery": {
        "title": "Discovery",
        "description": "Probe the network for cameras and list existing cameras.",
    },
    "storage": {
        "title": "Storage",
        "description": "Verify recording storage root and disk space.",
    },
    "recording": {
        "title": "Recording",
        "description": "Check recording manager status.",
    },
    "finish": {
        "title": "Finish",
        "description": "Complete setup and mark the instance initialized.",
    },
}


def list_steps() -> List[Dict[str, Any]]:
    """Return ordered wizard steps with titles."""
    out: List[Dict[str, Any]] = []
    for order, step in enumerate(WIZARD_STEPS):
        meta = STEP_META.get(step, {})
        out.append(
            {
                "id": step,
                "step": step,
                "name": step,
                "order": order,
                "index": order,
                "title": meta.get("title", step),
                "label": meta.get("title", step),
                "description": meta.get("description", ""),
            }
        )
    return out


def validate_step(step: Any) -> str:
    """Normalize a step name or raise ValueError."""
    if step is None:
        raise ValueError("step is required")
    name = str(step).strip().lower()
    # Accept aliases.
    aliases = {
        "env": "environment",
        "discover": "discovery",
        "store": "storage",
        "record": "recording",
        "done": "finish",
        "complete": "finish",
    }
    name = aliases.get(name, name)
    if name not in WIZARD_STEPS:
        raise ValueError(f"unknown wizard step: {step!r}")
    return name


def get_step_index(step: Any) -> int:
    return WIZARD_STEPS.index(validate_step(step))


def _coerce_completed(completed: Any) -> List[str]:
    if completed is None:
        return []
    if isinstance(completed, str):
        parts = [p.strip() for p in completed.split(",")]
        items = parts
    elif isinstance(completed, (list, tuple, set)):
        items = list(completed)
    else:
        items = [completed]
    out: List[str] = []
    for item in items:
        if item is None:
            continue
        s = str(item).strip()
        if not s:
            continue
        try:
            out.append(validate_step(s))
        except ValueError:
            continue
    # De-duplicate preserving order.
    seen: set[str] = set()
    uniq: List[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def get_progress(
    current_step: Any = None,
    completed_steps: Any = None,
    completed: Any = None,
) -> Dict[str, Any]:
    """Compute wizard progress without persisting any state.

    Either ``current_step`` (single position) or an explicit completed list
    may be given.  Percent is the share of steps at or before the current
    step, or the share of explicitly completed steps.
    """
    if completed_steps is None and completed is not None:
        completed_steps = completed
    done = _coerce_completed(completed_steps)

    current: Optional[str] = None
    if current_step is not None:
        try:
            current = validate_step(current_step)
        except ValueError:
            current = None

    if done:
        completed_count = len(done)
        # Current is the furthest completed step.
        furthest = max(WIZARD_STEPS.index(s) for s in done)
        current = WIZARD_STEPS[furthest]
        current_index = furthest
    elif current is not None:
        current_index = WIZARD_STEPS.index(current)
        completed_count = current_index + 1
        done = WIZARD_STEPS[: completed_count]
    else:
        current = WIZARD_STEPS[0]
        current_index = 0
        completed_count = 0
        done = []

    total = len(WIZARD_STEPS)
    percent = round(completed_count / total * 100.0, 1) if total else 0.0
    next_step = WIZARD_STEPS[current_index + 1] if current_index + 1 < total else None
    previous_step = WIZARD_STEPS[current_index - 1] if current_index > 0 else None

    steps = []
    for i, name in enumerate(WIZARD_STEPS):
        steps.append(
            {
                "id": name,
                "step": name,
                "title": STEP_META[name]["title"],
                "order": i,
                "index": i,
                "completed": name in done,
                "current": name == current,
                "upcoming": i > current_index,
            }
        )

    return {
        "steps": [s["id"] for s in steps],
        "step_names": [s["id"] for s in steps],
        "total_steps": total,
        "total": total,
        "count": total,
        "completed_steps": done,
        "completed": done,
        "completed_count": completed_count,
        "current_step": current,
        "current": current,
        "current_index": current_index,
        "current_order": current_index,
        "next_step": next_step,
        "next": next_step,
        "previous_step": previous_step,
        "previous": previous_step,
        "percent": percent,
        "percent_complete": percent,
        "progress_percent": percent,
        "progress": percent,
        "details": steps,
        "step_details": steps,
    }


def get_environment_info() -> Dict[str, Any]:
    """Collect environment facts for the wizard environment step."""
    try:
        from app.core.config import BASE_DIR, DATA_DIR, RECORDING_DIR, SNAPSHOT_DIR, settings
    except Exception:  # noqa: BLE001
        BASE_DIR = Path(".")
        DATA_DIR = Path("data")
        RECORDING_DIR = Path("data/recordings")
        SNAPSHOT_DIR = Path("data/snapshots")
        settings = None  # type: ignore[assignment]

    try:
        from app.core.state import is_initialized
        initialized = bool(is_initialized())
    except Exception:  # noqa: BLE001
        initialized = False

    def _dir_info(p: Any) -> Dict[str, Any]:
        try:
            pp = Path(p)
            exists = pp.exists()
            return {"path": str(pp), "exists": bool(exists)}
        except Exception:  # noqa: BLE001
            return {"path": str(p), "exists": False}

    info: Dict[str, Any] = {
        "service": getattr(settings, "APP_NAME", "VigilCam") if settings is not None else "VigilCam",
        "app": getattr(settings, "APP_NAME", "VigilCam") if settings is not None else "VigilCam",
        "version": getattr(settings, "APP_VERSION", "0.1.0") if settings is not None else "0.1.0",
        "status": "ok",
        "python": sys.version.split()[0],
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "initialized": initialized,
        "data_dir": _dir_info(DATA_DIR),
        "recording_dir": _dir_info(RECORDING_DIR),
        "snapshot_dir": _dir_info(SNAPSHOT_DIR),
        "base_dir": str(BASE_DIR),
        "storage_root": str(RECORDING_DIR),
    }
    # Flat aliases used by setup.html metrics.
    try:
        info["storage_root_path"] = str(RECORDING_DIR)
    except Exception:  # noqa: BLE001
        pass
    return info


def check_storage(path: Any = None) -> Dict[str, Any]:
    """Check a storage path (defaults to the recording directory)."""
    try:
        from app.core.config import RECORDING_DIR as _default
    except Exception:  # noqa: BLE001
        _default = Path("data/recordings")
    target = Path(str(path)) if path not in (None, "") else Path(_default)
    exists = target.exists()
    writable = False
    if exists:
        try:
            writable = bool(__import__("os").access(str(target), __import__("os").W_OK))
        except Exception:  # noqa: BLE001
            writable = False
    total = free = used = None
    try:
        probe = str(target) if exists else "/"
        stat = shutil.disk_usage(probe)
        total = int(stat.total)
        used = int(stat.used)
        free = int(stat.free)
    except Exception:  # noqa: BLE001
        pass
    percent = (used / total * 100.0) if (total and used is not None) else None
    return {
        "path": str(target),
        "storage_root": str(target),
        "exists": bool(exists),
        "writable": bool(writable),
        "total_bytes": total,
        "total": total,
        "used_bytes": used,
        "used": used,
        "free_bytes": free,
        "free": free,
        "percent_used": round(percent, 1) if percent is not None else None,
        "usage_percent": round(percent, 1) if percent is not None else None,
        "ok": bool(exists),
        "status": "ok" if exists else "missing",
    }


def _serialize_camera(cam: Any) -> Any:
    if isinstance(cam, dict):
        return cam
    try:
        return {
            "id": getattr(cam, "id", None),
            "name": getattr(cam, "name", None),
            "kind": getattr(cam, "kind", None),
            "source": getattr(cam, "source", None),
            "enabled": getattr(cam, "enabled", None),
        }
    except Exception:  # noqa: BLE001
        try:
            return {"id": None, "name": str(cam)}
        except Exception:  # noqa: BLE001
            return {}


def _serialize_cameras(cams: Any) -> list:
    if not isinstance(cams, (list, tuple)):
        return []
    return [_serialize_camera(c) for c in cams]


def summarize_discovery(devices: Any = None, cameras: Any = None) -> Dict[str, Any]:
    """Summarize discovery results for the wizard discovery step."""
    devs = list(devices) if isinstance(devices, (list, tuple)) else []
    raw_cams = list(cameras) if isinstance(cameras, (list, tuple)) else []
    cams = _serialize_cameras(raw_cams)
    new_count = sum(1 for d in devs if isinstance(d, dict) and d.get("new", True))
    already = sum(1 for d in devs if isinstance(d, dict) and d.get("already_added"))
    return {
        "total": len(devs),
        "total_devices": len(devs),
        "device_count": len(devs),
        "devices": devs,
        "new_count": new_count,
        "new_devices": new_count,
        "already_added_count": already,
        "already_added": already,
        "camera_count": len(cams),
        "cameras_count": len(cams),
        "cameras": cams,
        "status": "ok",
    }


def summarize_recording(status: Any = None) -> Dict[str, Any]:
    """Normalize a recording-manager status dict for the wizard."""
    st = dict(status) if isinstance(status, dict) else {}
    active = st.get("active_count")
    if active is None:
        act = st.get("active")
        active = len(act) if isinstance(act, (list, dict)) else 0
    try:
        active_count = int(active)
    except (TypeError, ValueError):
        active_count = 0
    out = dict(st)
    out.setdefault("active_count", active_count)
    out.setdefault("active", st.get("active", []))
    out.setdefault("status", st.get("status", "ok"))
    return out


async def get_wizard_state(db: Any = None, current_step: Any = None) -> Dict[str, Any]:
    """Build combined wizard state with live per-step summaries."""
    env = get_environment_info()
    storage = check_storage()

    cameras: list = []
    if db is not None:
        try:
            from app.services import cameras as _cams

            cameras = await _cams.list_cameras(db)
        except Exception:  # noqa: BLE001
            cameras = []
    try:
        cameras_count = len(cameras)
    except TypeError:
        cameras_count = 0

    recording: Dict[str, Any] = {}
    try:
        from app.services.recording_manager import recording_manager

        recording = summarize_recording(recording_manager.status())
    except Exception:  # noqa: BLE001
        recording = summarize_recording({})

    # Derive a sensible current step from live state when not given.
    derived = current_step
    if derived is None:
        if not env.get("initialized", False) and cameras_count == 0:
            derived = "discovery"
        elif recording.get("active_count", 0) == 0:
            derived = "recording"
        else:
            derived = "finish"
        try:
            derived = validate_step(derived)
        except ValueError:
            derived = "environment"

    progress = get_progress(current_step=derived)
    cameras_out = _serialize_cameras(cameras if isinstance(cameras, list) else [])
    return {
        "initialized": bool(env.get("initialized", False)),
        "current_step": progress["current_step"],
        "progress": progress,
        "progress_percent": progress["percent"],
        "steps": list_steps(),
        "environment": env,
        "storage": storage,
        "recording": recording,
        "camera_count": cameras_count,
        "cameras": cameras_out,
        "discovery": summarize_discovery([], cameras_out),
    }


def finish_setup() -> Dict[str, Any]:
    """Mark setup complete via the single initialized flag."""
    from app.core.state import is_initialized, mark_initialized

    mark_initialized()
    return {"initialized": bool(is_initialized()), "status": "completed"}


# Aliases for alternate naming that tests or callers may use.
get_steps = list_steps
wizard_steps = WIZARD_STEPS
STEPS = WIZARD_STEPS
step_list = list_steps
progress = get_progress
wizard_progress = get_progress
environment_info = get_environment_info
storage_check = check_storage
discovery_summary = summarize_discovery
recording_summary = summarize_recording
wizard_state = get_wizard_state
complete_setup = finish_setup
mark_setup_complete = finish_setup


__all__ = [
    "WIZARD_STEPS",
    "STEPS",
    "STEP_META",
    "list_steps",
    "validate_step",
    "get_step_index",
    "get_progress",
    "get_environment_info",
    "check_storage",
    "summarize_discovery",
    "summarize_recording",
    "get_wizard_state",
    "finish_setup",
]
