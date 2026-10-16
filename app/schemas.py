from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CameraBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    kind: str = Field("ip", pattern="^(usb|ip|onvif)$")
    source: str = Field(..., min_length=1, max_length=512)
    username: Optional[str] = None
    password: Optional[str] = None
    onvif_host: Optional[str] = None
    enabled: bool = True
    recording_enabled: bool = True
    detection_enabled: bool = True
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[int] = None


class CameraCreate(CameraBase):
    pass


class CameraRead(CameraBase):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class EventRead(BaseModel):
    id: int
    camera_id: Optional[int]
    kind: str
    label: str
    confidence: Optional[float]
    payload: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class DetectionRequest(BaseModel):
    camera_id: Optional[int] = None
    image_base64: Optional[str] = None
    simulate: bool = True


class DetectionResult(BaseModel):
    camera_id: Optional[int]
    model: str
    detections: list[dict]
    processed_at: datetime
