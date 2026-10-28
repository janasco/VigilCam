from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.firmware import router as firmware_router
from app.services import firmware as fw


def test_firmware_imports():
    import app.services.firmware  # noqa: F401
    import app.api.firmware  # noqa: F401
    from app.services.firmware import (  # noqa: F401
        check_firmware,
        compare_versions,
        parse_version,
        get_latest_version,
        is_update_available,
        firmware_service,
    )


def test_firmware_version_parsing():
    assert fw.parse_version("1.2.3") == (1, 2, 3)
    assert fw.parse_version("v2.0.1") == (2, 0, 1)
    assert fw.parse_version("1.2") == (1, 2, 0)
    assert fw.parse_version(None) == (0, 0, 0)
    assert fw.parse_version("") == (0, 0, 0)


def test_firmware_compare_versions():
    assert fw.compare_versions("1.0.0", "2.0.0") == -1
    assert fw.compare_versions("2.5.0", "2.5.0") == 0
    assert fw.compare_versions("3.0.0", "2.9.9") == 1
    assert fw.compare_versions("1.9.4", "1.10.0") == -1


def test_firmware_update_available():
    assert fw.is_update_available("1.0.0", "2.5.0") is True
    assert fw.is_update_available("2.5.0", "2.5.0") is False
    assert fw.is_update_available("3.0.0", "2.5.0") is False
    # Mock model lookup.
    assert fw.is_update_available("1.0.0", model="generic") is True
    assert fw.is_update_available("9.9.9", model="generic") is False


def test_firmware_check_mock():
    fw.reset_mock_data()
    res = fw.check_firmware(current_version="1.0.0", model="generic")
    assert res["current_version"] == "1.0.0"
    assert res["latest_version"] == "2.5.0"
    assert res["update_available"] is True
    assert res["status"] == "update_available"

    up = fw.check_firmware(current_version="2.5.0", model="generic")
    assert up["update_available"] is False
    assert up["up_to_date"] is True
    assert up["status"] == "up_to_date"

    newer = fw.check_firmware(current_version="9.9.9", model="generic")
    assert newer["update_available"] is False
    assert newer["status"] == "newer_than_latest"

    with_cam = fw.check_firmware(current_version="1.0.0", model="generic", camera_id=42)
    assert with_cam["camera_id"] == 42


def _client():
    app = FastAPI()
    app.include_router(firmware_router)
    return TestClient(app)


def test_firmware_router_endpoints():
    client = _client()

    r = client.get("/api/firmware/latest", params={"model": "generic"})
    assert r.status_code == 200, r.text
    assert r.json()["latest_version"] == "2.5.0"

    r = client.get("/api/firmware/check", params={"current_version": "1.0.0", "model": "generic"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["update_available"] is True
    assert data["latest_version"] == "2.5.0"

    r = client.post("/api/firmware/check", json={"current_version": "2.5.0", "model": "generic"})
    assert r.status_code == 200, r.text
    assert r.json()["update_available"] is False

    r = client.get("/api/firmware/status", params={"current_version": "1.0.0", "model": "generic"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "update_available"

    r = client.get("/api/cameras/7/firmware", params={"current_version": "1.0.0", "model": "generic"})
    assert r.status_code == 200, r.text
    assert r.json()["camera_id"] == 7


def test_firmware_app_registers_router():
    from app.main import app as _app

    paths = list(_app.openapi()["paths"].keys())
    assert "/api/firmware/check" in paths
    assert "/api/firmware/latest" in paths
