"""Audit gap finder tests.

Reference (read-only): OpenNVR ``server/tests/test_audit_gaps.py`` pins
that state-changing endpoints (PTZ move/stop, alert ack, recording
protect/export, firewall admin, skill claim/release) must each leave an
audit row, and that an audit failure never fails the audited action.

VigilCam previously had no audit gap detection (recording gaps are
covered by ``app.services.gap_detector``; audit rows by
``app.services.audit`` with export/prune helpers, but nothing that finds
missing audit time ranges or missing expected actions). These tests pin
the new finder: ``app.services.audit_gaps`` + ``app.api.audit_gaps``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _entries_two_clusters():
    base = datetime(2024, 1, 1, 12, 0, 0)
    return [
        {"action": "auth.login", "timestamp": base.isoformat()},
        {"action": "camera.create", "timestamp": (base + timedelta(minutes=5)).isoformat()},
        # 3-hour silence -> temporal gap.
        {"action": "ptz.move", "timestamp": (base + timedelta(hours=3, minutes=10)).isoformat()},
        {"action": "ptz.stop", "timestamp": (base + timedelta(hours=3, minutes=12)).isoformat()},
    ]


def test_audit_gaps_imports():
    import app.api.audit_gaps as api
    import app.services.audit_gaps as svc

    assert hasattr(api, "router")
    assert hasattr(svc, "AuditGapsDetector")
    assert hasattr(svc, "detect_audit_gaps")
    assert hasattr(svc, "find_audit_gaps")
    assert hasattr(svc, "find_missing_actions")
    assert hasattr(svc, "check_action_coverage")
    assert hasattr(svc, "get_status")


def test_find_temporal_gap_basic():
    from app.services import audit_gaps as svc

    gaps = svc.find_audit_gaps(entries=_entries_two_clusters(), max_gap_seconds=3600)
    assert len(gaps) == 1
    assert gaps[0]["duration_seconds"] == timedelta(hours=3, minutes=5).total_seconds()
    assert gaps[0]["start_iso"] is not None and gaps[0]["end_iso"] is not None

    # Contiguous writes -> no gaps.
    base = datetime(2024, 1, 1, 12, 0, 0)
    tight = [
        {"action": "a", "timestamp": base.isoformat()},
        {"action": "b", "timestamp": (base + timedelta(minutes=10)).isoformat()},
        {"action": "c", "timestamp": (base + timedelta(minutes=20)).isoformat()},
    ]
    assert svc.find_audit_gaps(entries=tight, max_gap_seconds=3600) == []

    # Tiny silence below min_gap is ignored.
    assert svc.find_audit_gaps(entries=tight, max_gap_seconds=60, min_gap_seconds=3600) == []


def test_missing_actions_coverage():
    from app.services import audit_gaps as svc

    entries = [
        {"action": "ptz.move", "timestamp": "2024-01-01T12:00:00"},
        {"action": "ptz.stop", "timestamp": "2024-01-01T12:01:00"},
    ]
    missing = svc.find_missing_actions(
        entries=entries, expected_actions=["ptz.move", "ptz.stop", "alerts.ack"]
    )
    assert missing == ["alerts.ack"]

    cov = svc.check_action_coverage(
        entries=entries, expected_actions=["ptz.move", "ptz.stop", "alerts.ack"]
    )
    assert cov["covered_count"] == 2
    assert cov["missing_count"] == 1
    assert cov["complete"] is False

    full = svc.check_action_coverage(
        entries=entries, expected_actions=["ptz.move", "ptz.stop"]
    )
    assert full["complete"] is True
    assert full["ok"] is True


def test_detect_report_combines_both_gap_kinds():
    from app.services import audit_gaps as svc

    rep = svc.detect_audit_gaps(
        entries=_entries_two_clusters(),
        max_gap_seconds=3600,
        expected_actions=["auth.login", "alerts.ack", "recording.export"],
    )
    assert rep["status"] == "gaps"
    assert rep["has_gaps"] is True
    assert rep["total_gaps"] == 1
    assert set(rep["missing_actions"]) == {"alerts.ack", "recording.export"}
    assert rep["total_entries"] == 4
    assert rep["ok"] is False

    clean = svc.detect_audit_gaps(
        entries=[
            {"action": "auth.login", "timestamp": "2024-01-01T12:00:00"},
            {"action": "alerts.ack", "timestamp": "2024-01-01T12:01:00"},
        ],
        max_gap_seconds=3600,
        expected_actions=["auth.login", "alerts.ack"],
    )
    assert clean["status"] == "ok"
    assert clean["ok"] is True
    assert clean["has_gaps"] is False


def test_detector_class_and_aliases():
    from app.services import audit_gaps as svc

    det = svc.AuditGapsDetector(max_gap_seconds=3600)
    rep = det.detect(entries=_entries_two_clusters(),
                     expected_actions=["auth.login", "alerts.ack"])
    assert rep["total_gaps"] == 1
    assert det.scan(entries=_entries_two_clusters())["total_gaps"] == 1
    assert svc.AuditGapDetector is svc.AuditGapsDetector
    st = det.get_status(entries=_entries_two_clusters())
    assert st["total_gaps"] == 1
    assert svc.get_status(entries=_entries_two_clusters())["total_gaps"] == 1


def _mounted_client() -> TestClient:
    from app.api.audit_gaps import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_audit_gaps_api_status_and_scan():
    client = _mounted_client()
    r = client.get("/api/audit/gaps/")
    assert r.status_code == 200, r.text
    assert "status" in r.json()

    r2 = client.post("/api/audit/gaps/scan", json={
        "entries": _entries_two_clusters(),
        "max_gap_seconds": 3600,
        "expected_actions": ["auth.login", "alerts.ack"],
    })
    assert r2.status_code == 200, r2.text
    data = r2.json()
    assert data["total_gaps"] == 1
    assert "alerts.ack" in data["missing_actions"]

    r3 = client.post("/api/audit/gaps/coverage", json={
        "entries": [{"action": "ptz.move"}],
        "expected_actions": ["ptz.move", "ptz.stop"],
    })
    assert r3.status_code == 200, r3.text
    assert r3.json()["missing"] == ["ptz.stop"]

    r4 = client.get("/api/audit-gaps/status")
    assert r4.status_code == 200, r4.text


def test_audit_gaps_router_prefix():
    from app.api.audit_gaps import router

    paths = [getattr(r, "path", "") for r in router.routes]
    assert any("/api/audit/gaps" in p for p in paths)
    assert any("/api/audit-gaps" in p for p in paths)


def test_audit_gaps_on_full_app():
    import pkgutil

    import app.api as api_pkg
    from app.main import app

    # Auto-loader in app.main scans every app.api module with a `router`
    # attr; the new module must be discoverable without a main.py edit.
    assert any(m.name == "audit_gaps" for m in pkgutil.iter_modules(api_pkg.__path__))
    from app.api.audit_gaps import router as audit_gaps_router

    assert audit_gaps_router is not None
    # Prefer the auto-registered route; fall back to a local mount if the
    # host app snapshot predates this module (keeps the test fast: no
    # app.openapi() generation over 1000+ routes).
    paths = [getattr(r, "path", "") for r in app.routes]
    if any("audit" in p and "gap" in p for p in paths):
        client = TestClient(app)
        r = client.get("/api/audit/gaps/status")
        assert r.status_code == 200, r.text
    else:
        mounted = [getattr(r, "path", "") for r in audit_gaps_router.routes]
        assert any("audit" in p and "gap" in p for p in mounted)


def test_audit_gaps_reads_default_store():
    from app.services.audit import _default_store, append_audit_entry
    from app.services import audit_gaps as svc

    _default_store.clear()
    try:
        append_audit_entry({"action": "auth.login",
                            "timestamp": "2024-01-01T12:00:00"})
        append_audit_entry({"action": "auth.login",
                            "timestamp": "2024-01-01T16:00:00"})
        rep = svc.detect_audit_gaps(max_gap_seconds=3600,
                                    expected_actions=["auth.login"])
        assert rep["total_gaps"] == 1
        assert rep["total_entries"] >= 2
    finally:
        _default_store.clear()
