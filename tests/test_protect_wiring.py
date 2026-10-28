"""Protection wiring tests: retention preview + quota enforce must skip locked segments."""

import os
import time

from app.services import rec_protect as rec_protect_mod
from app.services import retention_preview as retention_preview_mod
from app.services.storage_quota import StorageQuotaManager


def _reset():
    rec_protect_mod.clear()


def _make_old(path, size=100, age_days=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    old_mtime = time.time() - (age_days * 86400)
    os.utime(path, (old_mtime, old_mtime))


def test_preview_excludes_protected(tmp_path):
    _reset()
    cam = tmp_path / "cam1"
    old_file = cam / "old.mp4"
    old2 = cam / "old2.mp4"
    _make_old(old_file)
    _make_old(old2)
    rec_protect_mod.protect_segment(str(old_file), reason="evidence")

    mgr = retention_preview_mod.RetentionPreviewService(storage_root=str(tmp_path))
    result = mgr.preview(max_age_days=1)
    assert result.get("dry_run") is True
    deleted = result.get("deleted") or []
    assert str(old_file) not in deleted
    assert str(old2) in deleted
    assert old_file.exists() and old2.exists()
    assert (result.get("protected_excluded") or 0) >= 1
    _reset()


def test_preview_selector_excludes_protected(tmp_path):
    _reset()
    cam = tmp_path / "cam1"
    old_file = cam / "old.mp4"
    _make_old(old_file)
    rec_protect_mod.protect_segment(str(old_file))

    mgr = retention_preview_mod.RetentionPreviewService(storage_root=str(tmp_path))
    selected = mgr.select_for_deletion(max_age_days=1)
    assert str(old_file) not in [str(p) for p in selected]
    assert old_file.exists()
    _reset()


def test_quota_enforce_dry_run_excludes_protected(tmp_path):
    _reset()
    cam = tmp_path / "cam1"
    a = cam / "a.mp4"
    b = cam / "b.mp4"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_bytes(b"x" * 100)
    b.write_bytes(b"y" * 100)
    mt = time.time() - 100
    os.utime(a, (mt, mt))
    os.utime(b, (mt + 1, mt + 1))
    rec_protect_mod.protect_segment(str(a))

    qm = StorageQuotaManager(path=str(tmp_path), max_bytes=100)
    res = qm.enforce(dry_run=True)
    selected = res.get("selected") or []
    assert str(a) not in selected
    assert str(b) in selected
    assert a.exists() and b.exists()
    assert (res.get("protected_excluded") or 0) >= 1
    _reset()


def test_quota_enforce_real_run_keeps_protected(tmp_path):
    _reset()
    cam = tmp_path / "cam1"
    a = cam / "a.mp4"
    b = cam / "b.mp4"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_bytes(b"x" * 100)
    b.write_bytes(b"y" * 100)
    mt = time.time() - 100
    os.utime(a, (mt, mt))
    os.utime(b, (mt + 1, mt + 1))
    rec_protect_mod.protect_segment(str(a))

    qm = StorageQuotaManager(path=str(tmp_path), max_bytes=100)
    res = qm.enforce()
    assert str(a) not in (res.get("deleted") or [])
    assert a.exists()
    assert not b.exists()
    _reset()


def test_unprotect_restores_preview_selection(tmp_path):
    _reset()
    cam = tmp_path / "cam1"
    old_file = cam / "old.mp4"
    _make_old(old_file)
    rec_protect_mod.protect_segment(str(old_file))

    mgr = retention_preview_mod.RetentionPreviewService(storage_root=str(tmp_path))
    assert str(old_file) not in (mgr.preview(max_age_days=1).get("deleted") or [])

    rec_protect_mod.unprotect_segment(str(old_file))
    assert str(old_file) in (mgr.preview(max_age_days=1).get("deleted") or [])
    _reset()
