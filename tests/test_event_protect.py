"""Tests for flagged-event auto-protect (segments locked via rec_protect).

Ground truth verified here: ``app.services.rec_protect`` locks are manual
only -- ``protect_segment`` / ``unprotect_segment`` / ``clear`` with no
event import, and event creation never protects. ``app.services.event_protect``
layers event-driven auto-protection on top for flagged events.
"""

import inspect

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.event_protect import router as event_protect_router  # noqa: F401
from app.services import event_protect as protect_mod  # noqa: F401
from app.services import event_segments as event_segments_mod  # noqa: F401
from app.services import rec_protect as rec_protect_mod  # noqa: F401
from app.services.event_protect import (  # noqa: F401
    auto_protect_event,
    backfill_flagged,
    disable,
    enable,
    get_status,
    is_enabled,
    is_flagged,
    set_config,
    toggle,
    event_protect_service,
)


def _reset():
    rec_protect_mod.clear()
    event_segments_mod.clear()
    protect_mod.clear()
    try:
        from app.services import event_ack as _ack

        _ack.clear()
    except Exception:  # noqa: BLE001
        pass


def _make_segment_file(tmp_path, camera="cam1", name="20240101_120000_main.mp4"):
    cam_dir = tmp_path / camera
    cam_dir.mkdir(parents=True, exist_ok=True)
    p = cam_dir / name
    p.write_bytes(b"fake-segment-bytes")
    return p


def test_event_protect_imports():
    import app.services.event_protect  # noqa: F401
    import app.api.event_protect  # noqa: F401
    from app.services.event_protect import (  # noqa: F401
        is_flagged,
        auto_protect_event,
        backfill_flagged,
        is_enabled,
        enable,
        disable,
    )
    from app.api.event_protect import router  # noqa: F401


def test_rec_protect_manual_only_no_event_automation():
    """rec_protect must stay manual: no event/flag/auto protection inside."""
    import app.services.cameras as cameras_mod
    import app.api.events as events_api_mod

    src = inspect.getsource(rec_protect_mod)
    lowered = src.lower()
    for token in ("event", "flag", "auto_protect", "autoprotect", "event_protect"):
        assert token not in lowered, token

    cameras_src = inspect.getsource(cameras_mod.record_event)
    assert "rec_protect" not in cameras_src
    assert "event_protect" not in cameras_src
    assert "protect_segment" not in cameras_src

    events_src = inspect.getsource(events_api_mod)
    assert "rec_protect" not in events_src
    assert "protect_segment" not in events_src
    assert "event_protect" not in events_src

    # Default toggle is off -> hook path is a no-op, proving no automatic protection.
    _reset()
    assert is_enabled() is False
    out = protect_mod.maybe_auto_protect_event(
        {"id": 999, "camera_name": "cam1", "created_at": "2024-01-01T12:01:00Z", "flagged": True}
    )
    assert out.get("protected", 0) == 0
    assert out.get("skipped") is True


def test_is_flagged_detection():
    _reset()
    assert is_flagged({"id": 1, "flagged": True}) is True
    assert is_flagged({"id": 1, "flag": True}) is True
    assert is_flagged({"id": 1, "important": True}) is True
    assert is_flagged({"id": 1, "starred": True}) is True
    assert is_flagged({"id": 1, "tags": ["flagged"]}) is True
    assert is_flagged({"id": 1, "tags": ["important", "person"]}) is True
    assert is_flagged({"id": 1, "tags": "flagged, person"}) is True
    assert is_flagged({"id": 1, "tags": ["person"]}) is False
    assert is_flagged({"id": 1, "label": "person"}) is False
    assert is_flagged({"id": 1, "flagged": False}) is False
    # acknowledged / escalated operator state counts as flagged
    from app.services import event_ack as ack_mod

    ack_mod.acknowledge(501, actor="op")
    assert is_flagged({"id": 501}) is True
    assert is_flagged(501) is True
    ack_mod.clear()
    assert is_flagged({"id": 501}) is False
    _reset()


def test_auto_protect_flagged_tmp(tmp_path):
    _reset()
    seg = _make_segment_file(tmp_path, camera="cam1", name="20240101_120000_main.mp4")
    protect_mod.set_config(recording_dir=str(tmp_path))
    event = {"id": 11, "event_id": 11, "camera_name": "cam1",
             "created_at": "2024-01-01T12:01:00Z", "flagged": True}
    out = auto_protect_event(event)
    assert out["flagged"] is True
    assert out["discovered"] >= 1
    assert out["protected"] >= 1
    assert rec_protect_mod.is_protected(str(seg)) is True
    # Retention selection must skip the auto-protected segment.
    from app.services.retention import RetentionManager
    import os
    import time

    old_mtime = time.time() - (10 * 86400)
    os.utime(seg, (old_mtime, old_mtime))
    mgr = RetentionManager(storage_root=tmp_path, max_age_days=1)
    selected = mgr.select_for_deletion()
    assert str(seg) in [str(p) for p in selected]
    kept = rec_protect_mod.exclude_protected(selected)
    assert str(seg) not in [str(p) for p in kept]
    _reset()


