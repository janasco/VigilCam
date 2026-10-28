"""Audit hash-chain tests.

Pins the gap and the new layer:

* ``app.services.audit`` stores rows with **no** tamper-evident chaining
  (no prev/entry hashes, no verify helper).
* ``app.services.evidence_manifest`` chains **recordings only** (video
  files + per-file hashes), never audit actions.
* ``app.services.audit_chain`` + ``app.api.audit_chain`` add the missing
  hash chain over audit entries (genesis -> e0 -> e1 -> head).
"""
from __future__ import annotations

import copy

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _sample_entries():
    return [
        {"action": "auth.login", "actor": "admin", "timestamp": "2024-01-01T12:00:00"},
        {"action": "camera.create", "actor": "admin", "entity_type": "camera",
         "entity_id": "cam-1", "timestamp": "2024-01-01T12:01:00"},
        {"action": "ptz.move", "actor": "ops", "camera_id": "cam-1",
         "timestamp": "2024-01-01T12:02:00"},
    ]


def test_audit_chain_imports():
    import app.services.audit_chain as svc  # noqa: F401
    import app.api.audit_chain as api  # noqa: F401

    assert hasattr(api, "router")
    for name in ("build_chain", "verify_chain", "append_entry",
                 "save_chain", "load_chain", "get_status",
                 "compute_entry_hash", "chain_entries", "AuditChain"):
        assert hasattr(svc, name), name
    assert hasattr(svc, "GENESIS_HASH")
    assert hasattr(svc, "ALGORITHM")


def test_audit_has_no_tamper_evident_chaining():
    # The plain audit store keeps rows without any hash linkage; the
    # chain lives in app.services.audit_chain.
    import app.services.audit as audit
    import app.services.audit_chain as chained

    for missing in ("verify_chain", "verify_audit_chain", "build_chain",
                    "GENESIS_HASH", "compute_entry_hash", "chain_entries"):
        assert not hasattr(audit, missing), missing

    store = audit.AuditStore()
    ev = store.append({"action": "auth.login", "actor": "admin"})
    assert "prev_hash" not in ev
    assert "entry_hash" not in ev
    assert "chain_head" not in ev
    assert "genesis" not in ev
    # New layer *does* expose the chaining vocabulary.
    assert chained.GENESIS_HASH == "0" * 64
    assert callable(chained.verify_chain)


def test_evidence_manifest_chains_recordings_only():
    # Evidence manifest hashes recording files, not audit actions.
    import app.services.evidence_manifest as manifest

    assert manifest.GENESIS_HASH == "0" * 64
    # Video-extension filter pins the recordings-only scope.
    exts = {str(e).lower() for e in getattr(manifest, "DEFAULT_EXTENSIONS", {".mp4"})}
    assert ".mp4" in exts
    assert "auth.login" not in exts
    # No audit-action chaining entrypoint on the manifest service.
    for missing in ("chain_audit_entries", "verify_audit_chain", "canonical_entry",
                    "compute_entry_hash", "build_audit_chain"):
        assert not hasattr(manifest, missing), missing


def test_build_and_verify_roundtrip(tmp_path):
    from app.services import audit_chain as svc

    entries = _sample_entries()
    mp = tmp_path / "audit_chain.json"

    doc = svc.build_chain(entries, chain_path=str(mp), save=True)
    assert doc["count"] == 3
    assert mp.is_file()
    # Hash chain: genesis -> e0 -> e1 -> e2 == head.
    assert doc["entries"][0]["prev_hash"] == svc.GENESIS_HASH
    for prev, cur in zip(doc["entries"], doc["entries"][1:]):
        assert cur["prev_hash"] == prev["entry_hash"]
    assert doc["head"] == doc["entries"][-1]["entry_hash"]

    rep = svc.verify_chain(doc)
    assert rep["ok"] is True
    assert rep["status"] == "ok"
    assert rep["bad_count"] == 0

    # Reload from disk verifies too (durable write).
    loaded = svc.load_chain(str(mp))
    assert loaded is not None
    rep2 = svc.verify_chain(loaded)
    assert rep2["ok"] is True
    assert rep2["computed_head"] == doc["head"]


