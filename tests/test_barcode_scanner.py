"""Unit tests for Barcode & QR Code Scanner service and API."""

from __future__ import annotations

import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.services.barcode_scanner import (
    add_to_history,
    clear_history,
    clear_mock_detector,
    get_history,
    scan_image,
    set_mock_detector,
    set_mock_results,
)
from app.services.qr_onboard import render_qr_png


@pytest.fixture(autouse=True)
def clean_state():
    clear_history()
    clear_mock_detector()
    yield
    clear_history()
    clear_mock_detector()


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def create_sample_qr_jpeg(payload: str = "TEST_QR_PAYLOAD_ABC") -> bytes:
    png_bytes = render_qr_png(payload)
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_scan_real_qr_code():
    jpeg = create_sample_qr_jpeg("VIGILCAM:CAM-42:SECRET")
    detections = scan_image(jpeg, camera_id="cam_front", record_history=True)

    assert len(detections) >= 1
    det = detections[0]
    assert det["type"] == "QRCODE"
    assert det["data"] == "VIGILCAM:CAM-42:SECRET"
    assert "rect" in det
    rect = det["rect"]
    assert rect["w"] > 0
    assert rect["h"] > 0

    # Verify history recorded
    hist = get_history()
    assert len(hist) == 1
    assert hist[0]["data"] == "VIGILCAM:CAM-42:SECRET"
    assert hist[0]["camera_id"] == "cam_front"
    assert "scanned_at" in hist[0]
    assert "id" in hist[0]


def test_scan_blank_image():
    blank = Image.new("RGB", (200, 200), color=(255, 255, 255))
    buf = io.BytesIO()
    blank.save(buf, format="JPEG")

    detections = scan_image(buf.getvalue(), record_history=True)
    assert detections == []
    assert len(get_history()) == 0


def test_scan_empty_bytes():
    assert scan_image(b"") == []


def test_mock_results_and_detector():
    mock_item = {"type": "EAN13", "data": "1234567890128", "rect": {"x": 10, "y": 10, "w": 50, "h": 20}}
    set_mock_results([mock_item])

    detections = scan_image(b"some_bytes", camera_id="cam_mock", record_history=False)
    assert len(detections) == 1
    assert detections[0]["type"] == "EAN13"
    assert detections[0]["data"] == "1234567890128"

    clear_mock_detector()
    # Now with custom detector callable
    set_mock_detector(lambda _: [{"type": "CODE128", "data": "CUSTOM_BARCODE", "rect": {"x": 0, "y": 0, "w": 1, "h": 1}}])
    detections2 = scan_image(b"some_bytes", record_history=False)
    assert len(detections2) == 1
    assert detections2[0]["type"] == "CODE128"


def test_history_management():
    for i in range(15):
        add_to_history(
            {"type": "QRCODE", "data": f"item_{i}", "rect": {}},
            camera_id=f"cam_{i % 3}",
        )

    # Test limit
    hist = get_history(limit=5)
    assert len(hist) == 5

    # Test camera filter
    cam1_hist = get_history(camera_id="cam_1")
    assert len(cam1_hist) == 5
    for item in cam1_hist:
        assert item["camera_id"] == "cam_1"

    # Test clear
    clear_history()
    assert len(get_history()) == 0


def test_api_scan_multipart(client: TestClient):
    jpeg = create_sample_qr_jpeg("API_SCAN_MULTIPART_TEST")
    files = {"file": ("snap.jpg", jpeg, "image/jpeg")}
    resp = client.post("/api/barcodes/scan?camera_id=gate_cam", files=files)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["count"] >= 1
    assert data["camera_id"] == "gate_cam"
    assert data["detections"][0]["data"] == "API_SCAN_MULTIPART_TEST"

    # Check that history has it
    h_resp = client.get("/api/barcodes/history")
    assert h_resp.status_code == 200
    h_data = h_resp.json()
    assert h_data["count"] >= 1
    assert any(x["data"] == "API_SCAN_MULTIPART_TEST" for x in h_data["history"])


def test_api_scan_json_base64(client: TestClient):
    jpeg = create_sample_qr_jpeg("API_SCAN_JSON_BASE64")
    b64_str = base64.b64encode(jpeg).decode("utf-8")

    resp = client.post("/api/barcodes/scan", json={"image_base64": b64_str, "camera_id": "lane_2"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["count"] >= 1
    assert data["detections"][0]["data"] == "API_SCAN_JSON_BASE64"


def test_api_scan_raw_body(client: TestClient):
    jpeg = create_sample_qr_jpeg("API_SCAN_RAW_BYTES")
    resp = client.post(
        "/api/barcodes/scan?camera_id=raw_cam",
        content=jpeg,
        headers={"content-type": "image/jpeg"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["count"] >= 1
    assert data["detections"][0]["data"] == "API_SCAN_RAW_BYTES"


def test_api_scan_missing_image(client: TestClient):
    resp = client.post("/api/barcodes/scan", json={})
    assert resp.status_code == 400


def test_api_clear_history(client: TestClient):
    add_to_history({"type": "QRCODE", "data": "item_to_delete"})
    assert len(get_history()) >= 1

    del_resp = client.delete("/api/barcodes/history")
    assert del_resp.status_code == 200
    assert del_resp.json()["cleared"] is True

    hist_resp = client.get("/api/barcodes/history")
    assert hist_resp.json()["count"] == 0
