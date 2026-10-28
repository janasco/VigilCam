from fastapi.testclient import TestClient

from app.main import app


def test_codec_breakdown_import():
    import app.services.codec_breakdown  # noqa: F401
    import app.api.codec_breakdown  # noqa: F401
    from app.services.codec_breakdown import (  # noqa: F401
        CodecBreakdownService,
        codec_breakdown,
        analyze,
        normalize_codec,
        detect_codec,
    )
    from app.api.codec_breakdown import router  # noqa: F401


def test_codec_breakdown_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/codec_breakdown" in paths or "/api/codec-breakdown" in paths
    assert any("codec" in p for p in paths)


def test_codec_breakdown_service_per_codec(tmp_path):
    from app.services.codec_breakdown import CodecBreakdownService

    cam_a = tmp_path / "cam_a"
    cam_b = tmp_path / "cam_b"
    cam_a.mkdir()
    cam_b.mkdir()
    (cam_a / "clip_h264.mp4").write_bytes(b"x" * 100)
    (cam_a / "clip_h265.mp4").write_bytes(b"y" * 50)
    (cam_a / "snap.mjpeg").write_bytes(b"z" * 25)
    (cam_b / "movie.mp4").write_bytes(b"w" * 200)

    svc = CodecBreakdownService(storage_root=str(tmp_path))
    result = svc.analyze(probe=False)
    assert result.get("ok") is True
    by_codec = result.get("by_codec") or {}
    assert by_codec.get("h264") == 300
    assert by_codec.get("h265") == 50
    assert by_codec.get("mjpeg") == 25
    assert result.get("total_bytes") == 375
    assert result.get("h264_bytes") == 300
    assert result.get("h265_bytes") == 50
    assert result.get("mjpeg_bytes") == 25

    cams = result.get("cameras") or result.get("per_camera") or []
    by_name = {c.get("camera_name"): c for c in cams}
    assert by_name["cam_a"].get("by_codec", {}).get("h264") == 100
    assert by_name["cam_a"].get("by_codec", {}).get("h265") == 50
    assert by_name["cam_b"].get("h264_bytes") == 200


def test_codec_breakdown_service_single_camera(tmp_path):
    from app.services.codec_breakdown import CodecBreakdownService

    cam = tmp_path / "front_door"
    cam.mkdir()
    (cam / "clip_h265.mp4").write_bytes(b"q" * 64)

    svc = CodecBreakdownService(storage_root=str(tmp_path))
    result = svc.analyze(camera_name="front_door", probe=False)
    cams = result.get("cameras") or []
    assert len(cams) == 1
    assert cams[0].get("total_bytes") == 64
    assert (cams[0].get("by_codec") or {}).get("h265") == 64


def test_codec_breakdown_api(tmp_path):
    client = TestClient(app)
    target = tmp_path / "api_codecs"
    cam = target / "cam1"
    cam.mkdir(parents=True)
    (cam / "r_h264.mp4").write_bytes(b"v" * 77)

    paths = list(app.openapi()["paths"].keys())
    prefix = None
    for cand in ("/api/codec_breakdown", "/api/codec-breakdown", "/api/storage/codecs"):
        if cand in paths:
            prefix = cand
            break
    assert prefix is not None, paths

    r = client.post(prefix, json={"path": str(target), "probe": False})
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, dict)
    by_codec = data.get("by_codec") or {}
    assert by_codec.get("h264") == 77

    r2 = client.get(prefix, params={"path": str(target), "probe": False})
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), dict)
