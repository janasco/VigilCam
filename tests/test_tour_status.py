from fastapi.testclient import TestClient

from app.main import app
from app.services import ptz_tour
from app.services import tour_shuffle
from app.services import tour_status


def test_tour_status_imports():
    import app.services.tour_status  # noqa: F401
    import app.api.tour_status  # noqa: F401
    from app.services.tour_status import (  # noqa: F401
        get_combined_status,
        get_status,
        get_tour_status,
        list_tour_statuses,
    )
    from app.api.tour_status import router  # noqa: F401


def test_tour_status_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any("tour_status" in p.lower() or "tour-status" in p.lower() for p in paths)
    assert any(p.rstrip("/").endswith(("tour_status", "tour-status", "combined_status"))
               for p in paths)


def test_tour_status_combines_tour_progress_shuffle():
    ptz_tour.clear()
    tour_shuffle.clear()
    t = ptz_tour.create_tour(camera_id=31, presets=["p1", "p2", "p3"], name="combo")
    ptz_tour.start_tour(t["id"])
    tour_shuffle.enable_shuffle(t["id"], seed=3)

    combined = tour_status.get_tour_status(t["id"])
    # Tour section.
    assert combined.get("tour_id") == t["id"]
    assert combined.get("status") == "running"
    # Progress section (live fields).
    assert combined.get("current_preset_index") == 0
    assert combined.get("current_preset") == "p1"
    assert "elapsed_seconds" in combined
    assert combined.get("total_presets") == 3
    assert combined.get("progress_percent") is not None
    # Shuffle section.
    assert combined.get("shuffle_enabled") is True
    assert sorted(combined.get("shuffle_order")) == [0, 1, 2]
    # Nested sections for detailed UI rendering.
    assert isinstance(combined.get("tour"), dict)
    assert isinstance(combined.get("progress"), dict)
    assert isinstance(combined.get("shuffle"), dict)
    assert combined.get("ok") is True

    items = tour_status.list_tour_statuses()
    assert any(i.get("tour_id") == t["id"] for i in items)


def test_tour_status_endpoint_basic():
    ptz_tour.clear()
    tour_shuffle.clear()
    client = TestClient(app)
    r = client.post("/api/ptz_tour", json={"camera_id": 32, "presets": ["a", "b"]})
    assert r.status_code == 200, r.text
    tid = r.json().get("id") or r.json().get("tour_id")
    assert tid
    client.post("/api/ptz_tour/start", json={"id": tid})
    client.post("/api/tour_shuffle/enable", json={"tour_id": tid, "seed": 11})

    r2 = client.get(f"/api/tour_status/{tid}")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body.get("tour_id") == tid
    assert "progress" in body
    assert "shuffle" in body
    assert "tour" in body
    assert body.get("shuffle_enabled") is True

    r3 = client.get("/api/tour_status", params={"tour_id": tid})
    assert r3.status_code == 200, r3.text
    assert r3.json().get("tour_id") == tid

    r4 = client.get("/api/tour_status")
    assert r4.status_code == 200, r4.text

    r5 = client.get("/api/tour-status")
    assert r5.status_code == 200, r5.text


def test_tour_status_missing_404():
    ptz_tour.clear()
    tour_shuffle.clear()
    client = TestClient(app)
    r = client.get("/api/tour_status/no-such-tour-id")
    assert r.status_code == 404, r.text
