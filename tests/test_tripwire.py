"""Tests for the virtual tripwire service (line-crossing, not polygon zones)."""

from fastapi.testclient import TestClient

from app.main import app
from app.services import tripwire
from app.services.tripwire import (
    TripLine,
    define_trip_line,
    evaluate_crossing,
    evaluate_track,
)


def _vertical_line(camera="cam1"):
    # Line pointing up: left-to-right crossings read as "forward"
    # (neg -> pos side of the directed segment).
    tripwire.clear_trip_lines(camera)
    return define_trip_line(camera, (0.5, 1.0), (0.5, 0.0))


def test_tripwire_imports():
    import app.services.tripwire  # noqa: F401
    import app.api.tripwire  # noqa: F401
    from app.api.tripwire import router  # noqa: F401
    from app.services.tripwire import tripwire_service, TripwireService  # noqa: F401


def test_tripwire_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any("tripwire" in p or "trip_wire" in p for p in paths)


def test_define_and_list_lines():
    tripwire.clear_trip_lines("camtest")
    created = define_trip_line("camtest", (0.0, 0.5), (1.0, 0.5), name="door")
    assert created["camera_id"] == "camtest"
    lines = tripwire.get_trip_lines("camtest")
    assert len(lines) == 1
    assert lines[0]["name"] == "door"


def test_crossing_forward_and_backward():
    line = TripLine(p1=(0.5, 1.0), p2=(0.5, 0.0), camera_id="c1")
    fwd = evaluate_crossing(line, (0.1, 0.5), (0.9, 0.5))
    assert fwd["crossed"] is True
    assert fwd["direction"] == "forward"
    bwd = evaluate_crossing(line, (0.9, 0.5), (0.1, 0.5))
    assert bwd["crossed"] is True
    assert bwd["direction"] == "backward"


def test_no_crossing_parallel_or_short():
    line = TripLine(p1=(0.5, 1.0), p2=(0.5, 0.0), camera_id="c1")
    # Same side: no crossing.
    assert evaluate_crossing(line, (0.1, 0.2), (0.2, 0.8))["crossed"] is False
    # Parallel segment beyond the line extent: no crossing.
    assert evaluate_crossing(line, (0.6, 1.5), (0.9, 1.5))["crossed"] is False


def test_direction_filter():
    tripwire.clear_trip_lines("camdir")
    define_trip_line("camdir", (0.5, 1.0), (0.5, 0.0), direction="forward")
    fwd = evaluate_track("camdir", [(0.1, 0.5), (0.9, 0.5)])
    assert fwd["crossed"] is True
    assert fwd["count"] == 1
    bwd = evaluate_track("camdir", [(0.9, 0.5), (0.1, 0.5)])
    assert bwd["crossed"] is False
    assert bwd["count"] == 0


def test_evaluate_track_emits_event():
    _vertical_line("camev")
    res = evaluate_track("camev", [(0.1, 0.5), (0.9, 0.5)])
    assert res["crossed"] is True
    assert res["count"] == 1
    ev = res["events"][0]
    assert ev["direction"] in ("forward", "backward")
    assert ev["segment_index"] == 1
    assert ev["point"] is not None


def test_tripwire_api_endpoints():
    tripwire.clear_trip_lines("camapi")
    client = TestClient(app)
    r = client.post("/api/tripwire", json={"camera_id": "camapi", "p1": [0.5, 0.0], "p2": [0.5, 1.0]})
    assert r.status_code == 200, r.text
    r2 = client.get("/api/tripwire", params={"camera_id": "camapi"})
    assert r2.status_code == 200
    assert isinstance(r2.json(), list)
    assert len(r2.json()) == 1
    r3 = client.post("/api/tripwire/evaluate", json={"camera_id": "camapi", "points": [[0.1, 0.5], [0.9, 0.5]]})
    assert r3.status_code == 200, r3.text
    assert r3.json()["crossed"] is True
