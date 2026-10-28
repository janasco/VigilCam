import inspect

from fastapi.testclient import TestClient

from app.main import app
from app.services import ptz_tour
from app.services import tour_shuffle


def test_ptz_tour_steps_sequentially_no_shuffle_mode():
    """Baseline: the core engine steps sequentially with no shuffle/random."""
    src = inspect.getsource(ptz_tour.step_tour)
    assert "shuffle" not in src.lower()
    assert "random" not in src.lower()
    service_src = inspect.getsource(ptz_tour.PTZTourService.step_tour)
    assert "shuffle" not in service_src.lower()
    assert "random" not in service_src.lower()

    ptz_tour.clear()
    t = ptz_tour.create_tour(camera_id=11, presets=["p1", "p2", "p3"], name="seq")
    ptz_tour.start_tour(t["id"])
    # NOTE: step_tour returns the live stored dict, so snapshot the values
    # immediately instead of holding references across steps.
    first = ptz_tour.step_tour(t["id"]).get("current_preset")
    second = ptz_tour.step_tour(t["id"]).get("current_preset")
    third = ptz_tour.step_tour(t["id"]).get("current_preset")
    assert first == "p2"
    assert second == "p3"
    # Loops back around sequentially.
    assert third == "p1"


def test_tour_shuffle_imports():
    import app.services.tour_shuffle  # noqa: F401
    import app.api.tour_shuffle  # noqa: F401
    from app.services.tour_shuffle import (  # noqa: F401
        clear,
        disable_shuffle,
        enable_shuffle,
        get_shuffle,
        is_shuffle_enabled,
        reshuffle,
        set_shuffle,
        step_shuffled,
    )
    from app.api.tour_shuffle import router  # noqa: F401


def test_tour_shuffle_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any("shuffle" in p.lower() for p in paths)
    assert any(p.rstrip("/").endswith(("tour_shuffle", "tour-shuffle", "tour/shuffle"))
               for p in paths)


def test_tour_shuffle_service_cycle():
    ptz_tour.clear()
    tour_shuffle.clear()
    t = ptz_tour.create_tour(camera_id=12, presets=["a", "b", "c", "d"], name="shuf")
    ptz_tour.start_tour(t["id"])
    assert tour_shuffle.is_shuffle_enabled(t["id"]) is False

    state = tour_shuffle.enable_shuffle(t["id"], seed=42)
    assert state.get("enabled") is True
    assert sorted(state.get("order")) == [0, 1, 2, 3]
    # Non-trivial order (not sequential).
    assert state.get("order") != [0, 1, 2, 3]

    seen = []
    for _ in range(4):
        stepped = tour_shuffle.step_shuffled(t["id"])
        seen.append(stepped.get("current_preset"))
    # Full shuffled cycle visits every preset exactly once.
    assert sorted(seen) == ["a", "b", "c", "d"]

    disabled = tour_shuffle.disable_shuffle(t["id"])
    assert disabled.get("enabled") is False
    assert tour_shuffle.is_shuffle_enabled(t["id"]) is False


def test_tour_shuffle_seed_deterministic():
    ptz_tour.clear()
    tour_shuffle.clear()
    t1 = ptz_tour.create_tour(camera_id=13, presets=["x", "y", "z"], name="s1")
    t2 = ptz_tour.create_tour(camera_id=14, presets=["x", "y", "z"], name="s2")
    o1 = tour_shuffle.enable_shuffle(t1["id"], seed=7).get("order")
    o2 = tour_shuffle.enable_shuffle(t2["id"], seed=7).get("order")
    assert o1 == o2
    r = tour_shuffle.reshuffle(t1["id"], seed=99)
    assert r.get("enabled") is True
    assert sorted(r.get("order")) == [0, 1, 2]


def test_tour_shuffle_endpoint_basic():
    ptz_tour.clear()
    tour_shuffle.clear()
    client = TestClient(app)
    r = client.post("/api/ptz_tour", json={"camera_id": 15, "presets": ["p1", "p2", "p3"]})
    assert r.status_code == 200, r.text
    tid = r.json().get("id") or r.json().get("tour_id")
    assert tid

    r2 = client.post("/api/tour_shuffle/enable", json={"tour_id": tid, "seed": 5})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("enabled") is True

    r3 = client.post("/api/tour_shuffle/step", json={"tour_id": tid})
    assert r3.status_code == 200, r3.text
    assert r3.json().get("current_preset") in ("p1", "p2", "p3")

    r4 = client.get(f"/api/tour_shuffle/{tid}")
    assert r4.status_code == 200, r4.text

    r5 = client.get("/api/tour_shuffle")
    assert r5.status_code == 200, r5.text
