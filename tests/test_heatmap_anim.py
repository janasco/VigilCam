from datetime import datetime, timedelta

from fastapi.testclient import TestClient


def test_heatmap_anim_import():
    import app.api.heatmap_anim  # noqa: F401
    import app.services.heatmap_anim  # noqa: F401
    from app.api.heatmap_anim import router  # noqa: F401
    from app.services.heatmap_anim import render_heatmap_gif  # noqa: F401


def _assert_valid_gif(data: bytes):
    assert isinstance(data, (bytes, bytearray))
    raw = bytes(data)
    assert raw[:6] in (b"GIF87a", b"GIF89a")
    assert len(raw) > 100


def test_heatmap_image_is_single_png_only():
    """Guard: the PNG renderer stays a single static frame (no animation)."""
    import io

    from PIL import Image

    from app.services.heatmap_image import render_heatmap_png

    png = render_heatmap_png({"per_hour": {"2": 3, "5": 1}})
    assert isinstance(png, (bytes, bytearray))
    assert bytes(png)[:8] == b"\x89PNG\r\n\x1a\n"
    # Must not be a GIF.
    assert bytes(png)[:6] not in (b"GIF87a", b"GIF89a")
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG"
    assert getattr(img, "is_animated", False) is False
    assert getattr(img, "n_frames", 1) == 1


def test_render_empty_returns_valid_gif():
    from app.services.heatmap_anim import render_heatmap_gif

    _assert_valid_gif(render_heatmap_gif(None))
    _assert_valid_gif(render_heatmap_gif({}))


def test_render_matrix_returns_animated_gif():
    import io

    from PIL import Image

    from app.services.heatmap import build_heatmap
    from app.services.heatmap_anim import extract_matrix, render_heatmap_gif

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
    gif = render_heatmap_gif(result)
    _assert_valid_gif(gif)

    img = Image.open(io.BytesIO(gif))
    assert img.format == "GIF"
    assert getattr(img, "is_animated", False) is True
    assert getattr(img, "n_frames", 1) > 1


def test_render_gif_frames_param():
    import io

    from PIL import Image

    from app.services.heatmap_anim import render_heatmap_gif

    gif = render_heatmap_gif({"per_hour": {"2": 3, "5": 1}}, frames=4)
    _assert_valid_gif(gif)
    img = Image.open(io.BytesIO(gif))
    assert img.format == "GIF"
    assert getattr(img, "n_frames", 1) >= 2


def test_heatmap_anim_paths_present():
    from app.api.heatmap_anim import router

    paths = [getattr(r, "path", "") for r in router.routes]
    assert any("heatmap" in p and "anim" in p for p in paths), paths


def test_heatmap_anim_api_returns_gif():
    from app.main import app

    client = TestClient(app)
    prefix = "/api/heatmap/anim"
    r = client.get(prefix)
    assert r.status_code == 200, r.text
    assert "image/gif" in r.headers.get("content-type", "")
    _assert_valid_gif(r.content)

    r2 = client.get(prefix, params={"camera_id": 1, "hours": 24, "frames": 4})
    assert r2.status_code == 200, r2.text
    _assert_valid_gif(r2.content)

    r3 = client.post(prefix, json={"events": []})
    assert r3.status_code == 200, r3.text
    _assert_valid_gif(r3.content)
