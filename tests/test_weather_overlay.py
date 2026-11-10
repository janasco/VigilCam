"""Unit tests for Weather Overlay service and API."""

from __future__ import annotations

import base64
import io
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.services.solar_schedule import _CONFIGS
from app.services.weather_overlay import (
    clear_weather_cache,
    get_direction_string,
    get_weather,
    get_weather_by_ip,
    render_weather_overlay,
)


@pytest.fixture(autouse=True)
def reset_cache():
    clear_weather_cache()
    yield
    clear_weather_cache()


@pytest.fixture
def client():
    return TestClient(app)


def create_sample_jpeg(width: int = 320, height: int = 240, color=(100, 150, 200)) -> bytes:
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_get_direction_string():
    assert get_direction_string(0) == "N"
    assert get_direction_string(90) == "E"
    assert get_direction_string(180) == "S"
    assert get_direction_string(270) == "W"
    assert get_direction_string(45) == "NE"
    assert get_direction_string(315) == "NW"
    assert get_direction_string(720) == "N"
    assert get_direction_string("invalid") == "N"


def test_get_weather_mocked_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "current": {
            "temperature_2m": 18.5,
            "relative_humidity_2m": 65,
            "weather_code": 2,
            "wind_speed_10m": 12.3,
            "wind_direction_10m": 180,
        }
    }
    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp

    data = get_weather(52.52, 13.41, client=mock_client)
    assert data["temperature"] == 18.5
    assert data["humidity"] == 65
    assert data["condition"] == "Partly cloudy"
    assert data["wind_speed"] == 12.3
    assert data["wind_dir"] == "S"
    assert data["source"] == "open-meteo"

    # Test caching: second call should not call client.get
    mock_client.get.reset_mock()
    cached = get_weather(52.52, 13.41, client=mock_client)
    assert cached["temperature"] == 18.5
    mock_client.get.assert_not_called()

    # Clear cache and call again
    clear_weather_cache()
    get_weather(52.52, 13.41, client=mock_client)
    mock_client.get.assert_called_once()


def test_get_weather_fallback_on_network_error():
    mock_client = MagicMock()
    mock_client.get.side_effect = Exception("Network unreachable")

    data = get_weather(10.0, 20.0, client=mock_client)
    assert data["temperature"] == 20.0
    assert data["source"] == "fallback"
    assert data["latitude"] == 10.0
    assert data["longitude"] == 20.0


def test_get_weather_by_ip_mocked_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "current_condition": [
            {
                "temp_C": "25",
                "humidity": "40",
                "windspeedKmph": "15",
                "winddir16Point": "ENE",
                "winddirDegree": "70",
                "weatherDesc": [{"value": "Sunny"}],
            }
        ],
        "nearest_area": [{"latitude": "34.05", "longitude": "-118.25"}],
    }
    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp

    data = get_weather_by_ip("8.8.8.8", client=mock_client)
    assert data["temperature"] == 25.0
    assert data["humidity"] == 40
    assert data["condition"] == "Sunny"
    assert data["wind_speed"] == 15.0
    assert data["wind_dir"] == "ENE"
    assert data["source"] == "wttr.in"


def test_render_weather_overlay_positions():
    jpeg = create_sample_jpeg(640, 480)
    weather = {
        "temperature": 21.0,
        "temperature_unit": "°C",
        "condition": "Clear",
        "humidity": 55,
        "wind_speed": 10.5,
        "wind_dir": "NW",
    }

    positions = [
        "top_right", "top_left", "bottom_right", "bottom_left",
        "top_middle", "bottom_middle", "center",
    ]
    for pos in positions:
        rendered = render_weather_overlay(jpeg, weather, position=pos)
        assert isinstance(rendered, bytes)
        assert len(rendered) > 0

        # Validate result is a valid JPEG
        img = Image.open(io.BytesIO(rendered))
        assert img.format == "JPEG"
        assert img.size == (640, 480)


def test_render_weather_overlay_grayscale_input():
    # Test handling of 1-channel / Grayscale inputs
    img_l = Image.new("L", (200, 200), color=128)
    buf = io.BytesIO()
    img_l.save(buf, format="JPEG")

    rendered = render_weather_overlay(buf.getvalue(), {"temperature": 15.0}, position="top_left")
    img_out = Image.open(io.BytesIO(rendered))
    assert img_out.format == "JPEG"
    assert img_out.mode == "RGB"


def test_api_weather_current(client: TestClient):
    # Mock get_weather to avoid external API calls during unit test
    with patch("app.api.weather.get_weather") as mock_gw:
        mock_gw.return_value = {
            "temperature": 19.5,
            "condition": "Clear sky",
            "humidity": 50,
            "wind_speed": 8.0,
            "wind_dir": "N",
        }
        resp = client.get("/api/weather/current?lat=52.5&lon=13.4")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["temperature"] == 19.5
        mock_gw.assert_called_once_with(52.5, 13.4)


def test_api_weather_camera(client: TestClient):
    # Test with camera configured in solar_schedule
    _CONFIGS["cam_test_99"] = {"lat": 48.8566, "lon": 2.3522}
    try:
        with patch("app.api.weather.get_weather") as mock_gw:
            mock_gw.return_value = {
                "temperature": 24.0,
                "condition": "Sunny",
                "humidity": 45,
                "wind_speed": 6.0,
                "wind_dir": "SW",
            }
            resp = client.get("/api/weather/camera/cam_test_99")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["camera_id"] == "cam_test_99"
            assert data["temperature"] == 24.0
    finally:
        _CONFIGS.pop("cam_test_99", None)


def test_api_weather_camera_not_found(client: TestClient):
    resp = client.get("/api/weather/camera/99999999")
    assert resp.status_code == 404


def test_api_weather_overlay_multipart(client: TestClient):
    jpeg = create_sample_jpeg(320, 240)
    with patch("app.api.weather.get_weather_by_ip") as mock_ip:
        mock_ip.return_value = {
            "temperature": 20.0,
            "condition": "Cloudy",
            "humidity": 60,
            "wind_speed": 5.0,
            "wind_dir": "E",
        }
        files = {"file": ("test.jpg", jpeg, "image/jpeg")}
        resp = client.post("/api/weather/overlay?position=top_left", files=files)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert len(resp.content) > 0
        img = Image.open(io.BytesIO(resp.content))
        assert img.size == (320, 240)


def test_api_weather_overlay_json_base64(client: TestClient):
    jpeg = create_sample_jpeg(320, 240)
    b64_str = base64.b64encode(jpeg).decode("utf-8")
    payload = {
        "image_base64": b64_str,
        "position": "bottom_right",
        "weather": {
            "temperature": 28.0,
            "condition": "Hot",
            "humidity": 30,
            "wind_speed": 2.0,
            "wind_dir": "S",
        },
    }
    resp = client.post("/api/weather/overlay", json=payload)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    img = Image.open(io.BytesIO(resp.content))
    assert img.size == (320, 240)


def test_api_weather_overlay_missing_image(client: TestClient):
    resp = client.post("/api/weather/overlay", json={})
    assert resp.status_code == 400
