"""Tests for the opt-in event auto-merge scheduler.

Verifies:
- event merge itself runs only on demand (no auto-start on import);
- scheduler service imports and is disabled by default;
- config roundtrip, pure scheduled merge, dry-run preview;
- scheduler start/stop is non-blocking;
- router is registered with canonical + underscore paths.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
import app.services.merge_schedule as svc
from app.services.merge_schedule import MergeScheduleScheduler


@pytest.fixture(autouse=True)
def _clean_state():
    prev = svc.get_config()
    svc.update_config(
        enabled=False,
        interval_seconds=300,
        gap_seconds=60.0,
        camera_id=None,
        limit=500,
    )
    try:
        svc.reset_state()
    except Exception:  # noqa: BLE001
        pass
    try:
        svc.stop_merge_schedule_scheduler()
    except Exception:  # noqa: BLE001
        pass
    yield
    svc.update_config(
        enabled=bool(prev.get("enabled")),
        interval_seconds=prev.get("interval_seconds"),
        gap_seconds=prev.get("gap_seconds"),
        camera_id=prev.get("camera_id"),
        limit=prev.get("limit"),
    )
    try:
        svc.reset_state()
    except Exception:  # noqa: BLE001
        pass
    try:
        svc.stop_merge_schedule_scheduler()
    except Exception:  # noqa: BLE001
        pass


def _ev(eid, camera=1, minutes=0, tags=None, label="motion"):
    return {
        "id": eid,
        "event_id": eid,
        "camera_id": camera,
        "kind": "motion",
        "label": label,
        "confidence": 0.5 + eid * 0.01,
        "payload": None,
        "tags": tags or [],
        "created_at": (datetime(2026, 1, 1, 12, 0, 0) + timedelta(minutes=minutes)).isoformat(),
    }


def test_merge_schedule_imports():
    import app.api.merge_schedule  # noqa: F401
    import app.services.merge_schedule  # noqa: F401
    from app.api.merge_schedule import router  # noqa: F401
    from app.services.merge_schedule import (  # noqa: F401
        get_config,
        get_status,
        run_scheduled_merge,
        update_config,
    )


def test_event_merge_is_manual_only_by_default():
    # Scheduler must be opt-in: disabled and not running after import.
    cfg = svc.get_config()
    assert cfg["enabled"] is False
    assert svc.get_status()["enabled"] is False
    assert svc.get_status()["running"] is False
    # Base event-merge service exposes no background scheduler hooks.
    import app.services.event_merge as em

    assert not hasattr(em, "start_scheduler")
    assert not hasattr(em, "start_merge_scheduler")
    for name in ("start_scheduler", "scheduler", "start_merge_schedule_scheduler"):
        assert not hasattr(em, name)


def test_config_update_roundtrip():
    out = svc.update_config(enabled=True, interval_seconds=120, gap_seconds=90)
    assert out["enabled"] is True
    assert out["interval_seconds"] == 120
    assert out["gap_seconds"] == 90
    # Interval floors at the minimum.
    out = svc.update_config(interval_seconds=1)
    assert out["interval_seconds"] == svc.MIN_INTERVAL_SECONDS
    svc.update_config(enabled=False)


def test_scheduled_merge_pure_events():
    import asyncio

    events = [_ev(1, minutes=0), _ev(2, minutes=1), _ev(3, minutes=30)]
    result = asyncio.run(svc.run_scheduled_merge(events=events, gap_seconds=120))
    assert result["success"] is True
    assert result["groups"] == 1
    assert result["merged_count"] == 1
    assert result["merged"][0]["merge_count"] == 2

    # Dry-run suggests but does not merge.
    preview = asyncio.run(
        svc.run_scheduled_merge(events=events, gap_seconds=120, dry_run=True)
    )
    assert preview["dry_run"] is True
    assert preview["merged_count"] == 0
    assert len(preview["suggestions"]) == 1


def test_scheduler_start_stop_nonblocking():
    sched = MergeScheduleScheduler(interval_seconds=10_000)
    sched.start()
    assert sched.is_running is True
    sched.stop()
    assert sched.is_running is False


def test_router_registered_and_endpoints(monkeypatch):
    import app.api.merge_schedule as api_mod  # noqa: F401

    assert hasattr(api_mod, "router")
    assert hasattr(api_mod, "underscore_router")
    paths = list(app.openapi()["paths"].keys())
    assert "/api/merge-schedule/status" in paths
    assert "/api/merge_schedule/status" in paths
    assert "/api/merge-schedule/config" in paths
    assert "/api/merge-schedule/run" in paths

    async def fake_run(**kwargs):
        return {
            "success": True,
            "merged": [{"id": 1, "merge_count": 2}],
            "merged_count": 1,
            "groups": 1,
            "suggestions": [],
            "counts": {"total": 2, "groups": 1, "merged": 1},
        }

    monkeypatch.setattr(svc, "run_scheduled_merge", fake_run)
    client = TestClient(app)

    r = client.get("/api/merge-schedule/status")
    assert r.status_code == 200
    assert "enabled" in r.json()

    r = client.get("/api/merge_schedule/status")
    assert r.status_code == 200

    r = client.get("/api/merge-schedule/config")
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    r = client.post("/api/merge-schedule/config", json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is True

    r = client.post("/api/merge-schedule/run", json={})
    assert r.status_code == 200
    assert r.json()["merged_count"] == 1

    r = client.post("/api/merge_schedule/scan", json={})
    assert r.status_code == 200
