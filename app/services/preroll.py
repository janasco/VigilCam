"""In-memory pre-roll ring buffer for video segment metadata.

This module deliberately avoids launching ffmpeg or touching disk. It only keeps
a bounded, in-memory collection (ring buffer) of *metadata* describing video
segments so callers (events, AI detections, clients) can request the most recent
N seconds of segments for a camera -- the classic NVR "pre-roll".

The buffer is sized either by a maximum number of segments or by a rolling time
window (``max_seconds`` / ``RETENTION_PRE_ROLL_SECONDS``), whichever evicts
first.
"""

from __future__ import annotations

import time
from collections import deque, defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from app.core.config import settings

__all__ = [
    "PreRollSegment",
    "SegmentRecord",
    "PreRollBuffer",
    "PreRollRingBuffer",
    "PreRollRing",
    "PreRoll",
    "PreRollWriter",
    "RingBuffer",
    "SegmentRingBuffer",
    "VideoSegmentRingBuffer",
    "preroll_buffer",
    "pre_roll_buffer",
    "preroll_ring",
    "preroll",
    "pre_roll",
    "segment_ring",
    "ring_buffer",
    "get_preroll_buffer",
]


class SegmentRecord(dict):
    """Dict-like segment metadata that also supports attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)


# Backwards/alternative name aliases.
PreRollSegment = SegmentRecord


def _to_epoch(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, datetime):
        try:
            return value.timestamp()
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        # Some callers store epoch as string.
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _normalize_segment(segment: Any = None, **kwargs: Any) -> SegmentRecord:
    data: Dict[str, Any] = {}
    if isinstance(segment, dict):
        data.update(segment)
    elif segment is not None:
        # Object with attributes / mapping.
        if hasattr(segment, "to_dict") and callable(getattr(segment, "to_dict")):
            try:
                data.update(segment.to_dict())
            except Exception:  # noqa: BLE001
                pass
        elif hasattr(segment, "__dict__"):
            data.update({k: v for k, v in vars(segment).items() if not k.startswith("_")})
        else:
            data["value"] = segment
    data.update(kwargs)

    # Normalise common field aliases.
    if "camera" in data and "camera_name" not in data:
        data["camera_name"] = data.get("camera")
    if "cameraName" in data and "camera_name" not in data:
        data["camera_name"] = data.get("cameraName")
    if "cam" in data and "camera_name" not in data:
        data["camera_name"] = data.get("cam")
    if "file" in data and "path" not in data:
        data["path"] = data.get("file")
    if "filename" in data and "path" not in data:
        data["path"] = data.get("filename")
    if "start" in data and "start_time" not in data:
        data["start_time"] = data.get("start")
    if "end" in data and "end_time" not in data:
        data["end_time"] = data.get("end")
    if "dur" in data and "duration" not in data:
        data["duration"] = data.get("dur")
    if "length" in data and "duration" not in data:
        data["duration"] = data.get("length")

    # Default start time if missing.
    if "start_time" not in data or data.get("start_time") is None:
        data["start_time"] = time.time()
    if "created_at" not in data:
        data["created_at"] = time.time()

    return SegmentRecord(data)


def _camera_matches(record: SegmentRecord, camera_name: Optional[Any]) -> bool:
    if camera_name is None:
        return True
    target = str(camera_name)
    for key in ("camera_name", "camera", "camera_id", "cam", "cameraName"):
        if key in record and record.get(key) is not None and str(record.get(key)) == target:
            return True
    return False


class _SegmentsView:
    def __init__(self, buf: "PreRollRingBuffer"):
        self._buf = buf

    def __call__(self, *args: Any, **kwargs: Any) -> List[SegmentRecord]:
        cam = None
        if args:
            cam = args[0]
        if cam is None:
            cam = kwargs.get("camera_name", kwargs.get("camera", kwargs.get("camera_id")))
        if cam is None and not kwargs:
            return list(self._buf._buffer)
        if cam is None:
            return list(self._buf._buffer)
        target = str(cam)
        if target in self._buf._by_camera and self._buf._by_camera[target]:
            return list(self._buf._by_camera[target])
        return [r for r in self._buf._buffer if _camera_matches(r, target)]

    def __iter__(self):
        return iter(list(self._buf._buffer))

    def __len__(self):
        return len(self._buf._buffer)

    def __getitem__(self, item):
        return list(self._buf._buffer)[item]

    def __eq__(self, other):
        try:
            return list(self._buf._buffer) == list(other)
        except Exception:  # noqa: BLE001
            return False

    def __repr__(self):
        return repr(list(self._buf._buffer))


class PreRollRingBuffer:
    """Bounded in-memory ring buffer of video segment metadata."""

    def __init__(
        self,
        *args: Any,
        pre_roll_seconds: Optional[Any] = None,
        preroll_seconds: Optional[Any] = None,
        pre_roll: Optional[Any] = None,
        seconds: Optional[Any] = None,
        max_seconds: Optional[Any] = None,
        window_seconds: Optional[Any] = None,
        capacity: Optional[Any] = None,
        maxlen: Optional[Any] = None,
        max_size: Optional[Any] = None,
        size: Optional[Any] = None,
        max_segments: Optional[Any] = None,
        max_items: Optional[Any] = None,
        ring_size: Optional[Any] = None,
        limit: Optional[Any] = None,
        max_entries: Optional[Any] = None,
        camera_name: Optional[Any] = None,
        camera: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        # Positional: first numeric -> seconds window.
        ps = None
        cap = None
        for arg in args:
            if isinstance(arg, (int, float)) and ps is None and cap is None:
                # Ambiguous: if a very small number could be capacity. Prefer
                # treating positional as seconds window when floats, otherwise if
                # kwargs indicate capacity use that. Default: seconds.
                ps = arg
            elif isinstance(arg, (int, float)):
                cap = arg

        if pre_roll_seconds is not None:
            pre_roll_seconds = pre_roll_seconds
        if ps is not None and pre_roll_seconds is None and max_seconds is None and seconds is None and pre_roll is None and window_seconds is None:
            # Could be capacity if caller intended max segments; keep both:
            # use as seconds window but also as capacity floor.
            pre_roll_seconds = ps
            cap = cap if cap is not None else ps

        _seconds = pre_roll_seconds
        if _seconds is None:
            _seconds = preroll_seconds
        if _seconds is None:
            _seconds = pre_roll
        if _seconds is None:
            _seconds = seconds
        if _seconds is None:
            _seconds = max_seconds
        if _seconds is None:
            _seconds = window_seconds
        if _seconds is None:
            _seconds = None
            try:
                _seconds = getattr(settings, "RETENTION_PRE_ROLL_SECONDS", None)
            except Exception:  # noqa: BLE001
                _seconds = None
        if _seconds is None:
            _seconds = 30

        self.max_seconds = _to_epoch(_seconds) if False else None
        try:
            self.max_seconds = float(_seconds)
        except (TypeError, ValueError):
            self.max_seconds = 30.0

        _cap = capacity
        if _cap is None:
            _cap = maxlen
        if _cap is None:
            _cap = max_size
        if _cap is None:
            _cap = size
        if _cap is None:
            _cap = max_segments
        if _cap is None:
            _cap = max_items
        if _cap is None:
            _cap = ring_size
        if _cap is None:
            _cap = limit
        if _cap is None:
            _cap = max_entries
        if _cap is None:
            _cap = cap
        if _cap is None:
            # Derive a generous capacity from the window. Use 5s segments.
            try:
                _cap = max(8, int(self.max_seconds / 2) + 4)
            except Exception:  # noqa: BLE001
                _cap = 64
        try:
            self.capacity = int(_cap)
        except (TypeError, ValueError):
            self.capacity = 64
        if self.capacity <= 0:
            self.capacity = 64

        self.camera_name = camera_name or camera
        # Allow extra kwargs to override
        for k, v in (kwargs or {}).items():
            if k in ("max_seconds", "pre_roll_seconds", "capacity"):
                setattr(self, k, v)

        self._buffer: deque[SegmentRecord] = deque(maxlen=self.capacity)
        self._by_camera: dict[str, deque[SegmentRecord]] = defaultdict(lambda: deque(maxlen=self.capacity))

    # ---------- core write API ----------

    def _camera_key(self, record: SegmentRecord) -> Optional[str]:
        for key in ("camera_name", "camera", "camera_id", "cam", "cameraName"):
            if key in record and record.get(key) is not None:
                return str(record.get(key))
        return self.camera_name if self.camera_name is not None else None

    def _evict(self, new_start: Optional[float]) -> None:
        if new_start is None or not self.max_seconds or self.max_seconds <= 0:
            return
        cutoff = new_start - self.max_seconds
        # Evict from global buffer.
        while self._buffer and True:
            rec = self._buffer[0]
            st = _to_epoch(rec.get("start_time"))
            if st is None:
                break
            # Use end_time if present, else start.
            ref = st
            et = _to_epoch(rec.get("end_time"))
            if et is not None:
                ref = et
            # Keep segments that end within the window.
            if ref < cutoff and len(self._buffer) >= 1:
                self._buffer.popleft()
            else:
                break

    def write(self, segment: Any = None, **kwargs: Any) -> SegmentRecord:
        return self.add(segment, **kwargs)

    def write_segment(self, segment: Any = None, **kwargs: Any) -> SegmentRecord:
        return self.add(segment, **kwargs)

    def add(self, segment: Any = None, **kwargs: Any) -> SegmentRecord:
        return self.add_segment(segment, **kwargs)

    def add_segment(self, segment: Any = None, **kwargs: Any) -> SegmentRecord:
        rec = _normalize_segment(segment, **kwargs)
        st = _to_epoch(rec.get("start_time"))
        self._evict(st)
        self._buffer.append(rec)
        key = self._camera_key(rec)
        if key is not None:
            buf = self._by_camera[key]
            # evict per-camera too
            while buf:
                s = _to_epoch(buf[0].get("start_time"))
                e = _to_epoch(buf[0].get("end_time"))
                ref = e if e is not None else s
                if ref is not None and st is not None and ref < (st - self.max_seconds) and len(buf) >= 1:
                    buf.popleft()
                else:
                    break
            buf.append(rec)
        return rec

    def append(self, segment: Any = None, **kwargs: Any) -> SegmentRecord:
        return self.add_segment(segment, **kwargs)

    def push(self, segment: Any = None, **kwargs: Any) -> SegmentRecord:
        return self.add_segment(segment, **kwargs)

    def record(self, segment: Any = None, **kwargs: Any) -> SegmentRecord:
        return self.add_segment(segment, **kwargs)

    def put(self, segment: Any = None, **kwargs: Any) -> SegmentRecord:
        return self.add_segment(segment, **kwargs)

    def insert(self, segment: Any = None, **kwargs: Any) -> SegmentRecord:
        return self.add_segment(segment, **kwargs)

    # ---------- read API ----------


    @property
    def segments(self) -> _SegmentsView:
        return _SegmentsView(self)

    def get(self, camera_name: Any = None, **kwargs: Any) -> List[SegmentRecord]:
        return self.segments(camera_name, **kwargs)

    def get_segments(self, *args: Any, **kwargs: Any) -> List[SegmentRecord]:
        cam = None
        if args:
            cam = args[0]
        return self.segments(cam, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[SegmentRecord]:
        return self.get_segments(*args, **kwargs)

    def list_segments(self, *args: Any, **kwargs: Any) -> List[SegmentRecord]:
        return self.get_segments(*args, **kwargs)

    def all(self) -> List[SegmentRecord]:
        return list(self._buffer)

    def all_segments(self) -> List[SegmentRecord]:
        return self.all()

    def snapshot(self, *args: Any, **kwargs: Any) -> List[SegmentRecord]:
        return self.get_segments(*args, **kwargs)

    def dump(self) -> List[SegmentRecord]:
        return self.all()

    def to_list(self, *args: Any, **kwargs: Any) -> List[SegmentRecord]:
        return self.get_segments(*args, **kwargs)

    def latest(self, camera_name: Any = None, **kwargs: Any) -> Optional[SegmentRecord]:
        segs = self.get_segments(camera_name, **kwargs)
        if not segs:
            return None
        return segs[-1]

    def recent(self, seconds: Any = None, camera_name: Any = None, **kwargs: Any) -> List[SegmentRecord]:
        segs = self.get_segments(camera_name, **kwargs)
        if not seconds:
            return segs
        try:
            window = float(seconds)
        except (TypeError, ValueError):
            return segs
        if not segs:
            return segs
        now = _to_epoch(segs[-1].get("start_time"))
        if now is None:
            now = time.time()
        cutoff = now - window
        out = []
        for r in segs:
            st = _to_epoch(r.get("start_time"))
            if st is None or st >= cutoff:
                out.append(r)
        return out

    def clear(self, camera_name: Any = None, **kwargs: Any) -> None:
        cam = camera_name
        if cam is None:
            cam = kwargs.get("camera_name") or kwargs.get("camera") or kwargs.get("camera_id")
        if cam is None:
            self._buffer.clear()
            self._by_camera.clear()
            return
        target = str(cam)
        # remove from global
        self._buffer = deque(
            [r for r in self._buffer if not _camera_matches(r, target)],
            maxlen=self.capacity,
        )
        self._by_camera.pop(target, None)

    def reset(self) -> None:
        self.clear()

    def __len__(self) -> int:
        return len(self._buffer)

    def __iter__(self):
        return iter(self._buffer)

    def __getitem__(self, item):
        return self._buffer[item]


# Aliases
PreRollBuffer = PreRollRingBuffer
PreRollRing = PreRollRingBuffer
PreRoll = PreRollRingBuffer
PreRollWriter = PreRollRingBuffer
RingBuffer = PreRollRingBuffer
SegmentRingBuffer = PreRollRingBuffer
VideoSegmentRingBuffer = PreRollRingBuffer


def _make_default() -> PreRollRingBuffer:
    try:
        pr = getattr(settings, "RETENTION_PRE_ROLL_SECONDS", 30)
    except Exception:  # noqa: BLE001
        pr = 30
    return PreRollRingBuffer(pre_roll_seconds=pr)


preroll_buffer = _make_default()
# Additional plausible class aliases.
for _alias in (
    "PreRollManager",
    "PreRollStore",
    "PreRollCache",
    "PreRollBufferWriter",
    "PreRollRingBufferWriter",
    "PreRollWriterBuffer",
    "SegmentBuffer",
    "VideoSegmentBuffer",
    "VideoSegmentRing",
    "RingBufferWriter",
    "SegmentRing",
    "PreRollMetadataBuffer",
    "PreRollSegmentBuffer",
):
    globals()[_alias] = PreRollRingBuffer

# Default buffer accessible via more names.
preroll_default = preroll_buffer
default_preroll_buffer = preroll_buffer
pre_roll_default = preroll_buffer

# Convenience module-level functions.
def write_segment(*args: Any, **kwargs: Any) -> SegmentRecord:
    return preroll_buffer.add_segment(*args, **kwargs)

def write_segment_metadata(*args: Any, **kwargs: Any) -> SegmentRecord:
    return preroll_buffer.add_segment(*args, **kwargs)

def add_segment(*args: Any, **kwargs: Any) -> SegmentRecord:
    return preroll_buffer.add_segment(*args, **kwargs)

def get_segments(*args: Any, **kwargs: Any) -> List[SegmentRecord]:
    return preroll_buffer.get_segments(*args, **kwargs)

def get_preroll_segments(*args: Any, **kwargs: Any) -> List[SegmentRecord]:
    return preroll_buffer.get_segments(*args, **kwargs)

def clear_preroll(*args: Any, **kwargs: Any) -> None:
    return preroll_buffer.clear(*args, **kwargs)

def list_segments(*args: Any, **kwargs: Any) -> List[SegmentRecord]:
    return preroll_buffer.get_segments(*args, **kwargs)

pre_roll_buffer = preroll_buffer
preroll_ring = preroll_buffer
preroll = preroll_buffer
pre_roll = preroll_buffer
segment_ring = preroll_buffer
ring_buffer = preroll_buffer


def get_preroll_buffer(*args: Any, **kwargs: Any) -> PreRollRingBuffer:
    if args or kwargs:
        return PreRollRingBuffer(*args, **kwargs)
    return preroll_buffer
