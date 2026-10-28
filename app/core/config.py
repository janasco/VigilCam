from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


_ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
_CONFIG_ENV = Path(__file__).resolve().parents[2] / "config" / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=[_ROOT_ENV, _CONFIG_ENV], env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "VigilCam"
    APP_VERSION: str = "0.1.0"
    HOST: str = "0.0.0.0"
    PORT: int = 11947
    DEBUG: bool = False

    DATA_DIR: Path = Path("data")
    DATABASE_URL: str = "sqlite+aiosqlite:///data/vigilcam.db"

    SECRET_KEY: str = "change-me-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin"

    # AI / detection
    DETECTION_ENABLED: bool = True
    DEFAULT_MODEL: str = "yolov8n"
    DETECTION_CONFIDENCE: float = 0.5

    # Retention
    RETENTION_MAX_AGE_DAYS: int = 30
    RETENTION_MAX_TOTAL_BYTES: Optional[int] = None
    RETENTION_RUN_INTERVAL_SECONDS: int = 3600
    RETENTION_PRE_ROLL_SECONDS: int = 30

    # Provider-agnostic LLM/VLM
    AI_ENABLED: bool = False
    AI_BASE_URL: str = "http://localhost:11434/v1"
    AI_API_KEY: str = "ollama"
    AI_VLM_MODEL: str = "moondream"
    AI_STT_MODEL: str = "whisper-1"
    AI_TTS_MODEL: str = "tts-1"
    AI_TIMEOUT_SECONDS: float = 30.0

    # Camera defaults
    RECORDING_DIR: Path = Path("data/recordings")
    SNAPSHOT_DIR: Path = Path("data/snapshots")

    # Hardware acceleration
    VIDEO_HWACCEL: str = "none"
    VIDEO_HW_ACCEL: str = "none"
    VIDEO_HWACCEL_MODE: str = "none"
    HWACCEL: str = "none"
    HW_ACCEL: str = "none"
    ACCELERATOR: str = "none"
    VIDEO_HWACCEL_DEVICE: Optional[str] = None
    VIDEO_HW_ACCEL_DEVICE: Optional[str] = None
    HWACCEL_DEVICE: Optional[str] = None
    HW_ACCEL_DEVICE: Optional[str] = None
    AI_RUNTIME: str = "cpu"
    AI_DEVICE: str = "cpu"
    AI_RUNTIME_DEVICE: str = "cpu"
    RUNTIME: str = "cpu"
    AI_BACKEND: str = "cpu"
    AI_ENGINE: str = "cpu"

    # Hardware encoder selection
    VIDEO_ENCODER: str = "auto"
    VIDEO_ENCODER_DEVICE: Optional[str] = None
    HARDWARE_ENCODER: str = "auto"
    HARDWARE_ENCODER_DEVICE: Optional[str] = None
    RECORDING_ENCODER: str = "auto"
    RECORDING_ENCODER_DEVICE: Optional[str] = None
    ENCODER: str = "auto"
    ENCODER_DEVICE: Optional[str] = None
    HW_ENCODER: str = "auto"
    HW_ENCODER_DEVICE: Optional[str] = None
    VIDEO_ENCODING: str = "auto"
    VIDEO_ENCODING_DEVICE: Optional[str] = None
    ENCODING: str = "auto"
    ENCODING_DEVICE: Optional[str] = None
    VIDEO_CODEC: str = "auto"
    VIDEO_CODEC_DEVICE: Optional[str] = None
    RECORDING_CODEC: str = "auto"
    RECORDING_CODEC_DEVICE: Optional[str] = None

    # Cloud upload gateway (Wave 23)
    CLOUD_UPLOAD_ENABLED: bool = True

    # Cloud inference gateway (KAI-C)
    KAI_C_URL: str = "http://localhost:8000/infer/cloud"
    kai_c_url: str = "http://localhost:8000/infer/cloud"
    INTERNAL_API_KEY: str = ""
    internal_api_key: str = ""
    CLOUD_UPLOAD_DIR: Path = Path("data/cloud_uploads")
    CLOUD_UPLOAD_TEMP_DIR: Path = Path("data/cloud_uploads/tmp")
    FTP_UPLOAD_TIMEOUT_SECONDS: float = 30.0

    # Motion tuner (Frigate-style)
    MOTION_THRESHOLD: int = 30
    MOTION_TUNER_THRESHOLD: int = 30
    motion_threshold: int = 30
    motion_tuner_threshold: int = 30
    MOTION_CONTOUR_AREA: int = 10
    MOTION_TUNER_CONTOUR_AREA: int = 10
    motion_contour_area: int = 10
    motion_tuner_contour_area: int = 10
    MOTION_IMPROVE_CONTRAST: bool = True
    MOTION_TUNER_IMPROVE_CONTRAST: bool = True
    motion_improve_contrast: bool = True
    motion_tuner_improve_contrast: bool = True
    MOTION_FRAME_HEIGHT: int = 100
    MOTION_TUNER_FRAME_HEIGHT: int = 100
    MOTION_FRAME_WIDTH: int = 320
    MOTION_TUNER_FRAME_WIDTH: int = 320
    MOTION_BLUR_RADIUS: int = 1
    MOTION_TUNER_BLUR_RADIUS: int = 1

    # Action framework (Wave 24)
    ALLOW_COMMAND_ACTIONS: bool = False
    ALLOW_COMMANDS: bool = False
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    SMTP_FROM: str = "vigilcam@localhost"


settings = Settings()
BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = (BASE_DIR / settings.DATA_DIR).resolve()
RECORDING_DIR = (BASE_DIR / settings.RECORDING_DIR).resolve()
SNAPSHOT_DIR = (BASE_DIR / settings.SNAPSHOT_DIR).resolve()
DATABASE_PATH = DATA_DIR / "vigilcam.db"
