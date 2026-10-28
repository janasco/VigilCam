"""Alert auto-snapshot tests: service + router.

Verification: ``app/services/cameras.record_event`` never sets
``snapshot_path`` and ``app/api/events.py`` only attaches snapshots on manual
``POST /api/events/{event_id}/snapshot`` (``api_event_upload_snapshot`` calling
``app.services.snapshots.save_snapshot``). There is no automatic snapshot on
alert/event creation in the pre-existing code. This suite covers the new
``app/services/alert_snapshot.py`` + ``app/api/alert_snapshot.py`` opt-in
auto-snapshot layer.
"""

import inspect
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.alert_snapshot import router as alert_snapshot_router  # noqa: F401
from app.services import alert_snapshot as snap_mod  # noqa: F401
from app.services.alert_snapshot import (  # noqa: F401
    backfill,
    disable,
    enable,
    get_status,
    is_enabled,
    set_config,
    snapshot_event,
    toggle,
    alert_snapshot_service,
)


def _snap_dir(monkeypatch, tmp_path):
    import app.services.snapshot_now as sn
    import app.core.config as cfg

    monkeypatch.setattr(sn, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(cfg, "SNAPSHOT_DIR", tmp_path)
    return tmp_path


def test_alert_snapshot_imports():
    import app.api.alert_snapshot  # noqa: F401
    import app.services.alert_snapshot  # noqa: F401
    from app.api.alert_snapshot import router  # noqa: F401
    from app.services.alert_snapshot import (  # noqa: F401
        snapshot_event, backfill, is_enabled, enable, disable,
        capture_alert_snapshot, ensure_alert_snapshot, auto_snapshot,
    )
    assert router is not None


def test_snapshots_only_attach_on_manual_post():
    """Pre-existing behaviour: snapshots attach only via manual POST."""
    import app.services.cameras as cameras_mod
    import app.api.events as events_api_mod

    cameras_src = inspect.getsource(cameras_mod.record_event)
    assert "snapshot_path" not in cameras_src
    assert "snapshot" not in cameras_src.lower()

    events_src = inspect.getsource(events_api_mod)
    # Manual attach endpoint exists.
    assert "snapshot" in events_src
    assert "{event_id}/snapshot" in events_src or "{event_id}/snapshot" in events_src.replace(" ", "")
    manual_src = inspect.getsource(events_api_mod.api_event_upload_snapshot)
    assert "save_snapshot" in manual_src
    assert "snapshot_path" in manual_src

    # Default toggle is off -> snapshot_event is a no-op (no automatic snapshot).
    snap_mod.clear()
    assert is_enabled() is False
    out = snapshot_event({"id": 999, "camera_id": 1, "label": "person"})
    assert out.get("skipped") is True
    assert out.get("attached") == 0
    assert out.get("snapshot_path") is None


def test_alert_snapshot_toggle():
    snap_mod.clear()
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
    snap_mod.clear()
    assert is_enabled() is False


def test_alert_snapshot_service_explicit_tmp(tmp_path, monkeypatch):
    _snap_dir(monkeypatch, tmp_path)
    snap_mod.clear()
    # Explicit force works even when toggle is off.
    assert is_enabled() is False
    out = snapshot_event({"id": 11, "camera_id": 1, "label": "person"}, force=True)
    assert out["success"] is True
    assert out["attached"] == 1
    assert out["snapshot_path"]
    assert Path(str(out["snapshot_path"])).is_file()
    # Disabled without force is skipped.
    out2 = snapshot_event({"id": 12, "camera_id": 1}, force=False)
    assert out2["attached"] == 0
    assert out2.get("skipped") is True


def test_alert_snapshot_service_auto_when_enabled_tmp(tmp_path, monkeypatch):
    _snap_dir(monkeypatch, tmp_path)
    snap_mod.clear()
    enable()
    out = snapshot_event({"id": 21, "camera_id": 2, "label": "car"})
    assert out["enabled"] is True
    assert out["attached"] == 1
    assert Path(str(out["snapshot_path"])).is_file()
    disable()
    out2 = snapshot_event({"id": 22, "camera_id": 2})
    assert out2["attached"] == 0


def test_alert_snapshot_aliases_tmp(tmp_path, monkeypatch):
    _snap_dir(monkeypatch, tmp_path)
    snap_mod.clear()
    assert callable(snap_mod.capture_alert_snapshot)
    assert callable(snap_mod.ensure_alert_snapshot)
    assert callable(snap_mod.auto_snapshot)
    out = snap_mod.capture_alert_snapshot({"id": 31}, force=True)
    assert out["attached"] == 1
    assert Path(str(out["snapshot_path"])).is_file()


def test_alert_snapshot_backfill_tmp(tmp_path, monkeypatch):
    _snap_dir(monkeypatch, tmp_path)
    snap_mod.clear()
    # Backfill is explicit: works even when toggle is off.
    assert is_enabled() is False
    events = [{"id": 41, "camera_id": 1}, {"id": 42, "camera_id": 1}]
    out = backfill(events=events)
    assert out["success"] is True
    assert out["events_processed"] == 2
    assert out["attached"] == 2
    for item in out["per_event"]:
        assert Path(str(item["snapshot_path"])).is_file()
    # Dry run attaches nothing.
    out2 = backfill(events=events, dry_run=True)
    assert out2["attached"] == 0


def test_alert_snapshot_hook_installed():
    import app.services.cameras as cameras_mod

    assert snap_mod.ensure_hook_installed() is True
    assert getattr(cameras_mod.record_event, "__alert_snapshot_wrapped__", False) is True


def test_alert_snapshot_api_toggle_and_status():
    snap_mod.clear()
    a = FastAPI()
    a.include_router(alert_snapshot_router)
    client = TestClient(a)

    s = client.get("/api/alert-snapshot")
    assert s.status_code == 200, s.text
    assert s.json()["enabled"] is False

    e = client.post("/api/alert-snapshot/enable", json={})
    assert e.status_code == 200
    assert e.json()["enabled"] is True

    d = client.post("/api/alert-snapshot/disable", json={})
    assert d.status_code == 200
    assert d.json()["enabled"] is False

    t = client.post("/api/alert-snapshot/toggle", json={})
    assert t.status_code == 200
    assert t.json()["enabled"] is True
    snap_mod.clear()


def test_alert_snapshot_api_capture_tmp(tmp_path, monkeypatch):
    _snap_dir(monkeypatch, tmp_path)
    snap_mod.clear()
    a = FastAPI()
    a.include_router(alert_snapshot_router)
    client = TestClient(a)
    # Explicit capture bypasses disabled toggle.
    assert is_enabled() is False
    r = client.post("/api/alert-snapshot/capture", json={"event_id": 51, "camera_id": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["attached"] == 1
    assert Path(str(body["snapshot_path"])).is_file()

    # Underscore alias.
    r2 = client.post("/api/alert_snapshot/attach", json={"event_id": 52})
    assert r2.status_code == 200, r2.text
    assert r2.json()["attached"] == 1


def test_alert_snapshot_paths_present():
    router_paths = sorted(set(r.path for r in alert_snapshot_router.routes))
    assert "/api/alert-snapshot" in router_paths
    assert "/api/alert_snapshot" in router_paths
    assert any("capture" in p for p in router_paths)
    assert any("backfill" in p for p in router_paths)
    assert any("enable" in p for p in router_paths)
    # pkgutil auto-loader registers without main.py edits (no app.main import
    # needed for the fast path): check main.py has the auto-loader and this
    # router exposes ``router``.
    import pathlib

    main_py = pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py"
    text = main_py.read_text()
    assert "pkgutil.iter_modules" in text
    assert "importlib.import_module" in text
    # Isolated app still serves the endpoint (proves router is mountable).
    a = FastAPI()
    a.include_router(alert_snapshot_router)
    client = TestClient(a)
    assert client.get("/api/alert-snapshot").status_code == 200
    assert client.get("/api/alert_snapshot").status_code == 200
    # Best-effort full-app registration check (slow: imports 400+ routers).
    # Skipped by default to keep the suite fast; run with
    # VIGILCAM_CHECK_FULL_APP=1 to enforce.
    import os

    if os.environ.get("VIGILCAM_CHECK_FULL_APP") == "1":
        from app.main import app as full_app  # noqa: PLC0415

        assert any(
            getattr(r, "original_router", None) is alert_snapshot_router
            for r in full_app.routes
        ), "alert_snapshot router not registered on app"
        full_client = TestClient(full_app)
        assert full_client.get("/api/alert-snapshot").status_code == 200
