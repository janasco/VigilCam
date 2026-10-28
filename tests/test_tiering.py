import os
import time

from fastapi.testclient import TestClient

from app.main import app
from app.services import tiering as tiering_mod


def test_tiering_import():
    import app.services.tiering  # noqa: F401
    import app.api.tiering  # noqa: F401
    from app.services.tiering import TieringManager, tiering_service  # noqa: F401
    from app.api.tiering import router  # noqa: F401


def test_tiering_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/tiering" in paths or "/api/storage/tiering" in paths
    assert any("tier" in p for p in paths)


def test_tiering_service_preview_dry_run(tmp_path):
    hot = tmp_path / "hot"
    cam = hot / "cam1"
    cam.mkdir(parents=True)
    archive = tmp_path / "archive"
    old_file = cam / "old.mp4"
    new_file = cam / "new.mp4"
    old_file.write_bytes(b"x" * 100)
    new_file.write_bytes(b"y" * 100)
    old_mtime = time.time() - (10 * 86400)
    os.utime(old_file, (old_mtime, old_mtime))

    mgr = tiering_mod.TieringManager(storage_root=str(hot), archive_root=str(archive), max_age_days=1)
    result = mgr.run(dry_run=True)
    assert result.get("dry_run") is True
    assert result.get("status") == "completed"
    moved = result.get("moved") or result.get("tiered") or []
    assert str(old_file) in moved
    assert str(new_file) not in moved
    # dry run does not move
    assert old_file.exists()
    assert not (archive / "cam1" / "old.mp4").exists()


def test_tiering_service_actual_move(tmp_path):
    hot = tmp_path / "hot"
    cam = hot / "cam1"
    cam.mkdir(parents=True)
    archive = tmp_path / "archive"
    old_file = cam / "old.mp4"
    new_file = cam / "new.mp4"
    old_file.write_bytes(b"x" * 64)
    new_file.write_bytes(b"y" * 64)
    old_mtime = time.time() - (10 * 86400)
    os.utime(old_file, (old_mtime, old_mtime))

    mgr = tiering_mod.TieringManager(storage_root=str(hot), archive_root=str(archive), max_age_days=1)
    result = mgr.run(dry_run=False)
    assert result.get("dry_run") is False
    moved = result.get("moved") or result.get("tiered") or []
    assert str(old_file) in moved
    assert not old_file.exists()
    assert new_file.exists()
    assert (archive / "cam1" / "old.mp4").exists()


def test_tiering_service_size_limit(tmp_path):
    hot = tmp_path / "hot"
    hot.mkdir(parents=True)
    archive = tmp_path / "archive"
    a = hot / "a.mp4"
    b = hot / "b.mp4"
    a.write_bytes(b"x" * 100)
    b.write_bytes(b"y" * 100)
    mt = time.time() - 100
    os.utime(a, (mt, mt))
    os.utime(b, (mt + 1, mt + 1))
    mgr = tiering_mod.TieringManager(storage_root=str(hot), archive_root=str(archive), max_age_days=3650, max_total_bytes=100)
    result = mgr.run(dry_run=True)
    assert result.get("moved_count") == 1 or result.get("tiered_count") == 1 or result.get("archived_count") == 1
    selected = result.get("selected") or result.get("tiered") or []
    assert str(a) in selected


def test_tiering_policy_get_update():
    policy = tiering_mod.get_policy()
    assert isinstance(policy, dict)
    assert "max_age_days" in policy
    updated = tiering_mod.update_policy(max_age_days=7)
    assert updated.get("max_age_days") == 7
    tiering_mod.update_policy(max_age_days=policy.get("max_age_days", 30))


def test_tiering_status(tmp_path):
    hot = tmp_path / "hot"
    hot.mkdir(parents=True)
    archive = tmp_path / "archive"
    (hot / "c.mp4").write_bytes(b"v" * 10)
    mgr = tiering_mod.TieringManager(storage_root=str(hot), archive_root=str(archive))
    st = mgr.get_status()
    assert isinstance(st, dict)
    assert "policy" in st or "storage_root" in st


def test_tiering_api_status(tmp_path):
    client = TestClient(app)
    for path in ("/api/tiering", "/api/tiering/status"):
        r = client.get(path)
        assert r.status_code == 200, f"{path}: {r.text}"
        assert isinstance(r.json(), dict)


def test_tiering_api_preview_and_run(tmp_path):
    client = TestClient(app)
    hot = tmp_path / "hot"
    cam = hot / "cam1"
    cam.mkdir(parents=True)
    archive = tmp_path / "archive"
    old = cam / "old.mp4"
    old.write_bytes(b"z" * 50)
    old_mtime = time.time() - (10 * 86400)
    os.utime(old, (old_mtime, old_mtime))

    r = client.post("/api/tiering/preview", json={"storage_root": str(hot), "archive_dir": str(archive), "max_age_days": 1})
    assert r.status_code == 200, r.text
    assert str(old) in (r.json().get("moved") or r.json().get("tiered") or [])
    assert old.exists()

    r2 = client.post("/api/tiering/run", json={"storage_root": str(hot), "archive_dir": str(archive), "max_age_days": 1, "dry_run": True})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("dry_run") is True
    assert old.exists()

    r3 = client.get("/api/tiering/policy")
    assert r3.status_code == 200, r3.text
    assert "max_age_days" in r3.json()
