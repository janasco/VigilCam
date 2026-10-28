import base64
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services import cpai_adapter as cp
from app.services.cpai_adapter import (
    register_adapter,
    get_adapter,
    list_adapters,
    infer,
    clear,
    cpai_adapter_service,
    build_endpoint,
    to_cpai_response,
)


def test_cpai_adapter_import():
    import importlib as _il
    _il.import_module("app.services.cpai_adapter")  # noqa: F401
    _il.import_module("app.api.cpai_adapter")  # noqa: F401
    from app.services.cpai_adapter import register_adapter, infer, cpai_adapter_service  # noqa: F401
    from app.api.cpai_adapter import router  # noqa: F401


def test_cpai_adapter_distinct_from_detector_adapter():
    import importlib as _il
    det = _il.import_module("app.services.detector_adapter")
    assert cp is not det
    assert cp.ADAPTERS is not det.ADAPTERS
    # CPAI defaults differ / extend Blue Iris-style defaults.
    assert cp.DEFAULT_ENDPOINT == "http://127.0.0.1:32168/v1/vision/detection"
    assert "cpai" in cp.ADAPTER_TYPES or "codeproject-ai" in cp.ADAPTER_TYPES


def test_cpai_adapter_paths_present():
    import importlib as _il
    _il.import_module("app.api.cpai_adapter")  # noqa: F401 -- ensure auto-loader saw it
    paths = list(app.openapi()["paths"].keys())
    assert "/api/cpai_adapter" in paths or "/api/cpai-adapter" in paths or "/api/cpai" in paths
    assert (
        "/api/cpai-adapter/infer" in paths
        or "/api/cpai_adapter/infer" in paths
        or "/api/cpai/infer" in paths
    )
    # Native CodeProject.AI shim paths.
    assert "/v1/vision/detection" in paths
    assert "/v1/info" in paths


def test_cpai_adapter_register_and_list():
    clear()
    rec = register_adapter(name="cpai-test", endpoint="http://127.0.0.1:32168/v1/vision/detection")
    assert rec["name"] == "cpai-test"
    assert rec["endpoint"] == "http://127.0.0.1:32168/v1/vision/detection"
    assert get_adapter("cpai-test")["name"] == "cpai-test"
    names = [a["name"] for a in list_adapters()]
    assert "cpai-test" in names
    assert cpai_adapter_service.list_adapters() is not None


def test_cpai_adapter_custom_model_endpoint():
    clear()
    rec = register_adapter(name="cpai-custom", base_url="http://127.0.0.1:32168", custom_model="my-model")
    assert "/custom/my-model" in rec["endpoint"]
    assert rec["endpoint"] == "http://127.0.0.1:32168/v1/vision/custom/my-model"
    assert build_endpoint("http://127.0.0.1:32168", "my-model").endswith("/v1/vision/custom/my-model")
    assert build_endpoint("http://127.0.0.1:32168/v1/vision/detection") == "http://127.0.0.1:32168/v1/vision/detection"


def test_cpai_adapter_infer_via_http_mocked():
    clear()
    register_adapter(name="cpai-test", endpoint="http://127.0.0.1:32168/v1/vision/detection")

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
                "moduleId": "ObjectDetectionYOLOv5-6.2",
                "executionProvider": "CPU",
                "canUseGPU": False,
                "inferenceMs": 80,
                "processMs": 360,
                "analysisRoundTripMs": 369,
            }

    with patch("app.services.cpai_adapter.httpx.post", return_value=Resp()):
        res = infer(adapter="cpai-test", image_bytes=b"fake-jpeg-bytes", min_confidence=0.25)
        assert res["success"] is True
        assert res["fallback"] is False
        assert res["count"] == 1
        assert res["predictions"][0]["label"] == "person"
        assert res["adapter"] == "cpai-test"
        # Dual-case CPAI compatibility.
        assert res["moduleId"] == "ObjectDetectionYOLOv5-6.2"
        assert res["module_id"] == "ObjectDetectionYOLOv5-6.2"
        assert res["inferenceMs"] == 80
        assert res["inference_ms"] == 80


