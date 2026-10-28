from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_RECOGNITION_THRESHOLD = 0.5


@dataclass(frozen=True)
class FaceRead:
    face_detected: bool
    recognized: bool
    person_id: Optional[str] = None
    name: Optional[str] = None
    category: Optional[str] = None
    similarity: Optional[float] = None
    face_bbox: Optional[Tuple[int, int, int, int]] = None
    threshold: Optional[float] = None
    correlation_id: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass
class FaceRecognitionPipelineConfig:
    recognition_threshold: float = DEFAULT_RECOGNITION_THRESHOLD

    def __post_init__(self) -> None:
        if not 0.0 < self.recognition_threshold <= 1.0:
            raise ValueError("recognition_threshold must be in (0.0, 1.0]")


class RecognitionClient:
    def recognize(self, frame_jpeg, *, threshold, correlation_id=None):
        raise NotImplementedError


def _coerce_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_str(value):
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_bbox(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x1, y1, x2, y2 = (int(v) for v in value)
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


def _parse_recognition_response(response, *, correlation_id=None):
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    bbox = _parse_bbox(result.get("face_bbox"))
    threshold = _coerce_float(result.get("threshold"))
    if not result.get("recognized"):
        return FaceRead(
            face_detected=bbox is not None,
            recognized=False,
            face_bbox=bbox,
            threshold=threshold,
            correlation_id=correlation_id,
            raw=dict(result),
        )
    return FaceRead(
        face_detected=True,
        recognized=True,
        person_id=_coerce_str(result.get("person_id")),
        name=_coerce_str(result.get("name")),
        category=_coerce_str(result.get("category")),
        similarity=_coerce_float(result.get("similarity")),
        face_bbox=bbox,
        threshold=threshold,
        correlation_id=correlation_id,
        raw=dict(result),
    )


class FaceRecognitionPipeline:
    def __init__(self, client, config=None):
        self.client = client
        self.config = config or FaceRecognitionPipelineConfig()

    def process_frame(self, frame_jpeg, *, correlation_id=None):
        try:
            response = self.client.recognize(
                frame_jpeg,
                threshold=self.config.recognition_threshold,
                correlation_id=correlation_id,
            )
        except Exception:
            logger.exception("face recognition call failed")
            return None
        return _parse_recognition_response(response, correlation_id=correlation_id)


FacePipeline = FaceRecognitionPipeline
FaceRecognition = FaceRecognitionPipeline
parse_bbox = _parse_bbox
parse_recognition_response = _parse_recognition_response
parse_face = _parse_recognition_response
