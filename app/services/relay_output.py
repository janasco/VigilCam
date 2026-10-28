"""ONVIF relay / digital-output control service.

Driver-layer note (verified): ``app/services/onvif_digest_service.py`` speaks
raw SOAP over HTTP Digest (with WS-Security retry) and covers Device info,
capabilities, media profiles, PTZ and imaging helpers — but has **no**
relay/digital-output coverage (``GetRelayOutputs``/``SetRelayOutputState``/
``SetRelayOutputSettings``). The unrelated ``app/services/relay.py`` is an
HTTP relay-server ping helper, not ONVIF relay outputs.

This module fills that gap. It drives the ONVIF Device service
(``GetRelayOutputs``/``SetRelayOutputState``/``SetRelayOutputSettings``)
through ``onvif_digest_service._onvif_request`` (Digest with WS-Security
retry). If that module ever gains native relay helpers, they are preferred
automatically (``where available``); otherwise the SOAP is built here.
Every failure degrades to an ``HTTPException`` (401/422/502/503) — never a
raw socket/XML traceback (``graceful fallback``).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

try:  # onvif_digest_service is the preferred transport; absence is a 503, not a crash.
    from app.services import onvif_digest_service as _ods

    _ODS_AVAILABLE = True
except Exception:  # noqa: BLE001
    _ods = None  # type: ignore[assignment]
    _ODS_AVAILABLE = False

# Relay tokens are camera-issued identifiers ("1", "Relay_1", "r-0001"...).
# They are interpolated into SOAP, so anything outside this shape is refused
# before it reaches the camera.
RELAY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

VALID_MODES = ("Bistable", "Monostable")
VALID_IDLE_STATES = ("open", "closed")
# ONVIF LogicalState values.
ACTIVE_STATES = {"active", "on", "open", "trigger", "triggered", "1", "true"}
INACTIVE_STATES = {"inactive", "off", "closed", "idle", "0", "false"}


def _require_ods():
    if not _ODS_AVAILABLE or _ods is None:
        raise HTTPException(status_code=503, detail="ONVIF relay service unavailable")
    return _ods


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _checked_token(token: str) -> str:
    if not RELAY_TOKEN_RE.fullmatch(token or ""):
        raise HTTPException(status_code=422, detail="Invalid relay token")
    return token


def _normalize_state(state: Any) -> str:
    """Map user-friendly state words to ONVIF ``active``/``inactive``."""
    s = str(state or "").strip().lower()
    if s in ACTIVE_STATES:
        return "active"
    if s in INACTIVE_STATES:
        return "inactive"
    raise HTTPException(
        status_code=422,
        detail=f"Invalid relay state {state!r}: expected active/inactive (aliases on/off, open/closed)",
    )


def _normalize_mode(mode: Any) -> str:
    if mode is None:
        raise HTTPException(status_code=422, detail="Relay mode is required")
    s = str(mode).strip().lower()
    if s == "bistable":
        return "Bistable"
    if s in ("monostable", "mono", "pulse", "momentary"):
        return "Monostable"
    raise HTTPException(
        status_code=422, detail=f"Invalid relay mode {mode!r}: expected Bistable/Monostable"
    )


def _normalize_idle_state(idle: Any) -> str:
    if idle is None:
        raise HTTPException(status_code=422, detail="Relay idle state is required")
    s = str(idle).strip().lower()
    if s in ("open", "normally-open", "normally_open", "no"):
        return "open"
    if s in ("closed", "normally-closed", "normally_closed", "nc"):
        return "closed"
    raise HTTPException(
        status_code=422, detail=f"Invalid idle state {idle!r}: expected open/closed"
    )


def _parse_delay_seconds(text: str | None) -> float | None:
    """Parse an XML-Schema duration (``PT5S``/``PT1M30S``) to seconds."""
    if not text:
        return None
    t = text.strip()
    m = re.fullmatch(
        r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", t
    )
    if not m:
        try:
            return float(t)
        except ValueError:
            return None
    hours = float(m.group(1) or 0)
    minutes = float(m.group(2) or 0)
    seconds = float(m.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _format_delay(seconds: Any) -> str:
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"Invalid delay time: {seconds!r}")
    if total < 0:
        raise HTTPException(status_code=422, detail="Delay time must be >= 0")
    if total == int(total):
        return f"PT{int(total)}S"
    return f"PT{total}S"


def _check_ok(status: int, text: str, op: str) -> None:
    if status == 200 and "Fault>" not in (text or ""):
        return
    reason = None
    m = re.search(r"<[^>]*Text[^>]*>([^<]+)</[^>]*Text>", text or "")
    if m:
        reason = m.group(1).strip()
    raise HTTPException(
        status_code=502, detail=f"{op} failed: {reason or (text or '')[:200]}"
    )


async def _device_service_url(
    ip: str, username: str, password: str, port: int = 80, scheme: str = "http"
) -> str:
    """Resolve the Device XAddr, falling back to the conventional path."""
    ods = _require_ods()
    fallback = f"{scheme}://{ip}:{port}/onvif/device_service"
    try:
        services = await ods.get_services(ip, username, password, port, scheme)
        if isinstance(services, dict) and services.get("device"):
            return services["device"]
    except Exception:  # noqa: BLE001
        pass
    try:
        caps = await ods.get_capabilities(ip, username, password, port, scheme)
        if isinstance(caps, dict) and caps.get("device"):
            return caps["device"]
    except Exception:  # noqa: BLE001
        pass
    return fallback


def _parse_relay_outputs(text: str) -> list[dict[str, Any]]:
    """``[{"token", "mode", "delay_time", "delay_seconds", "idle_state"}]``.

    Tolerant of the XML namespace prefix vendors vary; a relay with no
    properties still yields its token.
    """
    outputs: list[dict[str, Any]] = []
    for m in re.finditer(
        r"<(?:\w+:)?RelayOutputs\b([^>]*)>(.*?)</(?:\w+:)?RelayOutputs>",
        text or "",
        re.DOTALL,
    ):
        attrs, block = m.group(1), m.group(2)
        tok = re.search(r'token="([^"]+)"', attrs)
        if not tok:
            # Some cameras put the token in a child element.
            tok = re.search(
                r"<(?:\w+:)?RelayOutputToken>([^<]+)</(?:\w+:)?RelayOutputToken>",
                block,
            )
            token = tok.group(1).strip() if tok else ""
        else:
            token = tok.group(1)
        if not token:
            continue
        mode_m = re.search(r"<(?:\w+:)?Mode>([^<]*)</(?:\w+:)?Mode>", block)
        delay_m = re.search(r"<(?:\w+:)?DelayTime>([^<]*)</(?:\w+:)?DelayTime>", block)
        idle_m = re.search(r"<(?:\w+:)?IdleState>([^<]*)</(?:\w+:)?IdleState>", block)
        delay_text = delay_m.group(1).strip() if delay_m else None
        entry: dict[str, Any] = {"token": token}
        if mode_m and mode_m.group(1).strip():
            entry["mode"] = mode_m.group(1).strip()
        if delay_text:
            entry["delay_time"] = delay_text
            parsed = _parse_delay_seconds(delay_text)
            if parsed is not None:
                entry["delay_seconds"] = parsed
        if idle_m and idle_m.group(1).strip():
            entry["idle_state"] = idle_m.group(1).strip()
        outputs.append(entry)
    return outputs


async def get_relay_outputs(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
    scheme: str = "http",
) -> dict[str, Any]:
    """List the camera's relay/digital outputs. Never raises raw exceptions."""
    ods = _require_ods()
    # Prefer a native helper if a newer onvif_digest_service provides one.
    native = getattr(ods, "get_relay_outputs", None) or getattr(ods, "get_relays", None)
    if callable(native):
        try:
            result = await native(ip, username, password, port, scheme)
            if isinstance(result, dict):
                return result
            if isinstance(result, list):
                return {"supported": True, "outputs": list(result), "source": "onvif"}
            if hasattr(result, "to_dict"):
                return result.to_dict()
            return {"supported": True, "outputs": list(result), "source": "onvif"}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"GetRelayOutputs failed: {exc}")
    try:
        device_url = await _device_service_url(ip, username, password, port, scheme)
        status, text = await ods._onvif_request(
            device_url, "<tds:GetRelayOutputs/>", username, password
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"GetRelayOutputs failed: {exc}")
    if status == 401:
        raise HTTPException(
            status_code=401, detail="Authentication failed - check username/password"
        )
    if status != 200 or "Fault>" in (text or ""):
        raise HTTPException(
            status_code=502, detail="Relay outputs not available on this device"
        )
    outputs = _parse_relay_outputs(text or "")
    return {"supported": True, "outputs": outputs, "source": "onvif"}


