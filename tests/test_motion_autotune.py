import base64

import cv2
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import motion_autotune as at
from app.api import motion_autotune as at_api


def _encode(img) -> str:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _quiet_frames(n=6, h=120, w=160):
    rng = np.random.default_rng(0)
    base = np.full((h, w, 3), 128, dtype=np.int16)
    return [np.clip(base + rng.normal(0, 1.5, base.shape), 0, 255).astype(np.uint8)
            for _ in range(n)]


def _noisy_frames(n=6, h=120, w=160):
    rng = np.random.default_rng(1)
    base = np.full((h, w, 3), 128, dtype=np.int16)
    return [np.clip(base + rng.normal(0, 25.0, base.shape), 0, 255).astype(np.uint8)
            for _ in range(n)]


def test_import():
    import app.services.motion_autotune  # noqa: F401
    import app.api.motion_autotune  # noqa: F401
    from app.services.motion_autotune import autotune, analyze_frames, recommend_threshold  # noqa: F401
    from app.api.motion_autotune import router  # noqa: F401


def test_service_quiet_vs_noisy():
    quiet = at.autotune(_quiet_frames())
    noisy = at.autotune(_noisy_frames())
    assert quiet["recommended_threshold"] < noisy["recommended_threshold"]
    for res in (quiet, noisy):
        assert 10 <= res["recommended_threshold"] <= 60
        assert res["recommended_contour_area"] >= 10
        assert res["recommended_preset"]
        assert res["preset_config"]["name"] == res["recommended_preset"]
        assert res["noise"]["p99_absdiff"] >= 0


def test_service_single_frame():
    frame = np.full((100, 100, 3), 128, dtype=np.uint8)
    stats = at.analyze_frames([frame])
    assert stats["method"] == "spatial_proxy"
    res = at.autotune([frame])
    assert 10 <= res["recommended_threshold"] <= 60


def test_service_base64_and_dict_inputs():
    frames = _quiet_frames(n=3)
    b64 = [_encode(f) for f in frames]
    r1 = at.autotune(b64)
    assert r1["ok"] is True
    r2 = at.autotune({"frames": b64})
    assert r2["recommended_threshold"] == r1["recommended_threshold"]
    r3 = at.autotune(None, image_base64=b64[0], previous_image_base64=b64[1])
    assert 10 <= r3["recommended_threshold"] <= 60


def test_service_empty_raises():
    try:
        at.autotune([])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty frames")


def test_recommend_threshold_monotonic_with_margin():
    stats = {"p99_absdiff": 20.0, "p95_absdiff": 15.0}
    assert at.recommend_threshold(stats, margin=4) < at.recommend_threshold(stats, margin=16)


def test_router_endpoints():
    app = FastAPI()
    app.include_router(at_api.router)
    client = TestClient(app)

    r = client.get("/api/motion_autotune/status")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    r2 = client.post("/api/motion_autotune/analyze", json={"synthetic": True})
    assert r2.status_code == 200
    body = r2.json()
    assert 10 <= body["recommended_threshold"] <= 60
    assert body["recommended_preset"]

    frames = [_encode(f) for f in _quiet_frames(n=4)]
    r3 = client.post("/api/motion/autotune/analyze", json={"frames": frames})
    assert r3.status_code == 200
    assert r3.json()["ok"] is True

    r4 = client.post("/api/motion-autotune/recommend", json={"synthetic": True, "noise_sigma": 25})
    assert r4.status_code == 200
    assert r4.json()["recommended_threshold"] >= body["recommended_threshold"]

    r5 = client.post("/api/motion_autotune/analyze", json={})
    assert r5.status_code == 400


def test_paths_present():
    from app.main import app

    paths = list(app.openapi()["paths"].keys())
    assert "/api/motion_autotune" in paths or "/api/motion_autotune/analyze" in paths
    assert any(p.startswith("/api/motion_autotune") or p.startswith("/api/motion/autotune")
               for p in paths)
