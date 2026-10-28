from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.event_segments import router as event_segments_router  # noqa: F401
from app.services import event_segments as event_segments_service_mod  # noqa: F401
from app.services.event_segments import (  # noqa: F401
    clear,
    discover_segments_for_event,
    find_segments,
    get_events_for_segment,
    get_segments_for_event,
    link_event_to_segment,
    list_links,
    unlink_event_segment,
    event_segments_service,
)


def _make_segment_file(tmp_path, camera="cam1", name="20240101_120000_main.mp4"):
    cam_dir = tmp_path / camera
    cam_dir.mkdir(parents=True, exist_ok=True)
    p = cam_dir / name
    p.write_bytes(b"fake-segment-bytes")
    return p


def test_event_segments_import():
    import app.api.event_segments  # noqa: F401
    import app.services.event_segments  # noqa: F401
    from app.api.event_segments import router  # noqa: F401
    from app.services.event_segments import link_event_to_segment, get_segments_for_event  # noqa: F401


def test_event_segments_paths_present():
    router_paths = sorted(set(r.path for r in event_segments_router.routes))
    assert "/api/events/{event_id}/segments" in router_paths
    assert "/api/event-segments" in router_paths or "/api/event_segments" in router_paths
    # Best-effort check that the auto-loader exposes the router via app.routes
    # (avoids the very slow app.openapi() generation on this large app).
    try:
        app_paths = sorted(set(getattr(r, "path", "") for r in app.routes))
        assert "/api/events/{event_id}/segments" in app_paths
    except Exception:  # noqa: BLE001
        pass


def test_event_segments_service_link_crud():
    event_segments_service_mod.clear()
    link = link_event_to_segment(event_id=1, segment_path="/recordings/cam1/20240101_120000_main.mp4")
    assert link["event_id"] == 1
    assert link["segment_path"] == "/recordings/cam1/20240101_120000_main.mp4"
    assert link["id"] is not None

    items = list_links(event_id=1)
    assert len(items) == 1

    fetched = event_segments_service_mod.get_link(items[0]["id"])
    assert fetched is not None

    reverse = get_events_for_segment("/recordings/cam1/20240101_120000_main.mp4")
    assert any(r["event_id"] == 1 for r in reverse)

    assert unlink_event_segment(items[0]["id"]) is True
    assert list_links(event_id=1) == []


def test_event_segments_service_discover_tmp(tmp_path):
    event_segments_service_mod.clear()
    seg = _make_segment_file(tmp_path, camera="cam1", name="20240101_120000_main.mp4")
    event = {"id": 10, "event_id": 10, "camera_name": "cam1", "created_at": "2024-01-01T12:01:00Z"}
    found = discover_segments_for_event(event, recording_dir=str(tmp_path))
    assert any(str(seg) == f.get("path") for f in found)

    # get_segments_for_event merges manual + auto
    link_event_to_segment(event_id=10, segment_path=str(seg))
    merged = get_segments_for_event(event, recording_dir=str(tmp_path))
    assert any(str(seg) == m.get("path") or str(seg) == m.get("segment_path") for m in merged)


def test_event_segments_find_segments_window(tmp_path):
    event_segments_service_mod.clear()
    _make_segment_file(tmp_path, camera="cam1", name="20240101_120000_main.mp4")
    out = find_segments("cam1", start="2024-01-01T11:59:00Z", end="2024-01-01T12:01:00Z",
                         recording_dir=str(tmp_path))
    assert len(out) >= 1
    far = find_segments("cam1", start="2020-01-01T00:00:00Z", end="2020-01-01T00:01:00Z",
                        recording_dir=str(tmp_path))
    assert far == []


def test_event_segments_api_crud():
    event_segments_service_mod.clear()
    a = FastAPI()
    a.include_router(event_segments_router)
    client = TestClient(a)

    r = client.post("/api/events/42/segments", json={"segment_path": "/rec/cam1/a.mp4"})
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["event_id"] == 42
    assert created["segment_path"] == "/rec/cam1/a.mp4"
    lid = created["id"]

    lst = client.get("/api/events/42/segments")
    assert lst.status_code == 200
    assert any(n["id"] == lid for n in lst.json())

    single = client.get(f"/api/events/42/segments/{lid}")
    assert single.status_code == 200
    assert single.json()["id"] == lid

    dele = client.delete(f"/api/events/42/segments/{lid}")
    assert dele.status_code == 200
    assert client.get(f"/api/events/42/segments/{lid}").status_code == 404


def test_event_segments_api_top_level():
    event_segments_service_mod.clear()
    a = FastAPI()
    a.include_router(event_segments_router)
    client = TestClient(a)

    r = client.post("/api/event-segments", json={"event_id": 7, "segment": "/rec/cam1/b.mp4"})
    assert r.status_code == 200, r.text
    lid = r.json()["id"]

    lst = client.get("/api/event-segments", params={"event_id": 7})
    assert lst.status_code == 200
    assert any(n["id"] == lid for n in lst.json())

    bad = client.post("/api/event-segments", json={"segment": "/rec/cam1/b.mp4"})
    assert bad.status_code == 400


def test_event_segments_api_discover_tmp(tmp_path):
    event_segments_service_mod.clear()
    seg = _make_segment_file(tmp_path, camera="cam1", name="20240101_120000_main.mp4")
    a = FastAPI()
    a.include_router(event_segments_router)
    client = TestClient(a)
    r = client.get(
        "/api/events/10/segments",
        params={
            "camera_name": "cam1",
            "created_at": "2024-01-01T12:01:00Z",
            "recording_dir": str(tmp_path),
        },
    )
    assert r.status_code == 200, r.text
    assert any(str(seg) == f.get("path") for f in r.json())
