import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import source_presets as sp
from app.api import source_presets as sp_api


def test_service_parse_returns_dict():
    data = sp.parse_source_presets()
    assert isinstance(data, dict)
    assert len(data) > 0
    assert "2n Helios" in data or len(data) > 100
    for k, v in list(data.items())[:5]:
        assert isinstance(v, list)
        assert len(v) > 0
        assert v[0]["manufacturer"] == k
        assert "source" in v[0]
        assert "prefix" in v[0]
        assert "url" in v[0]


def test_service_aliases():
    a = sp.load_source_presets()
    b = sp.get_source_presets()
    c = sp.categorized_source_presets()
    assert a == b == c
    lst = sp.list_source_presets()
    assert isinstance(lst, list)
    assert len(lst) == len(a)
    assert "presets" in lst[0]
    assert lst[0]["presets"] == a[lst[0]["manufacturer"]]


def test_router_endpoints():
    app = FastAPI()
    app.include_router(sp_api.router)
    client = TestClient(app)
    r = client.get("/api/source_presets")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict)
    assert len(data) > 0
    r2 = client.get("/api/source_presets/categories")
    assert r2.status_code == 200
    assert isinstance(r2.json(), list)
    assert len(r2.json()) == len(data)


def test_env_override(tmp_path):
    xml = tmp_path / "Sources.xml"
    xml.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<Manufacturers>
  <Manufacturer name="Acme">
    <url version="1" prefix="rtsp://" Source="FFMPEG" url="/live" port="554" />
  </Manufacturer>
</Manufacturers>
""",
        encoding="utf-8",
    )
    os.environ["ISPY_SOURCES_XML"] = str(xml)
    try:
        data = sp.parse_source_presets()
        assert data == {"Acme": [{"manufacturer": "Acme", "name": "Acme", "version": "1", "confirmed": None, "prefix": "rtsp://", "source": "FFMPEG", "url": "/live", "fixed": None, "audio_model": None, "flags": None, "port": 554, "cookies": None, "audio_source": None, "audio_url": None, "id": None, "token_path": None, "token_post": None, "token_port": None, "Source": "FFMPEG", "Url": "/live", "Prefix": "rtsp://", "Version": "1", "Port": 554}]}
    finally:
        del os.environ["ISPY_SOURCES_XML"]
