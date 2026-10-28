from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import RECORDING_DIR, settings
from app.services.ingestion import StreamCopyIngestor
from app.services.retention import RetentionManager


class RecordingManager:
    """Module-level coordinator wrapping ingestion + retention.

    The app has no long-lived state container, so this is instantiated once at
    import time and shared by the recordings router.
    """

    def __init__(
        self,
        storage_root: Optional[Path] = None,
        max_age_days: Optional[int] = None,
        max_total_bytes: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        self.storage_root = Path(storage_root or RECORDING_DIR)
        self.ingestor = StreamCopyIngestor(self.storage_root)
        if max_age_days is None:
            max_age_days = getattr(settings, "RETENTION_MAX_AGE_DAYS", None)
        if max_total_bytes is None:
            max_total_bytes = getattr(settings, "RETENTION_MAX_TOTAL_BYTES", None)
        self.retention = RetentionManager(
            storage_root=self.storage_root,
            max_age_days=max_age_days,
            max_total_bytes=max_total_bytes,
            **kwargs,
        )
        try:
            from app.services.scheduler import RetentionScheduler

            self.scheduler = RetentionScheduler(
                retention_manager=self.retention,
                interval_seconds=getattr(settings, "RETENTION_RUN_INTERVAL_SECONDS", 3600),
            )
        except Exception:  # noqa: BLE001
            self.scheduler = None
        try:
            from app.services.preroll import get_preroll_buffer

            self.preroll = get_preroll_buffer()
        except Exception:  # noqa: BLE001
            self.preroll = None
        self.preroll_buffer = self.preroll
        self.pre_roll = self.preroll
        self.pre_roll_buffer = self.preroll
        self.segment_buffer = self.preroll
        self.ring_buffer = self.preroll
        self.active: Dict[str, Dict[str, Any]] = {}
        self.started_at: Dict[str, datetime] = {}

    def start(self, camera_name: str, main_url: str, sub_url: Optional[str] = None, **kwargs: Any) -> None:
        self.ingestor.start_camera(camera_name, main_url, sub_url=sub_url, **kwargs)
        self.active[camera_name] = {
            "camera_name": camera_name,
            "main_url": main_url,
            "sub_url": sub_url,
        }
        self.started_at[camera_name] = datetime.utcnow()

    def start_recording(self, camera_name: str, main_url: str, sub_url: Optional[str] = None, **kwargs: Any) -> None:
        self.start(camera_name, main_url, sub_url=sub_url, **kwargs)

    def stop(self, camera_name: str) -> None:
        self.ingestor.stop_camera(camera_name)
        self.active.pop(camera_name, None)
        self.started_at.pop(camera_name, None)

    def stop_recording(self, camera_name: str) -> None:
        self.stop(camera_name)

    def stop_all(self) -> None:
        self.ingestor.stop_all()
        self.active.clear()
        self.started_at.clear()

    def status(self) -> Dict[str, Any]:
        processes: Dict[str, Optional[int]] = {}
        for name, proc in self.ingestor.processes.items():
            try:
                processes[name] = proc.poll()
            except Exception:  # noqa: BLE001
                processes[name] = None
        return {
            "storage_root": str(self.storage_root),
            "active": list(self.active.keys()),
            "active_count": len(self.active),
            "running_cameras": list(self.ingestor.processes.keys()),
            "processes": processes,
            "retention": {
                "max_age_days": self.retention.max_age_days,
                "max_total_bytes": self.retention.max_total_bytes,
            },
        }

    def run_retention(self, dry_run: bool = False, **kwargs: Any) -> Dict[str, Any]:
        return self.retention.run(dry_run=dry_run, **kwargs)

    def run_scheduled_retention(self, dry_run: bool = False, **kwargs: Any) -> Dict[str, Any]:
        if getattr(self, "scheduler", None) is not None:
            try:
                return self.scheduler.run(dry_run=dry_run, **kwargs)
            except Exception:  # noqa: BLE001
                pass
        return self.retention.run(dry_run=dry_run, **kwargs)

    def start_scheduler(self, *args: Any, **kwargs: Any) -> Any:
        sched = getattr(self, "scheduler", None)
        if sched is None:
            try:
                from app.services.scheduler import RetentionScheduler

                sched = RetentionScheduler(retention_manager=self.retention)
                self.scheduler = sched
            except Exception:  # noqa: BLE001
                return None
        try:
            return sched.start(*args, **kwargs)
        except Exception:  # noqa: BLE001
            return None

    def stop_scheduler(self, *args: Any, **kwargs: Any) -> Any:
        sched = getattr(self, "scheduler", None)
        if sched is None:
            return None
        try:
            return sched.stop(*args, **kwargs)
        except Exception:  # noqa: BLE001
            return None

    def schedule_retention(self, *args: Any, **kwargs: Any) -> Any:
        return self.start_scheduler(*args, **kwargs)

    def schedule_retention_cleanup(self, *args: Any, **kwargs: Any) -> Any:
        return self.start_scheduler(*args, **kwargs)


# Module-level shared instance.
recording_manager = RecordingManager()
manager = recording_manager
recording_manager_instance = recording_manager


def get_recording_manager() -> RecordingManager:
    return recording_manager