def test_unflagged_skipped_unless_forced(tmp_path):
    _reset()
    _make_segment_file(tmp_path, camera="cam1", name="20240101_120000_main.mp4")
    protect_mod.set_config(recording_dir=str(tmp_path))
    event = {"id": 12, "camera_name": "cam1", "created_at": "2024-01-01T12:01:00Z"}
    out = auto_protect_event(event)
    assert out["flagged"] is False
    assert out.get("skipped") is True
    assert out["protected"] == 0
    out2 = auto_protect_event(event, force=True)
    assert out2["protected"] >= 1
    _reset()


def test_backfill_flagged_tmp(tmp_path):
    _reset()
    _make_segment_file(tmp_path, camera="cam1", name="20240101_120000_main.mp4")
    events = [
        {"id": 21, "camera_name": "cam1", "created_at": "2024-01-01T12:01:00Z", "flagged": True},
        {"id": 22, "camera_name": "cam1", "created_at": "2024-01-01T12:01:30Z"},
    ]
    out = backfill_flagged(events=events, recording_dir=str(tmp_path))
    assert out["success"] is True
    assert out["events_processed"] == 2
    assert out["protected"] >= 1
    assert rec_protect_mod.is_protected(str(tmp_path / "cam1" / "20240101_120000_main.mp4")) is True
    # dry run protects nothing
    _reset()
    event_segments_mod.clear()
    out2 = backfill_flagged(events=events, recording_dir=str(tmp_path), dry_run=True)
    assert out2["dry_run"] is True
    assert out2["protected"] == 0
    _reset()


def test_toggle_and_hook():
    _reset()
    assert is_enabled() is False
    s = enable()
    assert s["enabled"] is True
    assert is_enabled() is True
    s = disable()
    assert s["enabled"] is False
    s = toggle()
    assert s["enabled"] is True
    s = toggle()
    assert s["enabled"] is False
    assert protect_mod.ensure_hook_installed() is True
    import app.services.cameras as cameras_mod

    assert getattr(cameras_mod.record_event, "__event_protect_wrapped__", False) is True
    _reset()


def test_service_aliases(tmp_path):
    _reset()
    seg = _make_segment_file(tmp_path, camera="cam1")
    svc = event_protect_service
    assert svc.is_flagged({"flagged": True}) is True
    protect_mod.set_config(recording_dir=str(tmp_path))
    out = svc.protect({"id": 31, "camera_name": "cam1",
                       "created_at": "2024-01-01T12:01:00Z", "flagged": True})
    assert out["protected"] >= 1
    assert svc.status()["ok"] is True
    _reset()


def test_api_toggle_and_protect_tmp(tmp_path):
    _reset()
    seg = _make_segment_file(tmp_path, camera="cam1", name="20240101_120000_main.mp4")
    a = FastAPI()
    a.include_router(event_protect_router)
    client = TestClient(a)
    s = client.get("/api/event-protect")
    assert s.status_code == 200, s.text
    assert s.json()["enabled"] is False
    e = client.post("/api/event-protect/enable", json={})
    assert e.status_code == 200
    assert e.json()["enabled"] is True
    c = client.post("/api/event-protect/check", json={"id": 41, "flagged": True})
    assert c.status_code == 200
    assert c.json()["flagged"] is True
    r = client.post("/api/event-protect/protect", json={
        "event_id": 41, "camera_name": "cam1",
        "created_at": "2024-01-01T12:01:00Z", "flagged": True,
        "recording_dir": str(tmp_path),
    })
    assert r.status_code == 200, r.text
    assert r.json()["protected"] >= 1
    assert rec_protect_mod.is_protected(str(seg)) is True
    _reset()


def test_api_paths_present():
    router_paths = sorted(set(r.path for r in event_protect_router.routes))
    assert "/api/event-protect" in router_paths
    assert "/api/event_protect" in router_paths
    assert any("protect" in p for p in router_paths)
    assert any("backfill" in p for p in router_paths)
    assert any(
        getattr(r, "original_router", None) is event_protect_router for r in app.routes
    ), "event_protect router not registered on app"
    client = TestClient(app)
    assert client.get("/api/event-protect").status_code == 200
    assert client.get("/api/event_protect").status_code == 200
    _reset()
