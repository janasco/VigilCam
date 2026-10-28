import inspect
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import trigger_preroll as trigger_preroll_api
from app.services import trigger_preroll as trigger_preroll_service


def _get_main_app():
    from app.main import app as _app

    return _app


def test_imports():
    import app.services.trigger_preroll  # noqa: F401
    import app.api.trigger_preroll  # noqa: F401
    from app.services.trigger_preroll import (  # noqa: F401
        save_preroll_for_trigger,
        save_preroll_on_trigger,
        save_preroll_on_inbound_trigger,
        snapshot_preroll_for_trigger,
        list_saved_snapshots,
        get_saved_snapshot,
        clear_saved_snapshots,
        handle_inbound_trigger_with_preroll,
        get_window_for_camera,
    )
    from app.api.trigger_preroll import router  # noqa: F401
    assert router is not None


def test_preroll_ring_is_continuous_with_no_save_on_external_trigger():
    """The ring itself stays continuous: no inbound/save hooks inside preroll.py."""
    from app.services import preroll as preroll_mod

    source = inspect.getsource(preroll_mod)
    lowered = source.lower()
    # No save-on-external-trigger wiring in the ring module.
    assert "inbound_trigger" not in lowered
    assert "handle_inbound" not in lowered
    assert "save_preroll" not in lowered
    assert "trigger_preroll" not in lowered
    assert "external-trigger" not in lowered and "external_trigger" not in lowered
    # No outbound delivery in the ring either.
    assert "import httpx" not in source
    assert "webhook_queue" not in source
    # Ring is bounded in-memory and continuous: writes append, reads preserve.
    buf = preroll_mod.PreRollRingBuffer(pre_roll_seconds=60, capacity=10)
    buf.add_segment(camera_name="contcam", path="a.mp4", start_time=100)
    buf.add_segment(camera_name="contcam", path="b.mp4", start_time=101)
    before = [s["path"] for s in buf.get_segments("contcam")]
    assert before == ["a.mp4", "b.mp4"]
    # Saving via trigger_preroll must not mutate the ring.
    snap = trigger_preroll_service.save_preroll_for_trigger("contcam", {"kind": "motion"}, buffer=buf)
    assert snap["segment_count"] >= 1
    after = [s["path"] for s in buf.get_segments("contcam")]
    assert after == before


def test_service_save_uses_per_camera_window():
    from app.services.preroll import PreRollRingBuffer
    from app.services import preroll_config as cfg

    suffix = uuid.uuid4().hex[:6]
    cam = f"trigcam_{suffix}"
    buf = PreRollRingBuffer(pre_roll_seconds=100, capacity=50)
    buf.add_segment(camera_name=cam, path="old.mp4", start_time=0)
    buf.add_segment(camera_name=cam, path="new.mp4", start_time=90)
    try:
        cfg.set_seconds_for_camera(cam, 5)
        snap = trigger_preroll_service.save_preroll_for_trigger(cam, {"kind": "motion"}, buffer=buf)
        paths = [s.get("path") for s in snap["segments"]]
        assert "new.mp4" in paths
        assert "old.mp4" not in paths
        assert snap["camera_name"] == cam
        assert snap["window_seconds"] == 5.0
    finally:
        try:
            cfg.delete_camera_config(cam)
        except Exception:  # noqa: BLE001
            pass
        trigger_preroll_service.clear_saved_snapshots(cam)


def test_service_save_empty_camera_still_snapshots():
    snap = trigger_preroll_service.save_preroll_for_trigger(f"nocam_{uuid.uuid4().hex[:6]}", {"kind": "doorbell"})
    assert snap["segment_count"] == 0
    assert snap["segments"] == []
    assert snap["kind"]
    trigger_preroll_service.clear_saved_snapshots(snap["camera_name"])


def test_service_list_get_clear():
    cam = f"listcam_{uuid.uuid4().hex[:6]}"
    trigger_preroll_service.clear_saved_snapshots(cam)
    s1 = trigger_preroll_service.save_preroll_for_trigger(cam, {"kind": "motion", "label": "person"})
    assert s1["id"]
    listed = trigger_preroll_service.list_saved_snapshots(camera=cam)
    assert any(s["id"] == s1["id"] for s in listed)
    fetched = trigger_preroll_service.get_saved_snapshot(s1["id"])
    assert fetched is not None and fetched["id"] == s1["id"]
    assert trigger_preroll_service.clear_saved_snapshots(cam) >= 1
    assert trigger_preroll_service.list_saved_snapshots(camera=cam) == []


def test_trigger_preroll_service_has_no_outbound_delivery():
    source = inspect.getsource(trigger_preroll_service)
    assert "import httpx" not in source
    assert "urllib.request" not in source
    assert "ActionExecutor" not in source
    assert "webhook_queue." not in source


