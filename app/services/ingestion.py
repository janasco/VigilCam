from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from app.core.config import RECORDING_DIR


class StreamCopyIngestor:
    """Launch FFmpeg with zero-transcode, segmented recording.

    Each camera gets one FFmpeg process that writes five-minute raw H.264
    segments for both the main and sub streams.
    """

    def __init__(self, storage_root: Optional[Path] = None) -> None:
        self.storage_root = Path(storage_root or RECORDING_DIR)
        self.processes: Dict[str, subprocess.Popen] = {}

    def _camera_dir(self, camera_name: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in camera_name)
        path = self.storage_root / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def start_camera(
        self,
        camera_name: str,
        main_url: str,
        sub_url: Optional[str] = None,
        segment_seconds: int = 300,
    ) -> None:
        if camera_name in self.processes and self.processes[camera_name].poll() is None:
            return

        camera_dir = self._camera_dir(camera_name)
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            main_url,
        ]

        if sub_url:
            cmd += ["-rtsp_transport", "tcp", "-i", sub_url]

        main_pattern = str(camera_dir / "%Y%m%d_%H%M%S_main.mp4")
        cmd += [
            "-map",
            "0:v",
            "-c:v",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-reset_timestamps",
            "1",
            "-strftime",
            "1",
            main_pattern,
        ]

        if sub_url:
            sub_pattern = str(camera_dir / "%Y%m%d_%H%M%S_sub.mp4")
            cmd += [
                "-map",
                "1:v",
                "-c:v",
                "copy",
                "-f",
                "segment",
                "-segment_time",
                str(segment_seconds),
                "-reset_timestamps",
                "1",
                "-strftime",
                "1",
                sub_pattern,
            ]

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes[camera_name] = proc
        print(f"Ingestion started for {camera_name}: {camera_dir}")

    def stop_camera(self, camera_name: str) -> None:
        proc = self.processes.pop(camera_name, None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop_all(self) -> None:
        for camera_name in list(self.processes):
            self.stop_camera(camera_name)
