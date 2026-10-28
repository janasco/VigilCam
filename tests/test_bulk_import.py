import asyncio

from fastapi.testclient import TestClient


def test_bulk_import_imports():
    import app.services.bulk_import  # noqa: F401
    import app.api.bulk_import  # noqa: F401
    from app.services.bulk_import import bulk_import_devices, preview_bulk_import  # noqa: F401
    from app.api.bulk_import import router  # noqa: F401


def test_bulk_import_paths_present():
    from app.main import app

    paths = list(app.openapi()["paths"].keys())
    assert any("bulk-import" in p or "bulk_import" in p for p in paths)
    assert any(p.endswith("/bulk-import/preview") or p.endswith("/bulk_import/preview") for p in paths)
    assert any(p.endswith("/bulk-import/import") or p.endswith("/bulk_import/import") for p in paths)


def test_normalize_and_payload():
    from app.services import bulk_import as svc

    dev = svc.normalize_device({"ip": "192.168.1.10", "onvif": True})
    assert dev["ip"] == "192.168.1.10"
    assert dev["ip_address"] == "192.168.1.10"
    assert svc.extract_ip(dev) == "192.168.1.10"

    payload = svc.device_to_camera_payload(dev, username="admin", password="pw")
    assert payload.name
    assert payload.kind == "onvif"
    assert "192.168.1.10" in payload.source
    assert payload.ip_address == "192.168.1.10"
    assert payload.onvif_host == "192.168.1.10"

    rtsp = svc.normalize_device({"ip": "192.168.1.11", "source": "rtsp://192.168.1.11:554/live"})
    p2 = svc.device_to_camera_payload(rtsp)
    assert p2.ip_address == "192.168.1.11"


def test_filter_new_devices():
    from app.services import bulk_import as svc

    devices = [{"ip": "192.168.1.10"}, {"ip": "192.168.1.11"}]
    assert len(svc.filter_new_devices(devices, {"192.168.1.10"})) == 1
    assert len(svc.filter_new_devices(devices, set())) == 2


def test_bulk_import_devices_service(monkeypatch):
    from app.services import bulk_import as svc
    from app.services import cameras as cameras_service
    from app.models.entities import Camera

    async def fake_list(db):
        return []

    made = []

    async def fake_create(db, payload):
        cam = Camera(
            name=payload.name,
            kind=payload.kind,
            source=payload.source,
            onvif_host=getattr(payload, "onvif_host", None),
            ip_address=getattr(payload, "ip_address", None),
        )
        cam.id = len(made) + 1
        made.append(cam)
        return cam

    monkeypatch.setattr(cameras_service, "list_cameras", fake_list)
    monkeypatch.setattr(cameras_service, "create_camera", fake_create)

    devices = [
        {"ip": "192.168.1.10", "onvif": True, "name": "Cam 10"},
        {"ip": "192.168.1.11", "onvif": False, "source": "rtsp://192.168.1.11:554/live"},
    ]
    result = asyncio.run(svc.bulk_import_devices(object(), devices, username="admin", password="pw"))
    assert result["total_created"] == 2
    assert result["total_skipped"] == 0
    assert len(result["created"]) == 2

    # Second import with existing IPs should skip.
    async def fake_list2(db):
        return made

    monkeypatch.setattr(cameras_service, "list_cameras", fake_list2)
    result2 = asyncio.run(svc.bulk_import_devices(object(), devices))
    assert result2["total_created"] == 0
    assert result2["total_skipped"] == 2


def test_bulk_import_endpoints(monkeypatch):
    from app.main import app
    from app.api.deps import db_session
    from app.services import cameras as cameras_service
    from app.services import network_discovery as nd
    from app.models.entities import Camera

    async def fake_list(db):
        return []

    async def fake_create(db, payload):
        cam = Camera(
            name=payload.name,
            kind=payload.kind,
            source=payload.source,
            onvif_host=getattr(payload, "onvif_host", None),
            ip_address=getattr(payload, "ip_address", None),
        )
        cam.id = 99
        return cam

    async def fake_discover(timeout=0.5, **kwargs):
        return [{"ip": "192.168.1.50", "onvif": True, "name": "Cam 50"}]

    monkeypatch.setattr(cameras_service, "list_cameras", fake_list)
    monkeypatch.setattr(cameras_service, "create_camera", fake_create)
    monkeypatch.setattr(nd, "discover_network_cameras", fake_discover)

    async def fake_session():
        yield object()

    app.dependency_overrides[db_session] = fake_session
    try:
        client = TestClient(app)
        r = client.get("/api/bulk-import/preview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body, list)
        assert body[0]["ip"] == "192.168.1.50"

        r2 = client.post(
            "/api/bulk-import/import",
            json={"devices": [{"ip": "192.168.1.60", "onvif": True}], "username": "admin"},
        )
        assert r2.status_code == 200, r2.text
        data = r2.json()
        assert data["total_created"] == 1
        assert len(data["created"]) == 1

        r3 = client.post("/api/bulk_import/import", json={"devices": []})
        assert r3.status_code == 200, r3.text
    finally:
        app.dependency_overrides.pop(db_session, None)