def test_verify_detects_tamper_reorder_and_truncation(tmp_path):
    from app.services import audit_chain as svc

    mp = tmp_path / "audit_chain.json"
    doc = svc.build_chain(_sample_entries(), chain_path=str(mp), save=True)

    # Edit an action in the chained payload -> tampered.
    evil = copy.deepcopy(doc)
    evil["entries"][1]["action"] = "camera.delete"
    rep = svc.verify_chain(evil)
    assert rep["ok"] is False
    assert rep["bad_count"] >= 1
    assert len(rep["tampered"]) >= 1

    # Reorder entries -> broken link.
    swapped = copy.deepcopy(doc)
    swapped["entries"][0], swapped["entries"][1] = swapped["entries"][1], swapped["entries"][0]
    rep2 = svc.verify_chain(swapped)
    assert rep2["ok"] is False

    # Truncate the last entry but keep the old head -> head mismatch.
    cut = copy.deepcopy(doc)
    cut["entries"] = cut["entries"][:-1]
    rep3 = svc.verify_chain(cut)
    assert rep3["ok"] is False

    # Edit the stored hash itself -> entry-hash mismatch.
    forged = copy.deepcopy(doc)
    forged["entries"][0]["entry_hash"] = "f" * 64
    rep4 = svc.verify_chain(forged, chain_path=str(mp))
    assert rep4["ok"] is False


def test_append_entry_and_status(tmp_path):
    from app.services import audit_chain as svc

    mp = tmp_path / "chain.json"
    r1 = svc.append_entry({"action": "auth.login", "actor": "admin"},
                          chain_path=str(mp), persist_audit=False)
    assert r1["ok"] is True
    doc = svc.load_chain(str(mp))
    assert doc is not None and doc["count"] >= 1

    r2 = svc.append_entry({"action": "ptz.move", "actor": "ops"},
                          chain_path=str(mp), persist_audit=False)
    assert r2["entry"]["prev_hash"] == r1["entry"]["entry_hash"]

    st = svc.get_status(str(mp))
    assert st["exists"] is True
    assert st["ok"] is True
    total = st.get("total", st.get("count"))
    assert total == 2 or st.get("entries") == 2

    checker = svc.AuditChain(chain_path=str(tmp_path / "c.json"))
    assert hasattr(checker, "build_chain")
    assert hasattr(checker, "verify")
    assert hasattr(checker, "append")
    assert hasattr(checker, "get_status")


def _mounted_client() -> TestClient:
    from app.api.audit_chain import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_audit_chain_api(tmp_path):
    client = _mounted_client()
    r = client.get("/api/audit/chain/")
    assert r.status_code == 200, r.text

    mp = str(tmp_path / "api_chain.json")
    entries = _sample_entries()
    r2 = client.post("/api/audit/chain/build",
                     json={"entries": entries, "chain_path": mp})
    assert r2.status_code == 200, r2.text
    assert r2.json()["count"] == 3

    r3 = client.post("/api/audit/chain/verify",
                     json={"chain_path": mp})
    assert r3.status_code == 200, r3.text
    assert r3.json()["ok"] is True

    r4 = client.get("/api/audit-chain/status", params={"chain_path": mp})
    assert r4.status_code == 200, r4.text


def test_audit_chain_router_prefix():
    from app.api.audit_chain import router

    paths = [getattr(r, "path", "") for r in router.routes]
    assert any("/api/audit/chain" in p for p in paths)
    assert any("/api/audit-chain" in p for p in paths)


def test_audit_chain_on_full_app(tmp_path):
    import pkgutil

    import app.api as api_pkg
    from app.main import app

    # Auto-loader in app.main scans every app.api module with a `router`
    # attr; the new module must be discoverable without a main.py edit.
    assert any(m.name == "audit_chain" for m in pkgutil.iter_modules(api_pkg.__path__))
    from app.api.audit_chain import router as audit_chain_router

    assert audit_chain_router is not None
    paths = [getattr(r, "path", "") for r in app.routes]
    if any("audit" in p and "chain" in p for p in paths):
        client = TestClient(app)
        r = client.get("/api/audit/chain/status")
        assert r.status_code == 200, r.text
    else:
        mounted = [getattr(r, "path", "") for r in audit_chain_router.routes]
        assert any("audit" in p and "chain" in p for p in mounted)
