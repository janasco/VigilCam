import asyncio
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app


def test_heatmap_import():
    import app.api.heatmap  # noqa: F401
    import app.services.heatmap  # noqa: F401
    from app.api.heatmap import router  # noqa: F401
    from app.services.heatmap import build_heatmap, get_heatmap  # noqa: F401


def test_heatmap_service_per_hour_per_camera():
    from app.services.heatmap import build_heatmap

    base = datetime(2026, 1, 1, 0, 0, 0)
    events = [
        {"camera_id": 1, "created_at": base + timedelta(hours=2, minutes=5)},
        {"camera_id": 1, "created_at": base + timedelta(hours=2, minutes=40)},
        {"camera_id": 2, "created_at": base + timedelta(hours=5)},
        {"camera_id": 1, "created_at": base + timedelta(hours=5, minutes=10)},
    ]
    result = build_heatmap(events)
    assert result["total"] == 4
    assert result["per_hour"]["2"] == 2
    assert result["per_hour"]["5"] == 2
    assert result["per_camera"]["1"] == 3
    assert result["per_camera"]["2"] == 1
    assert result["per_camera_per_hour"]["1"]["2"] == 2
    assert result["per_camera_per_hour"]["2"]["5"] == 1
    assert len(result["hourly"]) == 24


def test_heatmap_service_filters():
    from app.services.heatmap import build_heatmap

    base = datetime(2026, 1, 1, 0, 0, 0)
    events = [
        {"camera_id": 1, "created_at": base + timedelta(hours=1)},
        {"camera_id": 2, "created_at": base + timedelta(hours=1)},
        {"camera_id": 1, "created_at": base + timedelta(hours=9)},
    ]
    only_cam1 = build_heatmap(events, camera_id=1)
    assert only_cam1["total"] == 2
    assert only_cam1["per_camera"]["1"] == 2
    ranged = build_heatmap(
        events,
        start=(base + timedelta(hours=8)).isoformat(),
        end=(base + timedelta(hours=10)).isoformat(),
    )
    assert ranged["total"] == 1
    assert ranged["per_hour"]["9"] == 1


def test_heatmap_service_async_no_db():
    from app.services.heatmap import get_heatmap

    data = asyncio.run(get_heatmap(None))
    assert isinstance(data, dict)
    assert data["total"] == 0
    assert len(data["hourly"]) == 24
    assert data["per_hour"]["0"] == 0


def test_heatmap_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/heatmap" in paths or "/api/heatmaps" in paths
    assert any("heatmap" in p for p in paths)


def test_heatmap_api_returns_payload():
    client = TestClient(app)
    paths = list(app.openapi()["paths"].keys())
    prefix = None
    for cand in ("/api/heatmap", "/api/heatmaps", "/api/events/heatmap"):
        if cand in paths:
            prefix = cand
            break
    assert prefix is not None, paths
    r = client.get(prefix)
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, dict)
    assert "per_hour" in data or "hourly" in data
    assert "per_camera" in data or "cameras" in data
    assert "total" in data or "count" in data

    r2 = client.get(prefix, params={"camera_id": 1, "hours": 24})
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), dict)
