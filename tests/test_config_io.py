"""Tests for full system config export/import (JSON document)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def test_config_io_imports():
    import app.api.config_io as api_config_io  # noqa: F401
    import app.services.config_io as svc_config_io  # noqa: F401
    from app.services.config_io import (  # noqa: F401
        config_io_service,
        export_config_db,
        import_config_db,
        export_system_config,
        import_system_config,
        build_envelope,
        extract_config,
        validate_config,
    )

    assert hasattr(api_config_io, "router")
    assert hasattr(svc_config_io, "config_io_service")


def test_config_io_routes_present():
    from app.main import app

    paths = set(app.openapi()["paths"].keys())
    assert "/api/config-io/export" in paths
    assert "/api/config_io/export" in paths or "/api/system/config/export" in paths
    assert "/api/config-io/sections" in paths
    assert "/api/config-io/validate" in paths


def _client():
    from app.main import app

    return TestClient(app)


def _sample_sections():
    return {
        "groups": [{"name": "ConfigIOTestGroup", "description": "config-io test group"}],
        "layouts": [{"name": "ConfigIOTestLayout", "json_layout": {"grid": [1]}}],
        "cameras": [
            {
                "name": "ConfigIOTestCam",
                "kind": "ip",
                "source": "rtsp://127.0.0.1/config-io-test",
                "enabled": True,
            }
        ],
    }


def test_pure_envelope_roundtrip():
    from app.services import config_io as svc

    sections = _sample_sections()
    envelope = svc.build_envelope(sections)
    assert envelope["version"] == svc.CONFIG_IO_VERSION
    assert envelope["kind"] == svc.CONFIG_KIND
    assert envelope["sections"]["groups"][0]["name"] == "ConfigIOTestGroup"
    assert envelope["counts"]["cameras"] == 1

    # Envelope, flat map, JSON string and bytes all parse.
    assert svc.extract_config(envelope)["groups"][0]["name"] == "ConfigIOTestGroup"
    assert svc.extract_config(sections)["layouts"][0]["name"] == "ConfigIOTestLayout"
    assert svc.extract_config(json.dumps(envelope))["cameras"][0]["name"] == "ConfigIOTestCam"
    assert svc.extract_config(json.dumps(envelope).encode("utf-8"))["cameras"][0]["name"] == "ConfigIOTestCam"
    assert svc.validate_config(envelope) is True
    assert svc.validate_config({}) is False
    assert svc.validate_config(None) is False
    assert svc.validate_config({"bogus_section": [{"a": 1}]}) is False

    text = svc.export_config_json(sections)
    assert svc.extract_config(text)["groups"][0]["name"] == "ConfigIOTestGroup"
    for alias in (svc.dumps, svc.dump_json, svc.to_json, svc.to_json_str, svc.serialize):
        assert "ConfigIOTestGroup" in alias(sections)
    for loader in (svc.parse_json, svc.loads, svc.from_json, svc.deserialize, svc.load_json):
        assert loader(text)["groups"][0]["name"] == "ConfigIOTestGroup"


def test_save_and_load_file(tmp_path):
    from app.services import config_io as svc

    path = tmp_path / "config.json"
    saved = svc.save_config(path, _sample_sections())
    assert str(path) in saved
    loaded = svc.load_config_file(path)
    assert loaded["groups"][0]["name"] == "ConfigIOTestGroup"
    assert svc.load_config_file(tmp_path / "missing.json") == {}


def test_sections_and_validate_endpoints():
    client = _client()
    r = client.get("/api/config-io/sections")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "cameras" in body["sections"]
    assert "groups" in body["sections"]
    assert "settings" in body["sections"]

    v = client.post("/api/config-io/validate", json={"sections": _sample_sections()})
    assert v.status_code == 200, v.text
    assert v.json()["valid"] is True
    assert v.json()["sections"]["groups"] == 1

    bad = client.post("/api/config-io/validate", json={"sections": {}})
    assert bad.status_code == 200, bad.text
    assert bad.json()["valid"] is False

    # Alias prefixes expose the same metadata.
    for prefix in ("/api/config_io", "/api/system/config", "/api/config"):
        rr = client.get(f"{prefix}/sections")
        assert rr.status_code == 200, (prefix, rr.text)


def test_export_import_roundtrip():
    client = _client()

    exp = client.get("/api/config-io/export")
    assert exp.status_code == 200, exp.text
    envelope = exp.json()
    assert envelope["kind"] == "vigilcam-system-config"
    assert isinstance(envelope.get("sections"), dict)
    assert "cameras" in envelope["sections"]
    assert "groups" in envelope["sections"]

    # Root export aliases agree.
    assert client.get("/api/config-io").status_code == 200
    assert client.get("/api/config_io/export").status_code == 200
    assert client.get("/api/system/config/export").status_code == 200

    # Merge-import the live export: idempotent, must not duplicate cameras.
    before = {c.get("name") for c in envelope["sections"].get("cameras", [])}
    imp = client.post("/api/config-io/import", json=envelope)
    assert imp.status_code == 200, imp.text
    assert imp.json().get("ok") is True
    assert imp.json().get("count", 0) >= 0

    again = client.get("/api/config-io/export").json()
    after = [c.get("name") for c in again["sections"].get("cameras", [])]
    assert len(after) == len(set(after)), "camera names duplicated by merge import"
    assert set(after) >= before


def test_import_new_group_and_layout_with_cleanup():
    client = _client()
    payload = {
        "sections": {
            "groups": [{"name": "ConfigIOTmpGroup", "description": "tmp"}],
            "layouts": [{"name": "ConfigIOTmpLayout", "json_layout": {"cells": []}}],
        }
    }
    imp = client.post("/api/config-io/import", json=payload)
    assert imp.status_code == 200, imp.text
    body = imp.json()
    assert body["imported"].get("groups", 0) + body["updated"].get("groups", 0) >= 1

    exp = client.get("/api/config-io/export").json()
    assert any(g.get("name") == "ConfigIOTmpGroup" for g in exp["sections"].get("groups", []))
    assert any(g.get("name") == "ConfigIOTmpLayout" for g in exp["sections"].get("layouts", []))

    # Re-import is idempotent for natural-key sections (no duplicates).
    imp2 = client.post("/api/config-io/import", json=payload)
    assert imp2.status_code == 200, imp2.text
    exp2 = client.get("/api/config-io/export").json()
    assert sum(1 for g in exp2["sections"].get("groups", []) if g.get("name") == "ConfigIOTmpGroup") == 1
    assert sum(1 for g in exp2["sections"].get("layouts", []) if g.get("name") == "ConfigIOTmpLayout") == 1

    # Cleanup via the groups/layouts APIs.
    groups = client.get("/api/groups")
    if groups.status_code == 200:
        for g in groups.json():
            if g.get("name") == "ConfigIOTmpGroup":
                client.delete(f"/api/groups/{g['id']}")
    layouts = client.get("/api/layouts")
    if layouts.status_code == 200:
        items = layouts.json()
        if isinstance(items, dict):
            items = items.get("layouts", items.get("items", []))
        for item in items or []:
            if isinstance(item, dict) and item.get("name") == "ConfigIOTmpLayout":
                client.delete(f"/api/layouts/{item['id']}")


def test_import_rejects_empty_and_bad_mode():
    client = _client()
    r = client.post("/api/config-io/import", json={"sections": {}})
    assert r.status_code == 400, r.text
    r2 = client.post("/api/config-io/import", json={})
    assert r2.status_code == 400, r2.text
    r3 = client.post("/api/config-io/import?mode=bogus", json={"sections": _sample_sections()})
    assert r3.status_code == 400, r3.text


def test_download_and_upload_roundtrip():
    client = _client()
    dl = client.get("/api/config-io/download")
    assert dl.status_code == 200, dl.text
    assert "attachment" in dl.headers.get("content-disposition", "")
    envelope = json.loads(dl.content.decode("utf-8"))
    assert envelope["kind"] == "vigilcam-system-config"

    up = client.post(
        "/api/config-io/upload",
        files={"file": ("config.json", dl.content, "application/json")},
    )
    assert up.status_code == 200, up.text
    assert up.json().get("ok") is True

    up2 = client.post(
        "/api/config-io/import-file",
        files={"file": ("config.json", dl.content, "application/json")},
    )
    assert up2.status_code == 200, up2.text
