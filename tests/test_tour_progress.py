import time

from fastapi.testclient import TestClient

from app.main import app
from app.services import ptz_tour
from app.services import tour_progress


def test_tour_progress_imports():
    import app.services.tour_progress  # noqa: F401
    import app.api.tour_progress  # noqa: F401
    from app.services.tour_progress import (  # noqa: F401
        get_progress,
        get_tour_progress,
        list_progress,
    )
    from app.api.tour_progress import router  # noqa: F401


def test_tour_progress_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any("tour" in p.lower() and "progress" in p.lower() for p in paths)
    assert any(p.rstrip("/").endswith(("tour_progress", "tour-progress", "tour/progress"))
               for p in paths)


def test_tour_progress_service_basic():
    ptz_tour.clear()
    t = ptz_tour.create_tour(camera_id=21, presets=["p1", "p2", "p3"], name="prog")
    ptz_tour.start_tour(t["id"])
    prog = tour_progress.get_progress(t["id"])
    # Live progress exposes current preset index + elapsed.
    assert prog.get("current_preset_index") == 0
    assert prog.get("current_index") == 0
    assert prog.get("current_preset") == "p1"
    assert "elapsed_seconds" in prog
    assert "elapsed" in prog
    assert float(prog.get("elapsed_seconds")) >= 0.0
    assert prog.get("total_presets") == 3
    assert prog.get("progress_percent") is not None


def test_tour_progress_tracks_steps_and_elapsed():
    ptz_tour.clear()
    t = ptz_tour.create_tour(camera_id=22, presets=["a", "b", "c"], name="prog2")
    ptz_tour.start_tour(t["id"])
    first = tour_progress.get_progress(t["id"])
    assert first.get("current_preset_index") == 0
    ptz_tour.step_tour(t["id"])
    second = tour_progress.get_progress(t["id"])
    assert second.get("current_preset_index") == 1
    assert second.get("current_preset") == "b"
    assert float(second.get("elapsed_seconds")) >= float(first.get("elapsed_seconds"))
    # Progress percent advances with index.
    assert float(second.get("progress_percent")) > float(first.get("progress_percent"))
    time.sleep(0.02)
    third = tour_progress.get_progress(t["id"])
    assert float(third.get("elapsed_seconds")) >= float(second.get("elapsed_seconds"))


def test_tour_progress_endpoint_basic():
    ptz_tour.clear()
    client = TestClient(app)
    r = client.post("/api/ptz_tour", json={"camera_id": 23, "presets": ["p1", "p2"]})
    assert r.status_code == 200, r.text
    tid = r.json().get("id") or r.json().get("tour_id")
    assert tid
    r2 = client.post("/api/ptz_tour/start", json={"id": tid})
    assert r2.status_code == 200, r2.text

    r3 = client.get(f"/api/tour_progress/{tid}")
    assert r3.status_code == 200, r3.text
    body = r3.json()
    assert body.get("current_preset_index") == 0
    assert "elapsed_seconds" in body

    r4 = client.get("/api/tour_progress", params={"tour_id": tid})
    assert r4.status_code == 200, r4.text
    assert r4.json().get("current_preset") == "p1"

    r5 = client.get("/api/tour/progress", params={"tour_id": tid})
    assert r5.status_code == 200, r5.text

    r6 = client.get("/api/tour_progress")
    assert r6.status_code == 200, r6.text
