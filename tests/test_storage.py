from fastapi.testclient import TestClient

from app.main import app


def test_storage_import():
    import app.api.storage  # noqa: F401
    import app.models.entities  # noqa: F401
    from app.models.entities import StorageArea  # noqa: F401
    from app.schemas import StorageAreaRead, StorageAreaCreate  # noqa: F401


def test_storage_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/storage/areas" in paths
    assert "/api/storage/reconcile" in paths
    assert "/api/storage/summary" in paths


def test_storage_area_crud_and_reconcile(tmp_path):
    client = TestClient(app)
    area_dir = tmp_path / "area"
    area_dir.mkdir()
    (area_dir / "file.txt").write_bytes(b"x" * 123)

    r = client.post("/api/storage/areas", json={"name": "testarea", "path": str(area_dir), "max_bytes": 1000})
    assert r.status_code in (200, 201), r.text
    area = r.json()
    aid = area["id"]
    assert area["used_bytes"] in (0, None) or area["used_bytes"] == 0

    lst = client.get("/api/storage/areas")
    assert lst.status_code == 200
    assert any(a["id"] == aid for a in lst.json())

    recon = client.post("/api/storage/reconcile", json={"area_id": aid})
    assert recon.status_code == 200
    data = recon.json()
    assert data.get("status") == "completed"

    area_after = next(a for a in client.get("/api/storage/areas").json() if a["id"] == aid)
    assert area_after["used_bytes"] == 123

    summary = client.get("/api/storage/summary")
    assert summary.status_code == 200
    s = summary.json()
    assert (s.get("total_used_bytes") or s.get("used_bytes")) == 123 or s.get("total_used_bytes", 0) >= 123

    delr = client.delete(f"/api/storage/areas/{aid}")
    assert delr.status_code in (200, 204), delr.text

    missing = client.delete(f"/api/storage/areas/{aid}")
    assert missing.status_code == 404
