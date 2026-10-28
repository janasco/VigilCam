"""Tests for mask-image zone import (upload mask PNG -> polygon zones)."""

import base64
import io

import cv2
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import mask_import as mi
from app.api import mask_import as mi_api


def _png_bytes(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return bytes(buf.tobytes())


def _rect_mask(w: int = 64, h: int = 48) -> bytes:
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[10:30, 12:44] = 255
    return _png_bytes(img)


def test_import():
    import app.services.mask_import  # noqa: F401
    import app.api.mask_import  # noqa: F401
    from app.services.mask_import import mask_bytes_to_zones, mask_to_zones  # noqa: F401
    from app.api.mask_import import router  # noqa: F401


def test_service_single_rectangle():
    zones = mi.mask_bytes_to_zones(_rect_mask())
    assert isinstance(zones, list)
    assert len(zones) >= 1
    for z in zones:
        pts = z["points"]
        assert len(pts) >= 3
        for x, y in pts:
            assert 0.0 <= float(x) <= 1.0
            assert 0.0 <= float(y) <= 1.0
        assert z.get("mode") in ("include", "exclude")


def test_service_empty_mask_returns_no_zones():
    img = np.zeros((32, 32, 3), dtype=np.uint8)
    zones = mi.mask_bytes_to_zones(_png_bytes(img))
    assert zones == []


def test_service_two_blobs_returns_two_zones():
    img = np.zeros((64, 64, 3), dtype=np.uint8)
    img[5:15, 5:15] = 255
    img[40:55, 40:55] = 255
    zones = mi.mask_bytes_to_zones(_png_bytes(img))
    assert len(zones) >= 2


def test_service_pixel_coords_option():
    zones = mi.mask_bytes_to_zones(_rect_mask(64, 48), normalize=False)
    assert len(zones) >= 1
    xs = [p[0] for p in zones[0]["points"]]
    assert max(xs) > 1.0  # pixel space, not 0-1


def test_service_base64_and_aliases():
    raw = _rect_mask()
    b64 = base64.b64encode(raw).decode("ascii")
    z1 = mi.mask_base64_to_zones(b64)
    assert len(z1) >= 1
    assert mi.base64_to_zones(b64) == z1 or len(mi.base64_to_zones(b64)) == len(z1)
    assert len(mi.mask_to_zones(raw)) >= 1
    assert len(mi.mask_to_polygons(raw)) >= 1
    assert len(mi.import_mask(raw)) >= 1
    assert len(mi.parse_mask(raw)) >= 1
    assert len(mi.convert_mask(raw)) >= 1
    assert len(mi.mask_bytes_to_polygons(raw)) >= 1


def test_service_invalid_bytes_raise():
    try:
        mi.mask_bytes_to_zones(b"not-an-image-at-all-xyz")
    except (ValueError, Exception):
        return
    raise AssertionError("expected ValueError for invalid image")


def test_service_polygon_zone_compat():
    from app.services.motion_zoning import MotionZoningEngine

    zones = mi.mask_bytes_to_zones(_rect_mask())
    assert len(zones) >= 1
    engine = MotionZoningEngine(64, 48, zones=zones)
    assert len(engine.zones) >= 1
    polys = mi.to_polygon_zones(_rect_mask())
    assert len(polys) == len(zones)
    assert hasattr(polys[0], "points")


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(mi_api.router)
    return TestClient(app)


def test_router_upload_png():
    client = _client()
    raw = _rect_mask()
    r = client.post("/api/mask_import/import", files={"file": ("mask.png", raw, "image/png")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("ok") is True
    assert isinstance(data.get("zones"), list)
    assert len(data["zones"]) >= 1
    assert data.get("count", len(data["zones"])) >= 1


def test_router_hyphen_and_base_paths():
    client = _client()
    raw = _rect_mask()
    for path in ("/api/mask-import/import", "/api/mask_import", "/api/motion/mask_import/import"):
        r = client.post(path, files={"file": ("mask.png", raw, "image/png")})
        assert r.status_code == 200, (path, r.text)
        assert len(r.json()["zones"]) >= 1


def test_router_preview_convert_parse_aliases():
    client = _client()
    raw = _rect_mask()
    for path in ("/api/mask_import/preview", "/api/mask_import/convert", "/api/mask_import/parse"):
        r = client.post(path, files={"file": ("mask.png", raw, "image/png")})
        assert r.status_code == 200, (path, r.text)


def test_router_missing_file_returns_400():
    client = _client()
    r = client.post("/api/mask_import/import", json={})
    assert r.status_code == 400


def test_router_invalid_bytes_returns_400():
    client = _client()
    r = client.post(
        "/api/mask_import/import",
        files={"file": ("mask.png", b"definitely-not-an-image", "image/png")},
    )
    assert r.status_code == 400


def test_router_json_base64():
    client = _client()
    raw = _rect_mask()
    b64 = base64.b64encode(raw).decode("ascii")
    r = client.post("/api/mask_import/import", json={"image": b64})
    assert r.status_code == 200, r.text
    assert len(r.json()["zones"]) >= 1


def test_router_status_and_usage():
    client = _client()
    r = client.get("/api/mask_import/status")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"
    r2 = client.get("/api/mask_import")
    assert r2.status_code == 200


def test_paths_present_in_main_app():
    from app.main import app as main_app

    paths = list(main_app.openapi()["paths"].keys())
    assert "/api/mask_import" in paths or "/api/mask_import/import" in paths
