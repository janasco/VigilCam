from __future__ import annotations

import base64
from typing import Any


def build_infer_payload(task: str, jpeg_bytes: Any, params: Any = None) -> dict[str, Any]:
    if isinstance(jpeg_bytes, (bytes, bytearray)):
        image = base64.b64encode(bytes(jpeg_bytes)).decode("ascii")
    else:
        image = jpeg_bytes
    return {
        "task": task,
        "image": image,
        "image_base64": image,
        "jpeg_bytes": jpeg_bytes,
        "params": params or {},
    }


import sys as _sys

_sys.modules.setdefault("services.adapter_contract", _sys.modules[__name__])
