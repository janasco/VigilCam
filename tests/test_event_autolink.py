import inspect
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.event_autolink import router as event_autolink_router  # noqa: F401
from app.services import event_autolink as autolink_mod  # noqa: F401
from app.services import event_segments as event_segments_mod  # noqa: F401
from app.services.event_autolink import (  # noqa: F401
    autolink_event,
    backfill,
    disable,
    enable,
    get_status,
    is_enabled,
    set_config,
    toggle,
    event_autolink_service,
)


def _make_segment_file(tmp_path, camera="cam1", name="20240101_120000_main.mp4"):
    cam_dir = tmp_path / camera
    cam_dir.mkdir(parents=True, exist_ok=True)
    p = cam_dir / name
    p.write_bytes(b"fake-segment-bytes")
    return p


def test_event_autolink_import():
    import app.api.event_autolink  # noqa: F401
    import app.services.event_autolink  # noqa: F401
    from app.api.event_autolink import router  # noqa: F401
    from app.services.event_autolink import (  # noqa: F401
        is_enabled, enable, disable, autolink_event, backfill,
    )


def test_event_segments_only_links_on_explicit_request():
    """event_segments must not auto-link on event creation."""
    import app.services.cameras as cameras_mod
    import app.api.events as events_api_mod

    cameras_src = inspect.getsource(cameras_mod.record_event)
    assert "event_segments" not in cameras_src
    assert "event_autolink" not in cameras_src or "mark_camera_activity" in cameras_src
    # record_event only persists + marks activity
    assert "link_event_to_segment" not in cameras_src
    assert "discover_segments_for_event" not in cameras_src

    events_src = inspect.getsource(events_api_mod)
    assert "event_segments" not in events_src
    assert "link_event_to_segment" not in events_src

    seg_src = inspect.getsource(event_segments_mod.link_event_to_segment)
    assert "event_id" in seg_src

    # Default toggle is off -> autolink is a no-op, proving no automatic linking.
    autolink_mod.clear()
    event_segments_mod.clear()
    assert is_enabled() is False
    out = autolink_event({"id": 999, "camera_name": "cam1", "created_at": "2024-01-01T12:01:00Z"})
    assert out["attached"] == 0
    assert out.get("skipped") is True
    assert event_segments_mod.list_links(event_id=999) == []


def test_event_autolink_toggle():
    autolink_mod.clear()
    assert is_enabled() is False
    s = enable()
    assert s["enabled"] is True
    assert is_enabled() is True
    s = disable()
    assert s["enabled"] is False
    assert is_enabled() is False
    s = toggle()
    assert s["enabled"] is True
    s = toggle()
    assert s["enabled"] is False
    s = toggle(enabled=True)
    assert s["enabled"] is True
    autolink_mod.clear()
    assert is_enabled() is False


def test_event_autolink_service_auto_attach_tmp(tmp_path):
    autolink_mod.clear()
    event_segments_mod.clear()
    seg = _make_segment_file(tmp_path, camera="cam1", name="20240101_120000_main.mp4")
    enable()
    set_config(recording_dir=str(tmp_path))
    event = {"id": 11, "event_id": 11, "camera_name": "cam1", "created_at": "2024-01-01T12:01:00Z"}
    out = autolink_event(event)
    assert out["enabled"] is True
    assert out["discovered"] >= 1
    assert out["attached"] >= 1
    assert any(str(seg) == (l.get("segment_path") or l.get("path")) for l in out["links"])
    # links persisted in event_segments store
    assert len(event_segments_mod.list_links(event_id=11)) >= 1
    # disabling stops future auto-attach
    disable()
    out2 = autolink_event({"id": 12, "camera_name": "cam1", "created_at": "2024-01-01T12:01:00Z"})
    assert out2["attached"] == 0
    assert event_segments_mod.list_links(event_id=12) == []


