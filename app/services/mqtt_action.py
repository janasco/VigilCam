"""MQTT publish action with Home Assistant discovery.

Gap (verified): ``grep -ri mqtt app/services app/api`` returns nothing —
no MQTT publishing exists. The ``webhook``/``image_webhook``/``webhook_queue``
actions only speak HTTP. This module fills that gap with a dependency-free
MQTT 3.1.1 publisher (raw ``socket`` + optional ``ssl``) so no
``paho-mqtt`` install is required, plus Home Assistant MQTT-discovery
helpers (``homeassistant/<component>/<node>/<object>/config`` with
``retain=True``).

Wire reference: MQTT-3.1.1 CONNECT / CONNACK / PUBLISH (QoS 0/1) /
DISCONNECT. Payloads are UTF-8 JSON unless the caller passes raw bytes.
Discovery follows the HA convention: each config payload carries
``unique_id``, ``state_topic``, ``device`` and is published retained.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import ssl
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_PORT = 1883
DEFAULT_TLS_PORT = 8883
DEFAULT_KEEPALIVE = 60
DEFAULT_QOS = 0
DEFAULT_TIMEOUT = 10.0
DEFAULT_BASE_TOPIC = "vigilcam"
DEFAULT_DISCOVERY_PREFIX = "homeassistant"
MQTT_PROTOCOL_LEVEL = 4  # MQTT 3.1.1

TOPIC_RE = re.compile(r"^[A-Za-z0-9_\-/+#]{1,512}$")

HOST_KEYS = (
    "host", "broker", "hostname", "mqtt_host", "broker_host",
    "server", "mqtt_server", "mqtt_broker", "address", "ip",
)
PORT_KEYS = ("port", "mqtt_port", "broker_port")
USERNAME_KEYS = ("username", "user", "mqtt_username", "mqtt_user", "login")
PASSWORD_KEYS = ("password", "pass_", "passwd", "mqtt_password", "secret")
TOPIC_KEYS = ("topic", "mqtt_topic", "target_topic", "destination", "channel")
PAYLOAD_KEYS = (
    "payload", "message", "data", "body", "content", "event",
    "event_data", "value", "state",
)
QOS_KEYS = ("qos", "quality_of_service", "qos_level")
RETAIN_KEYS = ("retain", "retained", "retain_flag")
CLIENT_ID_KEYS = ("client_id", "clientId", "client", "id_suffix")
TIMEOUT_KEYS = ("timeout", "timeout_seconds", "request_timeout", "deadline")
KEEPALIVE_KEYS = ("keepalive", "keep_alive", "keepAlive")
TLS_KEYS = ("use_tls", "tls", "ssl", "secure", "mqtts")
BASE_TOPIC_KEYS = ("base_topic", "base", "root_topic", "prefix_topic")
DISCOVERY_PREFIX_KEYS = ("discovery_prefix", "ha_prefix", "ha_discovery_prefix")
CAMERA_KEYS = ("camera_id", "camera", "camera_name", "source", "monitor")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _normalize_args(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (bytes, bytearray)):
            data.setdefault("payload", bytes(a))
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if "#" in s or ("/" in s and " " not in s and not s.lower().startswith(("http://", "https://"))):
                data.setdefault("topic", s)
            elif s.lower().startswith(("mqtt://", "mqtts://", "tcp://")):
                data.setdefault("host", s)
            elif data.get("topic") is None and data.get("host") is not None:
                data.setdefault("payload", s)
            elif data.get("host") is None and "." in s and " " not in s and len(s) < 256:
                data.setdefault("host", s)
            else:
                data.setdefault("payload", s)
        else:
            try:
                attrs = {
                    k: getattr(a, k)
                    for k in dir(a)
                    if not k.startswith("_") and not callable(getattr(a, k, None))
                }
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _broker_defaults_from_env() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for env_key, cfg_key in (
        ("MQTT_HOST", "host"), ("MQTT_BROKER", "host"),
        ("MQTT_PORT", "port"), ("MQTT_USERNAME", "username"),
        ("MQTT_USER", "username"), ("MQTT_PASSWORD", "password"),
        ("MQTT_BASE_TOPIC", "base_topic"),
        ("MQTT_DISCOVERY_PREFIX", "discovery_prefix"),
    ):
        val = os.environ.get(env_key)
        if val:
            out[cfg_key] = val
    return out


def slugify(value: Any) -> str:
    s = str(value or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "camera"


def build_topic(camera_id: Any = None, suffix: Any = None, base_topic: str = DEFAULT_BASE_TOPIC) -> str:
    base = str(base_topic or DEFAULT_BASE_TOPIC).strip().strip("/") or DEFAULT_BASE_TOPIC
    parts = [base]
    if camera_id is not None and str(camera_id).strip():
        parts.append(slugify(camera_id))
    if suffix is not None and str(suffix).strip():
        parts.append(str(suffix).strip().strip("/"))
    return "/".join(parts)


def build_state_topic(camera_id: Any, base_topic: str = DEFAULT_BASE_TOPIC) -> str:
    return build_topic(camera_id, "state", base_topic)


def build_event_topic(camera_id: Any, base_topic: str = DEFAULT_BASE_TOPIC) -> str:
    return build_topic(camera_id, "event", base_topic)


def ha_discovery_topic(
    component: str = "binary_sensor",
    node_id: str = "vigilcam",
    object_id: str = "motion",
    discovery_prefix: str = DEFAULT_DISCOVERY_PREFIX,
) -> str:
    prefix = str(discovery_prefix or DEFAULT_DISCOVERY_PREFIX).strip().strip("/")
    return f"{prefix}/{component}/{slugify(node_id)}/{slugify(object_id)}/config"


def build_ha_discovery_config(
    camera_id: Any = "camera",
    camera_name: Any = None,
    base_topic: str = DEFAULT_BASE_TOPIC,
    discovery_prefix: str = DEFAULT_DISCOVERY_PREFIX,
    device_model: str = "Camera",
) -> Dict[str, Dict[str, Any]]:
    """Return ``{discovery_topic: config_payload}`` for a camera.

    Two entities: a ``binary_sensor`` motion detector and a ``sensor``
    carrying the last event JSON. Both use retained discovery configs.
    """
    cam = str(camera_id or "camera")
    node = slugify(cam)
    cname = str(camera_name or cam)
    base = str(base_topic or DEFAULT_BASE_TOPIC).strip().strip("/") or DEFAULT_BASE_TOPIC
    state_topic = f"{base}/{node}/state"
    event_topic = f"{base}/{node}/event"
    availability_topic = f"{base}/{node}/availability"
    device = {
        "identifiers": [f"vigilcam_{node}"],
        "name": cname,
        "manufacturer": "VigilCam",
        "model": str(device_model or "Camera"),
    }
    motion_topic = ha_discovery_topic("binary_sensor", f"vigilcam_{node}", "motion", discovery_prefix)
    event_sensor_topic = ha_discovery_topic("sensor", f"vigilcam_{node}", "last_event", discovery_prefix)
    motion_config = {
        "name": f"{cname} Motion",
        "unique_id": f"vigilcam_{node}_motion",
        "object_id": f"vigilcam_{node}_motion",
        "device_class": "motion",
        "state_topic": state_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "availability_topic": availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device,
    }
    event_config = {
        "name": f"{cname} Last Event",
        "unique_id": f"vigilcam_{node}_last_event",
        "object_id": f"vigilcam_{node}_last_event",
        "state_topic": event_topic,
        "availability_topic": availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "value_template": "{{ value_json.kind | default(value) }}",
        "json_attributes_topic": event_topic,
        "device": device,
    }
    return {motion_topic: motion_config, event_sensor_topic: event_config}


def build_ha_discovery(
    camera_id: Any = "camera",
    camera_name: Any = None,
    base_topic: str = DEFAULT_BASE_TOPIC,
    discovery_prefix: str = DEFAULT_DISCOVERY_PREFIX,
) -> Dict[str, Dict[str, Any]]:
    return build_ha_discovery_config(camera_id, camera_name, base_topic, discovery_prefix)


def _coerce_payload(payload: Any) -> Tuple[bytes, bool]:
    """Return (wire_bytes, was_json)."""
    if payload is None:
        return b"", False
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload), False
    if isinstance(payload, str):
        return payload.encode("utf-8"), False
    try:
        return json.dumps(payload, default=str).encode("utf-8"), True
    except Exception:  # noqa: BLE001
        return str(payload).encode("utf-8"), False


def _encode_remaining_length(length: int) -> bytes:
    out = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length > 0:
            digit |= 0x80
        out.append(digit)
        if length == 0:
            break
    return bytes(out)


def _encode_str(s: str) -> bytes:
    raw = s.encode("utf-8")
    return len(raw).to_bytes(2, "big") + raw


def _build_connect(
    client_id: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    keepalive: int = DEFAULT_KEEPALIVE,
) -> bytes:
    flags = 0x02  # clean session
    payload = _encode_str(client_id)
    if username is not None:
        flags |= 0x80
        payload += _encode_str(str(username))
    if password is not None:
        flags |= 0x40
        payload += _encode_str(str(password))
    var_header = _encode_str("MQTT") + bytes([MQTT_PROTOCOL_LEVEL, flags]) + int(keepalive).to_bytes(2, "big")
    body = var_header + payload
    return bytes([0x10]) + _encode_remaining_length(len(body)) + body


def _build_publish(
    topic: str,
    payload: bytes,
    qos: int = 0,
    retain: bool = False,
    packet_id: int = 1,
) -> bytes:
    header = 0x30 | ((int(qos) & 0x03) << 1) | (0x01 if retain else 0x00)
    body = _encode_str(topic)
    if int(qos) > 0:
        body += int(packet_id).to_bytes(2, "big")
    body += bytes(payload or b"")
    return bytes([header]) + _encode_remaining_length(len(body)) + body


def _build_disconnect() -> bytes:
    return bytes([0xE0, 0x00])


def _recv_exact(sock: Any, n: int, timeout: float) -> bytes:
    sock.settimeout(timeout)
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _mqtt_publish_once(
    host: str,
    port: int,
    topic: str,
    payload_bytes: bytes,
    username: Optional[str] = None,
    password: Optional[str] = None,
    client_id: Optional[str] = None,
    qos: int = 0,
    retain: bool = False,
    keepalive: int = DEFAULT_KEEPALIVE,
    use_tls: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    cid = str(client_id or f"vigilcam-{uuid.uuid4().hex[:8]}")
    raw_sock = socket.create_connection((str(host), int(port)), timeout=timeout)
    sock = raw_sock
    try:
        if use_tls:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(raw_sock, server_hostname=str(host))
        sock.settimeout(timeout)
        sock.sendall(_build_connect(cid, username, password, keepalive))
        header = _recv_exact(sock, 2, timeout)
        if len(header) < 2 or header[0] != 0x20:
            raise ConnectionError("invalid CONNACK from broker")
        # Remaining length is header[1]; then 2 bytes flags+code.
        body = _recv_exact(sock, int(header[1]), timeout)
        if len(body) < 2 or body[1] != 0x00:
            code = body[1] if len(body) >= 2 else -1
            raise ConnectionError(f"broker refused connection (code {code})")
        sock.sendall(_build_publish(topic, payload_bytes, qos=qos, retain=retain))
        if int(qos) == 1:
            ack = _recv_exact(sock, 4, timeout)
            if len(ack) < 4 or (ack[0] & 0xF0) != 0x40:
                raise ConnectionError("missing PUBACK from broker")
        try:
            sock.sendall(_build_disconnect())
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass
    return {"client_id": cid, "bytes": len(payload_bytes or b"")}


def _validate_topic(topic: Any) -> Tuple[Optional[str], Optional[str]]:
    if topic is None:
        return None, "topic is required"
    t = str(topic).strip()
    if not t:
        return None, "topic is required"
    if len(t) > 512:
        return None, "topic exceeds 512 characters"
    if not TOPIC_RE.match(t):
        return None, "topic contains invalid characters"
    return t, None


def publish_mqtt(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Publish one MQTT message (QoS 0/1, optional retain/TLS).

    Tolerant inputs: ``host``/``broker``, ``topic``, ``payload``/``message``,
    ``username``/``password``, ``port``, ``qos``, ``retain``, ``client_id``,
    ``keepalive``, ``use_tls``, ``timeout``. Returns a result dict with
    ``ok``/``status`` — never raises for broker failures (transport errors
    become ``ok=False`` with ``detail``), but raises nothing except for
    programming misuse; validation failures are returned, not raised, so
    alert-action pipelines can record them.
    """
    data = _normalize_args(args, kwargs)
    env_defaults = _broker_defaults_from_env()
    for k, v in env_defaults.items():
        data.setdefault(k, v)

    host = _first(data, HOST_KEYS)
    # Allow mqtt(s)://host:port URLs.
    port_hint = _first(data, PORT_KEYS)
    use_tls_hint = _first(data, TLS_KEYS)
    if isinstance(host, str) and host.strip().lower().startswith(("mqtt://", "mqtts://", "tcp://")):
        url = host.strip()
        secure = url.lower().startswith("mqtts://")
        bare = re.sub(r"^[a-z]+://", "", url, flags=re.IGNORECASE)
        # strip credentials/path if embedded
        if "@" in bare:
            creds, bare = bare.rsplit("@", 1)
            if ":" in creds:
                u, p = creds.split(":", 1)
                data.setdefault("username", u)
                data.setdefault("password", p)
        bare = bare.split("/", 1)[0]
        if ":" in bare:
            h, prt = bare.rsplit(":", 1)
            host = h
            try:
                port_hint = int(prt)
            except ValueError:
                pass
        else:
            host = bare
        if secure and use_tls_hint is None:
            use_tls_hint = True

    topic = _first(data, TOPIC_KEYS)
    # Convenience: camera_id + suffix builds the topic when no explicit topic.
    camera = _first(data, CAMERA_KEYS)
    base_topic = _first(data, BASE_TOPIC_KEYS) or DEFAULT_BASE_TOPIC
    if topic is None and camera is not None:
        suffix = data.get("suffix") or data.get("event_kind") or data.get("kind") or "state"
        topic = build_topic(camera, suffix, str(base_topic))

    payload = _first(data, PAYLOAD_KEYS)
    if payload is None:
        # Anything left over that is not connection config becomes the payload.
        control = set([k.lower() for k in list(HOST_KEYS) + list(PORT_KEYS) + list(USERNAME_KEYS) + list(PASSWORD_KEYS) + list(TOPIC_KEYS) + list(QOS_KEYS) + list(RETAIN_KEYS) + list(CLIENT_ID_KEYS) + list(TIMEOUT_KEYS) + list(KEEPALIVE_KEYS) + list(TLS_KEYS) + list(BASE_TOPIC_KEYS) + list(DISCOVERY_PREFIX_KEYS) + list(CAMERA_KEYS) + ["suffix", "event_kind", "kind", "event_id"]])
        extra = {k: v for k, v in data.items() if k.lower() not in control}
        payload = extra if extra else ""

    qos_raw = _first(data, QOS_KEYS)
    try:
        qos = int(qos_raw) if qos_raw is not None else DEFAULT_QOS
    except (TypeError, ValueError):
        return {"ok": False, "success": False, "status": "error", "detail": f"invalid qos {qos_raw!r}", "action": "mqtt_publish", "type": "mqtt"}
    if qos not in (0, 1):
        return {"ok": False, "success": False, "status": "error", "detail": f"unsupported qos {qos!r}: expected 0 or 1", "action": "mqtt_publish", "type": "mqtt"}

    retain_raw = _first(data, RETAIN_KEYS)
    retain = bool(retain_raw) if isinstance(retain_raw, bool) else str(retain_raw).lower() in ("1", "true", "yes", "on", "retain", "retained") if retain_raw is not None else False

    username = _first(data, USERNAME_KEYS)
    password = _first(data, PASSWORD_KEYS)
    client_id = _first(data, CLIENT_ID_KEYS)
    try:
        port = int(port_hint) if port_hint is not None else DEFAULT_PORT
    except (TypeError, ValueError):
        return {"ok": False, "success": False, "status": "error", "detail": f"invalid port {port_hint!r}", "action": "mqtt_publish", "type": "mqtt"}
    try:
        keepalive = int(_first(data, KEEPALIVE_KEYS) or DEFAULT_KEEPALIVE)
    except (TypeError, ValueError):
        keepalive = DEFAULT_KEEPALIVE
    try:
        timeout = float(_first(data, TIMEOUT_KEYS) or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    use_tls: bool
    if use_tls_hint is None:
        use_tls = int(port) == DEFAULT_TLS_PORT
    elif isinstance(use_tls_hint, bool):
        use_tls = use_tls_hint
    else:
        use_tls = str(use_tls_hint).lower() in ("1", "true", "yes", "on", "tls", "ssl", "mqtts")

    if not host or not str(host).strip():
        return {"ok": False, "success": False, "status": "error", "detail": "mqtt host/broker is required", "action": "mqtt_publish", "type": "mqtt"}
    topic_str, topic_err = _validate_topic(topic)
    if topic_err:
        return {"ok": False, "success": False, "status": "error", "detail": topic_err, "action": "mqtt_publish", "type": "mqtt", "topic": topic}

    wire, was_json = _coerce_payload(payload)
    start = time.monotonic()
    try:
        info = _mqtt_publish_once(
            str(host).strip(), port, topic_str or "", wire,
            username=str(username) if username is not None else None,
            password=str(password) if password is not None else None,
            client_id=str(client_id) if client_id is not None else None,
            qos=qos, retain=retain, keepalive=keepalive,
            use_tls=use_tls, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - transport errors become result dicts
        latency = (time.monotonic() - start) * 1000.0
        return {
            "ok": False, "success": False, "status": "error",
            "detail": str(exc) or "mqtt publish failed",
            "action": "mqtt_publish", "type": "mqtt",
            "host": str(host).strip(), "port": port,
            "topic": topic_str, "qos": qos, "retain": retain,
            "payload_bytes": len(wire), "latency_ms": round(latency, 1),
            "request_id": uuid.uuid4().hex[:12], "sent_at": _now_iso(),
        }
    latency = (time.monotonic() - start) * 1000.0
    return {
        "ok": True, "success": True, "status": "success",
        "detail": f"published to {topic_str}",
        "action": "mqtt_publish", "type": "mqtt",
        "host": str(host).strip(), "port": port,
        "topic": topic_str, "qos": qos, "retain": retain,
        "payload_bytes": len(wire), "payload_json": was_json,
        "client_id": info.get("client_id"),
        "latency_ms": round(latency, 1),
        "request_id": uuid.uuid4().hex[:12], "sent_at": _now_iso(),
    }


async def publish_mqtt_async(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Async variant — same contract, runs the blocking socket in a thread."""
    return await asyncio.to_thread(publish_mqtt, *args, **kwargs)


def publish_event(
    camera_id: Any = None,
    event_kind: Any = "motion",
    state: Any = "ON",
    extra: Optional[Dict[str, Any]] = None,
    base_topic: str = DEFAULT_BASE_TOPIC,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Publish a camera event to ``<base>/<camera>/event`` + state topic.

    Also accepts a single dict positional (``publish_event({...})``).
    """
    data: Dict[str, Any] = {}
    if isinstance(camera_id, dict) and event_kind == "motion":
        data.update(camera_id)
        camera_id = data.pop("camera_id", data.pop("camera", None))
        event_kind = data.pop("event_kind", data.pop("kind", "motion"))
        state = data.pop("state", state)
        if isinstance(data.pop("extra", None), dict):
            extra = {**(data.pop("extra", {}) or {}), **(extra or {})}
        kwargs = {**data, **kwargs}
    cam = str(camera_id or kwargs.get("camera_id") or kwargs.get("camera") or "camera")
    kind = str(event_kind or kwargs.get("event_kind") or kwargs.get("kind") or "motion")
    base = str(kwargs.pop("base_topic", base_topic) or DEFAULT_BASE_TOPIC)
    payload_obj = {
        "camera_id": cam, "kind": kind, "state": state,
        "event_id": kwargs.get("event_id"), "sent_at": _now_iso(),
    }
    if isinstance(extra, dict):
        payload_obj.update(extra)
    for k in ("label", "score", "confidence", "snapshot"):
        if k in kwargs and k not in payload_obj:
            payload_obj[k] = kwargs[k]
    topic = kwargs.pop("topic", None) or build_event_topic(cam, base)
    merged = {**kwargs, "topic": topic, "payload": payload_obj}
    # state mirror for binary_sensor entities
    state_topic = build_state_topic(cam, base)
    state_payload = "ON" if str(state).upper() in ("ON", "1", "TRUE", "MOTION", "ACTIVE") else ("OFF" if str(state).upper() in ("OFF", "0", "FALSE", "CLEAR", "INACTIVE") else str(state))
    result = publish_mqtt(**merged)
    if result.get("ok"):
        mirror_kwargs = dict(merged)
        mirror_kwargs["topic"] = state_topic
        mirror_kwargs["payload"] = state_payload
        # QoS/retain of the state mirror follow the event call.
        mirror = publish_mqtt(**mirror_kwargs)
        result["state_topic"] = state_topic
        result["state_result"] = {"ok": mirror.get("ok"), "detail": mirror.get("detail")}
        result["event_topic"] = topic
    return result


async def publish_event_async(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return await asyncio.to_thread(publish_event, *args, **kwargs)


def publish_ha_discovery(
    camera_id: Any = "camera",
    camera_name: Any = None,
    base_topic: str = DEFAULT_BASE_TOPIC,
    discovery_prefix: str = DEFAULT_DISCOVERY_PREFIX,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Publish retained HA discovery configs for a camera.

    Returns ``{ok, published: [{topic, ok}], ...}``. Each config is sent
    with ``retain=True`` per HA discovery convention.
    """
    if isinstance(camera_id, dict):
        data = dict(camera_id)
        data.update(kwargs)
        camera_id = data.pop("camera_id", data.pop("camera", "camera"))
        camera_name = data.pop("camera_name", data.pop("name", camera_name))
        base_topic = data.pop("base_topic", base_topic)
        discovery_prefix = data.pop("discovery_prefix", discovery_prefix)
        kwargs = data
    base = str(kwargs.pop("base_topic", base_topic) or DEFAULT_BASE_TOPIC)
    prefix = str(kwargs.pop("discovery_prefix", discovery_prefix) or DEFAULT_DISCOVERY_PREFIX)
    configs = build_ha_discovery_config(camera_id, camera_name, base, prefix)
    published: List[Dict[str, Any]] = []
    ok_all = True
    for disc_topic, config in configs.items():
        merged = {**kwargs, "topic": disc_topic, "payload": config, "retain": True}
        # Discovery configs conventionally use QoS 0 retained.
        merged.setdefault("qos", 0)
        res = publish_mqtt(**merged)
        published.append({"topic": disc_topic, "ok": bool(res.get("ok")), "detail": res.get("detail")})
        if not res.get("ok"):
            ok_all = False
    return {
        "ok": ok_all, "success": ok_all,
        "status": "success" if ok_all else "error",
        "detail": f"published {sum(1 for p in published if p['ok'])}/{len(published)} discovery configs",
        "action": "mqtt_ha_discovery", "type": "mqtt",
        "camera_id": str(camera_id), "base_topic": base,
        "discovery_prefix": prefix,
        "published": published, "configs": configs,
        "request_id": uuid.uuid4().hex[:12], "sent_at": _now_iso(),
    }


async def publish_ha_discovery_async(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return await asyncio.to_thread(publish_ha_discovery, *args, **kwargs)


class MqttActionService:
    """Object-oriented facade over the MQTT action helpers."""

    def publish(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return publish_mqtt(*args, **kwargs)

    def send(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return publish_mqtt(*args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return publish_mqtt(*args, **kwargs)

    def trigger(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return publish_mqtt(*args, **kwargs)

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return publish_mqtt(*args, **kwargs)

    def publish_event(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return publish_event(*args, **kwargs)

    def publish_discovery(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return publish_ha_discovery(*args, **kwargs)

    def publish_ha_discovery(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return publish_ha_discovery(*args, **kwargs)

    def discovery_configs(self, *args: Any, **kwargs: Any) -> Dict[str, Dict[str, Any]]:
        cam = kwargs.pop("camera_id", args[0] if args else "camera")
        name = kwargs.pop("camera_name", args[1] if len(args) > 1 else None)
        return build_ha_discovery_config(cam, name, **kwargs)

    async def apublish(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await publish_mqtt_async(*args, **kwargs)

    async def aexecute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await publish_mqtt_async(*args, **kwargs)


# Compatibility aliases (repo convention: many names, same behaviour).
mqtt_publish = publish_mqtt
send_mqtt = publish_mqtt
send = publish_mqtt
post = publish_mqtt
trigger = publish_mqtt
execute = publish_mqtt
run = publish_mqtt
publish = publish_mqtt
deliver_mqtt = publish_mqtt
mqtt_send = publish_mqtt
mqtt_execute = publish_mqtt
preview_ha_discovery = build_ha_discovery_config
ha_discovery_configs = build_ha_discovery_config
discovery_configs = build_ha_discovery_config

mqtt_action_service = MqttActionService()
service = mqtt_action_service
manager = mqtt_action_service

MqttAction = MqttActionService
MqttService = MqttActionService
MqttActionManager = MqttActionService

__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_BASE_TOPIC",
    "DEFAULT_DISCOVERY_PREFIX",
    "build_topic",
    "build_state_topic",
    "build_event_topic",
    "ha_discovery_topic",
    "build_ha_discovery_config",
    "build_ha_discovery",
    "publish_mqtt",
    "publish_mqtt_async",
    "publish_event",
    "publish_event_async",
    "publish_ha_discovery",
    "publish_ha_discovery_async",
    "MqttActionService",
    "mqtt_action_service",
]
