from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _seed(root: Path, names=("a.mp4", "b.mp4")) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    out = []
    for n in names:
        p = root / n
        p.write_bytes(b"x" * 1024)
        out.append(p)
    return out


def test_orphans_imports():
    import app.services.orphans as svc  # noqa: F401
    import app.api.orphans as api  # noqa: F401

    assert hasattr(api, "router")
    assert hasattr(svc, "OrphanFinder")
    assert hasattr(svc, "find_orphans")
    assert hasattr(svc, "scan_directory")
    assert hasattr(svc, "delete_orphans")
    assert hasattr(svc, "get_status")


def test_find_orphans_with_known_paths(tmp_path):
    from app.services import orphans as svc

    root = tmp_path / "rec"
    a, b = _seed(root)
    # One file referenced in DB, one orphaned on disk.
    rep = svc.find_orphans(root, known_paths=[str(a)])
    assert rep["total"] == 2
    assert rep["orphan_count"] == 1
    assert rep["has_orphans"] is True
    assert rep["status"] == "has-orphans"
    assert str(b) in rep["orphans"]
    assert str(a) not in rep["orphans"]
    assert rep["db_available"] is True

    # All referenced -> ok.
    rep2 = svc.find_orphans(root, known_paths=[str(a), str(b)])
    assert rep2["orphan_count"] == 0
    assert rep2["ok"] is True
    assert rep2["status"] == "ok"

    # DB directory reference covers everything below it.
    rep3 = svc.find_orphans(root, known_paths=[str(root)])
    assert rep3["orphan_count"] == 0

    # Basename-only DB entries also count as referenced.
    rep4 = svc.find_orphans(root, known_paths=["a.mp4", "b.mp4"])
    assert rep4["orphan_count"] == 0


def test_empty_dir_no_orphans(tmp_path):
    from app.services import orphans as svc

    root = tmp_path / "empty"
    root.mkdir()
    rep = svc.find_orphans(root, known_paths=[])
    assert rep["total"] == 0
    assert rep["orphan_count"] == 0
    assert rep["ok"] is True


def test_scan_and_delete(tmp_path):
    from app.services import orphans as svc

    root = tmp_path / "rec"
    a, b = _seed(root)
    rep = svc.find_orphans(root, known_paths=[str(a)], delete=True)
    assert rep["deleted_count"] == 1
    assert not b.exists()
    assert a.exists()

    # Class + aliases.
    root2 = tmp_path / "rec2"
    c, d = _seed(root2)
    finder = svc.OrphanFinder(root2, known_paths=[str(c)])
    rep2 = finder.scan()
    assert rep2["orphan_count"] == 1
    st = finder.get_status()
    assert st["orphan_count"] == 1
    assert svc.OrphansFinder is svc.OrphanFinder
    assert svc.get_status(root2, known_paths=[str(c), str(d)])["ok"] is True


def _mounted_client() -> TestClient:
    from app.api.orphans import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_orphans_api_status_and_scan(tmp_path):
    client = _mounted_client()
    r = client.get("/api/orphans/")
    assert r.status_code == 200, r.text
    assert "status" in r.json()

    root = tmp_path / "api-rec"
    a, b = _seed(root)

    r2 = client.post("/api/orphans/scan", json={"path": str(root), "known_paths": [str(a)]})
    assert r2.status_code == 200, r2.text
    data = r2.json()
    assert data["total"] == 2
    assert data["orphan_count"] == 1

    r3 = client.post("/api/orphans/delete", json={"path": str(root), "known_paths": [str(a)]})
    assert r3.status_code == 200, r3.text
    assert r3.json()["deleted_count"] == 1
    assert not b.exists()


def test_orphans_router_prefix():
    from app.api.orphans import router

    assert any("/api/orphans" in getattr(r, "path", "") for r in router.routes)


def test_orphans_on_full_app():
    from app.main import app

    client = TestClient(app)
    paths = list(app.openapi()["paths"].keys())
    assert any("orphan" in p for p in paths)
    r = client.get("/api/orphans/status")
    assert r.status_code == 200, r.text