def test_event_autolink_backfill_tmp(tmp_path):
    autolink_mod.clear()
    event_segments_mod.clear()
    _make_segment_file(tmp_path, camera="cam1", name="20240101_120000_main.mp4")
    # backfill is explicit: works even when toggle is off
    assert is_enabled() is False
    events = [
        {"id": 21, "camera_name": "cam1", "created_at": "2024-01-01T12:01:00Z"},
        {"id": 22, "camera_name": "cam1", "created_at": "2024-01-01T12:01:30Z"},
    ]
    out = backfill(events=events, recording_dir=str(tmp_path))
    assert out["success"] is True
    assert out["events_processed"] == 2
    assert out["attached"] >= 2
    assert len(event_segments_mod.list_links(event_id=21)) >= 1
    assert len(event_segments_mod.list_links(event_id=22)) >= 1
    # dry run attaches nothing
    event_segments_mod.clear()
    out2 = backfill(events=events, recording_dir=str(tmp_path), dry_run=True)
    assert out2["dry_run"] is True
    assert out2["attached"] == 0
    assert event_segments_mod.list_links(event_id=21) == []


def test_event_autolink_hook_installed():
    import app.services.cameras as cameras_mod

    assert autolink_mod.ensure_hook_installed() is True
    assert getattr(cameras_mod.record_event, "__event_autolink_wrapped__", False) is True


def test_event_autolink_api_toggle_and_status():
    autolink_mod.clear()
    a = FastAPI()
    a.include_router(event_autolink_router)
    client = TestClient(a)

    s = client.get("/api/event-autolink")
    assert s.status_code == 200, s.text
    assert s.json()["enabled"] is False

    e = client.post("/api/event-autolink/enable", json={})
    assert e.status_code == 200
    assert e.json()["enabled"] is True

    d = client.post("/api/event-autolink/disable", json={})
    assert d.status_code == 200
    assert d.json()["enabled"] is False

    t = client.post("/api/event-autolink/toggle", json={})
    assert t.status_code == 200
    assert t.json()["enabled"] is True
    autolink_mod.clear()


def test_event_autolink_api_backfill_tmp(tmp_path):
    autolink_mod.clear()
    event_segments_mod.clear()
    seg = _make_segment_file(tmp_path, camera="cam1", name="20240101_120000_main.mp4")
    a = FastAPI()
    a.include_router(event_autolink_router)
    client = TestClient(a)
    r = client.post(
        "/api/event-autolink/backfill",
        json={
            "events": [
                {"id": 31, "camera_name": "cam1", "created_at": "2024-01-01T12:01:00Z"},
            ],
            "recording_dir": str(tmp_path),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["attached"] >= 1
    assert len(event_segments_mod.list_links(event_id=31)) >= 1


def test_event_autolink_api_attach_explicit_tmp(tmp_path):
    autolink_mod.clear()
    event_segments_mod.clear()
    seg = _make_segment_file(tmp_path, camera="cam1", name="20240101_120000_main.mp4")
    a = FastAPI()
    a.include_router(event_autolink_router)
    client = TestClient(a)
    # explicit attach bypasses disabled toggle
    assert is_enabled() is False
    r = client.post(
        "/api/event-autolink/attach",
        json={
            "event_id": 41,
            "camera_name": "cam1",
            "created_at": "2024-01-01T12:01:00Z",
            "recording_dir": str(tmp_path),
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["attached"] >= 1


def test_event_autolink_paths_present():
    router_paths = sorted(set(r.path for r in event_autolink_router.routes))
    assert "/api/event-autolink" in router_paths
    assert any("backfill" in p for p in router_paths)
    assert any("enable" in p for p in router_paths)
    # pkgutil auto-loader registers without main.py edits (FastAPI 0.143+
    # keeps included routers as lazy _IncludedRouter entries).
    assert any(
        getattr(r, "original_router", None) is event_autolink_router for r in app.routes
    ), "event_autolink router not registered on app"
    client = TestClient(app)
    assert client.get("/api/event-autolink").status_code == 200
    assert client.get("/api/event_autolink").status_code == 200
