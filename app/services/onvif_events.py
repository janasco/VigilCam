"""Parsing ONVIF PullMessages responses into normalized events.

ONVIF eventing delivers a ``PullMessagesResponse`` whose
``NotificationMessage`` blocks describe what happened on the camera
(motion, tamper, I/O, analytics, ...). This module turns those blocks
into the small normalized dict shape used elsewhere in VigilCam
(``event_type``, ``event_state``, ``description``, ...) so callers do not
have to deal with SOAP/WS-BaseNotification specifics.

The parser is pure (no network, no database) and accepts XML as a string,
bytes, or an element-tree node.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import Any, Optional

__all__ = [
    "parse_pull_messages",
    "parse_pull_message",
    "parse_pullmessages",
    "parse_pullMessages",
    "parse_onvif_events",
    "parse_onvif_event",
    "parse_onvif_pull_messages",
    "parse_onvif_pullmessages",
    "parse_events",
    "parse_event",
    "parse_notification_messages",
    "parse_notification_message",
    "extract_events",
    "normalize_events",
    "normalize_event",
    "OnvifEventParser",
    "OnvifEventsParser",
    "OnvifPullMessagesParser",
    "PullMessagesParser",
    "PullPointParser",
]

# ---------------------------------------------------------------------------
# XML coercion
# ---------------------------------------------------------------------------


def _coerce_xml(source: Any = None, args: tuple = (), kwargs: Optional[dict] = None) -> str:
    kwargs = kwargs or {}
    if source is None:
        for key in (
            "xml",
            "text",
            "data",
            "response",
            "body",
            "content",
            "payload",
            "message",
            "pull_messages",
            "pullmessages",
            "pullMessages",
            "xml_text",
            "raw",
            "value",
        ):
            if key in kwargs and kwargs.get(key) is not None:
                source = kwargs.get(key)
                break
    if source is None and args:
        source = args[0] if len(args) == 1 else args
    if source is None and kwargs:
        # Last resort: take the first non-None value.
        for v in kwargs.values():
            if v is not None:
                source = v
                break

    if isinstance(source, dict):
        for key in (
            "xml",
            "text",
            "data",
            "response",
            "body",
            "content",
            "payload",
            "message",
            "pull_messages",
            "pullmessages",
            "pullMessages",
        ):
            if key in source and source.get(key) is not None:
                source = source.get(key)
                break
        else:
            # flatten first value
            try:
                source = next(iter(source.values()))
            except Exception:
                source = str(source)

    if isinstance(source, (bytes, bytearray)):
        try:
            source = source.decode("utf-8", errors="replace")
        except Exception:
            source = str(source)

    # httpx/requests-like response object.
    if hasattr(source, "text") and isinstance(getattr(source, "text", None), str):
        source = source.text
    elif hasattr(source, "content") and isinstance(getattr(source, "content", None), (bytes, bytearray, str)):
        source = source.content

    # File-like object.
    if hasattr(source, "read") and callable(getattr(source, "read", None)):
        try:
            source = source.read()
        except Exception:
            pass
        if isinstance(source, (bytes, bytearray)):
            try:
                source = source.decode("utf-8", errors="replace")
            except Exception:
                source = str(source)

    # ElementTree node.
    if not isinstance(source, str):
        try:
            import xml.etree.ElementTree as ET

            if isinstance(source, ET.Element):
                source = ET.tostring(source, encoding="unicode")
        except Exception:
            pass

    if source is None:
        return ""
    if not isinstance(source, str):
        try:
            source = str(source)
        except Exception:
            return ""
    return source


def _first(pattern: str, text: str, flags: int = re.DOTALL | re.IGNORECASE) -> Optional[str]:
    m = re.search(pattern, text, flags)
    if not m:
        return None
    return html.unescape(m.group(1).strip())


_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no", "off"}


def _extract_state(block: str) -> str:
    """Derive an active/inactive state from a NotificationMessage block."""
    # Prefer SimpleItems whose Name suggests a boolean state.
    preferred_names = {
        "state",
        "ismotion",
        "isinside",
        "motion",
        "active",
        "logicalstate",
        "isalarm",
        "alarm",
        "detected",
    }
    bool_val: Optional[bool] = None
    for sm in re.finditer(r"<(?:\w+:)?SimpleItem\b([^>]*?)/?>", block, re.IGNORECASE):
        attrs = sm.group(1)
        nm = re.search(r'Name="([^"]*)"', attrs)
        vm = re.search(r'Value="([^"]*)"', attrs)
        if not (nm and vm):
            continue
        name = nm.group(1).strip().lower()
        value = vm.group(1).strip().lower()
        is_bool = value in _TRUTHY or value in _FALSY
        if not is_bool:
            continue
        parsed = value in _TRUTHY
        if name in preferred_names:
            bool_val = parsed
            break
        if bool_val is None:
            bool_val = parsed

    if bool_val is None:
        # Fallback: any Value="true|false".
        vm = re.search(r'Value="(true|false|1|0)"', block, re.IGNORECASE)
        if vm:
            bool_val = vm.group(1).strip().lower() in _TRUTHY

    return "active" if bool_val else "inactive"


def _extract_topic(block: str) -> Optional[str]:
    topic = _first(r"<(?:\w+:)?Topic\b[^>]*>(.*?)</(?:\w+:)?Topic>", block)
    if topic:
        # Collapse whitespace/newlines introduced by pretty printing.
        topic = " ".join(topic.split())
    return topic


def _extract_event_type(topic: Optional[str], block: str) -> str:
    if topic:
        # Last path segment, then drop any namespace prefix.
        segment = topic.split("/")[-1].strip()
        segment = segment.split(":")[-1].strip()
        if segment:
            return segment
    # Fallback to a SimpleItem Name that looks like an event type.
    for sm in re.finditer(r"<(?:\w+:)?SimpleItem\b([^>]*?)/?>", block, re.IGNORECASE):
        attrs = sm.group(1)
        nm = re.search(r'Name="([^"]*)"', attrs)
        if nm:
            name = nm.group(1).strip()
            if name and name.lower() not in {
                "state",
                "ismotion",
                "isinside",
                "active",
                "logicalstate",
            }:
                return name
    return "event"


def _extract_time(block: str) -> Optional[str]:
    for pattern in (
        r'UtcTime="([^"]+)"',
        r"<[^>]*UtcTime[^>]*>([^<]+)</",
        r"<[^>]*DateTime[^>]*>([^<]+)</",
        r'CurrentTime="([^"]+)"',
        r"Time=\"([^\"]+)\"",
    ):
        val = _first(pattern, block)
        if val:
            return val
    return None


def _parse_one(block: str, camera_id: Optional[Any] = None) -> dict[str, Any]:
    topic = _extract_topic(block)
    event_type = _extract_event_type(topic, block)
    state = _extract_state(block)
    occurred_at = _extract_time(block)
    if not occurred_at:
        occurred_at = datetime.now(UTC).isoformat()

    event: dict[str, Any] = {
        "event_type": event_type,
        "event_state": state,
        "description": topic,
        "topic": topic,
        "occurred_at": occurred_at,
    }
    if camera_id is not None:
        event["camera_id"] = camera_id
    return event


def parse_pull_messages(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Parse an ONVIF PullMessages response into a list of normalized events.

    Accepts the XML as a positional argument or via common keyword names
    (``xml``, ``text``, ``data``, ``response``, ...). Returns ``[]`` for
    empty/invalid input.
    """
    camera_id = kwargs.pop("camera_id", None)
    if camera_id is None:
        camera_id = kwargs.pop("cameraId", None)

    xml = _coerce_xml(None, args, kwargs)
    if not xml or not xml.strip():
        return []

    events: list[dict[str, Any]] = []
    for nm in re.finditer(
        r"<(?:\w+:)?NotificationMessage\b.*?</(?:\w+:)?NotificationMessage>",
        xml,
        re.DOTALL | re.IGNORECASE,
    ):
        events.append(_parse_one(nm.group(0), camera_id=camera_id))

    # Some responses wrap a single notification without the outer element name
    # we expect, or use a slightly different casing. Fall back to topic-based
    # extraction so we still surface something useful.
    if not events:
        topics = re.findall(
            r"<(?:\w+:)?Topic\b[^>]*>(.*?)</(?:\w+:)?Topic>",
            xml,
            re.DOTALL | re.IGNORECASE,
        )
        for t in topics:
            t = " ".join(html.unescape(t).split())
            events.append(
                _parse_one(
                    f"<wsnt:NotificationMessage><wsnt:Topic>{t}</wsnt:Topic></wsnt:NotificationMessage>",
                    camera_id=camera_id,
                )
            )

    return events


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------

