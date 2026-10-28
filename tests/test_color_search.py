import base64
import io

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.color_search import router as color_search_router  # noqa: F401
from app.services import color_search as cs  # noqa: F401


def _solid_b64(rgb, size=(32, 32)):
    from PIL import Image
    img = Image.new("RGB", size, rgb)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_color_search_import():
    import app.api.color_search  # noqa: F401
    import app.services.color_search  # noqa: F401
    from app.api.color_search import router  # noqa: F401
    assert router is not None


def test_normalize_color():
    assert cs.normalize_color("red") == "red"
    assert cs.normalize_color("RED") == "red"
    assert cs.normalize_color("grey") == "gray"
    assert cs.normalize_color("red car") == "red"
    assert cs.normalize_color("not-a-color") is None
    assert cs.normalize_color(None) is None


def test_rgb_to_color_name_basics():
    assert cs.rgb_to_color_name(220, 30, 30) == "red"
    assert cs.rgb_to_color_name(40, 100, 220) == "blue"
    assert cs.rgb_to_color_name(40, 160, 60) == "green"
    assert cs.rgb_to_color_name(240, 210, 30) == "yellow"
    assert cs.rgb_to_color_name(10, 10, 10) == "black"
    assert cs.rgb_to_color_name(250, 250, 250) == "white"


def test_dominant_color_solid_images():
    red = cs.analyze_image(_solid_b64((230, 20, 20)))
    assert red["dominant_color"] == "red"
    assert red["rgb"][0] > 150
    blue = cs.analyze_image(_solid_b64((30, 90, 220)))
    assert blue["dominant_color"] == "blue"
    # histogram present (snapshot histograms)
    assert len(red["histogram"]) == 3 * 8
    assert len(red["color_histogram"]) == 3 * 8


def test_search_events_by_color_name():
    events = [
        {"id": 1, "camera_id": 1, "label": "car", "color": "red"},
        {"id": 2, "camera_id": 1, "label": "car", "color": "blue"},
        {"id": 3, "camera_id": 2, "label": "person", "tags": ["red", "door"]},
    ]
    res = cs.search_events(events, color="red")
    ids = [e["id"] for e in res]
    assert 1 in ids
    assert 2 not in ids
    assert res[0]["dominant_color"] == "red"
    assert res[0]["score"] > 0


def test_search_events_by_snapshot_histogram():
    red_b64 = _solid_b64((230, 20, 20))
    blue_b64 = _solid_b64((20, 80, 220))
    events = [
        {"id": 10, "camera_id": 1, "label": "car", "image_base64": red_b64},
        {"id": 11, "camera_id": 1, "label": "car", "image_base64": blue_b64},
    ]
    res = cs.search_events(events, color="red")
    assert res
    assert res[0]["id"] == 10
    assert res[0]["dominant_color"] == "red"


def _client():
    app = FastAPI()
    app.include_router(color_search_router)
    return TestClient(app)


def test_api_router_paths_present():
    paths = sorted({getattr(r, "path", "") for r in color_search_router.routes})
    assert "/api/color-search" in paths
    assert "/api/color_search" in paths
    assert "/api/events/by-color" in paths
    assert "/api/color-search/colors" in paths
    assert "/api/color-search/analyze" in paths


def test_api_search_with_events_override():
    red_b64 = _solid_b64((230, 20, 20))
    blue_b64 = _solid_b64((20, 80, 220))
    client = _client()
    body = {
        "color": "red",
        "events": [
            {"id": 1, "camera_id": 1, "label": "car", "image_base64": red_b64},
            {"id": 2, "camera_id": 1, "label": "car", "image_base64": blue_b64},
        ],
    }
    r = client.post("/api/color-search/search", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert isinstance(out, list)
    assert out[0]["id"] == 1
    # GET with query + events not possible; GET without DB returns list
    r2 = client.get("/api/color-search", params={"color": "red"})
    assert r2.status_code == 200


def test_api_colors_and_analyze():
    client = _client()
    r = client.get("/api/color-search/colors")
    assert r.status_code == 200, r.text
    assert "red" in r.json()["colors"]
    r2 = client.post("/api/color-search/analyze", json={"image_base64": _solid_b64((230, 20, 20))})
    assert r2.status_code == 200, r2.text
    assert r2.json()["dominant_color"] == "red"
