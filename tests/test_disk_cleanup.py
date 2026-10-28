import os
import time

from fastapi.testclient import TestClient

from app.main import app
from app.services import disk_cleanup as disk_cleanup_mod


def test_disk_cleanup_import():
    import app.services.disk_cleanup  # noqa: F401
    import app.api.disk_cleanup  # noqa: F401
    from app.services.disk_cleanup import DiskCleanupManager, disk_cleanup_service  # noqa: F401
    from app.api.disk_cleanup import router  # noqa: F401


def test_disk_cleanup_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/disk_cleanup" in paths or "/api/disk-cleanup" in paths
    assert any("disk" in p and "cleanup" in p for p in paths) or any("cleanup" in p for p in paths)


def test_disk_cleanup_service_preview_dry_run(tmp_path):
    old_file = tmp_path / "old.mp4"
    new_file = tmp_path / "new.mp4"
    old_file.write_bytes(b"x" * 100)
    new_file.write_bytes(b"y" * 100)
    old_mtime = time.time() - (10 * 86400)
    os.utime(old_file, (old_mtime, old_mtime))

    mgr = disk_cleanup_mod.DiskCleanupManager()
    result = mgr.run(path=str(tmp_path), max_age_days=1, dry_run=True)
    assert result.get("dry_run") is True
    assert result.get("status") == "completed"
    deleted = result.get("deleted") or []
    assert str(old_file) in deleted
    assert str(new_file) not in deleted
    # dry run does not remove
    assert old_file.exists()


def test_disk_cleanup_service_actual_cleanup(tmp_path):
    old_file = tmp_path / "old2.mp4"
    new_file = tmp_path / "new2.mp4"
    old_file.write_bytes(b"x" * 64)
    new_file.write_bytes(b"y" * 64)
    old_mtime = time.time() - (10 * 86400)
    os.utime(old_file, (old_mtime, old_mtime))

    mgr = disk_cleanup_mod.DiskCleanupManager()
    result = mgr.run(path=str(tmp_path), max_age_days=1, dry_run=False)
    assert result.get("dry_run") is False
    assert str(old_file) in (result.get("deleted") or [])
    assert not old_file.exists()
    assert new_file.exists()


def test_disk_cleanup_service_temp_files(tmp_path):
    tmp_file = tmp_path / "scratch.tmp"
    tmp_file.write_bytes(b"t" * 32)
    mgr = disk_cleanup_mod.DiskCleanupManager()
    result = mgr.run(path=str(tmp_path), max_age_days=3650, dry_run=True)
    deleted = result.get("deleted") or []
    assert str(tmp_file) in deleted


def test_disk_cleanup_service_size_limit(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"x" * 100)
    b.write_bytes(b"y" * 100)
    mt = time.time() - 100
    os.utime(a, (mt, mt))
    os.utime(b, (mt + 1, mt + 1))
    mgr = disk_cleanup_mod.DiskCleanupManager()
    result = mgr.run(path=str(tmp_path), max_age_days=3650, max_total_bytes=100, cleanup_temp=False, dry_run=True)
    assert result.get("deleted_count") == 1
    assert str(a) in (result.get("selected") or [])


def test_disk_cleanup_policy_get_update():
    policy = disk_cleanup_mod.get_policy()
    assert isinstance(policy, dict)
    assert "max_age_days" in policy
    updated = disk_cleanup_mod.update_policy(max_age_days=7)
    assert updated.get("max_age_days") == 7
    # restore default-ish to avoid leaking state
    disk_cleanup_mod.update_policy(max_age_days=policy.get("max_age_days", 30))


def test_disk_cleanup_status():
    mgr = disk_cleanup_mod.DiskCleanupManager()
    st = mgr.get_status()
    assert isinstance(st, dict)
    assert "disk" in st
    assert "policy" in st


def test_disk_cleanup_api_status():
    client = TestClient(app)
    for path in ("/api/disk_cleanup", "/api/disk_cleanup/status", "/api/disk-cleanup"):
        r = client.get(path)
        assert r.status_code == 200, f"{path}: {r.text}"
        assert isinstance(r.json(), dict)


def test_disk_cleanup_api_preview_and_cleanup(tmp_path):
    client = TestClient(app)
    target = tmp_path / "api"
    target.mkdir()
    old = target / "old.mp4"
    old.write_bytes(b"z" * 50)
    old_mtime = time.time() - (10 * 86400)
    os.utime(old, (old_mtime, old_mtime))

    r = client.post("/api/disk_cleanup/preview", json={"path": str(target), "max_age_days": 1})
    assert r.status_code == 200, r.text
    assert str(old) in (r.json().get("deleted") or [])

    r2 = client.post("/api/disk_cleanup/run", json={"path": str(target), "max_age_days": 1, "dry_run": True})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("dry_run") is True
    assert old.exists()

    r3 = client.get("/api/disk_cleanup/policy")
    assert r3.status_code == 200, r3.text
    assert "max_age_days" in r3.json()
