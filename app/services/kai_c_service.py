from __future__ import annotations

from typing import Any


class KaiCService:
    async def capture_frame_bytes(self, url: str, cid: Any) -> bytes | None:
        return None


def get_kai_c_service() -> KaiCService:
    return KaiCService()


import sys as _sys

_sys.modules.setdefault("services.kai_c_service", _sys.modules[__name__])
