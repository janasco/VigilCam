from fastapi.testclient import TestClient

from app.main import app  # noqa: F401 - ensures auto-loader picks up router
from app.api.activity_log import router as activity_log_router  # noqa: F401
from app.services.activity_log import (  # noqa: F401
    ActivityLogEntry,
    ActivityLogStore,
    UserActivityService,
    ActivityLogService,
    log_activity,
    list_activity_logs,
    new_activity_id,
)


def test_activity_log_import():
    import app.api.activity_log  # noqa: F401
    import app.services.activity_log  # noqa: F401


def test_activity_log_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any(p.startswith("/api/activity") or p.startswith("/api/user-activity") for p in paths)


def test_activity_log_append_and_list(tmp_path):
    from fastapi import FastAPI
    a = FastAPI()
    a.include_router(activity_log_router)
    client = TestClient(a)
    r = client.post("/api/activity-log", json={"action": "login", "actor": "admin"})
    assert r.status_code in (200, 201), r.text
    entry = r.json()
    assert entry["action"] == "login"

    lst = client.get("/api/activity-log")
    assert lst.status_code == 200
    data = lst.json()
    assert isinstance(data, list)
    assert any(e.get("action") == "login" for e in data)


def test_activity_log_alias_paths():
    from fastapi import FastAPI
    a = FastAPI()
    a.include_router(activity_log_router)
    client = TestClient(a)
    r = client.post("/api/activity_log", json={"action": "camera.view", "username": "bob", "resource_type": "camera"})
    assert r.status_code in (200, 201), r.text
    assert r.json()["action"] == "camera.view"
    lst = client.get("/api/user-activity")
    assert lst.status_code == 200


def test_activity_log_store_file(tmp_path):
    p = tmp_path / "activity.jsonl"
    store = ActivityLogStore(path=str(p))
    ev = store.append({"action": "logout", "actor": "alice"})
    assert ev["action"] == "logout"
    assert p.exists()
    assert len(store.read_all()) == 1


def test_activity_log_service_helpers():
    from app.services import activity_log as mod
    store = ActivityLogStore()
    mod.log_activity(store, action="view", actor="tester", resource_type="camera", resource_id="1")
    entries = mod.list_activity_logs(store, action="view")
    assert len(entries) >= 1
    svc = UserActivityService(store=store)
    assert len(svc.read_all()) >= 1
