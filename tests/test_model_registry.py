import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import model_registry as mr
from app.api import model_registry as mr_api


def test_service_register_list_get():
    mr.clear()
    m = mr.register_model(name="m1", type="detection", provider="ultra")
    assert m["name"] == "m1"
    assert mr.get_model("m1") is not None
    assert len(mr.list_models()) == 1
    assert mr.exists("m1") is True


def test_service_active():
    mr.clear()
    mr.register_model(name="a", type="detection")
    mr.register_model(name="b", type="detection", active=True)
    assert mr.get_active("detection")["name"] == "b"
    assert mr.get_model("a")["active"] is False


def test_aliases():
    mr.clear()
    a = mr.register(name="x")
    b = mr.add_model(name="y")
    assert a is not None and b is not None
    assert mr.count() == 2


def test_api_endpoints():
    app = FastAPI()
    app.include_router(mr_api.router)
    client = TestClient(app)
    r = client.post("/api/model_registry", json={"name": "api_m", "type": "detection"})
    assert r.status_code == 200, r.text
    r2 = client.get("/api/model_registry")
    assert r2.status_code == 200
    assert isinstance(r2.json(), list)
    r3 = client.get("/api/model_registry/api_m")
    assert r3.status_code == 200
    assert r3.json()["name"] == "api_m"
