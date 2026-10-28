from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.config_history import router as config_history_router
import app.services.config_history as svc


def _make_client():
    app = FastAPI()
    app.include_router(config_history_router)
    return TestClient(app)


def test_imports():
    import app.api.config_history  # noqa: F401
    import app.services.config_history  # noqa: F401
    assert config_history_router is not None
    assert hasattr(svc, "snapshot_camera")
    assert hasattr(svc, "snapshot_camera_config")
    assert hasattr(svc, "list_history")
    assert hasattr(svc, "get_version")
    assert hasattr(svc, "diff_configs")
    assert hasattr(svc, "diff_versions")
    assert hasattr(svc, "clear_history")


def test_snapshot_versions_and_dedupe():
    svc.clear_history()
    v1 = svc.snapshot_camera(camera_id="cam-1", config={"name": "Front", "fps": 15, "enabled": True})
    assert v1.get("id")
    assert v1.get("camera_id") == "cam-1"
    assert v1.get("version") == 1
    assert v1.get("changes") == []

    # Identical snapshot must not create a new version (snapshot on change).
    v1_dup = svc.snapshot_camera(camera_id="cam-1", config={"name": "Front", "fps": 15, "enabled": True})
    assert str(v1_dup.get("id")) == str(v1.get("id"))
    assert len(svc.list_history(camera_id="cam-1")) == 1

    v2 = svc.snapshot_camera("cam-1", {"name": "Front", "fps": 30, "enabled": True}, reason="raise fps")
    assert v2.get("version") == 2
    paths = {c["path"] for c in v2.get("changes", [])}
    assert "fps" in paths
    assert v2.get("reason") == "raise fps"

    items = svc.list_history(camera_id="cam-1")
    assert [e.get("version") for e in items] == [1, 2]


def test_diff_configs_pure():
    changes = svc.diff_configs({"a": 1, "b": {"x": 1}}, {"a": 2, "b": {"x": 1}, "c": True})
    by_path = {c["path"]: c for c in changes}
    assert by_path["a"]["op"] == "changed"
    assert by_path["a"]["old"] == 1 and by_path["a"]["new"] == 2
    assert by_path["c"]["op"] == "added"
    assert "b.x" not in by_path

    removed = svc.diff_configs({"a": 1, "gone": "x"}, {"a": 1})
    assert any(c["path"] == "gone" and c["op"] == "removed" for c in removed)


def test_diff_versions_and_latest():
    svc.clear_history()
    v1 = svc.snapshot_camera(camera_id="cam-9", config={"fps": 10})
    v2 = svc.snapshot_camera(camera_id="cam-9", config={"fps": 20})
    diff = svc.diff_versions(v1.get("id"), v2.get("id"))
    assert any(c["path"] == "fps" and c["new"] == 20 for c in diff)
    assert svc.latest("cam-9").get("id") == v2.get("id")
    assert svc.latest_config("cam-9")["fps"] == 20
    assert svc.get_version(v1.get("id")).get("version") == 1


def test_router_snapshot_list_diff():
    svc.clear_history()
    client = _make_client()

    r = client.post("/api/cameras/42/config/history", json={"config": {"name": "Gate", "fps": 15}})
    assert r.status_code in (200, 201), r.text
    created = r.json()
    vid = created.get("id")
    assert vid
    assert str(created.get("camera_id")) == "42"

    r2 = client.post("/api/cameras/42/config/history", json={"config": {"name": "Gate", "fps": 25}})
    assert r2.status_code in (200, 201), r2.text
    vid2 = r2.json().get("id")
    assert vid2 and vid2 != vid

    r_list = client.get("/api/cameras/42/config/history")
    assert r_list.status_code == 200, r_list.text
    assert len(r_list.json()) == 2

    r_latest = client.get("/api/cameras/42/config/history/latest")
    assert r_latest.status_code == 200, r_latest.text
    assert str(r_latest.json().get("id")) == str(vid2)

    r_get = client.get(f"/api/config/history/{vid}")
    assert r_get.status_code == 200, r_get.text

    r_diff = client.get(f"/api/config/history/{vid}/diff/{vid2}")
    assert r_diff.status_code == 200, r_diff.text
    assert any(c.get("path") == "fps" for c in r_diff.json())

    r_health = client.get("/api/config/history/health")
    assert r_health.status_code == 200
    assert r_health.json().get("status") == "ok"

    r_missing = client.get("/api/config/history/does-not-exist")
    assert r_missing.status_code == 404
