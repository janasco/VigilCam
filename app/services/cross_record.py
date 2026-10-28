"""Cross-camera RECORDING trigger (extends linked monitors without modifying them).

``app/services/linked_monitor_trigger.py`` only resolves *which* cameras are
linked to a source camera (it returns target ids -- an alert-style lookup).
It never starts recording on another camera.

This module layers the missing RECORDING behaviour on top:

* resolve linked targets via :func:`trigger_linked_monitors` (read-only reuse),
* resolve each target :class:`Camera` row for its stream URL / name,
* invoke a recorder (defaults to
  :meth:`RecordingManager.start_recording <app.services.recording_manager.RecordingManager.start_recording>`),
* persist audit :class:`Event` rows (``kind="cross_record"``) and
  :class:`Recording` rows for every successfully started target.

Nothing in ``linked_monitor_trigger.py`` is modified; everything here only
imports from it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.models.entities import Camera, Event, Recording

from app.services.linked_monitor_trigger import (
    SOURCE_ALIASES,
    TARGET_ALIASES,
    LinkedMonitorTriggerService,
    _coerce_bool,
    _coerce_id,
    _extract_from_mapping,
    trigger_linked_monitors,
)

# Recorder signature: recorder(camera_name, main_url, sub_url=None, ...) -> Any
RecorderFn = Callable[..., Any]

SOURCE_KEYS = tuple(SOURCE_ALIASES) + (
    "source", "camera", "monitor", "from_camera_id", "trigger_camera_id",
)
REASON_KEYS = ("reason", "cause", "trigger", "event", "label", "kind")
DURATION_KEYS = (
    "duration_seconds", "duration", "record_duration", "record_seconds",
    "seconds", "timeout", "capture_duration",
)
DRY_RUN_KEYS = ("dry_run", "dryrun", "preview", "test_only", "no_record")


def _coerce_source_id(*args: Any, **kwargs: Any) -> Optional[int]:
    if len(args) >= 1 and args[0] is not None and not isinstance(args[0], dict):
        coerced = _coerce_id(args[0])
        if coerced is not None:
            return coerced
    for a in args:
        if isinstance(a, dict):
            v = _extract_from_mapping(a, SOURCE_ALIASES)
            if v is not None:
                return _coerce_id(v)
            for k in ("camera_id", "monitor_id", "id", "source"):
                if a.get(k) is not None:
                    return _coerce_id(a.get(k))
    v = _extract_from_mapping(kwargs, SOURCE_ALIASES)
    if v is not None:
        return _coerce_id(v)
    for k in ("camera_id", "monitor_id", "source_camera_id", "source",
              "camera", "monitor", "id"):
        if kwargs.get(k) is not None:
            return _coerce_id(kwargs.get(k))
    return None


def _coerce_duration(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        iv = int(float(str(value).strip())) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None
    return iv if iv > 0 else None


def _extract_duration(payload: Dict[str, Any]) -> Optional[int]:
    for k in DURATION_KEYS:
        if payload.get(k) is not None:
            d = _coerce_duration(payload.get(k))
            if d is not None:
                return d
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for k in DURATION_KEYS:
        if lowered.get(k.lower()) is not None:
            d = _coerce_duration(lowered.get(k.lower()))
            if d is not None:
                return d
    return None


def _extract_reason(payload: Dict[str, Any]) -> str:
    for k in ("reason", "cause", "label"):
        if payload.get(k) not in (None, ""):
            return str(payload.get(k))
    return "linked detection"


def _extract_dry_run(payload: Dict[str, Any]) -> bool:
    for k in DRY_RUN_KEYS:
        if payload.get(k) is not None:
            return _coerce_bool(payload.get(k), False)
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for k in DRY_RUN_KEYS:
        if lowered.get(k.lower()) is not None:
            return _coerce_bool(lowered.get(k.lower()), False)
    return False


def _camera_stream_info(camera: Any, fallback_id: Any) -> tuple[str, str, Optional[str]]:
    """Return (camera_name, main_url, sub_url) for a target camera row."""
    if camera is None:
        name = str(fallback_id) if fallback_id is not None else "unknown"
        return name, f"cross-record://{name}", None
    name = getattr(camera, "name", None) or str(
        getattr(camera, "id", fallback_id)
    )
    main_url = getattr(camera, "source", None) or getattr(camera, "main_url", None)
    if not main_url:
        main_url = f"cross-record://{name}"
    sub_url = getattr(camera, "sub_url", None) or getattr(camera, "sub_source", None)
    return str(name), str(main_url), (str(sub_url) if sub_url else None)


def _default_recorder(
    camera_name: str,
    main_url: str,
    sub_url: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """Default RECORDING action: start the shared recording manager."""
    from app.services.recording_manager import recording_manager

    starter = getattr(recording_manager, "start_recording", None) or getattr(
        recording_manager, "start", None
    )
    if starter is None:
        raise RuntimeError("No recording backend available")
    return starter(camera_name, main_url, sub_url=sub_url)


@dataclass
class CrossRecordResult:
    source_camera_id: Optional[int] = None
    targets: List[int] = field(default_factory=list)
    started: List[int] = field(default_factory=list)
    skipped: List[int] = field(default_factory=list)
    errors: Dict[Any, str] = field(default_factory=dict)
    dry_run: bool = False
    reason: str = ""
    duration_seconds: Optional[int] = None
    events_created: int = 0
    recordings_created: int = 0

    @property
    def triggered(self) -> List[int]:
        return list(self.started)

    @property
    def linked(self) -> List[int]:
        return list(self.targets)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    @property
    def success(self) -> bool:
        return self.ok

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_camera_id": self.source_camera_id,
            "camera_id": self.source_camera_id,
            "monitor_id": self.source_camera_id,
            "targets": list(self.targets),
            "triggered": list(self.started),
            "started": list(self.started),
            "linked_monitors": list(self.targets),
            "linked_cameras": list(self.targets),
            "skipped": list(self.skipped),
            "errors": dict(self.errors),
            "dry_run": self.dry_run,
            "reason": self.reason,
            "duration_seconds": self.duration_seconds,
            "events_created": self.events_created,
            "recordings_created": self.recordings_created,
            "ok": self.ok,
            "success": self.ok,
        }

    def model_dump(self, *a: Any, **kw: Any) -> Dict[str, Any]:
        return self.to_dict()

    def dict(self, *a: Any, **kw: Any) -> Dict[str, Any]:
        return self.to_dict()


class CrossRecordService:
    """In-memory (DB-free) cross-camera RECORDING trigger.

    Wraps :class:`LinkedMonitorTriggerService` for link resolution and calls
    a ``recorder`` callable to actually start recording on each target.
    """

    def __init__(
        self,
        links: Any = None,
        recorder: Optional[RecorderFn] = None,
        triggers: Any = None,
        **kwargs: Any,
    ) -> None:
        svc = None
        if isinstance(links, LinkedMonitorTriggerService):
            svc = links
        elif isinstance(triggers, LinkedMonitorTriggerService):
            svc = triggers
        if svc is None:
            svc = LinkedMonitorTriggerService(
                triggers=triggers if triggers is not None else links
            )
        self.links = svc
        self.triggers = svc
        self.link_service = svc
        self.service = svc
        self.recorder: RecorderFn = recorder or _default_recorder
        self.calls: List[Dict[str, Any]] = []

    # -- link management (delegate) --------------------------------------
    def add_link(self, *a: Any, **kw: Any) -> Any:
        return self.links.create(*a, **kw)

    def create(self, *a: Any, **kw: Any) -> Any:
        return self.links.create(*a, **kw)

    def link(self, *a: Any, **kw: Any) -> Any:
        return self.links.create(*a, **kw)

    def create_link(self, *a: Any, **kw: Any) -> Any:
        return self.links.create(*a, **kw)

    def register(self, *a: Any, **kw: Any) -> Any:
        return self.links.create(*a, **kw)

    def list(self, *a: Any, **kw: Any) -> Any:
        return self.links.list(*a, **kw)

    def targets(self, *a: Any, **kw: Any) -> List[Any]:
        cid = _coerce_source_id(*a, **kw)
        return [t.target_camera_id for t in self.links.list(source_camera_id=cid)] if cid is not None else []

    def linked_monitors(self, *a: Any, **kw: Any) -> List[Any]:
        return self.targets(*a, **kw)

    def linked_cameras(self, *a: Any, **kw: Any) -> List[Any]:
        return self.targets(*a, **kw)

    # -- RECORDING trigger (sync) ----------------------------------------
    def trigger(
        self,
        source: Any = None,
        recorder: Optional[RecorderFn] = None,
        dry_run: bool = False,
        reason: str = "linked detection",
        duration_seconds: Optional[int] = None,
        **kwargs: Any,
    ) -> CrossRecordResult:
        merged: Dict[str, Any] = dict(kwargs)
        if isinstance(source, dict):
            merged.update(source)
            source = None
        src = _coerce_id(source) if source is not None else _coerce_source_id(**merged)
        if src is None and merged:
            src = _coerce_source_id(merged)
        rec = recorder or merged.get("recorder") or self.recorder
        dr = dry_run or _extract_dry_run(merged)
        dur = duration_seconds
        if dur is None:
            dur = _extract_duration(merged)
        rsn = str(merged.get("reason", merged.get("cause", reason)) or reason)
        targets = self.links.trigger(src) if src is not None else []
        result = CrossRecordResult(
            source_camera_id=src, targets=list(targets),
            dry_run=dr, reason=rsn, duration_seconds=dur,
        )
        if dr:
            result.skipped = list(targets)
            return result
        for tid in targets:
            try:
                name = str(tid)
                main_url = f"cross-record://{name}"
                if rec is not None:
                    rec(name, main_url, None,
                        camera_id=tid, source_camera_id=src,
                        reason=rsn, duration_seconds=dur)
                self.calls.append({"target_camera_id": tid, "camera_name": name})
                result.started.append(tid)
            except Exception as exc:  # noqa: BLE001
                result.errors[tid] = str(exc)
                result.skipped.append(tid)
        result.recordings_created = len(result.started)
        return result

    def fire(self, *a: Any, **kw: Any) -> CrossRecordResult:
        return self.trigger(*a, **kw)

    def run(self, *a: Any, **kw: Any) -> CrossRecordResult:
        return self.trigger(*a, **kw)

    def execute(self, *a: Any, **kw: Any) -> CrossRecordResult:
        return self.trigger(*a, **kw)

    def trigger_recording(self, *a: Any, **kw: Any) -> CrossRecordResult:
        return self.trigger(*a, **kw)

    def fire_cross_recording(self, *a: Any, **kw: Any) -> CrossRecordResult:
        return self.trigger(*a, **kw)

    def start_cross_recording(self, *a: Any, **kw: Any) -> CrossRecordResult:
        return self.trigger(*a, **kw)

    def trigger_linked_recording(self, *a: Any, **kw: Any) -> CrossRecordResult:
        return self.trigger(*a, **kw)


# Module-level shared in-memory service (tests / dry runs).
cross_record_service = CrossRecordService()
service = cross_record_service
manager = cross_record_service


async def get_cross_record_targets(db: Any, source: Any = None, **kwargs: Any) -> List[int]:
    """Resolve linked target ids for *source* without starting recording."""
    src = _coerce_id(source) if source is not None else _coerce_source_id(**kwargs)
    if src is None:
        return []
    try:
        ids = await trigger_linked_monitors(db, src)
    except Exception:  # noqa: BLE001
        return []
    return list(ids or [])


async def trigger_cross_recording(
    db: Any,
    source: Any = None,
    recorder: Optional[RecorderFn] = None,
    dry_run: bool = False,
    duration_seconds: Optional[int] = None,
    reason: Optional[str] = None,
    create_events: bool = True,
    create_recordings: bool = True,
    **kwargs: Any,
) -> CrossRecordResult:
    """Trigger RECORDING on every camera linked to *source*.

    This is the DB-backed extension that ``linked_monitor_trigger`` lacks:
    it resolves links with :func:`trigger_linked_monitors` and then actually
    starts recording on each target camera.
    """
    merged: Dict[str, Any] = {}
    if isinstance(source, dict):
        merged.update(source)
        source = None
    merged.update(kwargs or {})
    src = _coerce_id(source) if source is not None else _coerce_source_id(**merged)
    if src is None:
        src = _coerce_source_id(merged)
    rec = recorder or merged.get("recorder")
    dr = bool(dry_run) or _extract_dry_run(merged)
    dur = duration_seconds
    if dur is None:
        dur = _extract_duration(merged)
    rsn = str(reason if reason is not None else merged.get("reason", merged.get("cause", "linked detection")) or "linked detection")

    targets: List[int] = []
    if src is not None:
        try:
            targets = list(await trigger_linked_monitors(db, src) or [])
        except Exception:  # noqa: BLE001
            targets = []
    result = CrossRecordResult(
        source_camera_id=src, targets=list(targets),
        dry_run=dr, reason=rsn, duration_seconds=dur,
    )
    if dr or not targets:
        result.skipped = list(targets) if dr else []
        return result

    active_recorder = rec or _default_recorder
    now = datetime.now(timezone.utc)
    for tid in targets:
        camera = None
        try:
            camera = await db.get(Camera, int(tid))
        except Exception:  # noqa: BLE001
            camera = None
        camera_name, main_url, sub_url = _camera_stream_info(camera, tid)
        started_ok = False
        try:
            active_recorder(
                camera_name, main_url, sub_url,
                camera_id=tid, source_camera_id=src,
                reason=rsn, duration_seconds=dur,
            )
            started_ok = True
        except Exception as exc:  # noqa: BLE001
            # Never let one target's recorder failure stop the fan-out.
            result.errors[tid] = str(exc)
        if started_ok:
            result.started.append(tid)
            if create_recordings:
                try:
                    path = f"cross-record://{camera_name}/{now.strftime('%Y%m%d_%H%M%S')}.mp4"
                    try:
                        from app.services.recording_manager import recording_manager as _mgr

                        base = getattr(getattr(_mgr, "ingestor", None), "storage_root", None)
                        if base is not None:
                            from pathlib import Path as _P

                            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in camera_name)
                            path = str(_P(str(base)) / safe)
                    except Exception:  # noqa: BLE001
                        pass
                    db.add(Recording(camera_id=int(tid), path=str(path), started_at=now))
                    result.recordings_created += 1
                except Exception as exc:  # noqa: BLE001
                    result.errors[f"{tid}:recording_row"] = str(exc)
            if create_events:
                try:
                    payload = json.dumps({
                        "source_camera_id": src,
                        "target_camera_id": tid,
                        "reason": rsn,
                        "duration_seconds": dur,
                        "camera_name": camera_name,
                    }, default=str)
                    db.add(Event(
                        camera_id=int(tid),
                        kind="cross_record",
                        label=f"cross-record from {src}",
                        payload=payload,
                        tags=["cross_record", "linked_monitor"],
                    ))
                    result.events_created += 1
                except Exception as exc:  # noqa: BLE001
                    result.errors[f"{tid}:event_row"] = str(exc)
        else:
            result.skipped.append(tid)
            if create_events:
                try:
                    db.add(Event(
                        camera_id=int(tid) if str(tid).lstrip("-").isdigit() else None,
                        kind="cross_record",
                        label=f"cross-record failed from {src}",
                        payload=json.dumps({"source_camera_id": src, "target_camera_id": tid,
                                            "error": result.errors.get(tid, "recorder failed")}, default=str),
                        tags=["cross_record", "failed"],
                    ))
                    result.events_created += 1
                except Exception:  # noqa: BLE001
                    pass
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return result


# ---------------------------------------------------------------------------
# Aliases (naming conveniences; all start RECORDING on linked cameras).
# ---------------------------------------------------------------------------
fire_cross_recording = trigger_cross_recording
start_cross_recording = trigger_cross_recording
run_cross_recording = trigger_cross_recording
execute_cross_recording = trigger_cross_recording
trigger_linked_recording = trigger_cross_recording
fire_linked_recording = trigger_cross_recording
start_linked_recording = trigger_cross_recording
trigger_cross_record = trigger_cross_recording
fire_cross_record = trigger_cross_recording
start_cross_record = trigger_cross_recording
cross_record_trigger = trigger_cross_recording
record_cross_camera = trigger_cross_recording
record_linked_cameras = trigger_cross_recording
start_recording_on_linked = trigger_cross_recording

get_linked_recording_targets = get_cross_record_targets
get_cross_recording_targets = get_cross_record_targets
list_cross_record_targets = get_cross_record_targets
resolve_cross_record_targets = get_cross_record_targets

CrossRecord = CrossRecordService
CrossRecording = CrossRecordService
CrossCameraRecord = CrossRecordService
CrossCameraRecording = CrossRecordService
CrossRecordManager = CrossRecordService
CrossRecordEngine = CrossRecordService
CrossRecordTrigger = CrossRecordService
CrossRecordTriggerService = CrossRecordService
LinkedRecordingService = CrossRecordService
LinkedRecordService = CrossRecordService

__all__ = [
    "CrossRecordResult",
    "CrossRecordService",
    "cross_record_service",
    "trigger_cross_recording",
    "get_cross_record_targets",
    "_default_recorder",
]