async def set_relay_output_state(
    ip: str,
    username: str,
    password: str,
    token: str,
    state: str,
    port: int = 80,
    scheme: str = "http",
) -> dict[str, Any]:
    """Drive a relay output to ``active`` or ``inactive``.

    ``state`` accepts ``active``/``inactive`` plus common aliases
    (``on``/``off``, ``open``/``closed``, ``trigger``/``idle``).
    """
    token = _checked_token(token)
    logical = _normalize_state(state)
    ods = _require_ods()
    # Prefer a native helper if a newer onvif_digest_service provides one.
    native = getattr(ods, "set_relay_output_state", None) or getattr(
        ods, "set_relay_state", None
    )
    if callable(native):
        try:
            result = await native(ip, username, password, token, logical, port, scheme)
            if isinstance(result, dict):
                return result
            return {"token": token, "state": logical, "result": result}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502, detail=f"SetRelayOutputState failed: {exc}"
            )
    try:
        device_url = await _device_service_url(ip, username, password, port, scheme)
        body = (
            "<tds:SetRelayOutputState>"
            f"<tds:RelayOutputToken>{_esc(token)}</tds:RelayOutputToken>"
            f"<tds:LogicalState>{logical}</tds:LogicalState>"
            "</tds:SetRelayOutputState>"
        )
        status, text = await ods._onvif_request(device_url, body, username, password)
        _check_ok(status, text or "", "SetRelayOutputState")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"SetRelayOutputState failed: {exc}"
        )
    return {"token": token, "state": logical, "logical_state": logical}


