import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services import i18n, localization

BASE = Path(__file__).resolve().parents[1]


def test_index_has_locale_switcher():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "localeSwitcher" in r.text
    assert "locale-switcher" in r.text or "localeSwitcher" in r.text
    assert "data-i18n" in r.text


def test_locale_strings_present():
    data = json.loads((BASE / "app" / "locales" / "en.json").read_text())
    assert isinstance(data, dict)
    # UI strings containers
    for key in ("ui", "nav", "tabs"):
        assert key in data
    # at least one known label
    assert data.get("ui", {}).get("live_grid") == "Live Grid" or data.get("live_grid") == "Live Grid"


def test_locale_api_lists_en():
    client = TestClient(app)
    r = client.get("/api/localization")
    assert r.status_code == 200
    body = r.json()
    locales = body.get("locales") if isinstance(body, dict) else body
    assert "en" in (locales or [])


def test_locale_api_returns_strings():
    client = TestClient(app)
    r = client.get("/api/localization/en")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    assert body.get("hello") == "Hello" or "hello" in body


def test_set_locale_endpoint():
    client = TestClient(app)
    r = client.post("/api/localization/set-locale", json={"locale": "en"})
    assert r.status_code == 200
    assert r.json().get("locale") == "en"


def test_services_expose_locale_switcher_helpers():
    # sanity: services still load
    assert i18n.load_translations()["hello"] == "Hello"
    assert "en" in localization.list_locales()
