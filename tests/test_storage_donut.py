import os
import time

from fastapi.testclient import TestClient

from app.main import app


def test_storage_donut_import():
    import app.services.storage_donut  # noqa: F401
    import app.api.storage_donut  # noqa: F401
    from app.services.storage_donut import (  # noqa: F401
        StorageDonutService,
        storage_donut,
        donut,
        get_donut,
        get_storage_donut,
    )
    from app.api.storage_donut import router  # noqa: F401


def _discover_prefix():
    try:
        route_paths = set()
        for rt in getattr(app, "routes", []):
            p = getattr(rt, "path", None)
            if p:
                route_paths.add(p)
        candidates = (
            "/api/storage-donut",
            "/api/storage_donut",
            "/api/storage/donut",
            "/api/storage/donuts",
        )
        for cand in candidates:
            if cand in route_paths:
                return cand
        for p in sorted(route_paths):
            if "donut" in p and "storage" in p and "{camera_id}" not in p:
                return p
    except Exception:  # noqa: BLE001
        pass
    paths = list(app.openapi()["paths"].keys())
    for cand in ("/api/storage-donut", "/api/storage_donut", "/api/storage/donut"):
        if cand in paths:
            return cand
    for p in paths:
        if "donut" in p and "storage" in p:
            return p
    raise AssertionError("no storage-donut path found")


def _seed_two_cams(root):
    cam_a = root / "cam_a"
    cam_b = root / "cam_b"
    cam_a.mkdir(parents=True)
    cam_b.mkdir(parents=True)
    now = time.time()
    f1 = cam_a / "a1.mp4"
    f1.write_bytes(b"x" * 100)
    os.utime(f1, (now, now))
    f2 = cam_b / "b1.mp4"
    f2.write_bytes(b"z" * 200)
    os.utime(f2, (now, now))
    return cam_a, cam_b


def test_storage_donut_service_segments(tmp_path):
    from app.services.storage_donut import StorageDonutService

    _seed_two_cams(tmp_path)
    svc = StorageDonutService(storage_root=str(tmp_path))
    result = svc.donut()
    assert result.get("ok") is True
    segs = result.get("segments") or result.get("slices") or result.get("data") or []
    assert len(segs) == 2
    by_label = {s.get("label"): s for s in segs}
    assert set(by_label) == {"cam_a", "cam_b"}
    for s in segs:
        assert isinstance(s.get("label"), str) and s["label"]
        assert isinstance(s.get("value"), int)
        assert isinstance(s.get("color"), str) and s["color"]
    assert by_label["cam_a"]["value"] == 100
    assert by_label["cam_b"]["value"] == 200
    assert result.get("total_bytes") == 300
    # Slices alias matches segments; labels/values/colors arrays present.
    assert result.get("slices") == segs
    assert result.get("labels") == [s["label"] for s in segs]
    assert result.get("values") == [s["value"] for s in segs]
    assert result.get("colors") == [s["color"] for s in segs]
    # Shares sum to ~100.
    total_share = sum(float(s.get("share", 0)) for s in segs)
    assert abs(total_share - 100.0) < 0.05


def test_storage_donut_empty_root(tmp_path):
    from app.services.storage_donut import StorageDonutService

    empty = tmp_path / "empty"
    empty.mkdir()
    svc = StorageDonutService(storage_root=str(empty))
    result = svc.analyze()
    assert result.get("ok") is True
    assert result.get("total_bytes") == 0
    assert (result.get("segments") or []) == []


def test_storage_donut_api(tmp_path):
    client = TestClient(app)
    target = tmp_path / "api_cams"
    _seed_two_cams(target)

    prefix = _discover_prefix()

    r = client.post(prefix, json={"path": str(target)})
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, dict)
    segs = data.get("segments") or data.get("slices") or data.get("data") or []
    assert len(segs) == 2
    by_label = {s.get("label"): s for s in segs}
    assert by_label["cam_a"]["value"] == 100
    assert by_label["cam_b"]["value"] == 200
    for s in segs:
        assert s.get("label") and isinstance(s.get("value"), int) and s.get("color")

    r2 = client.get(prefix, params={"path": str(target)})
    assert r2.status_code == 200, r2.text
    data2 = r2.json()
    segs2 = data2.get("segments") or data2.get("slices") or []
    assert any(s.get("label") == "cam_a" for s in segs2)

    # Bare-list mode for widgets that bind directly to [{label, value, color}].
    r3 = client.get(prefix, params={"path": str(target), "format": "list"})
    assert r3.status_code == 200, r3.text
    bare = r3.json()
    assert isinstance(bare, list) and len(bare) == 2
    assert all(("label" in s and "value" in s and "color" in s) for s in bare)
