from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.branding import router as branding_router  # noqa: F401
from app.services import branding as branding_service_mod  # noqa: F401
from app.services.branding import (  # noqa: F401
    get_branding,
    set_branding,
    update_branding,
    reset_branding,
    clear,
    branding_service,
)


def test_branding_import():
    import app.api.branding  # noqa: F401
    import app.services.branding  # noqa: F401
    from app.api.branding import router  # noqa: F401
    from app.services.branding import get_branding, set_branding  # noqa: F401


def test_branding_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/branding" in paths


def test_branding_service_crud():
    branding_service_mod.reset_branding()
    current = get_branding()
    assert current["site_name"] == "VigilCam"
    assert "primary_color" in current
    assert "logo_url" in current

    replaced = set_branding({"site_name": "Acme Cam", "logo_url": "https://example.com/logo.png",
                             "primary_color": "#ff0000"})
    assert replaced["site_name"] == "Acme Cam"
    assert replaced["logo_url"] == "https://example.com/logo.png"
    assert replaced["primary_color"] == "#ff0000"

    fetched = get_branding()
    assert fetched["site_name"] == "Acme Cam"

    updated = update_branding({"site_name": "Acme Cam 2"})
    assert updated["site_name"] == "Acme Cam 2"
    assert updated["primary_color"] == "#ff0000"

    reset = reset_branding()
    assert reset["site_name"] == "VigilCam"

    assert get_branding()["site_name"] == "VigilCam"
    branding_service_mod.reset_branding()


def test_branding_service_validation():
    branding_service_mod.reset_branding()
    try:
        set_branding({"primary_color": "not-a-color"})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for bad color")
    try:
        set_branding({"site_name": "   "})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty site_name")
    branding_service_mod.reset_branding()


def test_branding_api_crud():
    branding_service_mod.reset_branding()
    a = FastAPI()
    a.include_router(branding_router)
    client = TestClient(a)

    g = client.get("/api/branding")
    assert g.status_code == 200, g.text
    assert g.json()["site_name"] == "VigilCam"

    r = client.put("/api/branding", json={"site_name": "Acme", "primary_color": "#00ff00"})
    assert r.status_code == 200, r.text
    assert r.json()["site_name"] == "Acme"
    assert r.json()["primary_color"] == "#00ff00"

    p = client.patch("/api/branding", json={"site_name": "Acme Plus"})
    assert p.status_code == 200, p.text
    assert p.json()["site_name"] == "Acme Plus"
    assert p.json()["primary_color"] == "#00ff00"

    bad = client.patch("/api/branding", json={"primary_color": "nope"})
    assert bad.status_code == 400

    d = client.delete("/api/branding")
    assert d.status_code == 200
    assert d.json()["site_name"] == "VigilCam"
    branding_service_mod.reset_branding()
