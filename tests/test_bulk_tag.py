"""Bulk event tagging tests: service + router.

Verification: ``app/api/events.py`` only tags a single event
(``POST /api/events/{event_id}/tags`` + ``/tag`` alias); there is no bulk
tagging endpoint there -- only bulk *delete*. This suite covers the new
``app/services/bulk_tag.py`` + ``app/api/bulk_tag.py`` bulk-tagging layer.
"""

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.bulk_tag import router as bulk_tag_router  # noqa: F401
from app.services import bulk_tag as bulk_tag_mod  # noqa: F401
from app.services.bulk_tag import (  # noqa: F401
    apply_tags,
    bulk_add_event_tags,
    bulk_clear_event_tags,
    bulk_remove_event_tags,
    bulk_set_event_tags,
    bulk_tag_events,
    coerce_id_list,
    normalize_mode,
    normalize_tag,
    normalize_tags,
)


def test_bulk_tag_imports():
    import app.api.bulk_tag  # noqa: F401
    import app.services.bulk_tag  # noqa: F401
    from app.api.bulk_tag import router  # noqa: F401
    from app.services.bulk_tag import bulk_tag_events, normalize_tags  # noqa: F401
    assert router is not None


def test_bulk_tag_paths_present():
    # Fast path: router itself exposes the bulk endpoints (no slow openapi).
    route_paths = [getattr(r, "path", "") for r in bulk_tag_router.routes]
    assert "/api/events/bulk-tag" in route_paths
    assert "/api/events/bulk-set-tags" in route_paths
    assert "/api/events/bulk-remove-tags" in route_paths
    assert "/api/events/bulk-clear-tags" in route_paths
    # App auto-loader picks up app/api/bulk_tag.py without main.py edits:
    # TestClient(app) serves the bulk endpoint (proves auto-registration).
    client = TestClient(app)
    r = client.post("/api/events/bulk-tag", json={"tags": ["x"]})
    # missing ids -> 400 proves the route exists on the app (not 404).
    assert r.status_code == 400, r.text


def test_bulk_tag_service_normalize():
    assert normalize_tags("front, Front ,  ,back") == ["front", "back"]
    assert normalize_tags(["a", "A", "b"]) == ["a", "b"]
    assert normalize_tags('["x", "y"]') == ["x", "y"]
    assert normalize_tags({"tags": ["p", "p", None]}) == ["p"]
    assert normalize_tags(None) == []
    assert normalize_tag("  Lobby  ") == "Lobby"
    assert normalize_tag("none") is None
    assert normalize_tag("") is None
    assert coerce_id_list([1, "2", "x", None]) == [1, 2]
    assert coerce_id_list("3,4") == [3, 4]
    assert normalize_mode("APPEND") == "add"
    assert normalize_mode("replace") == "set"
    assert normalize_mode("DELETE") == "remove"
    assert normalize_mode("wipe") == "clear"


def test_bulk_tag_apply_modes():
    assert apply_tags(["a"], ["b"], mode="add") == ["a", "b"]
    assert apply_tags(["a", "b"], ["b", "c"], mode="add") == ["a", "b", "c"]
    assert apply_tags(["a", "old"], ["b"], mode="set") == ["b"]
    assert apply_tags(["a", "b"], ["A"], mode="remove") == ["b"]
    assert apply_tags(["a", "b"], [], mode="clear") == []


def test_bulk_tag_service_aliases():
    assert bulk_tag_mod.bulk_add is bulk_add_event_tags
    assert bulk_tag_mod.bulk_set is bulk_set_event_tags
    assert bulk_tag_mod.bulk_remove is bulk_remove_event_tags
    assert bulk_tag_mod.bulk_clear is bulk_clear_event_tags


def test_bulk_tag_router_validation():
    a = FastAPI()
    a.include_router(bulk_tag_router)
    client = TestClient(a)
    # missing event_ids -> 400 without touching the DB
    r = client.post("/api/events/bulk-tag", json={"tags": ["x"]})
    assert r.status_code == 400, r.text
    # missing tags for add -> 400
    r2 = client.post("/api/events/bulk-tag", json={"event_ids": [1]})
    assert r2.status_code == 400, r2.text


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _make_events(client: TestClient, count: int = 2):
    cname = _unique("bulktagcam")
    cr = client.post("/api/cameras", json={"name": cname, "kind": "usb", "source": "0"})
    assert cr.status_code == 201, cr.text
    cid = cr.json()["id"]
    try:
        dr = client.post(f"/api/cameras/{cid}/detect", json={"simulate": True})
        assert dr.status_code == 200, dr.text
        events = client.get("/api/events").json()
        assert len(events) >= 1
        # newest first; take the first `count` ids
        ids = [e["id"] for e in events[:count]]
        assert len(ids) >= 1
        return cid, ids
    except Exception:
        try:
            client.delete(f"/api/cameras/{cid}")
        except Exception:  # noqa: BLE001
            pass
        raise


def test_bulk_tag_add_set_remove_clear_flow():
    client = TestClient(app)
    cid, ids = _make_events(client, count=2)
    try:
        # add
        r = client.post("/api/events/bulk-tag", json={"event_ids": ids, "tags": ["Lobby", "indoor"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("updated_count") == len(ids)
        assert sorted(body.get("updated_event_ids", [])) == sorted(ids)

        # underscore alias also works (additive merge)
        r2 = client.post("/api/events/bulk_tag", json={"ids": ids, "tags": "entrance"})
        assert r2.status_code == 200, r2.text

        events = {e["id"]: e for e in client.get("/api/events").json()}
        for eid in ids:
            tags = [t.lower() for t in (events[eid].get("tags") or [])]
            assert "lobby" in tags
            assert "entrance" in tags

        # set / replace
        rs = client.post("/api/events/bulk-set-tags", json={"event_ids": ids, "tags": ["only"]})
        assert rs.status_code == 200, rs.text
        events = {e["id"]: e for e in client.get("/api/events").json()}
        for eid in ids:
            assert [t.lower() for t in (events[eid].get("tags") or [])] == ["only"]

        # remove
        rr = client.post("/api/events/bulk-remove-tags", json={"event_ids": ids, "tags": ["ONLY"]})
        assert rr.status_code == 200, rr.text
        events = {e["id"]: e for e in client.get("/api/events").json()}
        for eid in ids:
            assert events[eid].get("tags") in (None, [])

        # add again then clear
        ra = client.post("/api/events/bulk-tag", json={"event_ids": ids, "tags": ["a", "b"]})
        assert ra.status_code == 200, ra.text
        rc = client.post("/api/events/bulk-clear-tags", json={"event_ids": ids})
        assert rc.status_code == 200, rc.text
        assert rc.json().get("updated_count") == len(ids)
        events = {e["id"]: e for e in client.get("/api/events").json()}
        for eid in ids:
            assert events[eid].get("tags") in (None, [])
    finally:
        try:
            client.delete(f"/api/cameras/{cid}")
        except Exception:  # noqa: BLE001
            pass


def test_bulk_tag_missing_ids_reported():
    client = TestClient(app)
    cid, ids = _make_events(client, count=1)
    try:
        real = ids[0]
        fake = 999999999
        r = client.post("/api/events/bulk-tag", json={"event_ids": [real, fake], "tags": ["x"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert real in body.get("updated_event_ids", [])
        assert fake in body.get("missing_event_ids", []) or fake in body.get("not_found", [])
    finally:
        try:
            client.delete(f"/api/cameras/{cid}")
        except Exception:  # noqa: BLE001
            pass
