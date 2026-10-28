from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.widgets import router as widgets_router  # noqa: F401
from app.services import widgets as widgets_service_mod  # noqa: F401
from app.services.widgets import (  # noqa: F401
    get_widgets,
    set_widgets,
    update_widgets,
    reset_widgets,
    add_widget,
    remove_widget,
    get_widget,
    clear,
    widgets_service,
)


def test_widgets_import():
    import app.api.widgets  # noqa: F401
    import app.services.widgets  # noqa: F401
    from app.api.widgets import router  # noqa: F401
    from app.services.widgets import get_widgets, set_widgets  # noqa: F401


def test_widgets_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/widgets" in paths
    assert "/api/users/{user_id}/widgets" in paths
    assert "/api/dashboard/widgets" in paths


def test_widgets_service_crud():
    widgets_service_mod.clear()
    widgets = get_widgets("alice")
    assert isinstance(widgets, list)
    assert len(widgets) >= 1
    assert widgets[0]["id"]
    assert widgets[0]["type"]

    replaced = set_widgets("alice", [{"id": "cameras", "type": "cameras", "title": "Cams"}])
    assert len(replaced) == 1
    assert replaced[0]["id"] == "cameras"
    assert replaced[0]["type"] == "cameras"

    fetched = get_widgets("alice")
    assert len(fetched) == 1
    assert fetched[0]["title"] == "Cams"

    updated = update_widgets("alice", [{"id": "events", "type": "events"}])
    assert len(updated) == 2
    by_id = {w["id"]: w for w in updated}
    assert "cameras" in by_id
    assert "events" in by_id

    added = add_widget("alice", {"id": "stats", "type": "stats", "title": "Stats"})
    assert added["id"] == "stats"
    assert get_widget("alice", "stats")["title"] == "Stats"

    assert remove_widget("alice", "stats") is True
    assert get_widget("alice", "stats") is None

    reset = reset_widgets("alice")
    assert isinstance(reset, list)
    assert len(reset) >= 1

    assert get_widgets("alice")[0]["id"] == reset[0]["id"]
    widgets_service_mod.clear()


def test_widgets_service_defaults_and_types():
    widgets_service_mod.clear()
    defaults = widgets_service_mod.get_default_widgets()
    assert isinstance(defaults, list)
    assert len(defaults) >= 1
    types = widgets_service_mod.list_widget_types()
    assert "cameras" in types
    assert "events" in types
    assert widgets_service_mod.normalize_widget_type("Camera") == "cameras"
    widgets_service_mod.clear()


def test_widgets_api_crud():
    widgets_service_mod.clear()
    a = FastAPI()
    a.include_router(widgets_router)
    client = TestClient(a)

    r = client.put("/api/widgets", json={"user_id": "bob", "widgets": [{"id": "cameras", "type": "cameras"}]})
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1
    assert r.json()[0]["id"] == "cameras"

    g = client.get("/api/widgets", params={"user_id": "bob"})
    assert g.status_code == 200
    assert g.json()[0]["id"] == "cameras"

    p = client.patch("/api/widgets", json={"user_id": "bob", "widgets": [{"id": "events", "type": "events"}]})
    assert p.status_code == 200
    assert {w["id"] for w in p.json()} == {"cameras", "events"}

    scoped = client.get("/api/users/bob/widgets")
    assert scoped.status_code == 200
    assert {w["id"] for w in scoped.json()} == {"cameras", "events"}

    scoped_put = client.put("/api/users/bob/widgets", json={"widgets": [{"id": "stats", "type": "stats"}]})
    assert scoped_put.status_code == 200
    assert [w["id"] for w in scoped_put.json()] == ["stats"]

    item = client.get("/api/users/bob/widgets/stats")
    assert item.status_code == 200
    assert item.json()["id"] == "stats"

    missing = client.get("/api/users/bob/widgets/nope")
    assert missing.status_code == 404

    d = client.delete("/api/widgets", params={"user_id": "bob"})
    assert d.status_code == 200
    assert isinstance(d.json(), list)
    assert len(d.json()) >= 1


def test_widgets_api_top_level_validation():
    widgets_service_mod.clear()
    a = FastAPI()
    a.include_router(widgets_router)
    client = TestClient(a)

    bad = client.get("/api/widgets")
    assert bad.status_code == 400

    bad_put = client.put("/api/widgets", json={"type": "cameras"})
    assert bad_put.status_code == 400

    bad_widgets = client.put("/api/widgets", json={"user_id": "bob"})
    assert bad_widgets.status_code == 422
