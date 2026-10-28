"""Mock camera firmware check service for VigilCam.

Provides pure-Python version parsing and comparison against a mock
"latest firmware" table. No network or database access; intended as a
placeholder until real vendor firmware checks are integrated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Mock latest firmware versions per camera model.
MOCK_LATEST_FIRMWARE: Dict[str, str] = {
    "generic": "2.5.0",
    "vigilcam-pro": "3.1.2",
    "vigilcam-mini": "1.9.4",
    "onvif-generic": "2.0.1",
}

DEFAULT_MODEL = "generic"
DEFAULT_LATEST_VERSION = MOCK_LATEST_FIRMWARE[DEFAULT_MODEL]


def _clean_version(value: Any) -> str:
    if value is None:
        return "0.0.0"
    s = str(value).strip()
    if not s:
        return "0.0.0"
    # Strip leading 'v' (e.g. 'v1.2.3') and build metadata.
    if s[:1].lower() == "v":
        s = s[1:]
    s = s.split("+", 1)[0].split("-", 1)[0].strip()
    return s or "0.0.0"


def parse_version(value: Any) -> Tuple[int, ...]:
    """Parse a dotted version string into a tuple of ints.

    Non-numeric segments are treated as 0. Empty/None -> (0, 0, 0).
    Always returns at least 3 components for stable comparison.
    """
    s = _clean_version(value)
    parts = re.split(r"[.\s_]+", s)
    nums: List[int] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        m = re.match(r"(\d+)", p)
        if m:
            try:
                nums.append(int(m.group(1)))
            except ValueError:
                nums.append(0)
        else:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def compare_versions(current: Any, latest: Any) -> int:
    """Compare two version strings.

    Returns -1 if current < latest, 0 if equal, 1 if current > latest.
    """
    c = parse_version(current)
    L = parse_version(latest)
    size = max(len(c), len(L))
    c = c + (0,) * (size - len(c))
    L = L + (0,) * (size - len(L))
    if c < L:
        return -1
    if c > L:
        return 1
    return 0


def get_latest_version(model: Any = None) -> str:
    """Return the mock latest firmware version for a model."""
    if model is None:
        return DEFAULT_LATEST_VERSION
    key = str(model).strip() or DEFAULT_MODEL
    if key in MOCK_LATEST_FIRMWARE:
        return MOCK_LATEST_FIRMWARE[key]
    lowered = {k.lower(): v for k, v in MOCK_LATEST_FIRMWARE.items()}
    return lowered.get(key.lower(), DEFAULT_LATEST_VERSION)


def get_latest_firmware(model: Any = None) -> str:
    return get_latest_version(model)


def is_update_available(current_version: Any, latest_version: Any = None, model: Any = None) -> bool:
    """Return True when ``current_version`` is older than latest."""
    latest = latest_version if latest_version is not None else get_latest_version(model)
    return compare_versions(current_version, latest) < 0


def check_firmware(
    current_version: Any = None,
    latest_version: Any = None,
    model: Any = None,
    camera_id: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Check firmware status with mock version comparison.

    Accepts flexible kwarg aliases (current/current_fw/fw_version/version,
    model/camera_model, camera/camera_id/id) for compatibility.
    """
    if current_version is None:
        for key in ("current", "current_fw", "fw_version", "firmware_version", "version", "currentVersion"):
            if kwargs.get(key) is not None:
                current_version = kwargs.get(key)
                break
    if model is None:
        for key in ("camera_model", "model_name", "modelName", "kind"):
            if kwargs.get(key) is not None:
                model = kwargs.get(key)
                break
    if camera_id is None:
        for key in ("camera", "id", "cam_id", "cameraId"):
            if kwargs.get(key) is not None:
                camera_id = kwargs.get(key)
                break
    if latest_version is None:
        for key in ("latest", "latest_fw", "latestVersion", "target_version"):
            if kwargs.get(key) is not None:
                latest_version = kwargs.get(key)
                break

    model_name = str(model).strip() if model is not None else DEFAULT_MODEL
    if not model_name:
        model_name = DEFAULT_MODEL
    latest = str(latest_version).strip() if latest_version is not None else get_latest_version(model_name)
    current = _clean_version(current_version) if current_version is not None else "0.0.0"
    # Keep original-ish display but normalized.
    comparison = compare_versions(current, latest)
    update_available = comparison < 0
    up_to_date = comparison >= 0
    if comparison == 0:
        status = "up_to_date"
    elif comparison < 0:
        status = "update_available"
    else:
        status = "newer_than_latest"

    cid: Optional[int] = None
    if camera_id is not None:
        try:
            if hasattr(camera_id, "id") and not isinstance(camera_id, (int, str, float)):
                camera_id = getattr(camera_id, "id")
            cid = int(camera_id)
        except (TypeError, ValueError):
            cid = None

    return {
        "camera_id": cid,
        "model": model_name,
        "current_version": current,
        "latest_version": latest,
        "update_available": update_available,
        "up_to_date": up_to_date,
        "needs_update": update_available,
        "status": status,
        "comparison": comparison,
        "ok": True,
        "success": True,
    }


