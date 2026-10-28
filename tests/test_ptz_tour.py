from fastapi.testclient import TestClient

from app.main import app
from app.services import ptz_tour


def test_ptz_tour_imports():
    import app.services.ptz_tour  # noqa: F401
    import app.api.ptz_tour  # noqa: F401
    from app.services.ptz_tour import (  # noqa: F401
        PTZTourService,
        create_tour,
        list_tours,
        ptz_tour_service,
        start_tour,
        step_tour,
        stop_tour,
    )
    from app.api.ptz_tour import router  # noqa: F401


def test_ptz_tour_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any("tour" in p.lower() for p in paths)
    assert any(p.rstrip("/").endswith(("/ptz_tour", "/ptz-tour", "/ptz/tour", "/tour"))
               for p in paths)


def test_ptz_tour_service_basic():
    ptz_tour.clear()
    t = ptz_tour.create_tour(camera_id=1, presets=["p1", "p2", "p3"], name="lobby")
    assert isinstance(t, dict)
    assert t.get("id")
    assert len(t.get("presets")) == 3
    started = ptz_tour.start_tour(t["id"])
    assert started.get("status") == "running"
    assert started.get("current_preset") == "p1"
    stepped = ptz_tour.step_tour(t["id"])
    assert stepped.get("current_preset") == "p2"
    assert stepped.get("current_index") == 1


def test_ptz_tour_pause_resume():
    ptz_tour.clear()
    t = ptz_tour.create_tour(camera_id=2, presets=["a", "b"])
    ptz_tour.start_tour(t["id"])
    paused = ptz_tour.pause_tour(t["id"])
    assert paused.get("status") == "paused"
    resumed = ptz_tour.resume_tour(t["id"])
    assert resumed.get("status") == "running"


def test_ptz_tour_stop_and_list():
    ptz_tour.clear()
    t1 = ptz_tour.create_tour(camera_id=3, presets=["x", "y"])
    ptz_tour.start_tour(t1["id"])
    assert len(ptz_tour.list_tours()) == 1
    stopped = ptz_tour.stop_tour(t1["id"])
    assert stopped.get("status") == "stopped"
    assert ptz_tour.is_running(t1["id"]) is False


def test_ptz_tour_patrol_aliases():
    ptz_tour.clear()
    t = ptz_tour.create_patrol(camera_id=4, presets=["m1", "m2"])
    assert t.get("id")
    s = ptz_tour.start_patrol(t["id"])
    assert s.get("status") == "running"
    st = ptz_tour.step_patrol(t["id"])
    assert st.get("current_preset") == "m2"


def test_ptz_tour_endpoint_basic():
    ptz_tour.clear()
    client = TestClient(app)
    r = client.post("/api/ptz_tour", json={"camera_id": 5, "presets": ["p1", "p2"]})
    assert r.status_code == 200, r.text
    tid = r.json().get("id") or r.json().get("tour_id")
    assert tid
    r2 = client.post("/api/ptz_tour/start", json={"id": tid})
    assert r2.status_code == 200
    assert r2.json().get("status") == "running"
    r3 = client.post("/api/ptz_tour/step", json={"id": tid})
    assert r3.status_code == 200
    assert r3.json().get("current_preset") == "p2"
    r4 = client.get("/api/ptz_tour")
    assert r4.status_code == 200
    assert isinstance(r4.json(), list)


def test_ptz_tour_endpoint_patrol_path():
    ptz_tour.clear()
    client = TestClient(app)
    r = client.post("/api/patrol", json={"camera_id": 6, "presets": ["q1", "q2"]})
    assert r.status_code == 200, r.text
    r2 = client.get("/api/ptz/tours")
    assert r2.status_code == 200
