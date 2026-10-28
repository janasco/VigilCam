from __future__ import annotations

import socket
from typing import Optional, Tuple

from app.core.config import settings

_zeroconf = None
_info = None


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _is_initialized() -> bool:
    from app.core.state import is_initialized

    return is_initialized()


def start_broadcast(port: Optional[int] = None) -> None:
    global _zeroconf, _info
    try:
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
    except Exception:
        print("zeroconf not installed; mDNS broadcast disabled")
        return

    port = port or settings.PORT
    initialized = _is_initialized()
    local_ip = _local_ip()
    properties = {
        "status": "ready" if initialized else "unconfigured",
        "api_version": "v1",
        "setup_url": f"http://{local_ip}:{port}",
    }
    _info = ServiceInfo(
        "_vigilcam._tcp.local.",
        "VigilCam Server._vigilcam._tcp.local.",
        addresses=[socket.inet_aton(local_ip)],
        port=port,
        properties=properties,
        server="vigilcam.local.",
    )
    try:
        _zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        _zeroconf.register_service(_info)
        print(f"VigilCam broadcasting on http://{local_ip}:{port} (vigilcam.local:{port})")
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"mDNS broadcast failed: {exc!r}")


def stop_broadcast() -> None:
    global _zeroconf, _info
    try:
        if _zeroconf is not None and _info is not None:
            _zeroconf.unregister_service(_info)
    except Exception:
        pass
    try:
        if _zeroconf is not None:
            _zeroconf.close()
    except Exception:
        pass
    _zeroconf = None
    _info = None
