"""Tests for the recording protection lock (retention/quota bypass guard)."""

import os
import time

from fastapi.testclient import TestClient

from app.main import app
from app.services import rec_protect as rec_protect_mod
from app.services.rec_protect import RecProtectService
from app.services.retention import RetentionManager


def _reset():
    rec_protect_mod.clear()


def test_rec_protect_imports():
    import app.services.rec_protect  # noqa: F401
    import app.api.rec_protect  # noqa: F401
    from app.services.rec_protect import (  # noqa: F401
        RecProtectService,
        rec_protect_service,
        protect_segment,
        unprotect_segment,
        is_protected,
        list_protected,
        exclude_protected,
        clear,
    )
    from app.api.rec_protect import router  # noqa: F401


def test_protect_unprotect_roundtrip(tmp_path):
    _reset()
    seg = tmp_path / "cam1" / "seg001.mp4"
    seg.parent.mkdir(parents=True)
    seg.write_bytes(b"x" * 10)

    rec = rec_protect_mod.protect_segment(str(seg), reason="incident-42")
    assert rec.get("protected") is True
    assert rec_protect_mod.is_protected(str(seg)) is True
    assert rec_protect_mod.is_protected(seg) is True

    items = rec_protect_mod.list_protected()
    assert any(str(seg) in str(i.get("path")) for i in items)

    assert rec_protect_mod.unprotect_segment(str(seg)) is True
    assert rec_protect_mod.is_protected(str(seg)) is False
    assert rec_protect_mod.unprotect_segment(str(seg)) is False
    _reset()


def test_protected_excluded_from_retention_selection(tmp_path):
    """Retention selects old segments; protection filter must drop locked ones."""
    _reset()
    cam = tmp_path / "cam1"
    cam.mkdir(parents=True)
    old_file = cam / "old.mp4"
    new_file = cam / "new.mp4"
    old_file.write_bytes(b"x" * 100)
    new_file.write_bytes(b"y" * 100)
    old_mtime = time.time() - (10 * 86400)
    os.utime(old_file, (old_mtime, old_mtime))

    mgr = RetentionManager(storage_root=tmp_path, max_age_days=1)
    selected = mgr.select_for_deletion()
    assert str(old_file) in [str(p) for p in selected]

    # Lock the old segment: retention must no longer delete it.
    rec_protect_mod.protect_segment(str(old_file), reason="evidence")
    deletable = rec_protect_mod.exclude_protected(selected)
    assert str(old_file) not in [str(p) for p in deletable]
    assert new_file.exists()  # filter never deletes

    # Unlock restores normal retention behavior.
    rec_protect_mod.unprotect_segment(str(old_file))
    deletable2 = rec_protect_mod.exclude_protected(selected)
    assert str(old_file) in [str(p) for p in deletable2]
    _reset()


def test_protected_excluded_from_quota_enforce(tmp_path):
    """Quota oldest-first selection must also respect protection locks."""
    _reset()
    from app.services.storage_quota import StorageQuotaManager

    cam = tmp_path / "cam1"
    cam.mkdir(parents=True)
    a = cam / "a.mp4"
    b = cam / "b.mp4"
    a.write_bytes(b"x" * 100)
    b.write_bytes(b"y" * 100)
    mt = time.time() - 100
    os.utime(a, (mt, mt))
    os.utime(b, (mt + 1, mt + 1))

    qm = StorageQuotaManager(path=str(tmp_path), max_bytes=100)
    res = qm.enforce(dry_run=True)
    selected = res.get("selected") or []
    assert str(a) in selected

    rec_protect_mod.protect_segment(str(a))
    kept = rec_protect_mod.exclude_protected(selected)
    assert str(a) not in kept
    assert a.exists() and b.exists()  # dry run + filter never delete
    _reset()


def test_service_instance_aliases(tmp_path):
    _reset()
    svc = RecProtectService(storage_root=str(tmp_path))
    seg = tmp_path / "s.mp4"
    seg.write_bytes(b"z")
    svc.protect(str(seg), reason="t")
    assert svc.is_protected(str(seg)) is True
    assert svc.filter_candidates([str(seg)]) == []
    assert svc.status_for(str(seg))["protected"] is True
    svc.unprotect(str(seg))
    assert svc.is_protected(str(seg)) is False
    _reset()


def test_rec_protect_api(tmp_path):
    _reset()
    client = TestClient(app)
    paths = list(app.openapi()["paths"].keys())
    prefix = None
    for cand in ("/api/recordings/protection", "/api/rec-protect", "/api/recordings/protect"):
        if cand in paths or f"{cand}/protect" in paths:
            prefix = cand
            break
    assert prefix is not None, paths

    seg = tmp_path / "ev.mp4"
    seg.write_bytes(b"e" * 8)

    r = client.post(f"{prefix}/protect", json={"path": str(seg), "reason": "case-1"})
    assert r.status_code == 200, r.text
    assert r.json().get("protected") is True

    r2 = client.get(f"{prefix}/status", params={"path": str(seg)})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("protected") is True

    r3 = client.get(f"{prefix}/list")
    assert r3.status_code == 200, r3.text
    assert r3.json().get("count", 0) >= 1

    r4 = client.post(f"{prefix}/filter", json={"paths": [str(seg), str(tmp_path / "other.mp4")]})
    assert r4.status_code == 200, r4.text
    assert str(seg) not in (r4.json().get("deletable") or [])

    r5 = client.post(f"{prefix}/unprotect", json={"path": str(seg)})
    assert r5.status_code == 200, r5.text
    r6 = client.get(f"{prefix}/status", params={"path": str(seg)})
    assert r6.json().get("protected") is False
    _reset()
