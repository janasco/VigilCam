from fastapi.testclient import TestClient

from app.main import app
from app.services import ptz_tracking


def test_ptz_tracking_imports():
    import app.services.ptz_tracking  # noqa: F401
    import app.api.ptz_tracking  # noqa: F401
    from app.services.ptz_tracking import start_tracking, ptz_tracking_service, PTZTrackingService  # noqa: F401
    from app.api.ptz_tracking import router  # noqa: F401
    from app.schemas import PTZTrackingBase, PTZTrackingRead  # noqa: F401


def test_ptz_tracking_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any("track" in p.lower() and "ptz" in p.lower() for p in paths)
    assert any(p.endswith("/ptz/tracking") or p.endswith("/ptz_tracking") or p.endswith("/ptz-tracking") for p in paths)


def test_ptz_tracking_service_basic():
    ptz_tracking.clear()
    sess = ptz_tracking.start_tracking(camera_id=1, target={"x": 0.8, "y": 0.2})
    assert isinstance(sess, dict)
    assert sess.get("status") == "tracking"
    assert sess.get("camera_id") == 1
    assert ptz_tracking.get(sess["id"]) is not None
    assert len(ptz_tracking.list_tracking()) == 1


def test_ptz_tracking_velocity():
    v = ptz_tracking.compute_velocity({"x": 0.8, "y": 0.2})
    assert len(v) == 3
    assert v[0] > 0
    assert isinstance(v, dict)
    assert "x" in v


def test_ptz_tracking_stop():
    ptz_tracking.clear()
    sess = ptz_tracking.start_tracking(camera_id=2, target={"x": 0.5, "y": 0.5})
    stopped = ptz_tracking.stop_tracking(sess["id"])
    assert stopped.get("status") == "stopped"
    assert ptz_tracking.is_tracking(sess["id"]) is False


def test_ptz_tracking_update():
    ptz_tracking.clear()
    sess = ptz_tracking.start_tracking(camera_id=3, target={"x": 0.1, "y": 0.1})
    upd = ptz_tracking.update_target(sess["id"], target={"x": 0.9, "y": 0.9})
    assert upd.get("status") == "tracking"


def test_ptz_tracking_process():
    ptz_tracking.clear()
    ptz_tracking.start_tracking(camera_id=4, target={"x": 0.5, "y": 0.5})
    res = ptz_tracking.process()
    assert isinstance(res, dict)
    assert res.get("processed") == 1


def test_ptz_tracking_endpoint_basic():
    ptz_tracking.clear()
    client = TestClient(app)
    r = client.post("/api/ptz_tracking", json={"camera_id": 5, "target": {"x": 0.7, "y": 0.3}})
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "tracking"
    r2 = client.get("/api/ptz_tracking")
    assert r2.status_code == 200
    assert isinstance(r2.json(), list)


def test_ptz_tracking_endpoint_stop():
    ptz_tracking.clear()
    client = TestClient(app)
    r = client.post("/api/ptz_tracking/start", json={"camera_id": 6, "target": {"x": 0.5, "y": 0.5}})
    assert r.status_code == 200
    tid = r.json().get("id") or r.json().get("tracking_id")
    r2 = client.post("/api/ptz_tracking/stop", json={"id": tid})
    assert r2.status_code == 200
    assert r2.json().get("status") == "stopped"
