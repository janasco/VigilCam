from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.audit_prune import router as audit_prune_router
from app.services import audit_prune as svc
from app.services.audit import _default_store, append_audit_entry


def _make_client():
    app = FastAPI()
    app.include_router(audit_prune_router)
    return TestClient(app)


def _old_ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)).isoformat()


def test_audit_prune_import():
    import app.api.audit_prune  # noqa: F401
    import app.services.audit_prune  # noqa: F401
    assert audit_prune_router is not None
    assert hasattr(svc, "prune")
    assert hasattr(svc, "preview")
    from app.services.audit_prune import (  # noqa: F401
        AuditPruneService,
        audit_prune_service,
        prune,
        preview,
        select_for_deletion,
    )
    from app.api.audit_prune import router  # noqa: F401


def test_audit_prune_paths_present():
    # Router-level check only (fast). The main app auto-registers every
    # app.api module exposing ``router``, so no app/main.py edit is needed.
    # Full app.openapi() is intentionally not built here: it is expensive
    # with the large auto-registered surface.
    routes = [getattr(r, "path", "") for r in audit_prune_router.routes]
    assert any("audit" in p and "prune" in p for p in routes), routes
    assert "/api/audit/prune" in routes
    assert "/api/audit/prune/preview" in routes
    assert "/api/audit/prune/status" in routes


def test_audit_prune_service_dry_run():
    _default_store.clear()
    append_audit_entry({"action": "old-action", "actor": "alice", "timestamp": _old_ts(30)})
    append_audit_entry({"action": "new-action", "actor": "bob"})
    total_before = len(_default_store.read_all())

    result = svc.prune(retention_days=7, dry_run=True)
    assert result.get("dry_run") is True
    selected = result.get("selected") or result.get("candidates") or []
    assert any((e.get("action") == "old-action") for e in selected)
    assert all((e.get("action") != "new-action") for e in selected)
    # dry run removes nothing
    assert len(_default_store.read_all()) == total_before


def test_audit_prune_service_deletes_old():
    _default_store.clear()
    append_audit_entry({"action": "old-action", "actor": "alice", "timestamp": _old_ts(30)})
    append_audit_entry({"action": "new-action", "actor": "bob"})

    result = svc.prune(retention_days=7)
    assert result.get("dry_run") is False
    remaining = _default_store.read_all()
    assert all(e.get("action") != "old-action" for e in remaining)
    assert any(e.get("action") == "new-action" for e in remaining)


def test_audit_prune_service_max_entries():
    _default_store.clear()
    for i in range(5):
        append_audit_entry({"action": f"action-{i}", "actor": "alice"})
    result = svc.prune(max_entries=2)
    remaining = _default_store.read_all()
    assert len(remaining) == 2
    assert result.get("remaining_count") == 2 or result.get("remaining") == 2


def test_audit_prune_router():
    _default_store.clear()
    append_audit_entry({"action": "old-action", "actor": "alice", "timestamp": _old_ts(30)})
    append_audit_entry({"action": "new-action", "actor": "bob"})
    client = _make_client()

    r_preview = client.post("/api/audit/prune/preview", json={"retention_days": 7})
    assert r_preview.status_code == 200, r_preview.text
    data = r_preview.json()
    assert data.get("dry_run") is True
    # preview keeps rows
    assert len(_default_store.read_all()) == 2

    r_prune = client.post("/api/audit/prune", json={"retention_days": 7})
    assert r_prune.status_code == 200, r_prune.text
    assert len(_default_store.read_all()) == 1

    r_status = client.get("/api/audit/prune/status")
    assert r_status.status_code == 200, r_status.text
