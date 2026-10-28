import os
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.storage_forecast import router as storage_forecast_router


def test_storage_forecast_import():
    import app.services.storage_forecast  # noqa: F401
    import app.api.storage_forecast  # noqa: F401
    from app.services.storage_forecast import (  # noqa: F401
        StorageForecastService,
        storage_forecast,
        forecast,
        predict,
        get_forecast,
    )
    from app.api.storage_forecast import router  # noqa: F401


def test_storage_forecast_router_auto_registered():
    # app.main auto-registers every app.api.* module exposing `router`
    # (no app/main.py edit needed). Check discoverability without building
    # the giant app's OpenAPI schema, which is very slow on this large app.
    import pkgutil

    names = [m.name for m in pkgutil.iter_modules(__import__("app.api", fromlist=["__path__"]).__path__)]
    assert "storage_forecast" in names
    assert len(storage_forecast_router.routes) > 0
    paths = {getattr(r, "path", None) for r in storage_forecast_router.routes}
    assert "/api/storage/forecast" in paths or "/api/storage-forecast" in paths


def _make_client():
    tapp = FastAPI()
    tapp.include_router(storage_forecast_router)
    return TestClient(tapp)


def _seed_two_cams(root):
    cam_a = root / "cam_a"
    cam_b = root / "cam_b"
    cam_a.mkdir(parents=True)
    cam_b.mkdir(parents=True)
    now = time.time()
    f1 = cam_a / "recent.mp4"
    f1.write_bytes(b"x" * 100)
    os.utime(f1, (now, now))
    f2 = cam_a / "older.mp4"
    f2.write_bytes(b"y" * 50)
    old = now - 3 * 86400
    os.utime(f2, (old, old))
    f3 = cam_b / "b1.mp4"
    f3.write_bytes(b"z" * 200)
    os.utime(f3, (now, now))
    return cam_a, cam_b


def test_storage_forecast_service_bytes_share_growth(tmp_path):
    from app.services.storage_forecast import StorageForecastService

    _seed_two_cams(tmp_path)
    svc = StorageForecastService(storage_root=str(tmp_path))
    result = svc.forecast(days=7, capacity_bytes=10 * 1024)
    assert result.get("ok") is True
    cams = result.get("cameras") or result.get("per_camera") or []
    assert len(cams) == 2
    by_name = {c.get("camera_name"): c for c in cams}
    assert by_name["cam_a"].get("total_bytes") == 150
    assert by_name["cam_b"].get("total_bytes") == 200
    assert result.get("total_bytes") == 350

    for c in cams:
        assert "share_percent" in c or "share" in c
        assert "growth_bytes_per_day" in c or "growth_per_day" in c
        assert isinstance(c.get("daily_bytes") or c.get("sparkline"), list)
        assert len(c.get("daily_bytes") or []) == 7

    total_share = sum(float(c.get("share_percent", c.get("share") or 0)) for c in cams)
    assert abs(total_share - 100.0) < 0.05

    # Global projection present with explicit capacity.
    assert result.get("days_until_full") is not None
    assert float(result.get("days_until_full")) > 0
    assert result.get("growth_bytes_per_day") is not None
    assert float(result.get("growth_bytes_per_day")) > 0
    assert result.get("capacity_bytes") == 10 * 1024
    assert result.get("free_bytes") is not None
    # Per-camera convenience mirrors the global projection.
    for c in cams:
        assert c.get("days_until_full") is not None


def test_storage_forecast_empty_root_no_growth(tmp_path):
    from app.services.storage_forecast import StorageForecastService

    empty = tmp_path / "empty"
    empty.mkdir()
    svc = StorageForecastService(storage_root=str(empty))
    result = svc.forecast(days=7, capacity_bytes=1024 * 1024)
    assert result.get("ok") is True
    assert result.get("total_bytes") == 0
    assert (result.get("cameras") or []) == []
    assert float(result.get("growth_bytes_per_day") or 0) == 0.0
    # No growth -> no finite exhaustion date.
    assert result.get("days_until_full") is None


def test_storage_forecast_days_until_full_math(tmp_path):
    from app.services.storage_forecast import StorageForecastService

    cam = tmp_path / "solo"
    cam.mkdir()
    now = time.time()
    # 700 bytes all in the last 24h, 7-day window -> 100 bytes/day.
    for i in range(7):
        f = cam / f"r{i}.mp4"
        f.write_bytes(b"q" * 100)
        os.utime(f, (now, now))
    svc = StorageForecastService(storage_root=str(tmp_path))
    # used=700, capacity=1700 -> free=1000, growth=100/day -> 10 days.
    result = svc.forecast(days=7, capacity_bytes=1700)
    assert result.get("total_bytes") == 700
    assert abs(float(result.get("growth_bytes_per_day") or 0) - 100.0) < 0.01
    assert abs(float(result.get("days_until_full") or 0) - 10.0) < 0.01
    assert result.get("full_date") is not None or result.get("depletion_date") is not None


def test_storage_forecast_api(tmp_path):
    client = _make_client()
    target = tmp_path / "api_cams"
    _seed_two_cams(target)

    r = client.post("/api/storage/forecast", json={"path": str(target), "days": 7, "capacity_bytes": 10240})
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, dict)
    cams = data.get("cameras") or data.get("per_camera") or []
    assert len(cams) == 2
    assert data.get("days_until_full") is not None
    assert data.get("growth_bytes_per_day") is not None

    r2 = client.get("/api/storage-forecast", params={"path": str(target), "days": 7, "capacity_bytes": 10240})
    assert r2.status_code == 200, r2.text
    data2 = r2.json()
    assert isinstance(data2, dict)
    cams2 = data2.get("cameras") or data2.get("per_camera") or []
    assert any(c.get("camera_name") == "cam_a" for c in cams2)