def list_models() -> List[str]:
    return sorted(MOCK_LATEST_FIRMWARE.keys())


def set_latest_version(model: Any, version: Any) -> str:
    """Override the mock latest version for a model (useful in tests)."""
    key = str(model).strip() or DEFAULT_MODEL
    MOCK_LATEST_FIRMWARE[key] = _clean_version(version)
    return MOCK_LATEST_FIRMWARE[key]


def reset_mock_data() -> None:
    MOCK_LATEST_FIRMWARE.clear()
    MOCK_LATEST_FIRMWARE.update(
        {
            "generic": "2.5.0",
            "vigilcam-pro": "3.1.2",
            "vigilcam-mini": "1.9.4",
            "onvif-generic": "2.0.1",
        }
    )


@dataclass
class FirmwareInfo:
    camera_id: Optional[int] = None
    model: str = DEFAULT_MODEL
    current_version: str = "0.0.0"
    latest_version: str = DEFAULT_LATEST_VERSION
    update_available: bool = False
    status: str = "up_to_date"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "model": self.model,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "update_available": self.update_available,
            "up_to_date": not self.update_available,
            "needs_update": self.update_available,
            "status": self.status,
            "ok": True,
            "success": True,
        }


class FirmwareService:
    """Class wrapper around the module-level mock firmware functions."""

    def parse_version(self, value: Any) -> Tuple[int, ...]:
        return parse_version(value)

    def compare(self, current: Any, latest: Any) -> int:
        return compare_versions(current, latest)

    def compare_versions(self, current: Any, latest: Any) -> int:
        return compare_versions(current, latest)

    def is_update_available(self, current_version: Any, latest_version: Any = None, model: Any = None) -> bool:
        return is_update_available(current_version, latest_version, model)

    def needs_update(self, *args: Any, **kwargs: Any) -> bool:
        return is_update_available(*args, **kwargs)

    def get_latest(self, model: Any = None) -> str:
        return get_latest_version(model)

    def get_latest_version(self, model: Any = None) -> str:
        return get_latest_version(model)

    def check(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check_firmware(*args, **kwargs)

    def check_firmware(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check_firmware(*args, **kwargs)

    def check_camera(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check_firmware(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return check_firmware(*args, **kwargs)

    def list_models(self) -> List[str]:
        return list_models()


class FirmwareChecker(FirmwareService):
    pass


class FirmwareManager(FirmwareService):
    pass


firmware_service = FirmwareService()
firmware_checker = firmware_service
firmware_manager = firmware_service
service = firmware_service
checker = firmware_service
default_service = firmware_service

# Compatibility aliases.
needs_update = is_update_available
check_update = check_firmware
check_camera_firmware = check_firmware
get_firmware_status = check_firmware
firmware_status = check_firmware
camera_firmware_check = check_firmware
firmware_check = check_firmware
status_for = check_firmware

__all__ = [
    "MOCK_LATEST_FIRMWARE",
    "DEFAULT_MODEL",
    "DEFAULT_LATEST_VERSION",
    "FirmwareInfo",
    "FirmwareService",
    "FirmwareChecker",
    "FirmwareManager",
    "firmware_service",
    "parse_version",
    "compare_versions",
    "get_latest_version",
    "get_latest_firmware",
    "is_update_available",
    "check_firmware",
    "list_models",
    "set_latest_version",
    "reset_mock_data",
]
