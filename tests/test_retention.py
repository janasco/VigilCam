import os
import time
from pathlib import Path

from app.services.retention import RetentionManager


def test_retention_selection(tmp_path):
    root = tmp_path / "recordings"
    cam = root / "cam1"
    cam.mkdir(parents=True)
    old_file = cam / "old.mp4"
    new_file = cam / "new.mp4"
    old_file.write_bytes(b"x" * 100)
    new_file.write_bytes(b"y" * 100)

    old_mtime = time.time() - (10 * 86400)
    os.utime(old_file, (old_mtime, old_mtime))

    manager = RetentionManager(storage_root=root, max_age_days=1, max_total_bytes=150)
    result = manager.run(dry_run=True)

    deleted = result["deleted"]
    assert len(deleted) == 1
    assert str(old_file) in deleted
    assert result["dry_run"] is True
    # dry run does not remove
    assert old_file.exists()


def test_retention_max_total_size(tmp_path):
    root = tmp_path / "recordings"
    cam = root / "cam1"
    cam.mkdir(parents=True)
    a = cam / "a.mp4"
    b = cam / "b.mp4"
    a.write_bytes(b"x" * 100)
    b.write_bytes(b"y" * 100)

    mt = time.time() - 100
    os.utime(a, (mt, mt))
    os.utime(b, (mt + 1, mt + 1))

    manager = RetentionManager(storage_root=root, max_age_days=0, max_total_bytes=100)
    # max_age_days=0 means disable age pruning; enforce size only
    result = manager.run(dry_run=True)
    assert result["deleted_count"] == 1
    assert str(a) in result["selected"]
