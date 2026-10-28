import os
from fastapi.testclient import TestClient

from app.main import app
from app.services import camera_health as ch


def test_camera_health_imports():
    from app.main import app as _app
    paths = list(_app.openapi()["paths"].keys())
    assert "/api/cameras/health" in paths
    assert "/api/cameras/{camera_id}/health" in paths


def test_camera_health_service_basic():
    ch.clear()
    cid = 9991
    st = ch.mark_frame_received(cid)
    assert st is not None
    assert st.online is True
    assert st.last_frame is not None
    assert st.reconnect_attempts == 0
    assert st.backoff_seconds == 0
    h = ch.get_health(cid)
    assert h.online is True
    assert h["online"] is True
    assert h["camera_id"] == cid
    # offline + reconnect backoff
    ch.mark_offline(cid)
    h2 = ch.get_health(cid)
    assert h2.online is False
    assert h2.reconnect_attempts >= 1
    assert h2.backoff_seconds > 0
    # frame resets
    ch.mark_frame_received(cid)
    assert ch.get_health(cid).reconnect_attempts == 0
    ch.remove(cid)


def test_camera_health_backoff_cap():
    ch.clear()
    cid = 9992
    for _ in range(20):
        ch.record_reconnect(cid)
    h = ch.get_health(cid)
    assert h.backoff_seconds <= 60.0
    assert h.reconnect_attempts >= 1
    ch.clear()


def test_camera_health_endpoints():
    client = TestClient(app)
    r = client.get("/api/cameras/health")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    # create a camera then check per-camera health
    cr = client.post("/api/cameras", json={"name": "hc", "kind": "ip", "source": "rtsp://10.0.0.1/x"})
    assert cr.status_code in (200, 201)
    cid = cr.json()["id"]
    # mark frame via service
    ch.mark_frame_received(cid)
    r2 = client.get(f"/api/cameras/{cid}/health")
    assert r2.status_code == 200
    data = r2.json()
    assert data["camera_id"] == cid
    assert data["online"] is True
    # 404 for unknown
    r3 = client.get("/api/cameras/999999/health")
    assert r3.status_code == 404
    # cleanup
    client.delete(f"/api/cameras/{cid}")


def test_camera_health_integration_motion_worker():
    from app.workers.motion_worker import MotionProcessManager
    import numpy as np

    ch.clear()
    m = MotionProcessManager(camera_id=7777)
    frame = np.zeros((4, 4, 3), dtype=__import__("numpy").uint8)
    m.submit_frame(frame)
    h = ch.get_health(7777)
    assert h is not None
    assert h.last_frame is not None
