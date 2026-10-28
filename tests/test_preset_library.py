from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.services import preset_library
from app.api.preset_library import router as preset_library_router  # noqa: F401
from app.services.preset_library import (  # noqa: F401
    clear,
    create_preset,
    delete_preset,
    get_preset,
    goto_preset,
    list_presets,
    preset_library_service,
    update_preset,
)


def test_preset_library_imports():
    import app.services.preset_library  # noqa: F401
    import app.api.preset_library  # noqa: F401
    from app.services.preset_library import PresetLibraryService, create_preset, list_presets  # noqa: F401
    from app.api.preset_library import router  # noqa: F401


def test_preset_library_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/preset_library" in paths
    assert any("preset" in p and "library" in p for p in paths)


def test_preset_library_service_basic():
    preset_library.clear()
    created = preset_library.create_preset(camera_id=1, name="door", pan=0.5, tilt=-0.2, zoom=1.0)
    assert isinstance(created, dict)
    assert created.get("id")
    assert created.get("camera_id") == 1
    assert created.get("name") == "door"
    assert created.get("pan") == 0.5
    assert created.get("tilt") == -0.2
    assert created.get("zoom") == 1.0

    fetched = preset_library.get_preset(created["id"])
    assert fetched is not None
    assert fetched.get("name") == "door"

    items = preset_library.list_presets(camera_id=1)
    assert len(items) == 1
    assert preset_library.list_presets(camera_id=999) == []


def test_preset_library_service_update_delete():
    preset_library.clear()
    created = preset_library.create_preset(camera_id=2, name="gate", preset_token="tok-1")
    updated = preset_library.update_preset(created["id"], name="gate-renamed", zoom=2.5)
    assert updated is not None
    assert updated.get("name") == "gate-renamed"
    assert updated.get("zoom") == 2.5
    assert updated.get("preset_token") == "tok-1"

    moved = preset_library.goto_preset(created["id"])
    assert moved.get("success") is True
    assert str(moved.get("camera_id")) == "2"

    assert preset_library.delete_preset(created["id"]) is True
    assert preset_library.get_preset(created["id"]) is None
    assert preset_library.delete_preset("missing") is False


def test_preset_library_service_validation():
    preset_library.clear()
    try:
        preset_library.create_preset(camera_id=1)
        assert False, "expected ValueError for missing name"
    except ValueError:
        pass
    try:
        preset_library.create_preset(name="noname")
        assert False, "expected ValueError for missing camera_id"
    except ValueError:
        pass


def test_preset_library_endpoints():
    preset_library.clear()
    client = TestClient(app)
    r = client.post("/api/preset_library", json={"camera_id": 5, "name": "lobby", "pan": 0.1})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    pid = body.get("id") or body.get("preset_id")
    assert pid is not None
    assert body.get("name") == "lobby"

    r2 = client.get("/api/preset_library")
    assert r2.status_code == 200
    assert isinstance(r2.json(), list)
    assert len(r2.json()) >= 1

    r3 = client.get(f"/api/preset_library/{pid}")
    assert r3.status_code == 200
    assert r3.json().get("id") == pid

    r4 = client.get("/api/preset_library", params={"camera_id": 5})
    assert r4.status_code == 200
    assert all(str(x.get("camera_id")) == "5" for x in r4.json())

    r5 = client.put(f"/api/preset_library/{pid}", json={"name": "lobby-renamed"})
    assert r5.status_code == 200
    assert r5.json().get("name") == "lobby-renamed"

    r6 = client.post("/api/preset_library/goto", json={"id": pid})
    assert r6.status_code == 200, r6.text
    assert r6.json().get("success") is True

    r7 = client.delete(f"/api/preset_library/{pid}")
    assert r7.status_code == 200
    assert client.get(f"/api/preset_library/{pid}").status_code == 404


def test_preset_library_isolated_router():
    preset_library.clear()
    isolated = FastAPI()
    from app.api.preset_library import router

    isolated.include_router(router)
    client = TestClient(isolated)

    r = client.post("/api/preset-library", json={"camera_id": 9, "name": "dock"})
    assert r.status_code in (200, 201), r.text
    pid = r.json().get("id")
    assert pid

    g = client.get("/api/preset-library")
    assert g.status_code == 200
    assert any(x.get("id") == pid for x in g.json())

    bad = client.post("/api/preset-library", json={"name": "missing-camera"})
    assert bad.status_code == 400
