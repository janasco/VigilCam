import asyncio
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app


def test_event_stats_import():
    import app.api.event_stats  # noqa: F401
    import app.services.event_stats  # noqa: F401
    from app.api.event_stats import router  # noqa: F401
    from app.services.event_stats import build_event_stats, get_event_stats  # noqa: F401


def test_event_stats_service_by_label_top_camera():
    from app.services.event_stats import build_event_stats

    base = datetime(2026, 1, 1, 0, 0, 0)
    events = [
        {"camera_id": 1, "label": "person", "created_at": base + timedelta(hours=2)},
        {"camera_id": 1, "label": "person", "created_at": base + timedelta(hours=3)},
        {"camera_id": 2, "label": "car", "created_at": base + timedelta(hours=5)},
        {"camera_id": 1, "label": "car", "created_at": base + timedelta(hours=6)},
    ]
    result = build_event_stats(events)
    assert result["total"] == 4
    # By-label aggregates.
    assert result["by_label"]["person"] == 2
    assert result["by_label"]["car"] == 2
    # By-camera / top-camera aggregates.
    assert result["by_camera"]["1"] == 3
    assert result["by_camera"]["2"] == 1
    assert result["top_cameras"][0]["camera_id"] == 1
    assert result["top_cameras"][0]["count"] == 3
    assert any(e["label"] == "person" and e["count"] == 2 for e in result["top_labels"])


def test_event_stats_service_filters():
    from app.services.event_stats import build_event_stats

    base = datetime(2026, 1, 1, 0, 0, 0)
    events = [
        {"camera_id": 1, "label": "person", "created_at": base + timedelta(hours=1)},
        {"camera_id": 2, "label": "car", "created_at": base + timedelta(hours=1)},
        {"camera_id": 1, "label": "person", "created_at": base + timedelta(hours=9)},
    ]
    only_cam1 = build_event_stats(events, camera_id=1)
    assert only_cam1["total"] == 2
    assert only_cam1["by_camera"]["1"] == 2
    ranged = build_event_stats(
        events,
        start=(base + timedelta(hours=8)).isoformat(),
        end=(base + timedelta(hours=10)).isoformat(),
    )
    assert ranged["total"] == 1
    assert ranged["by_label"]["person"] == 1


def test_event_stats_service_async_no_db():
    from app.services.event_stats import get_event_stats

    data = asyncio.run(get_event_stats(None))
    assert isinstance(data, dict)
    assert data["total"] == 0
    assert data["by_label"] == {}
    assert data["by_camera"] == {}
    assert data["top_cameras"] == []


def test_event_stats_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/event-stats" in paths or "/api/events/stats" in paths
    assert any("event-stats" in p or "event_stats" in p for p in paths)


def test_event_stats_api_returns_payload():
    client = TestClient(app)
    paths = list(app.openapi()["paths"].keys())
    prefix = None
    for cand in ("/api/event-stats", "/api/events/stats", "/api/event_stats"):
        if cand in paths:
            prefix = cand
            break
    assert prefix is not None, paths
    r = client.get(prefix)
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, dict)
    assert "by_label" in data or "top_labels" in data
    assert "by_camera" in data or "top_cameras" in data
    assert "total" in data or "count" in data

    r2 = client.get(prefix, params={"camera_id": 1, "hours": 24})
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), dict)
