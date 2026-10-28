from __future__ import annotations

import base64
import io
from typing import Optional

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False


def placeholder_snapshot(camera_id: Optional[int] = None, width: int = 640, height: int = 480) -> str:
    """Generate a base64 JPEG placeholder frame for AI tests."""
    if not PIL_AVAILABLE:
        return ""
    image = Image.new("RGB", (width, height), color=(24, 28, 36))
    draw = ImageDraw.Draw(image)
    label = f"VigilCam camera {camera_id if camera_id is not None else 'primary'}"
    draw.text((20, height // 2), label, fill=(255, 255, 255))
    draw.text((20, height // 2 + 24), "Placeholder frame", fill=(150, 150, 150))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")
