from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _good_mp4(path: Path, size: int = 4096) -> None:
    # Minimal ftyp + mdat + moov so header/structure sniffs pass.
    blob = bytearray(b"\x00\x00\x00\x20ftypisom\x00\x00\x00\x00isomiso2")
    blob += b"\x00\x00\x00\x08mdat" + b"\x01" * 64
    blob += b"\x00\x00\x00\x08moov" + b"\x02" * 64
    while len(blob) < size:
        blob += b"\x00" * 1024
    path.write_bytes(bytes(blob[:size]))


def test_integrity_imports():
    import app.services.integrity as svc  # noqa: F401
    import app.api.integrity as api  # noqa: F401

    assert hasattr(api, "router")
    assert hasattr(svc, "IntegrityChecker")
    assert hasattr(svc, "check_file")
    assert hasattr(svc, "scan_directory")
    assert hasattr(svc, "quarantine_file")
    assert hasattr(svc, "get_status")


def test_check_file_good_and_bad(tmp_path):
    from app.services import integrity as svc

    good = tmp_path / "good.mp4"
    _good_mp4(good)
    rep = svc.check_file(good)
    assert rep["ok"] is True
    assert rep["corrupt"] is False
    assert rep["issues"] == []

    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video at all" * 100)
    rep2 = svc.check_file(bad)
    assert rep2["ok"] is False
    assert rep2["corrupt"] is True
    assert len(rep2["issues"]) > 0

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert svc.check_file(empty)["corrupt"] is True
    assert svc.is_corrupt(bad) is True
    assert svc.is_valid(good) is True


def test_scan_and_quarantine(tmp_path):
    from app.services import integrity as svc

    root = tmp_path / "rec"
    root.mkdir()
    _good_mp4(root / "a.mp4")
    (root / "b.mp4").write_bytes(b"junk-bytes" * 200)

    rep = svc.scan_directory(root)
    assert rep["total"] == 2
    assert rep["ok_count"] == 1
    assert rep["bad_count"] == 1
    assert rep["status"] == "corrupt"

    qdir = tmp_path / "q"
    rep2 = svc.scan_directory(root, quarantine=True, quarantine_dir=qdir)
    assert rep2["quarantined_count"] == 1
    assert not (root / "b.mp4").exists()
    assert len(list(qdir.glob("*"))) == 1

    # Class + aliases.
    checker = svc.IntegrityChecker(root)
    assert hasattr(checker, "scan")
    st = checker.get_status()
    assert "ok_count" in st or "report" in st
    assert svc.RecordingIntegrityChecker is svc.IntegrityChecker


def test_compute_hash(tmp_path):
    from app.services import integrity as svc

    p = tmp_path / "f.mp4"
    _good_mp4(p)
    h = svc.compute_hash(p)
    assert isinstance(h, str) and len(h) == 64


def _mounted_client() -> TestClient:
    from app.api.integrity import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_integrity_api_status_and_scan(tmp_path):
    client = _mounted_client()
    r = client.get("/api/integrity/")
    assert r.status_code == 200, r.text
    assert "status" in r.json()

    root = tmp_path / "api-rec"
    root.mkdir()
    _good_mp4(root / "ok.mp4")
    (root / "broken.mp4").write_bytes(b"zzz" * 500)

    r2 = client.post("/api/integrity/scan", json={"path": str(root)})
    assert r2.status_code == 200, r2.text
    data = r2.json()
    assert data["total"] == 2
    assert data["bad_count"] == 1

    r3 = client.post("/api/integrity/check", json={"path": str(root)})
    assert r3.status_code == 200, r3.text


def test_integrity_router_prefix():
    from app.api.integrity import router

    assert any("/api/integrity" in getattr(r, "path", "") for r in router.routes)


def test_integrity_on_full_app():
    from app.main import app

    client = TestClient(app)
    paths = list(app.openapi()["paths"].keys())
    assert any("integrity" in p for p in paths)
    r = client.get("/api/integrity/status")
    assert r.status_code == 200, r.text
