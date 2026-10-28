from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.saved_filters import router as saved_filters_router  # noqa: F401
from app.services import saved_filters as saved_filters_service_mod  # noqa: F401
from app.services.saved_filters import (  # noqa: F401
    create_filter,
    list_filters,
    get_filter,
    update_filter,
    delete_filter,
    clear,
    saved_filters_service,
)


def test_saved_filters_import():
    import app.api.saved_filters  # noqa: F401
    import app.services.saved_filters  # noqa: F401
    from app.api.saved_filters import router  # noqa: F401
    from app.services.saved_filters import create_filter, list_filters  # noqa: F401


def test_saved_filters_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/saved-filters" in paths
    assert "/api/saved_filters" in paths or "/api/event-filters" in paths


def test_saved_filters_service_crud():
    saved_filters_service_mod.clear()
    created = create_filter(name="motion only", criteria={"label": "motion"})
    assert created["id"] is not None
    assert created["name"] == "motion only"
    assert created["criteria"].get("label") == "motion"

    items = list_filters()
    assert len(items) == 1

    fetched = get_filter(created["id"])
    assert fetched is not None
    assert fetched["name"] == "motion only"

    updated = update_filter(created["id"], description="updated desc")
    assert updated is not None
    assert updated["description"] == "updated desc"
    # Criteria preserved across unrelated update.
    assert updated["criteria"].get("label") == "motion"

    assert delete_filter(created["id"]) is True
    assert get_filter(created["id"]) is None
    assert list_filters() == []


def test_saved_filters_service_validation():
    saved_filters_service_mod.clear()
    try:
        create_filter(criteria={"label": "x"})
    except ValueError as exc:
        assert "name" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError for missing name")


def test_saved_filters_api_crud():
    saved_filters_service_mod.clear()
    a = FastAPI()
    a.include_router(saved_filters_router)
    client = TestClient(a)

    r = client.post("/api/saved-filters", json={"name": "door events", "criteria": {"label": "door"}})
    assert r.status_code in (200, 201), r.text
    created = r.json()
    assert created["name"] == "door events"
    fid = created["id"]

    lst = client.get("/api/saved-filters")
    assert lst.status_code == 200
    assert any(f["id"] == fid for f in lst.json())

    single = client.get(f"/api/saved-filters/{fid}")
    assert single.status_code == 200
    assert single.json()["id"] == fid

    upd = client.put(f"/api/saved-filters/{fid}", json={"description": "renamed"})
    assert upd.status_code == 200
    assert upd.json()["description"] == "renamed"

    qry = client.get(f"/api/saved-filters/{fid}/query")
    assert qry.status_code == 200
    assert qry.json()["criteria"].get("label") == "door"

    dele = client.delete(f"/api/saved-filters/{fid}")
    assert dele.status_code == 200
    assert client.get(f"/api/saved-filters/{fid}").status_code == 404


def test_saved_filters_api_aliases_and_errors():
    saved_filters_service_mod.clear()
    a = FastAPI()
    a.include_router(saved_filters_router)
    client = TestClient(a)

    r = client.post("/api/saved_filters", json={"name": "alias check", "camera_id": 3})
    assert r.status_code in (200, 201), r.text
    fid = r.json()["id"]

    lst = client.get("/api/event-filters")
    assert lst.status_code == 200
    assert any(f["id"] == fid for f in lst.json())

    bad = client.post("/api/saved-filters", json={"criteria": {"label": "x"}})
    assert bad.status_code == 400

    missing = client.get("/api/saved-filters/999999")
    assert missing.status_code == 404
