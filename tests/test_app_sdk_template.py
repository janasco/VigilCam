import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import app_sdk_template as tmpl
from app.api import app_sdk_template as tmpl_api


def test_service_register_list_get():
    tmpl.clear()
    t = tmpl.register_template(name="t1", type="sdk")
    assert t["name"] == "t1"
    assert tmpl.get_template("t1") is not None
    assert len(tmpl.list_templates()) == 1
    assert tmpl.exists("t1") is True


def test_service_active():
    tmpl.clear()
    tmpl.register_template(name="a", type="sdk")
    tmpl.register_template(name="b", type="sdk", active=True)
    assert tmpl.get_active("sdk")["name"] == "b"
    assert tmpl.get_template("a")["active"] is False


def test_aliases():
    tmpl.clear()
    a = tmpl.register(name="x")
    b = tmpl.add_template(name="y")
    assert a is not None and b is not None
    assert tmpl.count() == 2


def test_api_endpoints():
    app = FastAPI()
    app.include_router(tmpl_api.router)
    client = TestClient(app)
    r = client.post("/api/app_sdk_template", json={"name": "api_t", "type": "sdk"})
    assert r.status_code == 200, r.text
    r2 = client.get("/api/app_sdk_template")
    assert r2.status_code == 200
    assert isinstance(r2.json(), list)
    r3 = client.get("/api/app_sdk_template/api_t")
    assert r3.status_code == 200
    assert r3.json()["name"] == "api_t"
