"""Tests for zone shape templates (full/entry/exit/driveway/perimeter)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import zone_templates as zt
from app.api import zone_templates as zt_api


def test_import():
    import app.services.zone_templates  # noqa: F401
    import app.api.zone_templates  # noqa: F401
    from app.services.zone_templates import (  # noqa: F401
        ZoneShapeTemplate,
        get_template,
        list_templates,
        get_template_zones,
    )
    from app.api.zone_templates import router  # noqa: F401


def test_service_lists_five_presets():
    templates = zt.list_zone_templates()
    assert isinstance(templates, list)
    assert len(templates) >= 5
    names = {t["name"] for t in templates}
    for expected in ("full", "entry", "exit", "driveway", "perimeter"):
        assert expected in names


def test_each_template_has_valid_polygons():
    for name in ("full", "entry", "exit", "driveway", "perimeter"):
        zones = zt.get_template_zones(name)
        assert isinstance(zones, list) and len(zones) >= 1
        for z in zones:
            pts = z["points"]
            assert len(pts) >= 3
            for x, y in pts:
                assert 0.0 <= float(x) <= 1.0
                assert 0.0 <= float(y) <= 1.0
            assert z.get("mode") in ("include", "exclude")


def test_perimeter_is_multi_zone_and_full_is_single():
    assert len(zt.get_template_zones("full")) == 1
    assert len(zt.get_template_zones("perimeter")) >= 3


def test_service_aliases_and_normalization():
    assert zt.get_template("full") == zt.get_zone_template("full")
    assert zt.get_template("full") == zt.get_shape_template("full")
    assert zt.list_templates() == zt.list_zone_templates()
    assert zt.normalize_template_name("Full Frame") == "full"
    assert zt.normalize_template_name("ENTRY") == "entry"
    assert zt.normalize_template_name("border") == "perimeter"
    assert zt.is_valid_template("driveway") is True
    assert zt.is_valid_template("nope-unknown") is False


def test_polygon_zone_compat():
    from app.services.motion_zoning import MotionZoningEngine

    zones = zt.to_polygon_zones("entry")
    assert len(zones) == 1
    engine = MotionZoningEngine(40, 40, zones=[z.to_dict() for z in zones])
    assert len(engine.zones) == 1

    scaled = zt.get_template_zones("full", width=640, height=480)
    xs = [p[0] for p in scaled[0]["points"]]
    ys = [p[1] for p in scaled[0]["points"]]
    assert max(xs) == 640.0
    assert max(ys) == 480.0


def test_router_endpoints():
    app = FastAPI()
    app.include_router(zt_api.router)
    client = TestClient(app)

    r = client.get("/api/zone_templates")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) >= 5

    r2 = client.get("/api/zone_templates/full")
    assert r2.status_code == 200
    assert r2.json()["name"] == "full"
    assert len(r2.json()["zones"]) >= 1

    r3 = client.get("/api/zone-templates")
    assert r3.status_code == 200

    r4 = client.get("/api/zone_templates/perimeter/zones")
    assert r4.status_code == 200
    assert isinstance(r4.json(), list)
    assert len(r4.json()) >= 3

    r5 = client.post("/api/zone_templates/apply", json={"template": "driveway"})
    assert r5.status_code == 200
    assert r5.json()["template"] == "driveway"
    assert len(r5.json()["zones"]) >= 1

    r6 = client.get("/api/zone_templates/does-not-exist")
    assert r6.status_code == 404


def test_paths_present_in_main_app():
    from app.main import app as main_app

    paths = list(main_app.openapi()["paths"].keys())
    assert "/api/zone_templates" in paths
