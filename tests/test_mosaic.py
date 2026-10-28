import io

from fastapi.testclient import TestClient

from app.main import app
from app.services import mosaic, thumbnails


def test_mosaic_imports():
    import app.services.mosaic  # noqa: F401
    import app.api.mosaic  # noqa: F401
    from app.services.mosaic import build_mosaic, build_mosaic_with_meta, grid_dimensions  # noqa: F401
    from app.api.mosaic import router  # noqa: F401
    assert router is not None


def test_build_mosaic_returns_jpeg_with_expected_grid():
    thumbnails.clear()
    data, meta = mosaic.build_mosaic_with_meta(
        ["m1", "m2", "m3", "m4"], tile_width=160, tile_height=120, cols=2,
    )
    assert isinstance(data, (bytes, bytearray))
    assert bytes(data)[:2] == b"\xff\xd8"
    assert meta["cols"] == 2
    assert meta["rows"] == 2
    assert meta["count"] == 4
    assert meta["width"] == 320
    assert meta["height"] == 240

    from PIL import Image

    img = Image.open(io.BytesIO(bytes(data)))
    assert img.size == (320, 240)
    assert img.format == "JPEG"


def test_build_mosaic_single_camera_and_auto_cols():
    thumbnails.clear()
    assert mosaic.grid_dimensions(1) == (1, 1)
    assert mosaic.grid_dimensions(4) == (2, 2)
    assert mosaic.grid_dimensions(5, cols=2) == (2, 3)

    data = mosaic.build_mosaic(["solo-cam"], tile_width=160, tile_height=120)
    assert bytes(data)[:2] == b"\xff\xd8"
    from PIL import Image

    img = Image.open(io.BytesIO(bytes(data)))
    assert img.size == (160, 120)


def test_build_mosaic_uses_thumbnail_cache():
    thumbnails.clear()
    custom = thumbnails.get_thumbnail("mosaic-cache-cam", width=160, height=120)
    assert custom[:2] == b"\xff\xd8"
    # Second call uses cache; mosaic should still render fine.
    data, meta = mosaic.build_mosaic_with_meta(["mosaic-cache-cam"], tile_width=160, tile_height=120)
    assert meta["count"] == 1
    assert bytes(data)[:2] == b"\xff\xd8"


def test_mosaic_api_endpoints():
    thumbnails.clear()
    client = TestClient(app)

    r = client.get("/api/mosaic", params={"cameras": "1,2,3,4", "cols": 2})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert r.content[:2] == b"\xff\xd8"
    assert r.headers.get("X-Mosaic-Count") == "4"
    assert r.headers.get("X-Mosaic-Grid") == "2x2"

    r_default = client.get("/api/mosaic")
    assert r_default.status_code == 200
    assert r_default.headers["content-type"].startswith("image/")
    assert r_default.content[:2] == b"\xff\xd8"

    info = client.get("/api/mosaic/info", params={"cameras": "1,2,3,4", "cols": 2})
    assert info.status_code == 200
    body = info.json()
    assert body.get("count") == 4
    assert body.get("cols") == 2
    assert body.get("rows") == 2
    assert body.get("width") == 2 * mosaic.DEFAULT_TILE_WIDTH

    grid = client.get("/api/mosaic/grid", params={"cameras": "a,b", "cols": 2})
    assert grid.status_code == 200
    assert grid.content[:2] == b"\xff\xd8"


def test_mosaic_api_custom_tiles():
    thumbnails.clear()
    client = TestClient(app)
    r = client.get("/api/mosaic", params={"cameras": "x,y", "cols": 2, "tile_width": 160, "tile_height": 120})
    assert r.status_code == 200
    assert r.content[:2] == b"\xff\xd8"
    from PIL import Image

    img = Image.open(io.BytesIO(r.content))
    assert img.size == (320, 120)