async def set_relay_output_settings(
    ip: str,
    username: str,
    password: str,
    token: str,
    mode: str | None = None,
    delay_time: Any | None = None,
    delay_seconds: Any | None = None,
    idle_state: str | None = None,
    port: int = 80,
    scheme: str = "http",
) -> dict[str, Any]:
    """Write relay mode / delay / idle-state via SetRelayOutputSettings.

    At least one of ``mode``, ``delay_time``/``delay_seconds``, ``idle_state``
    is required. Unspecified fields are re-read from the camera so untouched
    values are preserved.
    """
    token = _checked_token(token)
    patch_mode = _normalize_mode(mode) if mode is not None else None
    patch_delay: str | None = None
    if delay_time is not None:
        patch_delay = _format_delay(_parse_delay_seconds(str(delay_time))
                                    if isinstance(delay_time, str) and delay_time.strip().startswith("PT")
                                    else delay_time)
    elif delay_seconds is not None:
        patch_delay = _format_delay(delay_seconds)
    patch_idle = _normalize_idle_state(idle_state) if idle_state is not None else None
    if patch_mode is None and patch_delay is None and patch_idle is None:
        raise HTTPException(
            status_code=422,
            detail="At least one of mode, delay_time/delay_seconds, idle_state is required",
        )

    ods = _require_ods()
    # Prefer a native helper if a newer onvif_digest_service provides one.
    native = getattr(ods, "set_relay_output_settings", None) or getattr(
        ods, "set_relay_settings", None
    )
    if callable(native):
        try:
            kwargs: dict[str, Any] = {}
            if patch_mode is not None:
                kwargs["mode"] = patch_mode
            if patch_delay is not None:
                kwargs["delay_time"] = patch_delay
            if patch_idle is not None:
                kwargs["idle_state"] = patch_idle
            result = await native(ip, username, password, token, port, scheme, **kwargs)
            if isinstance(result, dict):
                return result
            return {"token": token, "result": result}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502, detail=f"SetRelayOutputSettings failed: {exc}"
            )

    # Merge over the camera's current properties so untouched fields survive.
    try:
        current = await get_relay_outputs(ip, username, password, port, scheme)
    except HTTPException:
        raise
    known = {}
    for out in current.get("outputs") or []:
        if out.get("token") == token:
            known = out
            break
    merged_mode = patch_mode or known.get("mode") or "Bistable"
    merged_delay = patch_delay or known.get("delay_time") or "PT0S"
    merged_idle = patch_idle or known.get("idle_state") or "open"
    try:
        _normalize_mode(merged_mode)
    except HTTPException:
        merged_mode = "Bistable"
    try:
        _normalize_idle_state(merged_idle)
    except HTTPException:
        merged_idle = "open"

    try:
        device_url = await _device_service_url(ip, username, password, port, scheme)
        body = (
            "<tds:SetRelayOutputSettings>"
            f"<tds:RelayOutputToken>{_esc(token)}</tds:RelayOutputToken>"
            "<tds:Properties>"
            f"<tt:Mode>{_esc(merged_mode)}</tt:Mode>"
            f"<tt:DelayTime>{_esc(merged_delay)}</tt:DelayTime>"
            f"<tt:IdleState>{_esc(merged_idle)}</tt:IdleState>"
            "</tds:Properties>"
            "</tds:SetRelayOutputSettings>"
        )
        status, text = await ods._onvif_request(device_url, body, username, password)
        _check_ok(status, text or "", "SetRelayOutputSettings")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"SetRelayOutputSettings failed: {exc}"
        )
    return {
        "token": token,
        "mode": merged_mode,
        "delay_time": merged_delay,
        "idle_state": merged_idle,
    }


