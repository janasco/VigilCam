from fastapi.testclient import TestClient

from app.main import app


def test_storage_analyzer_import():
    import app.services.storage_analyzer  # noqa: F401
    import app.api.storage_analyzer  # noqa: F401
    from app.services.storage_analyzer import (  # noqa: F401
        StorageAnalyzer,
        storage_analyzer,
        analyze,
        analyze_per_camera,
        get_per_camera_usage,
    )
    from app.api.storage_analyzer import router  # noqa: F401


def test_storage_analyzer_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/storage_analyzer" in paths or "/api/storage/analyzer" in paths
    assert any("storage" in p and "analyz" in p for p in paths) or any(
        "per-camera" in p or "per_camera" in p for p in paths
    )


def test_storage_analyzer_service_per_camera(tmp_path):
    from app.services.storage_analyzer import StorageAnalyzer

    cam_a = tmp_path / "cam_a"
    cam_b = tmp_path / "cam_b"
    cam_a.mkdir()
    cam_b.mkdir()
    (cam_a / "a1.mp4").write_bytes(b"x" * 100)
    (cam_a / "a2.mp4").write_bytes(b"y" * 50)
    (cam_b / "b1.mp4").write_bytes(b"z" * 200)

    mgr = StorageAnalyzer(storage_root=str(tmp_path))
    result = mgr.analyze()
    assert result.get("ok") is True
    cams = result.get("cameras") or result.get("per_camera") or []
    by_name = {c.get("camera_name"): c for c in cams}
    assert "cam_a" in by_name
    assert "cam_b" in by_name
    assert by_name["cam_a"].get("total_bytes") == 150
    assert by_name["cam_b"].get("total_bytes") == 200
    assert by_name["cam_a"].get("file_count") == 2
    assert result.get("total_bytes") == 350


def test_storage_analyzer_service_single_camera(tmp_path):
    from app.services.storage_analyzer import StorageAnalyzer

    cam = tmp_path / "front_door"
    cam.mkdir()
    (cam / "clip.mp4").write_bytes(b"q" * 64)

    mgr = StorageAnalyzer(storage_root=str(tmp_path))
    result = mgr.analyze(camera_name="front_door")
    cams = result.get("cameras") or []
    assert len(cams) == 1
    assert cams[0].get("total_bytes") == 64


def test_storage_analyzer_api(tmp_path):
    client = TestClient(app)
    target = tmp_path / "api_cams"
    cam = target / "cam1"
    cam.mkdir(parents=True)
    (cam / "r.mp4").write_bytes(b"v" * 77)

    # Discover an available prefix dynamically.
    paths = list(app.openapi()["paths"].keys())
    prefix = None
    for cand in ("/api/storage_analyzer", "/api/storage/analyzer", "/api/storage/per-camera"):
        if cand in paths:
            prefix = cand
            break
    assert prefix is not None, paths

    r = client.post(prefix, json={"path": str(target)})
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, dict)
    cams = data.get("cameras") or data.get("per_camera") or data.get("breakdown") or []
    assert any((c.get("camera_name") == "cam1") for c in cams)

    r2 = client.get(prefix, params={"path": str(target)})
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), dict)
