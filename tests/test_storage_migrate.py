import os

from fastapi.testclient import TestClient

from app.main import app
from app.services import storage_migrate as migrate_mod


def _make_tree(base, files=("a.txt", "sub/b.txt", "cam1/clip.mp4")):
    for rel in files:
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 64)


def test_storage_migrate_import():
    import app.services.storage_migrate  # noqa: F401
    import app.api.storage_migrate  # noqa: F401
    from app.services.storage_migrate import StorageMigrateManager, storage_migrate_service  # noqa: F401
    from app.api.storage_migrate import router  # noqa: F401


def test_storage_migrate_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/storage/migrate" in paths or "/api/storage/migrate/status" in paths
    assert any("migrate" in p for p in paths)


def test_storage_migrate_service_preview_dry_run(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src)
    mgr = migrate_mod.StorageMigrateManager(source_root=str(src), dest_root=str(dst))
    result = mgr.run(dry_run=True)
    assert result.get("dry_run") is True
    assert result.get("status") in ("completed", "completed_with_errors")
    assert result.get("moved_count") == 3 or result.get("migrated_count") == 3
    # dry run does not move
    assert (src / "a.txt").exists()
    assert not (dst / "a.txt").exists()


def test_storage_migrate_service_actual_move(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src)
    mgr = migrate_mod.StorageMigrateManager(source_root=str(src), dest_root=str(dst))
    result = mgr.run(dry_run=False)
    assert result.get("dry_run") is False
    assert result.get("ok") is True
    moved = result.get("moved") or result.get("migrated") or []
    assert any("a.txt" in m for m in moved)
    assert not (src / "a.txt").exists()
    assert (dst / "a.txt").exists()
    assert (dst / "sub" / "b.txt").exists()
    assert (dst / "cam1" / "clip.mp4").exists()


def test_storage_migrate_service_copy_mode(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, files=("keep.txt",))
    mgr = migrate_mod.StorageMigrateManager(source_root=str(src), dest_root=str(dst), copy=True)
    result = mgr.run()
    assert result.get("copy") is True
    assert (src / "keep.txt").exists()
    assert (dst / "keep.txt").exists()


def test_storage_migrate_service_status(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, files=("s.txt",))
    mgr = migrate_mod.StorageMigrateManager(source_root=str(src), dest_root=str(dst))
    st = mgr.get_status()
    assert isinstance(st, dict)
    assert st.get("status") == "ok"
    assert st.get("file_count") == 1 or st.get("candidate_count") == 1


def test_storage_migrate_service_filters(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, files=("a.mp4", "b.txt"))
    mgr = migrate_mod.StorageMigrateManager(source_root=str(src), dest_root=str(dst), extensions=[".mp4"])
    result = mgr.run(dry_run=True)
    selected = result.get("selected") or result.get("moved") or []
    assert any("a.mp4" in s for s in selected)
    assert not any("b.txt" in s for s in selected)


def test_storage_migrate_api_status():
    client = TestClient(app)
    for path in ("/api/storage/migrate", "/api/storage/migrate/status"):
        r = client.get(path)
        assert r.status_code == 200, f"{path}: {r.text}"
        assert isinstance(r.json(), dict)


def test_storage_migrate_api_preview_and_run(tmp_path):
    client = TestClient(app)
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, files=("old.txt",))
    assert (src / "old.txt").exists()

    r = client.post("/api/storage/migrate/preview", json={"source_root": str(src), "dest_root": str(dst)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("dry_run") is True
    assert any("old.txt" in m for m in (body.get("moved") or body.get("migrated") or body.get("selected") or []))
    assert (src / "old.txt").exists()
    assert not (dst / "old.txt").exists()

    r2 = client.post(
        "/api/storage/migrate/run",
        json={"source_root": str(src), "dest_root": str(dst), "dry_run": True},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json().get("dry_run") is True
    assert (src / "old.txt").exists()

    r3 = client.post("/api/storage/migrate/run", json={"source_root": str(src), "dest_root": str(dst)})
    assert r3.status_code == 200, r3.text
    assert r3.json().get("ok") is True
    assert not (src / "old.txt").exists()
    assert (dst / "old.txt").exists()


def test_storage_migrate_api_area_to_area(tmp_path):
    client = TestClient(app)
    src = tmp_path / "area-a"
    dst = tmp_path / "area-b"
    src.mkdir(parents=True)
    dst.mkdir(parents=True)
    (src / "clip.mp4").write_bytes(b"v" * 32)

    ra = client.post("/api/storage/areas", json={"name": "migrate-area-a", "path": str(src)})
    assert ra.status_code in (200, 201), ra.text
    rb = client.post("/api/storage/areas", json={"name": "migrate-area-b", "path": str(dst)})
    assert rb.status_code in (200, 201), rb.text
    aid = ra.json()["id"]
    bid = rb.json()["id"]
    try:
        r = client.post(
            "/api/storage/migrate/run",
            json={"source_area_id": aid, "dest_area_id": bid},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert not (src / "clip.mp4").exists()
        assert (dst / "clip.mp4").exists()
    finally:
        client.delete(f"/api/storage/areas/{aid}")
        client.delete(f"/api/storage/areas/{bid}")


def test_storage_migrate_missing_paths_422():
    client = TestClient(app)
    r = client.post("/api/storage/migrate/run", json={})
    assert r.status_code == 422, r.text
    # Same-source/destination is rejected at the service layer.
    assert migrate_mod.StorageMigrateManager(source_root="/tmp/x", dest_root="/tmp/x").run()["ok"] is False
    # Tiering remains policy-only: no manual area-to-area migration there.
    from pathlib import Path as _Path

    from app.services import tiering as tiering_mod

    mgr = tiering_mod.TieringManager.__new__(tiering_mod.TieringManager)
    assert not hasattr(mgr, "migrate_area_to_area")
    tiering_src = (_Path(__file__).parent.parent / "app" / "services" / "tiering.py").read_text()
    assert "migrat" not in tiering_src.lower()
