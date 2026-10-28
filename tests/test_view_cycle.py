from fastapi.testclient import TestClient

from app.main import app
from app.services import view_cycle


def test_view_cycle_imports():
    import app.services.view_cycle  # noqa: F401
    import app.api.view_cycle  # noqa: F401
    from app.services.view_cycle import (  # noqa: F401
        ViewCycleService,
        create_cycle,
        list_cycles,
        view_cycle_service,
        start_cycle,
        step_cycle,
        stop_cycle,
    )
    from app.api.view_cycle import router  # noqa: F401


def test_view_cycle_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any("view_cycle" in p or "view-cycle" in p or "view-cycle" in p.lower() for p in paths)
    assert any(p.rstrip("/").endswith(("/view_cycle", "/view-cycle", "/view_cycles", "/view-cycles"))
               for p in paths)


def test_view_cycle_service_basic():
    view_cycle.clear()
    c = view_cycle.create_cycle(name="lobby-wall", views=["lobby", "parking", "warehouse"], dwell_seconds=5)
    assert isinstance(c, dict)
    assert c.get("id")
    assert len(c.get("views")) == 3
    assert c.get("moves_camera") is False
    assert c.get("display_only") is True
    started = view_cycle.start_cycle(c["id"])
    assert started.get("status") == "running"
    assert started.get("current_view") == "lobby"
    stepped = view_cycle.step_cycle(c["id"])
    assert stepped.get("current_view") == "parking"
    assert stepped.get("current_index") == 1


def test_view_cycle_pause_resume():
    view_cycle.clear()
    c = view_cycle.create_cycle(name="entrances", views=["front", "back"])
    view_cycle.start_cycle(c["id"])
    paused = view_cycle.pause_cycle(c["id"])
    assert paused.get("status") == "paused"
    resumed = view_cycle.resume_cycle(c["id"])
    assert resumed.get("status") == "running"


def test_view_cycle_stop_and_list():
    view_cycle.clear()
    c1 = view_cycle.create_cycle(name="floor1", views=["a", "b"])
    view_cycle.start_cycle(c1["id"])
    assert len(view_cycle.list_cycles()) == 1
    stopped = view_cycle.stop_cycle(c1["id"])
    assert stopped.get("status") == "stopped"
    assert view_cycle.is_running(c1["id"]) is False


def test_view_cycle_group_aliases():
    view_cycle.clear()
    c = view_cycle.create_view_cycle(name="perimeter", views=["north", "south"])
    assert c.get("id")
    s = view_cycle.start_view_cycle(c["id"])
    assert s.get("status") == "running"
    st = view_cycle.step_view_cycle(c["id"])
    assert st.get("current_view") == "south"


def test_view_cycle_endpoint_basic():
    view_cycle.clear()
    client = TestClient(app)
    r = client.post("/api/view_cycle", json={"name": "wall-1", "views": ["g1", "g2"]})
    assert r.status_code == 200, r.text
    cid = r.json().get("id") or r.json().get("cycle_id")
    assert cid
    r2 = client.post("/api/view_cycle/start", json={"id": cid})
    assert r2.status_code == 200
    assert r2.json().get("status") == "running"
    r3 = client.post("/api/view_cycle/step", json={"id": cid})
    assert r3.status_code == 200
    assert r3.json().get("current_view") == "g2"
    r4 = client.get("/api/view_cycle")
    assert r4.status_code == 200
    assert isinstance(r4.json(), list)


def test_view_cycle_endpoint_group_path():
    view_cycle.clear()
    client = TestClient(app)
    r = client.post("/api/group-view-cycle", json={"name": "wall-2", "views": ["h1", "h2"]})
    assert r.status_code == 200, r.text
    r2 = client.get("/api/view-cycles")
    assert r2.status_code == 200
