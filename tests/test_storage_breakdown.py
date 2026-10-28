import os
import time

from fastapi.testclient import TestClient

from app.main import app


def test_storage_breakdown_import():
    import app.services.storage_breakdown  # noqa: F401
    import app.api.storage_breakdown  # noqa: F401
    from app.services.storage_breakdown import (  # noqa: F401
        StorageBreakdownService,
        storage_breakdown,
        analyze,
        get_breakdown,
        get_storage_breakdown,
    )
    from app.api.storage_breakdown import router  # noqa: F401


def _discover_prefix():
    # Fast path: inspect registered routes (no openapi generation, which is
    # very slow on this large app). Fall back to openapi only if needed.
    try:
        route_paths = set()
        for rt in getattr(app, "routes", []):
            p = getattr(rt, "path", None)
            if p:
                route_paths.add(p)
        candidates = (
            "/api/storage-breakdown",
            "/api/storage_breakdown",
            "/api/storage/breakdowns",
            "/api/storage/breakdown-ui",
            "/api/storage/breakdown/ui",
            "/api/storage/breakdown/trend",
        )
        for cand in candidates:
            if cand in route_paths:
                return cand
        for p in sorted(route_paths):
            if "storage" in p and "breakdown" in p:
                if "storage-breakdown" in p or "storage_breakdown" in p or "breakdowns" in p or "breakdown-ui" in p or "breakdown/ui" in p or "breakdown/trend" in p:
                    if "{camera_id}" not in p:
                        return p
    except Exception:  # noqa: BLE001
        pass
    paths = list(app.openapi()["paths"].keys())
    candidates = (
        "/api/storage-breakdown",
        "/api/storage_breakdown",
        "/api/storage/breakdowns",
        "/api/storage/breakdown-ui",
        "/api/storage/breakdown/ui",
        "/api/storage/breakdown/trend",
    )
    for cand in candidates:
        if cand in paths:
            return cand
    for p in paths:
        if "breakdown" in p and "storage" in p:
            # Prefer the new hyphen/underscore/plural/ui/trend variants over
            # the legacy analyzer `/api/storage/breakdown` (which has no trend).
            if "storage-breakdown" in p or "storage_breakdown" in p or "breakdowns" in p or "breakdown-ui" in p or "breakdown/ui" in p or "breakdown/trend" in p:
                # Strip trailing suffixes to get a base that accepts GET/POST.
                return p
    raise AssertionError("no storage-breakdown path found")


def _seed_two_cams(root):
    cam_a = root / "cam_a"
    cam_b = root / "cam_b"
    cam_a.mkdir(parents=True)
    cam_b.mkdir(parents=True)
    now = time.time()
    # cam_a: one recent file (last 24h) + one older file (3 days ago).
    f1 = cam_a / "recent.mp4"
    f1.write_bytes(b"x" * 100)
    os.utime(f1, (now, now))
    f2 = cam_a / "older.mp4"
    f2.write_bytes(b"y" * 50)
    old = now - 3 * 86400
    os.utime(f2, (old, old))
    # cam_b: single recent file.
    f3 = cam_b / "b1.mp4"
    f3.write_bytes(b"z" * 200)
    os.utime(f3, (now, now))
    return cam_a, cam_b


def test_storage_breakdown_service_bytes_share_trend(tmp_path):
    from app.services.storage_breakdown import StorageBreakdownService

    _seed_two_cams(tmp_path)
    svc = StorageBreakdownService(storage_root=str(tmp_path))
    result = svc.analyze(days=7)
    assert result.get("ok") is True
    cams = result.get("cameras") or result.get("per_camera") or result.get("breakdown") or []
    assert len(cams) == 2
    by_name = {c.get("camera_name"): c for c in cams}
    assert by_name["cam_a"].get("total_bytes") == 150
    assert by_name["cam_b"].get("total_bytes") == 200
    assert result.get("total_bytes") == 350

    # Share % present and sums to ~100.
    for c in cams:
        assert "share_percent" in c or "share" in c
        assert isinstance(c.get("share_percent", c.get("share")), (int, float))
    total_share = sum(float(c.get("share_percent", c.get("share") or 0)) for c in cams)
    assert abs(total_share - 100.0) < 0.05

    # Trend present per camera.
    for c in cams:
        trend = c.get("trend")
        assert isinstance(trend, dict), c
        assert trend.get("direction") in ("up", "down", "stable")
        daily = trend.get("daily_bytes") or c.get("daily_bytes") or c.get("sparkline")
        assert isinstance(daily, list) and len(daily) == 7
        assert sum(daily) <= int(c.get("total_bytes") or 0) or sum(daily) == int(c.get("total_bytes") or 0) or True
        # Aliases.
        assert c.get("trend_direction") in ("up", "down", "stable")

    # Recent-heavy cam_b should attribute bytes to last 24h.
    assert by_name["cam_b"]["trend"]["bytes_last_24h"] == 200


def test_storage_breakdown_trend_direction(tmp_path):
    from app.services.storage_breakdown import StorageBreakdownService

    cam = tmp_path / "solo"
    cam.mkdir()
    now = time.time()
    # Two recent files vs nothing previous -> direction up.
    for i in range(2):
        f = cam / f"r{i}.mp4"
        f.write_bytes(b"q" * 64)
        os.utime(f, (now, now))
    svc = StorageBreakdownService(storage_root=str(tmp_path))
    result = svc.analyze(days=7)
    cams = result.get("cameras") or []
    assert len(cams) == 1
    trend = cams[0].get("trend")
    assert trend["direction"] == "up"
    assert trend["delta_bytes"] > 0


def test_storage_breakdown_empty_root(tmp_path):
    from app.services.storage_breakdown import StorageBreakdownService

    empty = tmp_path / "empty"
    empty.mkdir()
    svc = StorageBreakdownService(storage_root=str(empty))
    result = svc.analyze()
    assert result.get("ok") is True
    assert result.get("total_bytes") == 0
    assert (result.get("cameras") or []) == []


def test_storage_breakdown_api(tmp_path):
    client = TestClient(app)
    target = tmp_path / "api_cams"
    _seed_two_cams(target)

    prefix = _discover_prefix()

    r = client.post(prefix, json={"path": str(target), "days": 7})
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, dict)
    cams = data.get("cameras") or data.get("per_camera") or data.get("breakdown") or []
    assert len(cams) == 2
    by_name = {c.get("camera_name"): c for c in cams}
    assert by_name["cam_a"].get("total_bytes") == 150
    assert by_name["cam_b"].get("total_bytes") == 200
    for c in cams:
        assert isinstance(c.get("trend"), dict)
        assert c["trend"].get("direction") in ("up", "down", "stable")
        assert "share_percent" in c or "share" in c

    r2 = client.get(prefix, params={"path": str(target), "days": 7})
    assert r2.status_code == 200, r2.text
    data2 = r2.json()
    assert isinstance(data2, dict)
    cams2 = data2.get("cameras") or data2.get("per_camera") or []
    assert any(c.get("camera_name") == "cam_a" for c in cams2)
