import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services import model_registry as mr

BASE = Path(__file__).resolve().parents[1]
HTML = (BASE / "app" / "templates" / "index.html").read_text(encoding="utf-8")


def test_model_registry_ui_present():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    for token in [
        'id="modelRegistryPanel"',
        'id="modelRegistryList"',
        'id="modelRegistryForm"',
        'id="modelRegistryStatus"',
        'id="modelName"',
        'id="modelProvider"',
        'id="modelType"',
        'loadModelRegistry',
        '/api/model_registry',
    ]:
        assert token in html, token


def test_model_registry_ui_aliases():
    for token in [
        "model-registry",
        "model_registry",
        "modelRegistry",
        "modelsList",
        "registeredModels",
        "registerModel",
        "addModel",
        "createModel",
        "deleteModel",
        "activateModel",
        "setActiveModel",
        "getActiveModel",
        "searchModels",
        "filterModels",
        "clearModels",
        "resetModels",
        "importModels",
        "exportModels",
    ]:
        assert token in HTML, token


def test_model_registry_ui_api_endpoints():
    client = TestClient(app)
    mr.clear()
    r = client.post("/api/model_registry", json={"name": "ui_m", "type": "detection", "provider": "x"})
    assert r.status_code == 200, r.text
    r2 = client.get("/api/model_registry")
    assert r2.status_code == 200
    assert isinstance(r2.json(), list)
    r3 = client.get("/api/model_registry/ui_m")
    assert r3.status_code == 200
    assert r3.json()["name"] == "ui_m"
    r4 = client.post("/api/model_registry/ui_m/activate")
    assert r4.status_code == 200
    r5 = client.get("/api/model_registry/active")
    assert r5.status_code == 200
    r6 = client.delete("/api/model_registry/ui_m")
    assert r6.status_code == 200


def test_model_registry_ui_service_helpers():
    mr.clear()
    m = mr.register_model(name="svc_ui", type="detection", active=True)
    assert m["name"] == "svc_ui"
    assert mr.get_active("detection")["name"] == "svc_ui"
    assert mr.exists("svc_ui") is True
    assert mr.count() == 1
    assert "Registered" in HTML or "Activated" in HTML or "Loaded" in HTML
