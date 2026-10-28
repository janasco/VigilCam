"""Tests for backup manifest compare (diff two backup manifests)."""

from __future__ import annotations


def test_backup_compare_imports():
    import app.api.backup_compare as api_backup_compare  # noqa: F401
    import app.services.backup_compare as svc  # noqa: F401
    from app.services.backup_compare import (  # noqa: F401
        backup_compare_service,
        compare_backups,
        compare_manifests,
        diff_manifests,
        get_manifest,
    )
    assert hasattr(api_backup_compare, "router")
    assert hasattr(svc, "backup_compare_service")


def test_compare_manifests_pure():
    from app.services import backup_compare as svc

    first = {
        "backup_id": "a",
        "app_version": "0.1.0",
        "files": ["database/vigilcam.db", "config/root.env"],
    }
    second = {
        "backup_id": "b",
        "app_version": "0.2.0",
        "files": ["database/vigilcam.db", "config/config.env"],
    }
    out = svc.compare_manifests(first, second)
    assert out["added_files"] == ["config/config.env"]
    assert out["removed_files"] == ["config/root.env"]
    assert out["common_files"] == ["database/vigilcam.db"]
    assert out["identical"] is False
    paths = {c["path"] for c in out["manifest_diff"]}
    assert "app_version" in paths

    same = svc.compare_manifests(first, dict(first))
    assert same["identical"] is True
    assert same["added_files"] == [] and same["removed_files"] == []

    low_level = svc.diff_manifests({"x": 1}, {"x": 2})
    assert any(c["path"] == "x" and c["op"] == "changed" for c in low_level)


def test_compare_backups_tmpdir(tmp_path):
    from app.services.backup import BackupService
    from app.services import backup_compare as svc

    svc_tmp = BackupService(backup_dir=tmp_path / "backups")
    a = svc_tmp.create_backup(name="first")
    b = svc_tmp.create_backup(name="second")
    aid = a.get("backup_id") or a.get("id")
    bid = b.get("backup_id") or b.get("id")
    assert aid and bid and aid != bid

    out = svc.compare_backups(aid, bid, backup_dir=tmp_path / "backups")
    assert out.get("ok") is True
    assert out.get("first_id") == aid
    assert out.get("second_id") == bid
    assert "added_files" in out and "removed_files" in out
    assert "manifest_diff" in out

    same = svc.compare_backups(aid, aid, backup_dir=tmp_path / "backups")
    assert same["identical"] is True

    try:
        svc.compare_backups("no-such-a", "no-such-b", backup_dir=tmp_path / "backups")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for missing backups")

    try:
        svc.compare_backups(None, bid, backup_dir=tmp_path / "backups")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for missing id")


def test_backup_compare_routes_present():
    from fastapi.routing import APIRoute

    from app.main import app

    # Fast path: expand _IncludedRouter.original_router without full openapi().
    paths: list[str] = []
    for r in getattr(app, "routes", []):
        if isinstance(r, APIRoute) and getattr(r, "path", None):
            paths.append(r.path)
            continue
        orig = getattr(r, "original_router", None) or (r.__dict__.get("original_router") if hasattr(r, "__dict__") else None)
        if orig is not None:
            for ir in getattr(orig, "routes", []):
                p = getattr(ir, "path", None)
                if isinstance(p, str):
                    paths.append(p)
    assert any("compare" in p for p in paths), paths[:20]
    assert any("diff" in p and "backup" in p for p in paths), paths[:20]


def test_backup_compare_api_roundtrip():
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    r1 = client.post("/api/backup", json={"name": "compare-a"})
    assert r1.status_code in (200, 201), r1.text
    r2 = client.post("/api/backup", json={"name": "compare-b"})
    assert r2.status_code in (200, 201), r2.text
    aid = r1.json().get("backup_id") or r1.json().get("id")
    bid = r2.json().get("backup_id") or r2.json().get("id")
    assert aid and bid
    try:
        q = client.get("/api/backup-compare", params={"first_id": aid, "second_id": bid})
        assert q.status_code == 200, q.text
        body = q.json()
        assert body.get("first_id") == aid
        assert body.get("second_id") == bid

        d = client.get(f"/api/backup/{aid}/diff/{bid}")
        assert d.status_code == 200, d.text
        assert "added_files" in d.json() or "manifest_diff" in d.json()

        missing = client.get(
            "/api/backup-compare", params={"first_id": "no-such-a", "second_id": "no-such-b"}
        )
        assert missing.status_code == 404, missing.text

        bad = client.get("/api/backup-compare", params={"first_id": aid})
        assert bad.status_code == 400, bad.text
    finally:
        client.delete(f"/api/backup/{aid}")
        client.delete(f"/api/backup/{bid}")
