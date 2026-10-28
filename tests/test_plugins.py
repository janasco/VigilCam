import pathlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app as main_app
from app.services import plugins as P


def test_discover_plugins(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PLUGINS_DIR", tmp_path)
    monkeypatch.setattr(P, "STATE_FILE", tmp_path / "state.json")
    (tmp_path / "alpha.py").write_text("def register(app):\n    pass\n")
    items = P.discover_plugins()
    assert [i["name"] for i in items] == ["alpha"]
    assert items[0]["has_register"] is True


def test_load_plugins_calls_register(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PLUGINS_DIR", tmp_path)
    monkeypatch.setattr(P, "STATE_FILE", tmp_path / "state.json")
    (tmp_path / "beta.py").write_text(
        "def register(app):\n    app.state.beta_loaded = True\n"
    )
    a = FastAPI()
    loaded = P.load_plugins(a)
    assert loaded and loaded[0]["name"] == "beta"
    assert getattr(a.state, "beta_loaded", False) is True


def test_enable_disable_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PLUGINS_DIR", tmp_path)
    monkeypatch.setattr(P, "STATE_FILE", tmp_path / "state.json")
    (tmp_path / "gamma.py").write_text("def register(app):\n    pass\n")
    assert P.is_enabled("gamma") is True
    P.disable_plugin("gamma")
    assert P.is_enabled("gamma") is False
    P.enable_plugin("gamma")
    assert P.is_enabled("gamma") is True


def test_install_plugin_from_code(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PLUGINS_DIR", tmp_path)
    monkeypatch.setattr(P, "STATE_FILE", tmp_path / "state.json")
    code = "def register(app):\n    app.state.installed = True\n"
    info = P.install_plugin(name="installed", code=code, app=None, enable=True)
    assert info["name"] == "installed"
    assert (tmp_path / "installed.py").exists()
    assert P.is_enabled("installed") is True


def test_api_plugins_endpoints():
    client = TestClient(main_app)
    r = client.get("/api/plugins")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    # enable/disable alias for sample plugin
    r2 = client.post("/api/plugins/hello_plugin/disable")
    assert r2.status_code == 200
    r3 = client.post("/api/plugins/hello_plugin/enable")
    assert r3.status_code == 200


def test_api_install_endpoint():
    client = TestClient(main_app)
    code = "def register(app):\n    pass\n"
    r = client.post("/api/plugins/install", json={"name": "api_installed", "code": code})
    assert r.status_code == 200, r.text
    # cleanup
    pathlib.Path("plugins/api_installed.py").unlink(missing_ok=True)
