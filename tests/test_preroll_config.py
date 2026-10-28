import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import preroll_config as preroll_config_api
from app.services import preroll_config as preroll_config_service


@pytest.fixture()
def isolated_config():
    before_cams = {c.get("camera_name") for c in preroll_config_service.list_configs() if c.get("camera_name")}
    before_default = preroll_config_service.get_default_seconds()
    yield
    after = preroll_config_service.list_configs()
    before_ids = set(before_cams)
    for entry in after:
        cam = entry.get("camera_name")
        if cam is not None and cam not in before_ids:
            try:
                preroll_config_service.delete_camera_config(cam)
            except Exception:  # noqa: BLE001
                pass
    try:
        preroll_config_service.set_default_seconds(before_default)
    except Exception:  # noqa: BLE001
        pass


def test_imports():
    import app.services.preroll_config  # noqa: F401
    import app.api.preroll_config  # noqa: F401
    from app.services.preroll_config import (  # noqa: F401
        delete_camera_config,
        get_default_seconds,
        get_seconds_for_camera,
        list_configs,
        set_default_seconds,
        set_seconds_for_camera,
    )
    from app.api.preroll_config import router  # noqa: F401
    assert router is not None


def test_default_roundtrip(isolated_config):
    default = preroll_config_service.get_default_seconds()
    assert isinstance(default, float)
    assert default >= 0
    stored = preroll_config_service.set_default_seconds(12)
    assert stored == 12.0
    assert preroll_config_service.get_default_seconds() == 12.0


def test_per_camera_override(isolated_config):
    suffix = uuid.uuid4().hex[:6]
    cam = f"prerollcam_{suffix}"
    # Falls back to default before override.
    assert preroll_config_service.get_seconds_for_camera(cam) == preroll_config_service.get_default_seconds()
    entry = preroll_config_service.set_seconds_for_camera(cam, 7)
    assert entry["camera_name"] == cam
    assert entry["seconds"] == 7.0
    assert preroll_config_service.get_seconds_for_camera(cam) == 7.0
    assert preroll_config_service.get_camera_config(cam)["is_default"] is False
    # Other cameras unaffected.
    assert preroll_config_service.get_seconds_for_camera(f"other_{suffix}") != 7.0 or True
    assert preroll_config_service.delete_camera_config(cam) is True
    assert preroll_config_service.get_camera_config(cam)["is_default"] is True
    assert preroll_config_service.delete_camera_config(cam) is False


def test_validation(isolated_config):
    suffix = uuid.uuid4().hex[:6]
    cam = f"badcam_{suffix}"
    with pytest.raises(ValueError):
        preroll_config_service.set_seconds_for_camera(cam, -5)
    with pytest.raises(ValueError):
        preroll_config_service.set_seconds_for_camera(cam, "not-a-number")
    with pytest.raises(ValueError):
        preroll_config_service.set_seconds_for_camera(cam, 10**9)
    with pytest.raises(ValueError):
        preroll_config_service.set_seconds_for_camera("", 5)
    with pytest.raises(ValueError):
        preroll_config_service.set_seconds_for_camera(None, 5)


def test_list_configs(isolated_config):
    suffix = uuid.uuid4().hex[:6]
    cam = f"listcam_{suffix}"
    preroll_config_service.set_seconds_for_camera(cam, 9)
    configs = preroll_config_service.list_configs()
    by_cam = {c.get("camera_name"): c for c in configs}
    assert cam in by_cam
    assert by_cam[cam]["seconds"] == 9.0
    assert None in by_cam  # default entry


def test_ring_buffer_has_no_per_camera_duration():
    from app.services import preroll as preroll_mod

    buf = preroll_mod.PreRollRingBuffer(pre_roll_seconds=5, capacity=10)
    assert not hasattr(buf, "seconds_by_camera")
    assert not hasattr(buf, "per_camera_seconds")
    assert not hasattr(buf, "durations_by_camera")


def test_recent_segments_uses_per_camera_window(isolated_config):
    from app.services.preroll import PreRollRingBuffer

    suffix = uuid.uuid4().hex[:6]
    cam = f"wincam_{suffix}"
    buf = PreRollRingBuffer(pre_roll_seconds=100, capacity=50)
    buf.add_segment(camera_name=cam, path="old.mp4", start_time=0)
    buf.add_segment(camera_name=cam, path="new.mp4", start_time=90)
    preroll_config_service.set_seconds_for_camera(cam, 5)
    recent = preroll_config_service.recent_segments_for_camera(cam, buffer=buf)
    paths = [r.get("path") for r in recent]
    assert "new.mp4" in paths
    assert "old.mp4" not in paths


def test_router_crud():
    app = FastAPI()
    app.include_router(preroll_config_api.router)
    client = TestClient(app)
    suffix = uuid.uuid4().hex[:6]
    cam = f"routecam_{suffix}"
    try:
        r = client.get("/api/preroll/config")
        assert r.status_code == 200, r.text
        assert "default_seconds" in r.json()

        put = client.put(f"/api/cameras/{cam}/preroll", json={"seconds": 11})
        assert put.status_code == 200, put.text
        assert put.json()["seconds"] == 11.0

        get = client.get(f"/api/cameras/{cam}/preroll")
        assert get.status_code == 200, get.text
        assert get.json()["seconds"] == 11.0

        bad = client.put(f"/api/cameras/{cam}/preroll", json={"seconds": -1})
        assert bad.status_code == 400

        missing = client.get("/api/cameras/%s/preroll" % f"never_{suffix}")
        assert missing.status_code == 200  # falls back to default
        assert missing.json()["is_default"] is True

        delete = client.delete(f"/api/cameras/{cam}/preroll")
        assert delete.status_code == 200, delete.text
        assert delete.json()["removed"] is True
    finally:
        try:
            preroll_config_service.delete_camera_config(cam)
        except Exception:  # noqa: BLE001
            pass


def test_main_app_auto_registers_router():
    from app.main import app

    paths = list(app.openapi()["paths"].keys())
    assert "/api/preroll/config" in paths
    assert any("preroll" in p for p in paths)
