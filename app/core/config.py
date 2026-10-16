from __future__ import annotations

import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "VigilCam"
    APP_VERSION: str = "0.1.0"
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    DEBUG: bool = False

    DATA_DIR: Path = Path("data")
    DATABASE_URL: str = "sqlite+aiosqlite:///data/vigilcam.db"

    SECRET_KEY: str = "change-me-in-production"
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin"

    # AI / detection
    DETECTION_ENABLED: bool = True
    DEFAULT_MODEL: str = "yolov8n"
    DETECTION_CONFIDENCE: float = 0.5

    # Camera defaults
    RECORDING_DIR: Path = Path("data/recordings")
    SNAPSHOT_DIR: Path = Path("data/snapshots")


settings = Settings()
BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = (BASE_DIR / settings.DATA_DIR).resolve()
RECORDING_DIR = (BASE_DIR / settings.RECORDING_DIR).resolve()
SNAPSHOT_DIR = (BASE_DIR / settings.SNAPSHOT_DIR).resolve()
DATABASE_PATH = DATA_DIR / "vigilcam.db"
