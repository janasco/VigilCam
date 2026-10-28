"""Tests for the system backup/restore service (DB + config export/import)."""

from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient


def test_backup_imports():
    import app.api.backup as api_backup  # noqa: F401
    import app.services.backup as svc_backup  # noqa: F401
    from app.services.backup import (  # noqa: F401
        backup_service,
        create_backup,
        list_backups,
        get_backup,
        delete_backup,
        restore_backup,
        export_backup,
        import_backup,
    )
    assert hasattr(api_backup, "router")
    assert hasattr(svc_backup, "backup_service")


def test_backup_routes_present():
    from app.main import app

    paths = list(app.openapi()["paths"].keys())
    assert "/api/backup" in paths
    assert "/api/backups" in paths or "/api/system/backup" in paths


def _client():
    from app.main import app

    return TestClient(app)


def test_backup_create_list_download_delete():
    client = _client()

    r = client.post("/api/backup", json={"name": "test-backup"})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    backup_id = body.get("backup_id") or body.get("id")
    assert backup_id, body

    lst = client.get("/api/backup")
    assert lst.status_code == 200, lst.text
    items = lst.json()
    assert isinstance(items, list)
    assert any((i.get("backup_id") or i.get("id")) == backup_id for i in items)

    info = client.get(f"/api/backup/{backup_id}")
    assert info.status_code == 200, info.text
    assert (info.json().get("backup_id") or info.json().get("id")) == backup_id

    dl = client.get(f"/api/backup/{backup_id}/download")
    assert dl.status_code == 200, dl.text[:500] if hasattr(dl, "text") else dl.status_code
    assert dl.content[:2] == b"PK"
    zf = zipfile.ZipFile(io.BytesIO(dl.content))
    names = zf.namelist()
    assert "manifest.json" in names
    assert any(n.endswith("vigilcam.db") for n in names)

    missing = client.get("/api/backup/does-not-exist-123")
    assert missing.status_code == 404

    rm = client.delete(f"/api/backup/{backup_id}")
    assert rm.status_code in (200, 204), rm.text
    assert client.get(f"/api/backup/{backup_id}").status_code == 404


def test_backup_alias_prefixes():
    client = _client()
    r = client.post("/api/backups", json={"label": "alias-check"})
    assert r.status_code in (200, 201), r.text
    backup_id = r.json().get("backup_id") or r.json().get("id")
    assert backup_id

    sys_list = client.get("/api/system/backup")
    assert sys_list.status_code == 200, sys_list.text
    assert any((i.get("backup_id") or i.get("id")) == backup_id for i in sys_list.json())

    client.delete(f"/api/backup/{backup_id}")


def test_backup_export_and_restore_roundtrip():
    client = _client()
    created = client.post("/api/backup", json={"name": "roundtrip"}).json()
    backup_id = created.get("backup_id") or created.get("id")
    assert backup_id

    # Download the archive then re-import it via upload (config-only to be safe,
    # then full restore of the same DB snapshot).
    dl = client.get(f"/api/backup/{backup_id}/download")
    assert dl.status_code == 200

    up = client.post(
        "/api/backup/import",
        files={"file": ("roundtrip.zip", dl.content, "application/zip")},
    )
    assert up.status_code == 200, up.text
    assert up.json().get("ok") is True

    rest = client.post("/api/backup/restore", json={"backup_id": backup_id})
    assert rest.status_code == 200, rest.text
    assert rest.json().get("ok") is True

    # Restore via path-style endpoint too.
    rest2 = client.post(f"/api/backup/{backup_id}/restore", json={})
    assert rest2.status_code == 200, rest2.text

    client.delete(f"/api/backup/{backup_id}")


def test_backup_restore_missing_returns_404():
    client = _client()
    r = client.post("/api/backup/restore", json={"backup_id": "no-such-backup"})
    assert r.status_code in (400, 404), r.text

    r2 = client.post("/api/backup/restore", json={})
    assert r2.status_code == 400, r2.text


def test_backup_service_roundtrip_tmpdir(tmp_path):
    from app.services.backup import BackupService

    svc = BackupService(backup_dir=tmp_path / "backups")
    info = svc.create_backup(name="unit")
    assert info.get("ok") is True
    bid = info.get("backup_id") or info.get("id")
    assert bid

    items = svc.list_backups()
    assert any((i.get("backup_id") or i.get("id")) == bid for i in items)
    assert svc.get_backup(bid) is not None
    assert svc.backup_path(bid) is not None and svc.backup_path(bid).exists()

    raw = svc.export_bytes(bid)
    assert raw[:2] == b"PK"

    # Restore from raw bytes into the live DB path (same snapshot) is safe.
    out = svc.restore_backup(raw)
    assert out.get("ok") is True
    assert "database" in out.get("restored", [])

    assert svc.delete_backup(bid) is True
    assert svc.get_backup(bid) is None
    assert svc.delete_backup(bid) is False
