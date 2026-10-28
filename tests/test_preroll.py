import time

from app.services.preroll import (
    PreRollBuffer,
    PreRollRingBuffer,
    preroll_buffer,
)


def test_basic_write_and_read():
    buf = PreRollRingBuffer(max_seconds=100, capacity=10)
    rec = buf.write_segment(camera_name="cam1", path="a.mp4", duration=5, start_time=1)
    assert rec["path"] == "a.mp4"
    assert rec.path == "a.mp4"
    segs = buf.segments("cam1")
    assert len(segs) == 1
    assert segs[0]["path"] == "a.mp4"


def test_ring_eviction_by_capacity():
    buf = PreRollRingBuffer(max_seconds=10_000, max_items=2)
    buf.add_segment(camera_name="cam1", path="a.mp4", start_time=1)
    buf.add_segment(camera_name="cam1", path="b.mp4", start_time=2)
    buf.add_segment(camera_name="cam1", path="c.mp4", start_time=3)
    segs = buf.segments("cam1")
    assert len(segs) == 2
    assert [s["path"] for s in segs] == ["b.mp4", "c.mp4"]


def test_ring_eviction_by_time_window():
    buf = PreRollRingBuffer(pre_roll_seconds=5, capacity=100)
    buf.add_segment(camera_name="cam1", path="old.mp4", start_time=0)
    buf.add_segment(camera_name="cam1", path="new.mp4", start_time=10)
    segs = buf.get_segments("cam1")
    paths = [s["path"] for s in segs]
    assert "new.mp4" in paths
    assert "old.mp4" not in paths


def test_clear():
    buf = PreRollRingBuffer(capacity=10, max_seconds=100)
    buf.add_segment(camera_name="cam1", path="a.mp4", start_time=1)
    buf.clear("cam1")
    assert len(buf.segments("cam1")) == 0
    buf.add_segment(camera_name="cam2", path="b.mp4", start_time=1)
    buf.clear()
    assert len(buf.all()) == 0


def test_latest_and_recent():
    buf = PreRollRingBuffer(capacity=10, max_seconds=100)
    buf.add_segment(camera_name="cam1", path="a.mp4", start_time=1)
    buf.add_segment(camera_name="cam1", path="b.mp4", start_time=2)
    assert buf.latest("cam1")["path"] == "b.mp4"
    r = buf.recent(seconds=1, camera_name="cam1")
    assert len(r) <= 2


def test_module_default_exists():
    assert preroll_buffer is not None
    assert len(preroll_buffer.all()) >= 0


def test_aliases():
    # PreRollBuffer alias available
    b = PreRollBuffer(capacity=3, max_seconds=10)
    assert b is not None
