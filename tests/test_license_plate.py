import base64

from fastapi.testclient import TestClient

from app.main import app
from app.services import license_plate as svc


def test_service_import():
    import app.services.license_plate  # noqa: F401
    from app.services.license_plate import recognize_plate, list_plates, clear  # noqa: F401


def test_service_recognize():
    svc.clear()
    res = svc.recognize_plate(image_base64="dGVzdA==")
    assert isinstance(res, dict)
    assert res.get("plate")
    assert res.get("confidence") is not None
    assert res.get("plates")
    assert len(res["plates"]) >= 1


def test_service_aliases():
    svc.clear()
    a = svc.recognize("dGVzdA==")
    b = svc.detect_plate(image_base64="dGVzdA==")
    assert a is not None and b is not None
    assert svc.count() if hasattr(svc, "count") else True


def test_service_async_compat():
    import asyncio

    async def main():
        svc.clear()
        res = await svc.recognize_plate(image_base64="dGVzdA==")
        assert res.get("plate")
        lst = await svc.list_plates()
        assert isinstance(lst, list)
        assert len(lst) == 1

    asyncio.run(main())


def test_api_recognize():
    client = TestClient(app)
    r = client.post("/api/lpr/recognize", json={"image_base64": "dGVzdA=="})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("plate")
    assert data.get("confidence") is not None


def test_api_list_and_clear():
    client = TestClient(app)
    client.post("/api/license_plate/recognize", json={"image_base64": "dGVzdA=="})
    r = client.get("/api/license_plate")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    c = client.post("/api/license_plate/clear")
    assert c.status_code == 200


def test_api_status_health():
    client = TestClient(app)
    assert client.get("/api/lpr/status").status_code == 200
    assert client.get("/api/lpr/health").status_code == 200
