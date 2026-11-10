"""Live Weather Overlay Service.

Fetches live weather data from Open-Meteo or wttr.in with in-memory caching (TTL 15 minutes),
and renders weather information (temperature, humidity, condition, wind) onto camera snapshots/frames.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import httpx
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Cache duration: 15 minutes (900 seconds)
CACHE_TTL_SECONDS = 900
_weather_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_cache_lock = threading.Lock()

# WMO Weather interpretation codes mapping
WMO_CODE_MAP: Dict[int, Tuple[str, str]] = {
    0: ("Clear sky", "☀️"),
    1: ("Mainly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Foggy", "🌫️"),
    48: ("Depositing rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Moderate drizzle", "🌦️"),
    55: ("Dense drizzle", "🌧️"),
    56: ("Light freezing drizzle", "🌧️"),
    57: ("Dense freezing drizzle", "🌧️"),
    61: ("Slight rain", "🌧️"),
    63: ("Moderate rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    66: ("Light freezing rain", "🌧️"),
    67: ("Heavy freezing rain", "🌧️"),
    71: ("Slight snow fall", "🌨️"),
    73: ("Moderate snow fall", "🌨️"),
    75: ("Heavy snow fall", "❄️"),
    77: ("Snow grains", "❄️"),
    80: ("Slight rain showers", "🌦️"),
    81: ("Moderate rain showers", "🌧️"),
    82: ("Violent rain showers", "⛈️"),
    85: ("Slight snow showers", "🌨️"),
    86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm with slight hail", "⛈️"),
    99: ("Thunderstorm with heavy hail", "⛈️"),
}


def clear_weather_cache() -> None:
    """Clear in-memory weather cache."""
    with _cache_lock:
        _weather_cache.clear()


def get_direction_string(degrees: float | int) -> str:
    """Convert degrees (0-360) into compass direction string."""
    directions = [
        "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"
    ]
    try:
        deg = float(degrees) % 360.0
        idx = int((deg / 22.5) + 0.5) % 16
        return directions[idx]
    except Exception:
        return "N"


def get_weather(lat: float, lon: float, client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    """Fetch live weather for given latitude and longitude using Open-Meteo API.

    Cached in-memory with a 15-minute TTL.
    """
    lat_r = round(float(lat), 3)
    lon_r = round(float(lon), 3)
    cache_key = f"coord:{lat_r}:{lon_r}"

    now = time.time()
    with _cache_lock:
        cached = _weather_cache.get(cache_key)
        if cached:
            ts, data = cached
            if now - ts < CACHE_TTL_SECONDS:
                return dict(data)

    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat_r}&longitude={lon_r}"
        f"&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m"
        f"&current_weather=true"
    )

    weather_data: Optional[Dict[str, Any]] = None

    try:
        if client:
            resp = client.get(url, timeout=5.0)
        else:
            with httpx.Client(timeout=5.0) as default_client:
                resp = default_client.get(url)

        if resp.status_code == 200:
            payload = resp.json()
            current = payload.get("current") or {}
            current_weather = payload.get("current_weather") or {}

            temp = current.get("temperature_2m")
            if temp is None:
                temp = current_weather.get("temperature", 20.0)

            humidity = current.get("relative_humidity_2m", 50)

            code = current.get("weather_code")
            if code is None:
                code = current_weather.get("weathercode", 0)
            code = int(code)

            wind_speed = current.get("wind_speed_10m")
            if wind_speed is None:
                wind_speed = current_weather.get("windspeed", 0.0)

            wind_dir_deg = current.get("wind_direction_10m")
            if wind_dir_deg is None:
                wind_dir_deg = current_weather.get("winddirection", 0.0)

            cond_text, icon = WMO_CODE_MAP.get(code, ("Clear sky", "☀️"))
            wind_dir = get_direction_string(wind_dir_deg)

            weather_data = {
                "temperature": round(float(temp), 1),
                "temperature_unit": "°C",
                "humidity": int(humidity),
                "condition": cond_text,
                "condition_code": code,
                "icon": icon,
                "wind_speed": round(float(wind_speed), 1),
                "wind_direction": round(float(wind_dir_deg), 1),
                "wind_dir": wind_dir,
                "wind_unit": "km/h",
                "latitude": float(lat),
                "longitude": float(lon),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "open-meteo",
            }
    except Exception as exc:
        logger.warning("Error fetching weather from Open-Meteo (%s, %s): %s", lat, lon, exc)

    if not weather_data:
        weather_data = {
            "temperature": 20.0,
            "temperature_unit": "°C",
            "humidity": 50,
            "condition": "Clear sky",
            "condition_code": 0,
            "icon": "☀️",
            "wind_speed": 5.0,
            "wind_direction": 0.0,
            "wind_dir": "N",
            "wind_unit": "km/h",
            "latitude": float(lat),
            "longitude": float(lon),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "fallback",
        }

    with _cache_lock:
        _weather_cache[cache_key] = (now, dict(weather_data))

    return weather_data


def get_weather_by_ip(ip: Optional[str] = None, client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    """Fetch live weather for an IP address using wttr.in or Open-Meteo.

    Cached in-memory with a 15-minute TTL.
    """
    clean_ip = (ip or "").strip()
    cache_key = f"ip:{clean_ip or 'default'}"

    now = time.time()
    with _cache_lock:
        cached = _weather_cache.get(cache_key)
        if cached:
            ts, data = cached
            if now - ts < CACHE_TTL_SECONDS:
                return dict(data)

    is_private = (
        not clean_ip
        or clean_ip in ("127.0.0.1", "localhost", "::1")
        or clean_ip.startswith("10.")
        or clean_ip.startswith("192.168.")
        or clean_ip.startswith("172.16.")
        or clean_ip.startswith("172.17.")
        or clean_ip.startswith("172.18.")
        or clean_ip.startswith("172.19.")
        or clean_ip.startswith("172.2")
        or clean_ip.startswith("172.3")
    )

    url = "https://wttr.in/?format=j1" if is_private else f"https://wttr.in/{clean_ip}?format=j1"
    headers = {"User-Agent": "curl/7.68.0"}

    weather_data: Optional[Dict[str, Any]] = None

    try:
        if client:
            resp = client.get(url, headers=headers, timeout=5.0)
        else:
            with httpx.Client(timeout=5.0) as default_client:
                resp = default_client.get(url, headers=headers)

        if resp.status_code == 200:
            payload = resp.json()
            cond_list = payload.get("current_condition", [])
            if cond_list:
                cond = cond_list[0]
                temp = float(cond.get("temp_C", 20.0))
                humidity = int(cond.get("humidity", 50))
                wind_speed = float(cond.get("windspeedKmph", 0.0))
                wind_dir = cond.get("winddir16Point", "N")
                wind_deg = float(cond.get("winddirDegree", 0.0))
                descs = cond.get("weatherDesc", [])
                desc_text = descs[0].get("value", "Clear") if descs else "Clear"

                area_list = payload.get("nearest_area", [])
                lat = 0.0
                lon = 0.0
                if area_list:
                    lat = float(area_list[0].get("latitude", 0.0))
                    lon = float(area_list[0].get("longitude", 0.0))

                weather_data = {
                    "temperature": round(temp, 1),
                    "temperature_unit": "°C",
                    "humidity": humidity,
                    "condition": desc_text,
                    "condition_code": 0,
                    "icon": "🌤️",
                    "wind_speed": round(wind_speed, 1),
                    "wind_direction": round(wind_deg, 1),
                    "wind_dir": wind_dir,
                    "wind_unit": "km/h",
                    "latitude": lat,
                    "longitude": lon,
                    "ip": clean_ip or "auto",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "source": "wttr.in",
                }
    except Exception as exc:
        logger.warning("Error fetching weather for IP %s from wttr.in: %s", clean_ip, exc)

    if not weather_data:
        # Fall back to default location
        weather_data = {
            "temperature": 20.0,
            "temperature_unit": "°C",
            "humidity": 50,
            "condition": "Clear",
            "condition_code": 0,
            "icon": "☀️",
            "wind_speed": 5.0,
            "wind_direction": 0.0,
            "wind_dir": "N",
            "wind_unit": "km/h",
            "latitude": 0.0,
            "longitude": 0.0,
            "ip": clean_ip or "auto",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "fallback",
        }

    with _cache_lock:
        _weather_cache[cache_key] = (now, dict(weather_data))

    return weather_data


def render_weather_overlay(
    image_bytes: bytes,
    weather_data: Dict[str, Any],
    position: str = "top_right",
) -> bytes:
    """Renders weather info (temperature, humidity, condition, wind) onto a JPEG image using Pillow.

    Supported positions: 'top_right' (default), 'top_left', 'bottom_right', 'bottom_left',
    'top_center' / 'top_middle', 'bottom_center' / 'bottom_middle', 'center'.
    """
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    width, height = img.size

    # Prepare strings
    temp = weather_data.get("temperature", 20.0)
    unit = weather_data.get("temperature_unit", "°C")
    condition = weather_data.get("condition", "Clear")
    humidity = weather_data.get("humidity", 50)
    wind_speed = weather_data.get("wind_speed", 0.0)
    wind_dir = weather_data.get("wind_dir", "")
    wind_unit = weather_data.get("wind_unit", "km/h")

    line1 = f"{temp:.1f}{unit}  {condition}"
    line2 = f"Humidity: {humidity}%  Wind: {wind_speed} {wind_unit} {wind_dir}".strip()

    font = ImageFont.load_default()

    # Measure lines
    dummy_draw = ImageDraw.Draw(img)
    bb1 = dummy_draw.textbbox((0, 0), line1, font=font)
    bb2 = dummy_draw.textbbox((0, 0), line2, font=font)

    w1 = bb1[2] - bb1[0]
    h1 = bb1[3] - bb1[1]
    w2 = bb2[2] - bb2[0]
    h2 = bb2[3] - bb2[1]

    text_w = max(w1, w2)
    line_spacing = 4
    text_h = h1 + h2 + line_spacing

    pad_x = 10
    pad_y = 8
    box_w = text_w + (pad_x * 2)
    box_h = text_h + (pad_y * 2)

    margin = 12
    pos = position.lower().replace("-", "_")

    if pos in ("top_left", "tl"):
        box_x = margin
        box_y = margin
    elif pos in ("bottom_left", "bl"):
        box_x = margin
        box_y = max(margin, height - box_h - margin)
    elif pos in ("bottom_right", "br"):
        box_x = max(margin, width - box_w - margin)
        box_y = max(margin, height - box_h - margin)
    elif pos in ("top_center", "top_middle", "tc", "tm"):
        box_x = max(margin, (width - box_w) // 2)
        box_y = margin
    elif pos in ("bottom_center", "bottom_middle", "bc", "bm"):
        box_x = max(margin, (width - box_w) // 2)
        box_y = max(margin, height - box_h - margin)
    elif pos in ("center", "middle"):
        box_x = max(margin, (width - box_w) // 2)
        box_y = max(margin, (height - box_h) // 2)
    else:  # top_right default
        box_x = max(margin, width - box_w - margin)
        box_y = margin

    # Draw semi-transparent dark rounded badge using RGBA overlay
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)

    # Box coordinates
    x0, y0 = box_x, box_y
    x1, y1 = box_x + box_w, box_y + box_h

    # Semi-transparent dark background
    ov_draw.rounded_rectangle([x0, y0, x1, y1], radius=6, fill=(0, 0, 0, 160))

    # White text
    t_x = x0 + pad_x
    t_y = y0 + pad_y
    ov_draw.text((t_x, t_y), line1, fill=(255, 255, 255, 255), font=font)
    ov_draw.text((t_x, t_y + h1 + line_spacing), line2, fill=(220, 220, 220, 255), font=font)

    # Composite overlay onto image
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    composited = Image.alpha_composite(img, overlay).convert("RGB")

    output_buf = io.BytesIO()
    composited.save(output_buf, format="JPEG", quality=90)
    return output_buf.getvalue()
