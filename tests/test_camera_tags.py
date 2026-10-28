"""Camera tagging tests: service + router, plus group-vs-tags verification."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app

from app.api.camera_tags import router as camera_tags_router  # noqa: F401
from app.services import camera_tags as camera_tags_mod  # noqa: F401
from app.services.camera_tags import (  # noqa: F401
    add_tags,
    cameras_with_tag,
    clear,
    clear_tags,
    find_cameras_by_tags,
    get_tags,
    has_tag,
    list_all_tags,
    normalize_tag,
    normalize_tags,
    remove_tags,
    set_tags,
    tag_counts,
)


def test_camera_tags_import():
    import app.api.camera_tags  # noqa: F401
    import app.services.camera_tags  # noqa: F401
    from app.api.camera_tags import router  # noqa: F401
    from app.services.camera_tags import (  # noqa: F401
        get_tags, set_tags, add_tags, remove_tags, clear_tags,
    )


def test_cameras_have_only_group_string_no_tag_lists():
    """VigilCam cameras carry group/group_id; tag lists live on events only."""
    from app.models.entities import Camera

    cols = set(Camera.__table__.columns.keys())
    assert "group" in cols
    assert "group_id" in cols
    assert "tags" not in cols
    assert "tag" not in cols

    from app.schemas import CameraBase

    fields = set(CameraBase.model_fields.keys())
    assert "group" in fields
    assert "group_id" in fields
    assert "tags" not in fields
    assert "tag" not in fields


def test_camera_tags_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/cameras/{camera_id}/tags" in paths
    assert "/api/camera-tags" in paths or "/api/camera_tags" in paths


def test_camera_tags_service_normalize():
    assert normalize_tags("front, Front ,  ,back") == ["front", "back"]
    assert normalize_tags(["a", "A", "b"]) == ["a", "b"]
    assert normalize_tags('["x", "y"]') == ["x", "y"]
    assert normalize_tags({"tags": ["p", "p", None]}) == ["p"]
    assert normalize_tags(None) == []
    assert normalize_tag("  Lobby  ") == "Lobby"
    assert normalize_tag("none") is None
    assert normalize_tag("") is None


def test_camera_tags_service_crud():
    camera_tags_mod.clear()
    assert set_tags(1, ["Front Door", "outdoor", "front door"]) == ["Front Door", "outdoor"]
    assert get_tags(1) == ["Front Door", "outdoor"]
    assert has_tag(1, "front door") is True
    assert has_tag(1, "missing") is False

    assert add_tags(1, "garage, outdoor") == ["Front Door", "outdoor", "garage"]
    assert remove_tags(1, ["OUTDOOR"]) == ["Front Door", "garage"]
    assert set_tags(1, []) == []
    assert get_tags(1) == []


def test_camera_tags_service_lookup():
    camera_tags_mod.clear()
    set_tags(11, ["front", "outdoor"])
    set_tags(12, ["back", "outdoor"])
    set_tags(13, ["front", "back"])

    assert sorted(cameras_with_tag("OUTDOOR")) == [11, 12]
    assert sorted(find_cameras_by_tags(["front", "back"], match="any")) == [11, 12, 13]
    assert find_cameras_by_tags(["front", "back"], match="all") == [13]

    counts = tag_counts()
    assert counts.get("outdoor") == 2 or counts.get("Outdoor") == 2
    assert "outdoor" in [t.lower() for t in list_all_tags()]

    assert clear_tags(11) == 2
    assert get_tags(11) == []
    camera_tags_mod.clear()


def test_camera_tags_api_crud():
    camera_tags_mod.clear()
    a = FastAPI()
    a.include_router(camera_tags_router)
    client = TestClient(a)

    r = client.put("/api/cameras/42/tags", json={"tags": ["Lobby", "indoor"]})
    assert r.status_code == 200, r.text
    assert r.json()["tags"] == ["Lobby", "indoor"]

    g = client.get("/api/cameras/42/tags")
    assert g.status_code == 200
    assert g.json()["tags"] == ["Lobby", "indoor"]

    add = client.post("/api/cameras/42/tags/add", json={"tag": "entrance"})
    assert add.status_code == 200, add.text
    assert "entrance" in [t.lower() for t in add.json()["tags"]]

    rem = client.post("/api/cameras/42/tags/remove", json={"tags": ["INDOOR"]})
    assert rem.status_code == 200
    assert "indoor" not in [t.lower() for t in rem.json()["tags"]]

    top = client.get("/api/camera-tags")
    assert top.status_code == 200, top.text
    assert "lobby" in [t.lower() for t in top.json()["tags"]]

    by_tag = client.get("/api/cameras/by-tag", params={"tag": "lobby"})
    assert by_tag.status_code == 200
    assert 42 in by_tag.json()["camera_ids"]

    q = client.post("/api/camera-tags/query", json={"tags": ["lobby"], "match": "any"})
    assert q.status_code == 200
    assert 42 in q.json()["camera_ids"]

    scoped = client.get("/api/camera-tags/lobby/cameras")
    assert scoped.status_code == 200
    assert 42 in scoped.json()["camera_ids"]

    wiped = client.delete("/api/cameras/42/tags")
    assert wiped.status_code == 200
    assert wiped.json()["tags"] == []
    assert client.get("/api/cameras/42/tags").json()["tags"] == []
    camera_tags_mod.clear()


def test_camera_tags_api_aliases_and_validation():
    camera_tags_mod.clear()
    a = FastAPI()
    a.include_router(camera_tags_router)
    client = TestClient(a)

    # comma-separated string + trailing-slash-tolerant replace alias
    r = client.post("/api/cameras/7/tag", json={"tags": "a, b, a"})
    assert r.status_code == 200, r.text
    assert r.json()["tags"] == ["a", "b"]

    # add via query params
    r2 = client.post("/api/cameras/7/tags/add?tag=extra")
    assert r2.status_code == 200, r2.text
    assert "extra" in r2.json()["tags"]

    # missing camera id on top-level style misuse -> 400/404, not 500
    bad = client.get("/api/cameras/ /tags")
    assert bad.status_code in (400, 404, 422)

    camera_tags_mod.clear()