def test_router_paths_present():
    paths = [getattr(r, "path", None) for r in trigger_preroll_api.router.routes]
    assert "/api/trigger-preroll/status" in paths
    assert "/api/trigger-preroll/save" in paths
    assert "/api/trigger-preroll/saved" in paths
    assert any("trigger-preroll" in (p or "") for p in paths)
    # Auto-loader must have picked the router up without a main.py edit.
    _app = _get_main_app()
    included = [r for r in _app.routes if type(r).__name__ == "_IncludedRouter"]
    assert any(getattr(r, "original_router", None) is trigger_preroll_api.router for r in included)


def test_main_app_auto_registers_without_manual_edit():
    # No manual wiring: app/main.py must not mention trigger_preroll explicitly
    # (auto-loader via pkgutil handles it).
    from pathlib import Path

    main_src = Path("app/main.py").read_text(encoding="utf-8")
    assert "trigger_preroll" not in main_src
    _app = _get_main_app()
    paths = list(_app.openapi()["paths"].keys())
    assert "/api/trigger-preroll/status" in paths
    assert any("trigger-preroll" in p for p in paths)


def test_index_template_untouched():
    from pathlib import Path

    # Constraint: do not edit the landing template for this feature.
    idx = Path("app/templates/index.html")
    if idx.exists():
        assert "trigger-preroll" not in idx.read_text(encoding="utf-8").lower()
        assert "trigger_preroll" not in idx.read_text(encoding="utf-8").lower()


def test_router_crud_isolated():
    isolated = FastAPI()
    isolated.include_router(trigger_preroll_api.router)
    client = TestClient(isolated)
    r = client.get("/api/trigger-preroll/status")
    assert r.status_code == 200, r.text
    assert r.json().get("enabled") is True

    cam = f"routecam_{uuid.uuid4().hex[:6]}"
    try:
        save = client.post("/api/trigger-preroll/save", json={"camera_name": cam, "kind": "motion", "label": "person"})
        assert save.status_code == 200, save.text
        body = save.json()
        assert body.get("success") is True
        assert body.get("camera_name") == cam

        listed = client.get("/api/trigger-preroll/saved")
        assert listed.status_code == 200, listed.text
        assert listed.json().get("count", 0) >= 1

        cam_save = client.post(f"/api/cameras/{cam}/trigger-preroll", json={"kind": "doorbell"})
        assert cam_save.status_code == 200, cam_save.text
    finally:
        trigger_preroll_service.clear_saved_snapshots(cam)


def _make_camera(client):
    r = client.post("/api/cameras", json={"name": "trigpre_" + uuid.uuid4().hex[:8], "kind": "usb", "source": "0"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_inbound_trigger_with_preroll_end_to_end():
    from app.services import preroll as preroll_mod

    _app = _get_main_app()
    client = TestClient(_app)
    cam_id = _make_camera(client)
    # Seed the shared ring under both the numeric id and its string form so the
    # snapshot can find segments regardless of camera key normalisation.
    preroll_mod.preroll_buffer.add_segment(camera_name=str(cam_id), path=f"pre_{cam_id}.mp4", start_time=1000)
    preroll_mod.preroll_buffer.add_segment(camera_name=cam_id, path=f"pre2_{cam_id}.mp4", start_time=1001)
    try:
        r = client.post("/api/trigger-preroll/trigger", json={"camera_id": cam_id, "kind": "motion", "label": "person"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body.get("success") is True
        assert body.get("snapshot") is not None
        assert body.get("segment_count", 0) >= 1
        # Ring stays continuous: segments still present after save.
        segs = preroll_mod.preroll_buffer.get_segments(str(cam_id))
        assert len(segs) >= 1
    finally:
        trigger_preroll_service.clear_saved_snapshots(str(cam_id))
        trigger_preroll_service.clear_saved_snapshots(cam_id)
        try:
            preroll_mod.preroll_buffer.clear(str(cam_id))
        except Exception:  # noqa: BLE001
            pass


def test_camera_scoped_trigger_preroll_save():
    from app.services import preroll as preroll_mod

    _app = _get_main_app()
    client = TestClient(_app)
    cam_id = _make_camera(client)
    preroll_mod.preroll_buffer.add_segment(camera_name=str(cam_id), path=f"scoped_{cam_id}.mp4", start_time=2000)
    try:
        r = client.post(f"/api/cameras/{cam_id}/trigger-preroll", json={"kind": "motion"})
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True
    finally:
        trigger_preroll_service.clear_saved_snapshots(str(cam_id))
        try:
            preroll_mod.preroll_buffer.clear(str(cam_id))
        except Exception:  # noqa: BLE001
            pass
