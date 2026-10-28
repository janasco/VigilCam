import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.services import preset_library
from app.services import preset_snaps
from app.api.preset_snaps import router as preset_snaps_router  # noqa: F401
from app.services.preset_snaps import (  # noqa: F401
    attach_snapshot,
    capture_preset_snapshot,
    clear,
    count,
    delete_preset_snapshot,
    get_preset_snapshot,
    list_preset_snapshots,
    preset_snaps_service,
    save_preset_with_snapshot,
)


def _reset():
    preset_snaps.clear()
    preset_library.clear()


def test_preset_snaps_imports():
    import app.services.preset_snaps  # noqa: F401
    import app.api.preset_snaps  # noqa: F401
    from app.services.preset_snaps import PresetSnapsService, capture_preset_snapshot, list_preset_snapshots  # noqa: F401
    from app.api.preset_snaps import router  # noqa: F401
    assert router is not None


def test_preset_library_has_no_visual_thumbnails():
    # Verify the gap this feature fills: preset_library stores names/tokens
    # with no snapshot/thumbnail/image fields.
    _reset()
    created = preset_library.create_preset(camera_id=1, name="door", preset_token="tok-1")
    for field in ("snapshot_path", "thumbnail_base64", "snapshot_base64", "image", "data_url", "thumbnail"):
        assert field not in created, f"preset_library should not store {field}"


def test_capture_attaches_snapshot_when_saving_preset():
    _reset()
    created = preset_library.create_preset(camera_id=11, name="gate")
    snap = capture_preset_snapshot(preset_id=created["id"])
    assert isinstance(snap, dict)
    assert snap.get("preset_id") == created["id"]
    assert snap.get("ok") is True
    # Visual thumbnail present.
    assert snap.get("snapshot_path")
    assert snap.get("thumbnail_base64")
    assert snap.get("data_url", "").startswith("data:image/")
    raw = base64.b64decode(snap["thumbnail_base64"])
    assert raw[:2] == b"\xff\xd8"

    # Attached back onto the preset-library entry.
    fetched = preset_library.get_preset(created["id"])
    assert fetched is not None
    assert fetched.get("snapshot_path") == snap["snapshot_path"]
    assert fetched.get("thumbnail_base64") == snap["thumbnail_base64"]


def test_save_preset_with_snapshot_creates_preset():
    _reset()
    snap = save_preset_with_snapshot(camera_id=12, name="lobby")
    assert snap.get("preset_id")
    assert snap.get("snapshot_path")
    # Preset was auto-created.
    fetched = preset_library.get_preset(snap["preset_id"])
    assert fetched is not None
    assert fetched.get("name") == "lobby"
    assert fetched.get("snapshot_path")


def test_attach_custom_image_bytes():
    _reset()
    created = preset_library.create_preset(camera_id=13, name="dock")
    # Minimal custom JPEG bytes.
    custom = b"\xff\xd8\xff" + b"\x00" * 64
    snap = attach_snapshot(preset_id=created["id"], image_base64=base64.b64encode(custom).decode("ascii"))
    assert snap.get("method") == "attached"
    stored = preset_snaps.get_snapshot_bytes(created["id"])
    assert stored is not None
    assert bytes(stored) == custom


def test_service_list_get_delete_clear():
    _reset()
    a = save_preset_with_snapshot(camera_id=21, name="a")
    b = save_preset_with_snapshot(camera_id=21, name="b")
    assert count() == 2
    assert len(list_preset_snapshots(camera_id=21)) == 2
    assert len(list_preset_snapshots(camera_id=999)) == 0
    assert get_preset_snapshot(a["preset_id"]) is not None
    assert get_preset_snapshot("missing") is None
    assert preset_snaps.has_snapshot(a["preset_id"]) is True
    assert delete_preset_snapshot(a["preset_id"]) is True
    assert get_preset_snapshot(a["preset_id"]) is None
    assert delete_preset_snapshot("missing") is False
    assert b["preset_id"] in [x["preset_id"] for x in list_preset_snapshots()]
    n = clear()
    assert n == 1
    assert count() == 0


def test_preset_snaps_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/preset_snaps" in paths
    assert any("preset" in p and "snap" in p for p in paths)


def test_preset_snaps_endpoints():
    _reset()
    client = TestClient(app)
    # Capture while saving a new preset (camera_id + name, no preset_id yet).
    r = client.post("/api/preset_snaps/capture", json={"camera_id": 31, "name": "front"})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    pid = body.get("preset_id") or body.get("id")
    assert pid is not None
    assert body.get("snapshot_path")
    assert body.get("thumbnail_base64")

    # List + detail.
    r2 = client.get("/api/preset_snaps")
    assert r2.status_code == 200
    assert isinstance(r2.json(), list)
    assert any(x.get("preset_id") == pid for x in r2.json())

    r3 = client.get(f"/api/preset_snaps/{pid}")
    assert r3.status_code == 200
    assert r3.json().get("preset_id") == pid

    # Visual bytes endpoint.
    img = client.get(f"/api/preset_snaps/{pid}/image")
    assert img.status_code == 200
    assert img.headers["content-type"].startswith("image/")
    assert img.content[:2] == b"\xff\xd8"

    # Preset-library convenience route serves the same snapshot.
    lib = client.get(f"/api/preset_library/{pid}/snapshot")
    assert lib.status_code == 200
    assert lib.json().get("preset_id") == pid

    # Missing snapshot -> 404.
    assert client.get("/api/preset_snaps/does-not-exist").status_code == 404

    # Delete.
    d = client.delete(f"/api/preset_snaps/{pid}")
    assert d.status_code == 200
    assert client.get(f"/api/preset_snaps/{pid}").status_code == 404


def test_preset_snaps_isolated_router():
    _reset()
    isolated = FastAPI()
    from app.api.preset_snaps import router

    isolated.include_router(router)
    client = TestClient(isolated)

    r = client.post("/api/preset-snaps/capture", json={"camera_id": 32, "name": "side"})
    assert r.status_code in (200, 201), r.text
    pid = r.json().get("preset_id")
    assert pid

    g = client.get("/api/preset-snaps")
    assert g.status_code == 200
    assert any(x.get("preset_id") == pid for x in g.json())

    bad = client.post("/api/preset-snaps/capture", json={})
    assert bad.status_code in (400, 404)
