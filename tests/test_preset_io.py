import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import preset_io as svc
from app.api import preset_io as preset_io_api


def _sample_camera(name="Front Door", source="rtsp://192.168.1.10/live"):
    return {
        "name": name,
        "kind": "ip",
        "source": source,
        "username": "admin",
        "onvif_host": "192.168.1.10",
        "enabled": True,
        "recording_enabled": True,
        "detection_enabled": True,
        "width": 1920,
        "height": 1080,
        "fps": 15,
        "manufacturer": "Acme",
        "id": 999,  # runtime field, must be stripped
        "created_at": "2024-01-01T00:00:00",
    }


def test_camera_to_preset_strips_runtime_fields():
    preset = svc.camera_to_preset(_sample_camera())
    assert preset["name"] == "Front Door"
    assert preset["source"] == "rtsp://192.168.1.10/live"
    assert "id" not in preset
    assert "created_at" not in preset
    assert svc.validate_preset(preset) is True


def test_validate_rejects_bad_presets():
    assert svc.validate_preset({}) is False
    assert svc.validate_preset({"name": "x"}) is False
    assert svc.validate_preset({"source": "y"}) is False
    assert svc.validate_preset(None) is False
    assert svc.validate_preset({"name": "x", "source": "y", "kind": "bogus"}) is False
    assert svc.validate_preset({"name": "x", "source": "y"}) is True


def test_export_json_roundtrip():
    cameras = [_sample_camera("Cam A", "rtsp://a/x"), _sample_camera("Cam B", "rtsp://b/x")]
    text = svc.export_presets_json(cameras)
    data = json.loads(text)
    assert data["kind"] == svc.PRESET_KIND
    assert data["count"] == 2
    assert len(data["presets"]) == 2
    back = svc.extract_presets(text)
    assert len(back) == 2
    assert back[0]["name"] == "Cam A"
    # Bare list and envelope both parse.
    assert svc.extract_presets(data) == svc.extract_presets(data["presets"])
    assert svc.extract_presets({"cameras": data["presets"]})[0]["name"] == "Cam A"


def test_import_presets_data_skips_invalid():
    payload = {
        "version": "1.0",
        "kind": "camera-presets",
        "presets": [
            _sample_camera("Good", "rtsp://good/x"),
            {"name": "Bad"},  # missing source
        ],
    }
    out = svc.import_presets_data(payload)
    assert len(out) == 1
    assert out[0]["name"] == "Good"
    assert "id" not in out[0]
    out_all = svc.import_presets_data(payload, skip_invalid=False)
    assert len(out_all) == 2


def test_save_and_load_file(tmp_path):
    cameras = [_sample_camera("File Cam", "rtsp://file/x")]
    path = tmp_path / "presets.json"
    saved = svc.save_presets(path, cameras)
    assert str(path) in saved
    loaded = svc.load_presets_file(path)
    assert len(loaded) == 1
    assert loaded[0]["name"] == "File Cam"
    assert svc.load_presets_file(tmp_path / "missing.json") == []


def test_router_export_import_with_isolated_app():
    app = FastAPI()
    app.include_router(preset_io_api.router)
    client = TestClient(app)
    # Export against a fresh DB may be empty but must succeed.
    r = client.get("/api/preset-io/export")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "presets" in body
    assert isinstance(body["presets"], list)
    # Alias paths serve the same export.
    for path in ("/api/preset_io", "/api/preset-io", "/api/presets-io/export"):
        rr = client.get(path if path.endswith("export") else path + "/export" if path == "/api/presets-io/export" else path)
        assert rr.status_code == 200, (path, rr.text)
    # Validate endpoint works without DB.
    v = client.post(
        "/api/preset-io/validate",
        json={"presets": [_sample_camera("V", "rtsp://v/x"), {"name": "bad"}]},
    )
    assert v.status_code == 200, v.text
    assert v.json()["count"] == 2
    assert v.json()["valid"] == 1
    # Import with no presets is a 400.
    bad = client.post("/api/preset-io/import", json={"presets": []})
    assert bad.status_code in (400, 422), bad.text


def test_router_import_export_roundtrip_real_app():
    from app.main import app as real_app

    client = TestClient(real_app)
    created_ids: list[int] = []
    try:
        # Create a camera to export.
        c = client.post(
            "/api/cameras",
            json={"name": "PresetIOTestCam", "kind": "ip", "source": "rtsp://127.0.0.1/preset-io-test"},
        )
        assert c.status_code in (200, 201), c.text
        created_ids.append(c.json()["id"])

        exp = client.get("/api/preset-io/export")
        assert exp.status_code == 200, exp.text
        presets = exp.json()["presets"]
        assert any(p.get("name") == "PresetIOTestCam" for p in presets)

        mine = [p for p in presets if p.get("name") == "PresetIOTestCam"]
        assert mine, "exported preset missing"
        payload_preset = dict(mine[0])
        payload_preset["name"] = "PresetIOTestCamRestored"
        imp = client.post("/api/preset-io/import", json={"presets": [payload_preset]})
        assert imp.status_code == 200, imp.text
        assert imp.json()["count"] == 1
        restored = imp.json()["cameras"][0]
        assert restored["name"] == "PresetIOTestCamRestored"

        # Track restored camera for cleanup.
        lst = client.get("/api/cameras")
        assert lst.status_code == 200
        for cam in lst.json():
            if cam.get("name") in ("PresetIOTestCam", "PresetIOTestCamRestored"):
                created_ids.append(cam["id"])
    finally:
        seen = set()
        for cid in created_ids:
            if cid in seen:
                continue
            seen.add(cid)
            try:
                client.delete(f"/api/cameras/{cid}")
            except Exception:  # noqa: BLE001
                pass
