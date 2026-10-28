from __future__ import annotations

import multiprocessing as mp
import time
from typing import Any, Optional

import numpy as np

from app.services.motion import MotionDetector, MotionResult


def motion_worker(
    frame_queue: mp.Queue,
    result_queue: mp.Queue,
    threshold: int = 25,
    min_area: int = 500,
    camera_id: Any = None,
) -> None:
    """Isolated process that gates downstream AI using OpenCV motion detection."""
    detector = MotionDetector(threshold=threshold, min_area=min_area)
    while True:
        try:
            frame: Any = frame_queue.get(block=True)
            if frame is None:
                break

            if not isinstance(frame, np.ndarray):
                continue

            if camera_id is not None:
                try:
                    from app.services import camera_health as _ch

                    _ch.mark_frame_received(camera_id)
                except Exception:  # noqa: BLE001
                    pass

            result: MotionResult = detector.process(frame)
            if result.changed:
                try:
                    result_queue.put_nowait(
                        {
                            "camera_id": camera_id,
                            "motion": True,
                            "score": result.score,
                            "regions": [r.__dict__ for r in result.regions],
                            "timestamp": time.time(),
                        }
                    )
                except Exception:
                    pass
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Motion worker error: {exc}")
            continue


class MotionProcessManager:
    """Lightweight handle for a per-camera motion gate process."""

    def __init__(self, threshold: int = 25, min_area: int = 500, queue_size: int = 3, camera_id: Any = None):
        self.threshold = threshold
        self.min_area = min_area
        self.queue_size = queue_size
        self.camera_id = camera_id
        self.frame_queue: mp.Queue = mp.Queue(maxsize=queue_size)
        self.result_queue: mp.Queue = mp.Queue(maxsize=10)
        self.process: mp.Process | None = None
        self.last_activity_at: Optional[float] = None
        self.last_motion_at: Optional[float] = None

    def start(self) -> None:
        self.process = mp.Process(
            target=motion_worker,
            args=(self.frame_queue, self.result_queue, self.threshold, self.min_area, self.camera_id),
            name="VigilCam-Motion-Gate",
            daemon=True,
        )
        self.process.start()

    def submit_frame(self, frame: np.ndarray) -> bool:
        """Return False if the latest frame had to drop an old frame."""
        if self.camera_id is not None:
            try:
                from app.services import camera_health as _ch

                _ch.mark_frame_received(self.camera_id)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.frame_queue.put_nowait(frame)
            return True
        except Exception:
            try:
                _ = self.frame_queue.get_nowait()
            except Exception:
                pass
            if self.camera_id is not None:
                try:
                    from app.services import camera_health as _ch

                    _ch.mark_frame_received(self.camera_id)
                except Exception:  # noqa: BLE001
                    pass
            try:
                self.frame_queue.put_nowait(frame)
                return False
            except Exception:
                return False

    def get_results(self) -> list[dict]:
        if self.camera_id is not None:
            try:
                from app.services import camera_health as _ch

                _ch.mark_frame_received(self.camera_id)
            except Exception:  # noqa: BLE001
                pass
        results = []
        while not self.result_queue.empty():
            try:
                item = self.result_queue.get_nowait()
            except Exception:
                break
            results.append(item)
            if item.get("motion"):
                ts = item.get("timestamp") or time.time()
                cid = item.get("camera_id")
                if cid is None:
                    cid = self.camera_id
                if cid is not None:
                    self.last_motion_at = float(ts)
                    self.last_activity_at = float(ts)
                    try:
                        from app.services.activity import mark_motion, mark_activity

                        mark_motion(cid, when=float(ts))
                        mark_activity(cid, when=float(ts), kind="motion")
                    except Exception:  # noqa: BLE001
                        pass
        return results

    def stop(self) -> None:
        try:
            self.frame_queue.put_nowait(None)
        except Exception:
            pass
        if self.process is not None:
            self.process.join(timeout=5)
            if self.process.is_alive():
                self.process.terminate()
            self.process = None
