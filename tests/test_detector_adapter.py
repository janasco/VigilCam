import base64
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services import detector_adapter as da
from app.services.detector_adapter import (
    register_adapter,
    get_adapter,
    list_adapters,
    infer,
    clear,
    detector_adapter_service,
)


def test_detector_adapter_import():
    import importlib as _il
    _il.import_module("app.services.detector_adapter")  # noqa: F401
    _il.import_module("app.api.detector_adapter")  # noqa: F401
    from app.services.detector_adapter import register_adapter, infer, detector_adapter_service  # noqa: F401
    from app.api.detector_adapter import router  # noqa: F401


def test_detector_adapter_paths_present():
    import importlib as _il
    _il.import_module("app.api.detector_adapter")  # noqa: F401 -- ensure auto-loader saw it
    paths = list(app.openapi()["paths"].keys())
    assert "/api/detector_adapter" in paths or "/api/detector-adapter" in paths
    assert "/api/detector-adapter/infer" in paths or "/api/detector_adapter/infer" in paths


def test_detector_adapter_register_and_list():
    clear()
    rec = register_adapter(name="blue-candle-test", endpoint="http://127.0.0.1:32168/v1/vision/detection")
    assert rec["name"] == "blue-candle-test"
    assert rec["endpoint"] == "http://127.0.0.1:32168/v1/vision/detection"
    assert get_adapter("blue-candle-test")["name"] == "blue-candle-test"
    names = [a["name"] for a in list_adapters()]
    assert "blue-candle-test" in names
    assert detector_adapter_service.list_adapters() is not None


def test_detector_adapter_infer_via_http_mocked():
    clear()
    register_adapter(name="blue-candle-test", endpoint="http://127.0.0.1:32168/v1/vision/detection")

    class Resp:
        status_code = 200

        def json(self):
            return {
                "success": True,
                "message": "",
                "predictions": [
                    {"x_max": 200, "x_min": 10, "y_max": 200, "y_min": 10, "confidence": 0.9, "label": "person"}
                ],
                "count": 1,
                "command": "detect",
                "module_id": "Yolo8",
                "execution_provider": "CPU",
                "can_useGPU": False,
                "inference_ms": 80,
                "process_ms": 360,
                "analysis_round_trip_ms": 369,
            }

    with patch("app.services.detector_adapter.httpx.post", return_value=Resp()):
        res = infer(adapter="blue-candle-test", image_bytes=b"fake-jpeg-bytes", min_confidence=0.25)
        assert res["success"] is True
        assert res["fallback"] is False
        assert res["count"] == 1
        assert res["predictions"][0]["label"] == "person"
        assert res["adapter"] == "blue-candle-test"


def test_detector_adapter_fallback_mock():
    clear()
    register_adapter(name="down-detector", endpoint="http://127.0.0.1:9/v1/vision/detection")

    def _boom(*a, **k):
        raise ConnectionError("refused")

    with patch("app.services.detector_adapter.httpx.post", side_effect=_boom):
        res = infer(adapter="down-detector", image_bytes=b"fake-jpeg-bytes")
        assert res["success"] is True
        assert res["fallback"] is True
        assert res["mock"] is True
        assert isinstance(res["predictions"], list)
        assert res["count"] >= 1

    # No image at all also yields mock fallback, never raises.
    res2 = da.mock_infer(adapter="down-detector")
    assert res2["fallback"] is True
    assert res2["success"] is True


def test_detector_adapter_api():
    clear()
    client = TestClient(app)

    r = client.post("/api/detector-adapter/register", json={"name": "api-detector", "endpoint": "http://127.0.0.1:32168/v1/vision/detection"})
    assert r.status_code == 200, r.text
    assert r.json().get("name") == "api-detector"

    r = client.get("/api/detector-adapter")
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True

    r = client.get("/api/detector-adapter/status")
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "ok"

    b64 = base64.b64encode(b"fake-jpeg-bytes").decode()

    class Resp:
        status_code = 200

        def json(self):
            return {
                "success": True,
                "predictions": [
                    {"x_max": 50, "x_min": 5, "y_max": 50, "y_min": 5, "confidence": 0.8, "label": "car"}
                ],
                "count": 1,
            }

    with patch("app.services.detector_adapter.httpx.post", return_value=Resp()):
        r = client.post("/api/detector-adapter/infer", json={"adapter": "api-detector", "image_base64": b64})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("success") is True
        assert body.get("fallback") is False
        assert body.get("predictions")[0]["label"] == "car"

    def _boom(*a, **k):
        raise ConnectionError("down")

    with patch("app.services.detector_adapter.httpx.post", side_effect=_boom):
        r = client.post("/api/detector_adapter/detect", json={"adapter": "api-detector", "image_base64": b64})
        assert r.status_code == 200, r.text
        assert r.json().get("fallback") is True


def test_detector_adapter_clear():
    clear()
    assert list_adapters() == []
    register_adapter("x", "http://127.0.0.1:32168/v1/vision/detection")
    assert len(list_adapters()) == 1
    clear()
    assert list_adapters() == []
