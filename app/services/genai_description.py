from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CAPTION_TASKS = ("scene_caption", "image_captioning")
QA_TASKS = ("visual_qa",)
PER_MINUTE = 6
TIMEOUT_S = 45.0
MAX_WAITING = 3
LOCK_WAIT_S = 30.0
_ADAPTERS_TTL_S = 60.0

_lock = asyncio.Lock()
_calls: dict[str, deque[float]] = {}
_adapters: tuple[float, list[dict]] | None = None

_waiting = 0


class RateLimited(Exception):
    pass


class Busy(Exception):
    """The model is taken and the queue is full, or the wait ran out."""


def _allow(caller: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    for key in [k for k, v in list(_calls.items()) if not v or now - v[-1] > 60]:
        del _calls[key]
    q = _calls.setdefault(caller, deque())
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= PER_MINUTE:
        return False
    q.append(now)
    return True


def _kai_c_url() -> str:
    try:
        from app.core.config import settings

        return str(
            getattr(settings, "KAI_C_URL", None)
            or getattr(settings, "kai_c_url", None)
            or "http://127.0.0.1:8000"
        )
    except Exception:  # noqa: BLE001
        return "http://127.0.0.1:8000"


def _internal_api_key() -> str:
    try:
        from app.core.config import settings

        return str(
            getattr(settings, "INTERNAL_API_KEY", None)
            or getattr(settings, "internal_api_key", None)
            or ""
        )
    except Exception:  # noqa: BLE001
        return ""


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Internal-Api-Key": _internal_api_key(),
    }


async def _registry() -> list[dict]:
    global _adapters
    now = time.monotonic()
    if _adapters is not None and now - _adapters[0] < _ADAPTERS_TTL_S:
        return _adapters[1]
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            resp = await client.get(f"{_kai_c_url().rstrip('/')}/api/v1/adapters", headers=_headers())
        rows = resp.json().get("adapters") if resp.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001
        logger.debug("genai description: registry unavailable: %s", exc)
        rows = []
    usable = [
        a
        for a in (rows or [])
        if isinstance(a, dict)
        and a.get("approval_status", "approved") == "approved"
        and a.get("health_status", "ok") == "ok"
    ]
    _adapters = (now, usable)
    return usable


def pick(adapters: list[dict], question: str | None) -> tuple[str, str] | None:
    def having(tasks):
        for a in adapters:
            advertised = set(a.get("tasks_advertised") or [])
            for task in tasks:
                if task in advertised:
                    return a.get("name"), task
        return None

    if question:
        found = having(QA_TASKS)
        if found:
            return found
    return having(CAPTION_TASKS + QA_TASKS)


def text_of(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    if not isinstance(result, dict):
        result = body
    for key in ("answer", "caption", "text", "description"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:1000]
    return None


async def describe(camera, caller: str, question: str | None = None) -> dict[str, Any]:
    global _waiting
    import httpx

    from app.services import kai_c_service

    if not _allow(caller):
        raise RateLimited()
    if _waiting >= MAX_WAITING:
        raise Busy()
    _reg = _registry()
    if hasattr(_reg, "__await__"):
        _reg = await _reg
    choice = pick(_reg, question)
    if choice is None:
        return {"available": False, "description": None, "model": None, "task": None}
    name, task = choice

    url = getattr(camera, "rtsp_url", None) or getattr(camera, "source", None) or getattr(camera, "url", None) or ""
    jpeg = None
    try:
        service = kai_c_service.get_kai_c_service()
        if hasattr(service, "capture_frame_bytes"):
            jpeg = await service.capture_frame_bytes(url, getattr(camera, "id", None))
    except Exception:  # noqa: BLE001
        jpeg = None
    if not jpeg:
        raise LookupError("Could not capture a frame (camera offline?)")

    params: dict[str, Any] = {"camera_id": str(getattr(camera, "id", ""))}
    if question:
        params["question"] = params["prompt"] = question

    from app.services.adapter_contract import build_infer_payload

    payload = build_infer_payload(task=task, jpeg_bytes=jpeg, params=params)

    if _waiting >= MAX_WAITING:
        raise Busy()
    _waiting += 1
    try:
        await asyncio.wait_for(_lock.acquire(), LOCK_WAIT_S)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise Busy() from exc
    finally:
        _waiting -= 1
    try:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, trust_env=False) as client:
                resp = await client.post(
                    f"{_kai_c_url().rstrip('/')}/api/v1/infer/{name}",
                    json=payload,
                    headers=_headers(),
                )
        except httpx.HTTPError as exc:
            logger.info("genai description: %s unreachable: %s", name, exc)
            return {"available": False, "description": None, "model": name, "task": task}
    finally:
        _lock.release()

    if resp.status_code != 200:
        logger.info("genai description: %s answered %s", name, resp.status_code)
        return {"available": False, "description": None, "model": name, "task": task}
    try:
        text = text_of(resp.json())
    except ValueError:
        text = None
    return {"available": text is not None, "description": text, "model": name, "task": task}


# Top-level services aliases for opennvr-style imports.
import sys as _sys

_sys.modules.setdefault("services.genai_description", _sys.modules[__name__])
_sys.modules.setdefault("services.scene_description", _sys.modules[__name__])
_sys.modules.setdefault("services.genai", _sys.modules[__name__])
