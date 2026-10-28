from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.microphones import router as microphones_router  # noqa: F401
from app.services import microphones as microphones_service_mod  # noqa: F401
from app.services.microphones import (  # noqa: F401
    list_microphones,
    get_microphone,
    create_microphone,
    update_microphone,
    delete_microphone,
    clear,
    microphones_service,
)


def test_microphones_import():
    import app.api.microphones  # noqa: F401
    import app.services.microphones  # noqa: F401
    from app.api.microphones import router  # noqa: F401
    from app.services.microphones import list_microphones, create_microphone  # noqa: F401


def test_microphones_paths_present():
    # Router-level paths (fast, no full-app openapi generation).
    router_paths = [getattr(r, "path", "") for r in microphones_router.routes]
    assert "/api/microphones" in router_paths
    # Audio-input alias should also be registered.
    assert any(
        p in router_paths
        for p in (
            "/api/audio_inputs",
            "/api/audio-inputs",
        )
    )
    # Spot-check item routes.
    assert "/api/microphones/{microphone_id}" in router_paths
    assert "/api/audio_inputs/{microphone_id}" in router_paths


def test_microphones_service_crud():
    microphones_service_mod.clear()
    items = list_microphones()
    assert isinstance(items, list)
    assert len(items) == 0

    created = create_microphone({"name": "front-door-mic", "source": "alsa://hw:0"})
    assert created["id"]
    assert created["name"] == "front-door-mic"
    assert created["source"] == "alsa://hw:0"
    # Alias keys populated.
    assert created["microphone_id"] == created["id"]
    assert created["audio_input_id"] == created["id"]

    fetched = get_microphone(created["id"])
    assert fetched is not None
    assert fetched["name"] == "front-door-mic"

    updated = update_microphone(created["id"], {"name": "porch-mic"})
    assert updated is not None
    assert updated["name"] == "porch-mic"
    assert updated["id"] == created["id"]

    listed = list_microphones()
    assert len(listed) == 1
    assert listed[0]["name"] == "porch-mic"

    assert delete_microphone(created["id"]) is True
    assert get_microphone(created["id"]) is None
    assert list_microphones() == []
    microphones_service_mod.clear()


def test_microphones_service_aliases_and_validation():
    microphones_service_mod.clear()
    # Audio-input alias functions delegate to the same registry.
    created = microphones_service_mod.create_audio_input({"name": "cam-mic", "source": "rtsp://cam/audio"})
    assert created["name"] == "cam-mic"
    assert microphones_service_mod.get_audio_input(created["id"])["id"] == created["id"]
    assert len(microphones_service_mod.list_audio_inputs()) == 1

    # Name or source is required.
    try:
        create_microphone({})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty payload")

    microphones_service_mod.clear()


def test_microphones_api_crud():
    microphones_service_mod.clear()
    a = FastAPI()
    a.include_router(microphones_router)
    client = TestClient(a)

    r = client.post("/api/microphones", json={"name": "desk-mic", "source": "usb://mic0"})
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    assert r.json()["name"] == "desk-mic"

    g = client.get("/api/microphones")
    assert g.status_code == 200
    assert len(g.json()) == 1

    one = client.get(f"/api/microphones/{mid}")
    assert one.status_code == 200
    assert one.json()["id"] == mid

    p = client.patch(f"/api/microphones/{mid}", json={"name": "desk-mic-2"})
    assert p.status_code == 200
    assert p.json()["name"] == "desk-mic-2"

    # Audio-input alias path serves the same registry.
    alias = client.get("/api/audio_inputs")
    assert alias.status_code == 200
    assert len(alias.json()) == 1

    missing = client.get("/api/microphones/nope")
    assert missing.status_code == 404

    bad = client.post("/api/microphones", json={})
    assert bad.status_code == 422

    d = client.delete(f"/api/microphones/{mid}")
    assert d.status_code == 200
    assert client.get("/api/microphones").json() == []
    microphones_service_mod.clear()


def test_microphones_api_validation():
    microphones_service_mod.clear()
    a = FastAPI()
    a.include_router(microphones_router)
    client = TestClient(a)

    missing = client.get("/api/microphones/does-not-exist")
    assert missing.status_code == 404

    missing_patch = client.patch("/api/microphones/does-not-exist", json={"name": "x"})
    assert missing_patch.status_code == 404

    missing_delete = client.delete("/api/microphones/does-not-exist")
    assert missing_delete.status_code == 404
    microphones_service_mod.clear()
