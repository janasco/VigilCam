from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.preferences import router as preferences_router  # noqa: F401
from app.services import preferences as preferences_service_mod  # noqa: F401
from app.services.preferences import (  # noqa: F401
    get_preferences,
    set_preferences,
    update_preferences,
    reset_preferences,
    clear,
    preferences_service,
)


def test_preferences_import():
    import app.api.preferences  # noqa: F401
    import app.services.preferences  # noqa: F401
    from app.api.preferences import router  # noqa: F401
    from app.services.preferences import get_preferences, set_preferences  # noqa: F401


def test_preferences_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/preferences" in paths
    assert "/api/users/{user_id}/preferences" in paths


def test_preferences_service_crud():
    preferences_service_mod.clear()
    prefs = get_preferences("alice")
    assert prefs["user_id"] == "alice"
    assert prefs["theme"] == "light"
    assert prefs["language"] == "en"

    replaced = set_preferences("alice", {"theme": "dark", "language": "fr"})
    assert replaced["user_id"] == "alice"
    assert replaced["theme"] == "dark"
    assert replaced["language"] == "fr"

    fetched = get_preferences("alice")
    assert fetched["theme"] == "dark"

    updated = update_preferences("alice", {"theme": "auto"})
    assert updated["theme"] == "auto"
    assert updated["language"] == "fr"

    reset = reset_preferences("alice")
    assert reset["user_id"] == "alice"
    assert reset["theme"] == "light"

    assert get_preferences("alice")["theme"] == "light"
    preferences_service_mod.clear()


def test_preferences_api_crud():
    preferences_service_mod.clear()
    a = FastAPI()
    a.include_router(preferences_router)
    client = TestClient(a)

    r = client.put("/api/preferences", json={"user_id": "bob", "theme": "dark"})
    assert r.status_code == 200, r.text
    assert r.json()["theme"] == "dark"
    assert r.json()["user_id"] == "bob"

    g = client.get("/api/preferences", params={"user_id": "bob"})
    assert g.status_code == 200
    assert g.json()["theme"] == "dark"

    p = client.patch("/api/preferences", json={"user_id": "bob", "language": "es"})
    assert p.status_code == 200
    assert p.json()["language"] == "es"
    assert p.json()["theme"] == "dark"

    scoped = client.get("/api/users/bob/preferences")
    assert scoped.status_code == 200
    assert scoped.json()["language"] == "es"

    scoped_put = client.put("/api/users/bob/preferences", json={"theme": "light"})
    assert scoped_put.status_code == 200
    assert scoped_put.json()["theme"] == "light"

    d = client.delete("/api/preferences", params={"user_id": "bob"})
    assert d.status_code == 200
    assert d.json()["theme"] == "light"


def test_preferences_api_top_level_validation():
    preferences_service_mod.clear()
    a = FastAPI()
    a.include_router(preferences_router)
    client = TestClient(a)

    bad = client.get("/api/preferences")
    assert bad.status_code == 400

    bad_put = client.put("/api/preferences", json={"theme": "dark"})
    assert bad_put.status_code == 400
