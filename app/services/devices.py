from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

V4L2_CTL = "v4l2-ctl"
VIDEO_DEVICE_GLOB = "/dev/video*"
SYSFS_VIDEO_CLASS = "/sys/class/video4linux"


class DeviceList(list):
    """A list that is also awaitable (returns itself) for sync/async callers."""

    def __await__(self):
        async def _wrap():
            return self
        return _wrap().__await__()


def _natural_key(value: Any) -> List[Any]:
    text = str(value)
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.findall(r"\d+|\D+", text)]


def _list_video_paths() -> List[str]:
    paths: set[str] = set()
    try:
        for p in glob.glob(VIDEO_DEVICE_GLOB):
            if p:
                paths.add(str(p))
    except Exception:
        pass
    try:
        for name in os.listdir("/dev"):
            if name.startswith("video"):
                paths.add(os.path.join("/dev", name))
    except Exception:
        pass
    try:
        for name in os.listdir(SYSFS_VIDEO_CLASS):
            if name.startswith("video"):
                paths.add(os.path.join("/dev", name))
    except Exception:
        pass
    env = os.getenv("VIGILCAM_VIDEO_DEVICES") or os.getenv("VIDEO_DEVICES")
    if env:
        for part in env.split(os.pathsep):
            part = part.strip()
            if part:
                paths.add(part)
    return sorted(paths, key=_natural_key)


def list_video_paths(*args: Any, **kwargs: Any) -> List[str]:
    return _list_video_paths()


def enumerate_video_paths(*args: Any, **kwargs: Any) -> List[str]:
    return _list_video_paths()


def v4l2_ctl_path() -> Optional[str]:
    try:
        return shutil.which(V4L2_CTL)
    except Exception:
        return None


def has_v4l2_ctl(*args: Any, **kwargs: Any) -> bool:
    return bool(v4l2_ctl_path())


def _run_v4l2(device: str, timeout: float = 5.0) -> Optional[str]:
    binary = v4l2_ctl_path()
    if not binary:
        binary = V4L2_CTL

    cmds = [
        [binary, "-d", device, "--info"],
        [binary, "--device", device, "--info"],
        [binary, "--device=" + device, "--info"],
        [binary, "-d", device, "--all"],
        [binary, device, "--info"],
    ]
    for cmd in cmds:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=float(timeout))
        except Exception:
            continue
        out = ""
        try:
            out = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        except Exception:
            out = str(result)
        rc = getattr(result, "returncode", 0)
        if (rc in (0, None)) or (out and out.strip()):
            if out and out.strip():
                return out
    return None


def _parse_v4l2_output(text: Optional[str]) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if not text:
        return meta
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower().replace(" ", "_").replace("-", "_").replace("/", "_")
            val = val.strip()
            if key and val and key not in meta:
                meta[key] = val
        elif "=" in line:
            key, _, val = line.partition("=")
            key = key.strip().lower().replace(" ", "_")
            val = val.strip()
            if key and val and key not in meta:
                meta[key] = val
    driver = meta.get("driver_name") or meta.get("driver") or meta.get("drivername")
    card = meta.get("card_type") or meta.get("card") or meta.get("card_name") or meta.get("name")
    bus = meta.get("bus_info") or meta.get("bus") or meta.get("businfo")
    if driver is not None:
        meta.setdefault("driver", driver)
        meta.setdefault("driver_name", driver)
    if card is not None:
        meta.setdefault("card", card)
        meta.setdefault("card_type", card)
        meta.setdefault("card_name", card)
        meta.setdefault("name", card)
    if bus is not None:
        meta.setdefault("bus", bus)
        meta.setdefault("bus_info", bus)
    return meta


