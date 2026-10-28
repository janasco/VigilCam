from __future__ import annotations

from pathlib import Path

from app.core.config import BASE_DIR

LOCK_PATH = BASE_DIR / "config" / "initialized.lock"


def is_initialized() -> bool:
    return LOCK_PATH.exists()


def mark_initialized() -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text("initialized\n", encoding="utf-8")
