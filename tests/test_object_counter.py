from fastapi.testclient import TestClient

from app.main import app


def test_object_counter_imports():
    import app.api.object_counter  # noqa: F401
    import app.services.object_counter  # noqa: F401
    from app.api.object_counter import router  # noqa: F401
    from app.services.object_counter import (  # noqa: F401
        ObjectCounter,
        CountingLine,
        CrossingEvent,
        count_line_crossings,
        crossing_direction,
        segments_intersect,
    )


def _counter_with_horizontal_line(camera_id=1, line_id="door"):
    from app.services.object_counter import ObjectCounter

    counter = ObjectCounter()
    counter.define_line(camera_id=camera_id, line_id=line_id, p1=(0, 50), p2=(100, 50))
    return counter


def test_forward_crossing():
    counter = _counter_with_horizontal_line()
    # Horizontal line y=50 directed +x: side = +100*(y - 50), so a point
    # above (y < 50) is on the negative side and a point below is on the
    # positive side. Negative -> positive (downward here) is "forward".
    assert counter.update(camera_id=1, track_id="t1", x=10, y=40) is None
    event = counter.update(camera_id=1, track_id="t1", x=10, y=60)
    assert event is not None
    assert event["direction"] == "forward"
    counts = counter.get_counts(camera_id=1)
    assert counts["total"] == 1
    assert counts["forward"] == 1
    assert counts["backward"] == 0
    assert counts["per_camera"]["1"]["forward"] == 1
    assert counts["per_camera_per_line"]["1"]["door"]["total"] == 1


def test_backward_crossing():
    counter = _counter_with_horizontal_line()
    assert counter.update(camera_id=1, track_id="t2", x=20, y=60) is None
    event = counter.update(camera_id=1, track_id="t2", x=20, y=40)
    assert event is not None
    assert event["direction"] == "backward"
    counts = counter.get_counts()
    assert counts["backward"] == 1
    assert counts["positive"] == 0
    assert counts["negative"] == 1


def test_no_crossing_without_intersection():
    counter = _counter_with_horizontal_line()
    # Movement parallel to the line, same side: no crossing.
    assert counter.update(camera_id=1, track_id="t3", x=5, y=60) is None
    assert counter.update(camera_id=1, track_id="t3", x=90, y=65) is None
    # Movement that changes side but misses the segment (x > 100).
    assert counter.update(camera_id=1, track_id="t4", x=150, y=60) is None
    assert counter.update(camera_id=1, track_id="t4", x=150, y=40) is None
    assert counter.get_counts()["total"] == 0


def test_per_camera_and_per_line_isolation():
    from app.services.object_counter import ObjectCounter

    counter = ObjectCounter()
    counter.define_line(camera_id=1, line_id="a", p1=(0, 50), p2=(100, 50))
    counter.define_line(camera_id=2, line_id="a", p1=(0, 50), p2=(100, 50))
    counter.define_line(camera_id=1, line_id="b", p1=(0, 10), p2=(100, 10))
    counter.update(camera_id=1, track_id="t1", x=10, y=40)
    counter.update(camera_id=1, track_id="t1", x=10, y=60)  # crosses line a only (forward)
    cam1 = counter.get_counts(camera_id=1)
    cam2 = counter.get_counts(camera_id=2)
    assert cam1["total"] == 1
    assert cam2["total"] == 0
    line_b = counter.get_counts(camera_id=1, line_id="b")
    assert line_b["total"] == 0
    line_a = counter.get_counts(camera_id=1, line_id="a")
    assert line_a["total"] == 1


def test_stateless_batch_count():
    from app.services.object_counter import count_line_crossings

    result = count_line_crossings(
        tracks={"t1": [(10, 60), (10, 40)], "t2": [(20, 40), (20, 60)]},
        lines=[{"camera_id": 1, "line_id": "door", "p1": (0, 50), "p2": (100, 50)}],
        camera_id=1,
    )
    assert result["total"] == 2
    assert result["forward"] == 1
    assert result["backward"] == 1
    assert len(result["events"]) == 2


def test_detection_dict_and_bbox_inputs():
    counter = _counter_with_horizontal_line()
    assert counter.update({"camera_id": 1, "track_id": "tb", "bbox": [5, 30, 10, 10]}) is None  # center y=35
    event = counter.update({"camera_id": 1, "track_id": "tb", "bbox": [5, 50, 10, 10]})  # center y=55
    assert event is not None
    assert event["direction"] == "forward"


def test_reset_scope():
    counter = _counter_with_horizontal_line()
    counter.update(camera_id=1, track_id="t1", x=10, y=40)
    counter.update(camera_id=1, track_id="t1", x=10, y=60)
    assert counter.get_counts()["total"] == 1
    counter.reset(camera_id=1)
    assert counter.get_counts()["total"] == 0


def test_object_counter_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/object_counter" in paths
    assert any("object_counter" in p or "object-counter" in p for p in paths)


def test_object_counter_api_flow():
    from app.services.object_counter import default_counter

    default_counter.reset()
    client = TestClient(app)
    # define a line
    r = client.post("/api/object_counter/lines",
                    json={"camera_id": 7, "line_id": "gate", "p1": [0, 50], "p2": [100, 50]})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    # feed a crossing track
    r1 = client.post("/api/object_counter/update",
                     json={"camera_id": 7, "track_id": "apit1", "x": 10, "y": 40})
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/object_counter/update",
                     json={"camera_id": 7, "track_id": "apit1", "x": 10, "y": 60})
    assert r2.status_code == 200, r2.text
    assert r2.json()["crossed"] is True
    assert r2.json()["event"]["direction"] == "forward"
    # counts
    r3 = client.get("/api/object_counter/counts", params={"camera_id": 7})
    assert r3.status_code == 200, r3.text
    data = r3.json()
    assert data["total"] >= 1
    assert data["forward"] >= 1
    # reset scope and verify zero
    r4 = client.post("/api/object_counter/reset", json={"camera_id": 7})
    assert r4.status_code == 200, r4.text
    r5 = client.get("/api/object_counter/counts", params={"camera_id": 7})
    assert r5.status_code == 200, r5.text
    assert r5.json()["total"] == 0
