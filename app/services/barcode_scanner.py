"""Barcode and QR Code Scanner Service.

Detects QR codes and 1D barcodes from image bytes/frames using OpenCV, pyzbar,
or a fallback stub detector, and maintains in-memory scan history.
"""

from __future__ import annotations

import copy
import io
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Maximum scan history to preserve in memory
MAX_HISTORY_SIZE = 500

_history: List[Dict[str, Any]] = []
_history_lock = threading.Lock()

# Mock / stub detectors for test environments without native libraries
_mock_results: Optional[List[Dict[str, Any]]] = None
_mock_detector: Optional[Callable[[bytes], List[Dict[str, Any]]]] = None


def set_mock_results(results: Optional[List[Dict[str, Any]]]) -> None:
    """Set explicit mock results for scan_image (useful in unit tests)."""
    global _mock_results
    _mock_results = results


def set_mock_detector(detector_fn: Optional[Callable[[bytes], List[Dict[str, Any]]]]) -> None:
    """Set custom mock detector function (useful in unit tests)."""
    global _mock_detector
    _mock_detector = detector_fn


def clear_mock_detector() -> None:
    """Clear any installed mock detectors or results."""
    global _mock_results, _mock_detector
    _mock_results = None
    _mock_detector = None


def clear_history() -> None:
    """Clear barcode scan history."""
    with _history_lock:
        _history.clear()


def add_to_history(detection: Dict[str, Any], camera_id: Optional[str] = None) -> Dict[str, Any]:
    """Record a detected barcode into the in-memory history."""
    item = {
        "id": str(uuid.uuid4()),
        "type": detection.get("type", "UNKNOWN"),
        "data": detection.get("data", ""),
        "rect": detection.get("rect", {}),
        "camera_id": str(camera_id) if camera_id is not None else None,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }
    with _history_lock:
        _history.insert(0, item)
        if len(_history) > MAX_HISTORY_SIZE:
            _history.pop()
    return item


