from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.saved_search import router as saved_search_router  # noqa: F401
from app.services import saved_filters as saved_filters_mod  # noqa: F401
from app.services import saved_search as saved_search_mod  # noqa: F401
from app.services.saved_search import (  # noqa: F401
    execute,
    execute_async,
    filter_event_list,
    get_last_run,
    match_event,
    resolve_criteria,
    run_all,
    saved_search_service,
)


def _events():
    return [
        {"id": 1, "camera_id": 1, "label": "person", "kind": "detection",
         "confidence": 0.9, "tags": ["door"], "created_at": "2026-01-01T10:00:00"},
        {"id": 2, "camera_id": 2, "label": "vehicle", "kind": "detection",
         "confidence": 0.4, "tags": ["gate"], "created_at": "2026-01-02T10:00:00"},
        {"id": 3, "camera_id": 1, "label": "person", "kind": "detection",
         "confidence": 0.7, "tags": ["door", "night"], "created_at": "2026-01-03T10:00:00"},
    ]


def test_saved_search_import():
    import app.api.saved_search  # noqa: F401
    import app.services.saved_search  # noqa: F401
    from app.api.saved_search import router  # noqa: F401
    from app.services.saved_search import execute, execute_async, run_all  # noqa: F401


def test_saved_search_router_paths_present():
    paths = sorted({getattr(r, "path", "") for r in saved_search_router.routes})
    assert "/api/saved-search/{filter_id}/run" in paths
    assert "/api/saved_search/{filter_id}/run" in paths
    # execution aliases under the new namespace
    assert "/api/saved-search/{filter_id}/execute" in paths
    assert "/api/saved-search/{filter_id}/events" in paths
    # legacy execution suffix under the saved-filters prefix (CRUD router has no /run)
    assert "/api/saved-filters/{filter_id}/run" in paths


def test_service_execute_label_filter():
    saved_filters_mod.clear()
    saved_search_mod.clear_runs()
    f = saved_filters_mod.create_filter(name="persons", criteria={"label": "person"})
    res = execute(f["id"], events=_events())
    assert res["count"] == 2
    assert {e["id"] for e in res["events"]} == {1, 3}
    assert res["criteria"].get("label") == "person"
    assert res["filter"]["id"] == f["id"]
    # results mirrors events
    assert res["results"] == res["events"]


def test_service_execute_camera_confidence_and_limit():
    saved_filters_mod.clear()
    f = saved_filters_mod.create_filter(
        name="cam1 confident", criteria={"camera_id": 1, "min_confidence": 0.8})
    res = execute(f["id"], events=_events())
    assert res["count"] == 1
    assert res["events"][0]["id"] == 1
    # execution-time overrides merge over stored criteria
    res2 = execute(f["id"], events=_events(), min_confidence=0.5)
    assert res2["count"] == 2
    # limit / order overrides
    f2 = saved_filters_mod.create_filter(name="all persons", criteria={"label": "person"})
    res3 = execute(f2["id"], events=_events(), limit=1, order="asc")
    assert res3["count"] == 1
    assert res3["events"][0]["id"] == 1


def test_service_execute_by_name_and_errors():
    saved_filters_mod.clear()
    saved_filters_mod.create_filter(name="door events", criteria={"tags": ["door"]})
    res = execute("door events", events=_events())
    assert res["count"] == 2
    try:
        execute("does-not-exist", events=_events())
    except LookupError:
        pass
    else:
        raise AssertionError("expected LookupError for unknown filter")
    try:
        execute(events=_events())
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for missing filter ref")


def test_service_run_all_and_last_run():
    saved_filters_mod.clear()
    saved_search_mod.clear_runs()
    f1 = saved_filters_mod.create_filter(name="persons", criteria={"label": "person"})
    f2 = saved_filters_mod.create_filter(name="vehicles", criteria={"label": "vehicle"})
    out = run_all([f1["id"], f2["id"]], events=_events())
    assert out["counts"][str(f1["id"])] == 2
    assert out["counts"][str(f2["id"])] == 1
    assert out["total"] == 3
    assert get_last_run(f1["id"]) is not None
    assert get_last_run(f1["id"])["count"] == 2


def _client():
    a = FastAPI()
    a.include_router(saved_search_router)
    return TestClient(a)


def test_api_run_with_events_override():
    saved_filters_mod.clear()
    f = saved_filters_mod.create_filter(name="door filter", criteria={"tags": ["door"]})
    client = _client()
    r = client.post(f"/api/saved-search/{f['id']}/run", json={"events": _events()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert {e["id"] for e in body["events"]} == {1, 3}
    assert body["criteria"].get("tags") == ["door"]


def test_api_run_get_and_aliases():
    saved_filters_mod.clear()
    f = saved_filters_mod.create_filter(name="cam1", criteria={"camera_id": 1})
    client = _client()
    r = client.post(f"/api/saved-search/{f['id']}/execute", json={"events": _events()})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2
    r2 = client.post(f"/api/saved_search/{f['id']}/events", json={"events": _events()})
    assert r2.status_code == 200, r2.text
    assert r2.json()["count"] == 2
    # legacy prefix execution (CRUD router defines no /run suffix)
    r3 = client.post(f"/api/saved-filters/{f['id']}/run", json={"events": _events()})
    assert r3.status_code == 200, r3.text
    assert r3.json()["count"] == 2


def test_api_run_errors():
    saved_filters_mod.clear()
    client = _client()
    missing = client.get("/api/saved-search/999999/run")
    assert missing.status_code == 404
    f = saved_filters_mod.create_filter(name="x", criteria={"label": "person"})
    assert f["id"] is not None
    # no events + real DB session -> empty but 200 (no seed events guaranteed by envelope)
    ok = client.post(f"/api/saved-search/{f['id']}/run", json={"events": []})
    assert ok.status_code == 200
    assert ok.json()["count"] == 0
