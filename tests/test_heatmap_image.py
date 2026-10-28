from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app


def test_heatmap_image_import():
    import app.api.heatmap_image  # noqa: F401
    import app.services.heatmap_image  # noqa: F401
    from app.api.heatmap_image import router  # noqa: F401
    from app.services.heatmap_image import render_heatmap_png  # noqa: F401


def _assert_valid_png(data: bytes):
    assert isinstance(data, (bytes, bytearray))
    assert bytes(data)[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(bytes(data)) > 100


def test_render_empty_returns_valid_png():
    from app.services.heatmap_image import render_heatmap_png

    _assert_valid_png(render_heatmap_png(None))
    _assert_valid_png(render_heatmap_png({}))


def test_render_matrix_returns_valid_png():
    from app.services.heatmap import build_heatmap
    from app.services.heatmap_image import extract_matrix, render_heatmap_png

    base = datetime(2026, 1, 1, 0, 0, 0)
    events = [
        {"camera_id": 1, "created_at": base + timedelta(hours=2, minutes=5)},
        {"camera_id": 1, "created_at": base + timedelta(hours=2, minutes=40)},
        {"camera_id": 2, "created_at": base + timedelta(hours=5)},
        {"camera_id": 1, "created_at": base + timedelta(hours=5, minutes=10)},
    ]
    result = build_heatmap(events)
    labels, rows = extract_matrix(result)
    assert len(rows) >= 1
    assert len(rows[0]) == 24
    png = render_heatmap_png(result)
    _assert_valid_png(png)

    from PIL import Image

    import io

    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG"
    assert img.size[0] > 100
    assert img.size[1] > 40


def test_render_direct_dict():
    from app.services.heatmap_image import render_heatmap_png

    png = render_heatmap_png({"per_hour": {"2": 3, "5": 1}})
    _assert_valid_png(png)


def test_heatmap_image_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any("heatmap" in p and "image" in p for p in paths), paths


def test_heatmap_image_api_returns_png():
    client = TestClient(app)
    paths = list(app.openapi()["paths"].keys())
    prefix = None
    for cand in (
        "/api/heatmap/image",
        "/api/heatmap-image",
        "/api/heatmaps/image",
        "/api/events/heatmap/image",
    ):
        if cand in paths:
            prefix = cand
            break
    assert prefix is not None, paths
    r = client.get(prefix)
    assert r.status_code == 200, r.text
    assert "image/png" in r.headers.get("content-type", "")
    _assert_valid_png(r.content)

    r2 = client.get(prefix, params={"camera_id": 1, "hours": 24, "width": 480})
    assert r2.status_code == 200, r2.text
    _assert_valid_png(r2.content)

    r3 = client.post(prefix, json={"events": []})
    assert r3.status_code == 200, r3.text
    _assert_valid_png(r3.content)
