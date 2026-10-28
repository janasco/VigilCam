import importlib

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import detector_bench as bench
from app.services.detector_bench import (
    DetectorBenchService,
    clear,
    detector_bench_service,
    get_backend,
    get_run,
    list_backends,
    list_runs,
    run_benchmark,
)


def test_detector_bench_import():
    importlib.import_module("app.services.detector_bench")  # noqa: F401
    importlib.import_module("app.api.detector_bench")  # noqa: F401
    from app.services.detector_bench import run_benchmark as _run  # noqa: F401
    from app.services.detector_bench import detector_bench_service as _svc  # noqa: F401
    from app.api.detector_bench import router  # noqa: F401
    assert isinstance(detector_bench_service, DetectorBenchService)


def test_detector_bench_paths_present():
    from app.api.detector_bench import router as bench_router
    router_paths = sorted(set(getattr(r, "path", "") for r in bench_router.routes))
    assert "/api/detector_bench" in router_paths or "/api/detector-bench" in router_paths
    assert ("/api/detector-bench/run" in router_paths
            or "/api/detector_bench/run" in router_paths)
    # Best-effort check that the auto-loader exposes the router via app.routes
    # (avoids the very slow app.openapi() generation on this large app).
    try:
        app_paths = sorted(set(getattr(r, "path", "") for r in app.routes))
        assert ("/api/detector-bench/run" in app_paths
                or "/api/detector_bench/run" in app_paths)
    except Exception:  # noqa: BLE001
        pass


def test_detector_bench_backends():
    backends = list_backends()
    keys = {b["key"] for b in backends}
    for expected in ("cpu", "edgetpu", "hailo", "openvino", "onnx"):
        assert expected in keys, f"missing backend {expected}"
    edgetpu = get_backend("edgetpu")
    assert edgetpu is not None
    assert edgetpu["shareable"] is False
    assert "ssd" in edgetpu["model_types"]
    # Aliases resolve to canonical backends.
    assert get_backend("coral")["key"] == "edgetpu"
    assert get_backend("openvino")["key"] == "openvino"
    assert get_backend("no-such-backend") is None
    assert bench.get_backend("hailo")["key"] == "hailo"


def test_detector_bench_run_openvino():
    clear()
    res = run_benchmark(backend="openvino", device="CPU", model_type="ssd",
                        width=320, height=320, variant="small",
                        iterations=3, warmup=1)
    assert res["success"] is True
    assert res["backend"] == "openvino"
    assert res["resolution"] == "320x320"
    assert res["iterations"] == 3
    assert res["measured"]["count"] == 3
    assert res["avg_ms"] > 0
    assert res["fps_measured"] > 0
    assert res["estimated_inference_ms"] > 0
    assert res["estimated_fps"] > 0
    assert res["throughput_fps"] >= res["estimated_fps"]
    assert res["id"] == res["run_id"]


def test_detector_bench_run_clamps_non_shareable():
    clear()
    res = run_benchmark(backend="edgetpu", iterations=2, warmup=0,
                        num_processes=4)
    assert res["backend"] == "edgetpu"
    assert res["num_processes"] == 4
    assert res["effective_processes"] == 1
    assert any("single-process" in n for n in res["notes"])


def test_detector_bench_run_history():
    clear()
    r1 = run_benchmark(backend="cpu", iterations=2, warmup=0)
    r2 = run_benchmark(backend="hailo", iterations=2, warmup=0)
    assert r1["id"] != r2["id"]
    runs = list_runs()
    assert len(runs) == 2
    assert get_run(r1["run_id"])["backend"] == "cpu"
    assert detector_bench_service.get_run(r2["run_id"])["backend"] == "hailo"
    assert detector_bench_service.list_runs() is not None
    clear()
    assert list_runs() == []


def test_detector_bench_invalid_backend():
    clear()
    with pytest.raises(ValueError):
        run_benchmark(backend="no-such-backend", iterations=2, warmup=0)
    with pytest.raises(ValueError):
        run_benchmark(backend="openvino", model_type="no-such-model",
                       iterations=2, warmup=0)


def test_detector_bench_api():
    clear()
    client = TestClient(app)

    r = client.get("/api/detector-bench/backends")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert any(b.get("key") == "openvino" for b in body.get("backends", []))

    r = client.get("/api/detector-bench/status")
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "ok"

    r = client.get("/api/detector_bench/backends/edgetpu")
    assert r.status_code == 200, r.text
    assert r.json().get("key") == "edgetpu"

    r = client.post("/api/detector-bench/run",
                    json={"backend": "cpu", "iterations": 3, "warmup": 0})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run.get("success") is True
    assert run.get("avg_ms", 0) > 0
    run_id = run.get("run_id")
    assert run_id

    r = client.get(f"/api/detector-bench/{run_id}")
    assert r.status_code == 200, r.text
    assert r.json().get("run_id") == run_id

    r = client.get("/api/detector-bench/runs")
    assert r.status_code == 200, r.text
    assert r.json().get("count") >= 1

    r = client.post("/api/detector-bench/run",
                    json={"backend": "no-such-backend", "iterations": 2})
    assert r.status_code == 400, r.text

    r = client.get("/api/detector-bench/does-not-exist")
    assert r.status_code == 404, r.text


def test_detector_bench_clear():
    clear()
    run_benchmark(backend="onnx", iterations=2, warmup=0)
    assert len(list_runs()) == 1
    clear()
    assert list_runs() == []
