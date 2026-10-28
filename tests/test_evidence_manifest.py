from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _good_mp4(path: Path, size: int = 4096, seed: int = 0) -> None:
    blob = bytearray(b"\x00\x00\x00\x20ftypisom\x00\x00\x00\x00isomiso2")
    blob += b"\x00\x00\x00\x08mdat" + bytes((seed + i) % 256 for i in range(64))
    blob += b"\x00\x00\x00\x08moov" + b"\x02" * 64
    while len(blob) < size:
        blob += b"\x00" * 1024
    path.write_bytes(bytes(blob[:size]))


def _seed_root(root: Path) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    out = []
    for i, name in enumerate(("cam-a-001.mp4", "cam-a-002.mp4", "cam-b-001.mp4")):
        p = root / name
        _good_mp4(p, seed=i * 7 + 1)
        out.append(p)
    return out


def test_evidence_manifest_imports():
    import app.services.evidence_manifest as svc  # noqa: F401
    import app.api.evidence_manifest as api  # noqa: F401

    assert hasattr(api, "router")
    for name in ("build_manifest", "verify_manifest", "append_entry",
                 "save_manifest", "load_manifest", "get_status",
                 "compute_hash", "EvidenceManifest"):
        assert hasattr(svc, name), name


def test_integrity_has_no_hash_chain_manifest():
    # VigilCam integrity only validates files in isolation; the hash-chain
    # manifest lives in app.services.evidence_manifest.
    import app.services.integrity as integ

    for missing in ("build_manifest", "verify_manifest", "verify_chain",
                    "append_entry", "prev_hash", "entry_hash", "chain_head"):
        assert not hasattr(integ, missing), missing
    import app.api.integrity as integ_api

    paths = [getattr(r, "path", "") for r in integ_api.router.routes]
    assert not any("manifest" in p for p in paths)


def test_build_and_verify_roundtrip(tmp_path):
    from app.services import evidence_manifest as svc

    root = tmp_path / "rec"
    _seed_root(root)
    mp = tmp_path / "m" / "evidence_manifest.json"

    doc = svc.build_manifest(root, manifest_path=mp, save=True)
    assert doc["count"] == 3
    assert mp.is_file()
    # Hash chain: genesis -> e0 -> e1 -> e2 == head.
    assert doc["entries"][0]["prev_hash"] == svc.GENESIS_HASH
    for prev, cur in zip(doc["entries"], doc["entries"][1:]):
        assert cur["prev_hash"] == prev["entry_hash"]
    assert doc["head"] == doc["entries"][-1]["entry_hash"]

    rep = svc.verify_manifest(doc, root=root)
    assert rep["ok"] is True
    assert rep["status"] == "ok"
    assert rep["bad_count"] == 0

    # Reload from disk verifies too (durable write).
    loaded = svc.load_manifest(mp)
    assert loaded is not None
    rep2 = svc.verify_manifest(loaded, root=root)
    assert rep2["ok"] is True
    assert rep2["computed_head"] == doc["head"]


def test_verify_detects_tamper_and_missing(tmp_path):
    from app.services import evidence_manifest as svc

    root = tmp_path / "rec"
    files = _seed_root(root)
    mp = tmp_path / "evidence_manifest.json"

    doc = svc.build_manifest(root, manifest_path=mp, save=True)

    # Tamper with file bytes on disk.
    files[1].write_bytes(files[1].read_bytes() + b"TAMPERED")
    rep = svc.verify_manifest(doc, root=root)
    assert rep["ok"] is False
    assert rep["bad_count"] >= 1
    assert len(rep["tampered"]) >= 1

    # Delete a file -> missing.
    files[0].unlink()
    rep2 = svc.verify_manifest(doc, root=root)
    assert rep2["ok"] is False
    assert len(rep2["missing"]) >= 1

    # Edit the manifest itself -> entry-hash mismatch.
    import copy
    evil = copy.deepcopy(doc)
    evil["entries"][0]["sha256"] = "f" * 64
    rep3 = svc.verify_manifest(evil, root=root, verify_files=False)
    assert rep3["ok"] is False


def test_append_entry_and_status(tmp_path):
    from app.services import evidence_manifest as svc

    root = tmp_path / "rec"
    root.mkdir(parents=True, exist_ok=True)
    mp = tmp_path / "evidence_manifest.json"

    p1 = root / "one.mp4"
    _good_mp4(p1, seed=11)
    r1 = svc.append_entry(p1, manifest_path=mp, root=root)
    assert r1["ok"] is True
    r2doc = svc.load_manifest(mp)
    assert r2doc is not None and r2doc["count"] == 1

    p2 = root / "two.mp4"
    _good_mp4(p2, seed=22)
    r2 = svc.append_entry(p2, manifest_path=mp, root=root)
    assert r2["entry"]["prev_hash"] == r1["entry"]["entry_hash"]

    st = svc.get_status(mp)
    assert st["exists"] is True
    assert st["total"] == 2 or st.get("count") == 2 or st.get("entries") == 2

    checker = svc.EvidenceManifest(root=root, manifest_path=tmp_path / "c.json")
    assert hasattr(checker, "build_manifest")
    assert hasattr(checker, "verify")
    assert hasattr(checker, "append")
    assert hasattr(checker, "get_status")


def _mounted_client() -> TestClient:
    from app.api.evidence_manifest import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_evidence_manifest_api(tmp_path):
    client = _mounted_client()
    r = client.get("/api/evidence-manifest/")
    assert r.status_code == 200, r.text

    root = tmp_path / "rec"
    _seed_root(root)
    mp = str(tmp_path / "api_manifest.json")

    r2 = client.post("/api/evidence-manifest/build",
                     json={"root": str(root), "manifest_path": mp})
    assert r2.status_code == 200, r2.text
    assert r2.json()["count"] == 3

    r3 = client.post("/api/evidence-manifest/verify",
                     json={"root": str(root), "manifest_path": mp})
    assert r3.status_code == 200, r3.text
    assert r3.json()["ok"] is True

    r4 = client.get("/api/evidence_manifest/status",
                    params={"manifest_path": mp})
    assert r4.status_code == 200, r4.text


def test_evidence_manifest_router_prefix():
    from app.api.evidence_manifest import router

    paths = [getattr(r, "path", "") for r in router.routes]
    assert any("/api/evidence-manifest" in p for p in paths)
    assert any("/api/evidence_manifest" in p for p in paths)


def test_evidence_manifest_on_full_app():
    from app.main import app

    client = TestClient(app)
    paths = list(app.openapi()["paths"].keys())
    assert any("evidence-manifest" in p or "evidence_manifest" in p for p in paths)
    r = client.get("/api/evidence-manifest/status")
    assert r.status_code == 200, r.text