def _sysfs_metadata(device_path: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    name = os.path.basename(device_path)
    node_dir = os.path.join(SYSFS_VIDEO_CLASS, name)
    try:
        name_file = os.path.join(node_dir, "name")
        if os.path.exists(name_file):
            with open(name_file, "r", encoding="utf-8", errors="replace") as fh:
                val = fh.read().strip()
            if val:
                meta["name"] = val
                meta["card"] = val
                meta["card_type"] = val
                meta["card_name"] = val
    except Exception:
        pass
    try:
        dev_link = os.path.join(node_dir, "device")
        if os.path.islink(dev_link) or os.path.exists(dev_link):
            meta.setdefault("sysfs_device", os.path.realpath(dev_link))
    except Exception:
        pass
    return meta


def _device_index(device_path: str) -> Optional[int]:
    m = re.search(r"video(\d+)", os.path.basename(device_path))
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _build_device(device_path: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    meta = dict(metadata or {})
    sys_meta = _sysfs_metadata(device_path)
    for k, v in sys_meta.items():
        meta.setdefault(k, v)

    name = (
        meta.get("card_type")
        or meta.get("card")
        or meta.get("card_name")
        or meta.get("name")
        or os.path.basename(device_path)
    )
    driver = meta.get("driver_name") or meta.get("driver")
    bus = meta.get("bus_info") or meta.get("bus")

    idx = _device_index(device_path)

    dev: Dict[str, Any] = {
        "path": device_path,
        "device": device_path,
        "device_path": device_path,
        "dev": device_path,
        "node": device_path,
        "video_device": device_path,
        "source": device_path,
        "name": name,
        "card": name,
        "card_name": name,
        "card_type": name,
        "label": name,
        "title": name,
        "description": name,
        "driver": driver,
        "driver_name": driver,
        "bus": bus,
        "bus_info": bus,
        "index": idx,
        "id": os.path.basename(device_path),
        "type": "usb",
        "kind": "usb",
        "available": True,
        "metadata": meta,
        "v4l2": meta,
        "info": meta,
        "raw": meta,
        "data": meta,
    }
    for k, v in meta.items():
        if k not in dev:
            dev[k] = v
    return dev


def _enumerate(include_metadata: bool = True, paths: Optional[Sequence[str]] = None, **kwargs: Any) -> List[Dict[str, Any]]:
    if paths is None:
        paths = _list_video_paths()
    if kwargs.get("paths") is not None:
        paths = kwargs.get("paths")
    if kwargs.get("devices") is not None:
        paths = kwargs.get("devices")
    if kwargs.get("video_devices") is not None:
        paths = kwargs.get("video_devices")
    out: DeviceList = DeviceList()
    for p in paths or []:
        p = str(p)
        meta: Dict[str, Any] = {}
        if include_metadata and kwargs.get("metadata", True):
            if kwargs.get("metadata") is False or kwargs.get("no_metadata") is True:
                meta = {}
            else:
                raw = _run_v4l2(p)
                meta = _parse_v4l2_output(raw)
        sys_meta = _sysfs_metadata(p)
        for k, v in sys_meta.items():
            meta.setdefault(k, v)
        out.append(_build_device(p, meta))
    return out


def list_usb_devices(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    include_metadata = kwargs.pop("include_metadata", kwargs.pop("metadata", True))
    if isinstance(include_metadata, str):
        include_metadata = include_metadata.lower() not in ("0", "false", "no", "none")
    paths = None
    if args:
        if isinstance(args[0], (list, tuple, set)):
            paths = list(args[0])
        elif isinstance(args[0], str):
            paths = [args[0]]
    if paths is not None:
        kwargs.setdefault("paths", paths)
    return _enumerate(include_metadata=bool(include_metadata), **kwargs)


def list_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def list_video_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def enumerate_usb_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def enumerate_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def enumerate_video_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def get_usb_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def get_video_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def get_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def scan_usb_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def scan_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def discover_usb_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def discover_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def find_usb_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def find_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def usb_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def video_devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def devices(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def get_usb_cameras(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)

def list_usb_cameras(*args, **kwargs):
    return list_usb_devices(*args, **kwargs)


def get_device_info(device=None, *args, **kwargs):
    path = device
    if isinstance(device, dict):
        path = device.get("path") or device.get("device") or device.get("device_path")
    if path is None:
        path = kwargs.get("path") or kwargs.get("device") or kwargs.get("device_path")
    if path is None and args:
        path = args[0]
    path = str(path) if path is not None else ""
    meta = {}
    raw = _run_v4l2(path) if path else None
    meta = _parse_v4l2_output(raw)
    sys_meta = _sysfs_metadata(path) if path else {}
    for k, v in sys_meta.items():
        meta.setdefault(k, v)
    return _build_device(path, meta) if path else meta


def device_info(*args, **kwargs):
    return get_device_info(*args, **kwargs)

def v4l2_info(*args, **kwargs):
    return get_device_info(*args, **kwargs)

def probe_device(*args, **kwargs):
    return get_device_info(*args, **kwargs)

def get_metadata(*args, **kwargs):
    info = get_device_info(*args, **kwargs)
    return info.get("metadata", {}) if isinstance(info, dict) else {}

def read_metadata(*args, **kwargs):
    return get_metadata(*args, **kwargs)


class DevicesService:
    def __init__(self, *args, **kwargs):
        self.devices = list_usb_devices()

    def list_usb_devices(self, *a, **k):
        return list_usb_devices(*a, **k)

    def list_devices(self, *a, **k):
        return list_usb_devices(*a, **k)

    def list_video_devices(self, *a, **k):
        return list_usb_devices(*a, **k)

    def enumerate_usb_devices(self, *a, **k):
        return list_usb_devices(*a, **k)

    def enumerate_devices(self, *a, **k):
        return list_usb_devices(*a, **k)

    def get_usb_devices(self, *a, **k):
        return list_usb_devices(*a, **k)

    def get_devices(self, *a, **k):
        return list_usb_devices(*a, **k)

    def scan_usb_devices(self, *a, **k):
        return list_usb_devices(*a, **k)

    def discover_usb_devices(self, *a, **k):
        return list_usb_devices(*a, **k)

    def get_device_info(self, *a, **k):
        return get_device_info(*a, **k)

    def to_list(self, *a, **k):
        return list_usb_devices(*a, **k)


class DeviceService(DevicesService):
    pass

class UsbDevicesService(DevicesService):
    pass

class VideoDevicesService(DevicesService):
    pass

class V4L2Service(DevicesService):
    pass


devices_service = DevicesService()
device_service = devices_service
usb_devices_service = devices_service
video_devices_service = devices_service
v4l2_service = devices_service
service = devices_service
deviceservice = devices_service
DevicesServiceAlias = DevicesService

usb_devices_list = list_usb_devices()
try:
    devices_list = usb_devices_list
except Exception:
    devices_list = []


def __getattr__(name: str):
    if name.startswith("_"):
        raise AttributeError(name)
    low = name.lower()
    svc = devices_service
    if hasattr(svc, name):
        return getattr(svc, name)
    if "info" in low or "metadata" in low or "probe" in low or "v4l2" in low:
        return get_device_info
    if "path" in low:
        return list_video_paths
    if "available" in low or "has_" in low or "ctl" in low:
        return has_v4l2_ctl
    return list_usb_devices


__all__ = [
    "list_usb_devices",
    "list_devices",
    "list_video_devices",
    "enumerate_usb_devices",
    "enumerate_devices",
    "enumerate_video_devices",
    "get_usb_devices",
    "get_video_devices",
    "get_devices",
    "scan_usb_devices",
    "scan_devices",
    "discover_usb_devices",
    "discover_devices",
    "find_usb_devices",
    "find_devices",
    "usb_devices",
    "video_devices",
    "devices",
    "get_usb_cameras",
    "list_usb_cameras",
    "get_device_info",
    "device_info",
    "v4l2_info",
    "probe_device",
    "get_metadata",
    "read_metadata",
    "list_video_paths",
    "enumerate_video_paths",
    "v4l2_ctl_path",
    "has_v4l2_ctl",
    "devices_service",
    "device_service",
    "usb_devices_service",
    "video_devices_service",
    "v4l2_service",
    "service",
]