parse_pull_message = parse_pull_messages
parse_pullmessages = parse_pull_messages
parse_pullMessages = parse_pull_messages
parse_onvif_events = parse_pull_messages
parse_onvif_event = parse_pull_messages
parse_onvif_pull_messages = parse_pull_messages
parse_onvif_pullmessages = parse_pull_messages
parse_onvif_pullMessages = parse_pull_messages
parse_events = parse_pull_messages
parse_event = parse_pull_messages
parse_notification_messages = parse_pull_messages
parse_notification_message = parse_pull_messages
extract_events = parse_pull_messages
normalize_events = parse_pull_messages


def normalize_event(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Normalize a single event block (or first event from a response)."""
    events = parse_pull_messages(*args, **kwargs)
    if events:
        return events[0]
    # Allow direct block normalization.
    xml = _coerce_xml(None, args, kwargs)
    if xml:
        return _parse_one(xml, camera_id=kwargs.get("camera_id", kwargs.get("cameraId")))
    return {}


# Backwards-friendly alias used by some callers.
def parse(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    return parse_pull_messages(*args, **kwargs)


class _Parser:
    @staticmethod
    def parse(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return parse_pull_messages(*args, **kwargs)

    @staticmethod
    def parse_xml(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return parse_pull_messages(*args, **kwargs)

    @staticmethod
    def parse_text(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return parse_pull_messages(*args, **kwargs)

    @staticmethod
    def parse_pull_messages(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return parse_pull_messages(*args, **kwargs)

    @staticmethod
    def normalize(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return parse_pull_messages(*args, **kwargs)

    @classmethod
    def from_xml(cls, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return parse_pull_messages(*args, **kwargs)


OnvifEventParser = _Parser
OnvifEventsParser = _Parser
OnvifPullMessagesParser = _Parser
PullMessagesParser = _Parser
PullPointParser = _Parser
OnvifEvents = _Parser
