"""Tests for privacy mask regions (output blackout, distinct from motion zones)."""

import base64

import cv2
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import privacy_mask as pm
from app.api import privacy_mask as pm_api


def _white(w: int = 64, h: int = 48) -> np.ndarray:
    return np.full((h, w, 3), 255, dtype=np.uint8)


def _jpeg(frame: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok
    return bytes(buf.tobytes())


def setup_function(_func=None):
    pm.clear_all()


def test_imports():
    import app.services.privacy_mask  # noqa: F401
    import app.api.privacy_mask  # noqa: F401
    from app.services.privacy_mask import apply_privacy_masks, set_masks, get_masks  # noqa: F401
    from app.api.privacy_mask import router  # noqa: F401


def test_motion_zones_gate_detection_only_and_do_not_blackout_output():
    """Motion zones must not mutate the input frame (no output blackout)."""
    from app.services.motion_zoning import MotionZoningEngine

    f1 = np.zeros((40, 40, 3), dtype=np.uint8)
    f2 = f1.copy()
    f2[5:15, 5:15] = 255
    before = f2.copy()
    engine = MotionZoningEngine(40, 40, zones=[{"points": [[5, 5], [14, 5], [14, 14], [5, 14]], "mode": "exclude", "threshold_percentage": 1}])
    engine.process_and_evaluate(f1)
    engine.process_and_evaluate(f2)
    # Detection-only: caller frame untouched, no black rectangles painted.
    assert np.array_equal(before, f2)
    assert int(f2[10, 10, 0]) == 255


def test_set_get_clear_masks():
    assert pm.get_masks("cam1") == []
    stored = pm.set_masks("cam1", [{"points": [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]], "name": "door"}])
    assert len(stored) == 1
    assert pm.has_masks("cam1") is True
    assert len(pm.get_masks("cam1")) == 1
    assert pm.clear_masks("cam1") == 1
    assert pm.get_masks("cam1") == []


def test_apply_blacks_out_region_and_preserves_rest():
    pm.set_masks("cam9", [{"points": [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]}])
    frame = _white()
    before = frame.copy()
    out = pm.apply_privacy_masks(frame, camera_id="cam9")
    # Input never mutated.
    assert np.array_equal(frame, before)
    h, w = frame.shape[:2]
    # Center (inside mask) is black; corner (outside mask) stays white.
    assert out[h // 2, w // 2].tolist() == [0, 0, 0]
    assert out[2, 2].tolist() == [255, 255, 255]


def test_apply_with_pixel_and_rect_forms():
    frame = _white(100, 80)
    out = pm.apply_privacy_masks(frame, regions=[{"x": 10, "y": 10, "width": 20, "height": 20}])
    assert out[15, 15].tolist() == [0, 0, 0]
    assert out[70, 90].tolist() == [255, 255, 255]
    out2 = pm.apply_privacy_masks(frame, regions=[{"points": [[40, 30], [60, 30], [60, 50], [40, 50]]}])
    assert out2[40, 50].tolist() == [0, 0, 0]


def test_apply_to_jpeg_roundtrip():
    pm.set_masks("camJ", [{"points": [[0.0, 0.0], [1.0, 0.0], [1.0, 0.5], [0.0, 0.5]]}])
    jpeg = _jpeg(_white())
    masked = pm.apply_privacy_masks_to_jpeg(jpeg, camera_id="camJ")
    assert masked.startswith(b"\xff\xd8")
    frame = pm.decode_jpeg_bytes(masked)
    assert frame is not None
    # Top half masked -> dark; bottom half untouched -> bright.
    assert int(frame[5, 32, 0]) < 30
    assert int(frame[40, 32, 0]) > 200


def test_add_remove_mask():
    pm.set_masks("camA", [])
    pm.add_mask("camA", {"points": [[0, 0], [10, 0], [10, 10], [0, 10]], "name": "a"})
    pm.add_mask("camA", {"points": [[20, 20], [30, 20], [30, 30], [20, 30]], "name": "b"})
    assert len(pm.get_masks("camA")) == 2
    pm.remove_mask("camA", name="a")
    remaining = pm.get_masks("camA")
    assert len(remaining) == 1 and remaining[0]["name"] == "b"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(pm_api.router)
    return TestClient(app)


def test_router_crud():
    client = _client()
    regions = [{"points": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4], [0.1, 0.4]], "name": "window"}]
    r = client.post("/api/privacy_mask/cam1", json={"regions": regions})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    g = client.get("/api/privacy_mask/cam1")
    assert g.status_code == 200
    assert len(g.json()["regions"]) == 1
    # Plural + hyphen aliases.
    assert client.get("/api/privacy_masks/cam1").status_code == 200
    assert client.get("/api/privacy-masks/cam1").status_code == 200
    d = client.delete("/api/privacy_mask/cam1")
    assert d.status_code == 200
    assert client.get("/api/privacy_mask/cam1").json()["count"] == 0


def test_router_preview_masks_image():
    client = _client()
    client.post("/api/privacy_mask/camP", json={"regions": [{"points": [[0, 0], [1, 0], [1, 0.5], [0, 0.5]]}]})
    b64 = base64.b64encode(_jpeg(_white())).decode("ascii")
    r = client.post("/api/privacy_mask/camP/preview", json={"image_base64": b64})
    assert r.status_code == 200, r.text
    out = pm.decode_jpeg_bytes(r.json()["image_base64"])
    assert out is not None
    assert int(out[5, 32, 0]) < 30
    assert int(out[40, 32, 0]) > 200


def test_router_snapshot_and_mjpeg_apply_masks():
    client = _client()
    client.post("/api/privacy_mask/camS", json={"regions": [{"points": [[0, 0], [1, 0], [1, 1], [0, 1]]}]})
    snap = client.get("/api/privacy_mask/camS/snapshot")
    assert snap.status_code == 200
    assert snap.headers["content-type"] == "image/jpeg"
    frame = pm.decode_jpeg_bytes(snap.content)
    assert frame is not None
    # Full-frame mask -> everything black.
    assert int(frame.mean()) < 30
    mj = client.get("/api/privacy_mask/camS/mjpeg?frames=1&fps=5")
    assert mj.status_code == 200
    assert "multipart/x-mixed-replace" in mj.headers["content-type"]
    assert b"\xff\xd8" in mj.content


def test_router_missing_regions_returns_400():
    client = _client()
    r = client.post("/api/privacy_mask/camX", json={})
    assert r.status_code == 400


def test_router_status_present():
    client = _client()
    r = client.get("/api/privacy_mask/status")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_auto_loader_contract_picks_up_router():
    """No main.py edits needed: app/main.py auto-includes any app.api module
    exposing ``router``. Proved two ways: (1) the module router carries the
    expected paths, (2) the live main app serves them (FastAPI 0.143 stores
    includes as _IncludedRouter objects, so route tables are verified
    functionally via TestClient, not via .path attributes)."""
    import importlib as _importlib

    mod = _importlib.import_module("app.api.privacy_mask")
    _r = getattr(mod, "router", None)
    assert _r is not None
    module_paths = [getattr(r, "path", "") for r in _r.routes]
    assert "/api/privacy_mask/status" in module_paths
    assert "/api/privacy_masks/{camera_id}" in module_paths
    assert any(p.endswith("/snapshot") and "privacy" in p for p in module_paths)
    assert any(p.endswith("/mjpeg") and "privacy" in p for p in module_paths)

    from fastapi.testclient import TestClient as _TestClient

    from app.main import app as _main_app

    client = _TestClient(_main_app)
    r = client.get("/api/privacy_mask/status")
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "ok"