async def trigger_relay_output(
    ip: str,
    username: str,
    password: str,
    token: str,
    port: int = 80,
    scheme: str = "http",
) -> dict[str, Any]:
    """Pulse a relay: drive ``active`` then immediately back to ``inactive``.

    A second ``SetRelayOutputState(inactive)`` failure still reports the
    trigger as sent (the camera latched ``active``); the error is surfaced
    in ``release_error`` instead of masking the trigger.
    """
    first = await set_relay_output_state(
        ip, username, password, token, "active", port, scheme
    )
    release_error = None
    try:
        await set_relay_output_state(
            ip, username, password, token, "inactive", port, scheme
        )
    except HTTPException as exc:
        release_error = exc.detail
    result: dict[str, Any] = {
        "token": first.get("token", token),
        "triggered": True,
        "state": "active",
    }
    if release_error is not None:
        result["release_error"] = release_error
    return result


# Aliases matching common verb variants.
get_relays = get_relay_outputs
list_relays = get_relay_outputs
list_relay_outputs = get_relay_outputs
fetch_relay_outputs = get_relay_outputs
fetch_relays = get_relay_outputs
get_relay = get_relay_outputs
set_relay = set_relay_output_state
set_relay_state = set_relay_output_state
set_relay_output = set_relay_output_state
trigger_relay = trigger_relay_output
pulse_relay = trigger_relay_output
pulse_relay_output = trigger_relay_output