def test_cpai_adapter_camelcase_and_percent_confidence():
    clear()
    register_adapter(name="cpai-test", endpoint="http://127.0.0.1:32168/v1/vision/detection")

    class Resp:
        status_code = 200

        def json(self):
            return {
                "success": True,
                "Predictions": [
                    {"Confidence": 85, "Label": "car", "x_min": 5, "y_min": 5, "x_max": 50, "y_max": 50}
                ],
                "Count": 1,
                "ModuleId": "ObjectDetectionYOLOv5Net",
            }

    with patch("app.services.cpai_adapter.httpx.post", return_value=Resp()):
        res = infer(adapter="cpai-test", image_bytes=b"fake-jpeg-bytes")
        assert res["success"] is True
        assert res["fallback"] is False
        assert res["predictions"][0]["label"] == "car"
        assert abs(res["predictions"][0]["confidence"] - 0.85) < 1e-6


def test_cpai_adapter_fallback_mock():
    clear()
    register_adapter(name="down-cpai", endpoint="http://127.0.0.1:9/v1/vision/detection")

    def _boom(*a, **k):
        raise ConnectionError("refused")

    with patch("app.services.cpai_adapter.httpx.post", side_effect=_boom):
        res = infer(adapter="down-cpai", image_bytes=b"fake-jpeg-bytes")
        assert res["success"] is True
        assert res["fallback"] is True
        assert res["mock"] is True
        assert isinstance(res["predictions"], list)
        assert res["count"] >= 1

    res2 = cp.mock_infer(adapter="down-cpai")
    assert res2["fallback"] is True
    assert res2["success"] is True


def test_cpai_adapter_wire_shape():
    wire = to_cpai_response({
        "success": True,
        "message": "",
        "command": "detect",
        "module_id": "ObjectDetectionYOLOv5-6.2",
        "execution_provider": "CPU",
        "can_useGPU": False,
        "inference_ms": 10,
        "process_ms": 20,
        "analysis_round_trip_ms": 30,
        "count": 1,
        "predictions": [{"x_min": 1, "y_min": 2, "x_max": 3, "y_max": 4, "confidence": 0.5, "label": "dog"}],
    })
    assert wire["moduleId"] == "ObjectDetectionYOLOv5-6.2"
    assert wire["executionProvider"] == "CPU"
    assert wire["inferenceMs"] == 10
    assert wire["analysisRoundTripMs"] == 30
    assert wire["predictions"][0]["label"] == "dog"


def test_cpai_adapter_api():
    clear()
    client = TestClient(app)

    r = client.post("/api/cpai-adapter/register", json={"name": "api-cpai", "endpoint": "http://127.0.0.1:32168/v1/vision/detection"})
    assert r.status_code == 200, r.text
    assert r.json().get("name") == "api-cpai"

    r = client.get("/api/cpai-adapter")
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True

    r = client.get("/api/cpai-adapter/status")
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "ok"

    r = client.get("/v1/info")
    assert r.status_code == 200, r.text
    assert r.json().get("success") is True

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
                "moduleId": "ObjectDetectionYOLOv5-6.2",
                "inferenceMs": 5,
            }

    with patch("app.services.cpai_adapter.httpx.post", return_value=Resp()):
        r = client.post("/api/cpai-adapter/infer", json={"adapter": "api-cpai", "image_base64": b64})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("success") is True
        assert body.get("fallback") is False
        assert body.get("predictions")[0]["label"] == "car"

        # Native CodeProject.AI path returns camelCase wire shape.
        r = client.post("/v1/vision/detection", json={"image_base64": b64, "min_confidence": 0.25})
        assert r.status_code == 200, r.text
        wire = r.json()
        assert wire.get("success") is True
        assert "moduleId" in wire
        assert "analysisRoundTripMs" in wire

        r = client.post("/v1/vision/custom/my-model", json={"image_base64": b64})
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True

    def _boom(*a, **k):
        raise ConnectionError("down")

    with patch("app.services.cpai_adapter.httpx.post", side_effect=_boom):
        r = client.post("/api/cpai_adapter/detect", json={"adapter": "api-cpai", "image_base64": b64})
        assert r.status_code == 200, r.text
        assert r.json().get("fallback") is True


def test_cpai_adapter_clear():
    clear()
    assert list_adapters() == []
    register_adapter("x", "http://127.0.0.1:32168/v1/vision/detection")
    assert len(list_adapters()) == 1
    clear()
    assert list_adapters() == []