def get_history(limit: int = 50, camera_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieve recent barcode scan history."""
    with _history_lock:
        records = _history
        if camera_id is not None:
            records = [r for r in records if r.get("camera_id") == str(camera_id)]
        return copy.deepcopy(records[:limit])


def _scan_with_pyzbar(image_bytes: bytes) -> List[Dict[str, Any]]:
    """Attempt barcode/QR decoding using pyzbar if installed."""
    results: List[Dict[str, Any]] = []
    try:
        from pyzbar.pyzbar import decode as pyzbar_decode
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        decoded = pyzbar_decode(img)
        for obj in decoded:
            data = obj.data.decode("utf-8", errors="replace")
            rect = {
                "x": int(obj.rect.left),
                "y": int(obj.rect.top),
                "w": int(obj.rect.width),
                "h": int(obj.rect.height),
            }
            results.append({
                "type": str(obj.type).upper(),
                "data": data,
                "rect": rect,
            })
    except (ImportError, Exception) as exc:
        logger.debug("pyzbar scan skipped or failed: %s", exc)
    return results


def _scan_with_opencv(image_bytes: bytes) -> List[Dict[str, Any]]:
    """Attempt QR and barcode decoding using OpenCV."""
    results: List[Dict[str, Any]] = []
    try:
        import cv2
        import numpy as np

        arr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return results

        # 1. QR Code Detector
        if hasattr(cv2, "QRCodeDetector"):
            try:
                qr_det = cv2.QRCodeDetector()
                ok, decoded_info, points, _ = qr_det.detectAndDecodeMulti(img)
                if ok and decoded_info is not None:
                    for text, pts in zip(decoded_info, points):
                        if text:
                            x_min = int(pts[:, 0].min())
                            y_min = int(pts[:, 1].min())
                            x_max = int(pts[:, 0].max())
                            y_max = int(pts[:, 1].max())
                            results.append({
                                "type": "QRCODE",
                                "data": str(text),
                                "rect": {
                                    "x": x_min,
                                    "y": y_min,
                                    "w": max(0, x_max - x_min),
                                    "h": max(0, y_max - y_min),
                                },
                            })
                elif not ok:
                    # Fallback to single detector
                    data, pts, _ = qr_det.detectAndDecode(img)
                    if data and pts is not None:
                        x_min = int(pts[0, :, 0].min())
                        y_min = int(pts[0, :, 1].min())
                        x_max = int(pts[0, :, 0].max())
                        y_max = int(pts[0, :, 1].max())
                        results.append({
                            "type": "QRCODE",
                            "data": str(data),
                            "rect": {
                                "x": x_min,
                                "y": y_min,
                                "w": max(0, x_max - x_min),
                                "h": max(0, y_max - y_min),
                            },
                        })
            except Exception as e:
                logger.debug("OpenCV QR scan error: %s", e)

        # 2. 1D Barcode Detector (OpenCV 4.5.3+)
        if hasattr(cv2, "barcode_BarcodeDetector"):
            try:
                b_det = cv2.barcode_BarcodeDetector()
                ok_b, b_texts, b_types, b_points = b_det.detectAndDecodeWithType(img)
                if ok_b and b_texts is not None:
                    for text, b_type, pts in zip(b_texts, b_types, b_points):
                        if text:
                            x_min = int(pts[:, 0].min())
                            y_min = int(pts[:, 1].min())
                            x_max = int(pts[:, 0].max())
                            y_max = int(pts[:, 1].max())
                            b_type_str = str(b_type).upper() if b_type else "BARCODE"
                            results.append({
                                "type": b_type_str,
                                "data": str(text),
                                "rect": {
                                    "x": x_min,
                                    "y": y_min,
                                    "w": max(0, x_max - x_min),
                                    "h": max(0, y_max - y_min),
                                },
                            })
            except Exception as e:
                logger.debug("OpenCV Barcode scan error: %s", e)

        # 3. WeChat QR Code Detector (if installed/compiled)
        if hasattr(cv2, "wechat_qrcode_WeChatQRCode") and not results:
            try:
                wechat_det = cv2.wechat_qrcode_WeChatQRCode()
                texts, points = wechat_det.detectAndDecode(img)
                if texts:
                    for text, pts in zip(texts, points):
                        if text:
                            x_min = int(pts[:, 0].min())
                            y_min = int(pts[:, 1].min())
                            x_max = int(pts[:, 0].max())
                            y_max = int(pts[:, 1].max())
                            results.append({
                                "type": "QRCODE",
                                "data": str(text),
                                "rect": {
                                    "x": x_min,
                                    "y": y_min,
                                    "w": max(0, x_max - x_min),
                                    "h": max(0, y_max - y_min),
                                },
                            })
            except Exception as e:
                logger.debug("WeChat QR detector error: %s", e)

    except (ImportError, Exception) as exc:
        logger.debug("OpenCV scan skipped or failed: %s", exc)

    return results


def scan_image(
    image_bytes: bytes,
    camera_id: Optional[str] = None,
    record_history: bool = True,
) -> List[Dict[str, Any]]:
    """Scan image bytes (JPEG/PNG) for barcodes and QR codes.

    Returns list of detections: [{"type": "QRCODE"|"EAN13"|..., "data": str, "rect": {...}}].
    Automatically records results to history if record_history is True.
    """
    if not image_bytes:
        return []

    # 1. Custom mock override
    if _mock_results is not None:
        detections = list(_mock_results)
    elif _mock_detector is not None:
        try:
            detections = _mock_detector(image_bytes)
        except Exception:
            detections = []
    else:
        # 2. Try pyzbar first
        detections = _scan_with_pyzbar(image_bytes)

        # 3. If pyzbar returned nothing, try OpenCV
        if not detections:
            detections = _scan_with_opencv(image_bytes)

    # Deduplicate detections by (type, data)
    seen = set()
    unique_detections: List[Dict[str, Any]] = []
    for d in detections:
        key = (d.get("type"), d.get("data"))
        if key not in seen:
            seen.add(key)
            unique_detections.append(d)

    # Record to history
    if record_history:
        for det in unique_detections:
            add_to_history(det, camera_id=camera_id)

    return unique_detections
