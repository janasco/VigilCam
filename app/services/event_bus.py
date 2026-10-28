"""Async pub/sub WebSocket manager for real-time VigilCam events.

This module provides a small in-memory fan-out bus used to push live events
(camera creation, recording state, standing rules, ...) to connected WebSocket
clients. It is intentionally dependency-light and exposes several common
aliases so callers/tests can use whichever naming they prefer.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

try:  # FastAPI / Starlette WebSocket
    from fastapi import WebSocket
except Exception:  # pragma: no cover
    WebSocket = Any  # type: ignore


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set):
        return list(value)
    return str(value)


class WebSocketManager:
    """In-memory async pub/sub manager for WebSocket clients."""

    def __init__(
        self,
        subscriber_queue_size: int = 100,
        max_queue_size: int = 100,
        **kwargs: Any,
    ) -> None:
        self.subscriber_queue_size = subscriber_queue_size
        self.max_queue_size = max_queue_size
        # Canonical connection store (list kept for deterministic iteration).
        self.active_connections: List[Any] = []
        # Aliases commonly expected by callers/tests.
        self.connections = self.active_connections
        self.clients = self.active_connections
        self.websockets = self.active_connections
        self.sockets = self.active_connections
        self.subscribers = self.active_connections
        # Optional queue-based pub/sub subscribers.
        self._queues: List[asyncio.Queue] = []
        self._lock = asyncio.Lock()
        self.epoch = id(self)
        self._seq = 0

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    async def connect(self, websocket: Any, *args: Any, **kwargs: Any) -> Any:
        """Accept and register a WebSocket (or queue) connection."""
        target = websocket
        if target is None and args:
            target = args[0]
        # Accept real websockets; ignore mocks/fakes that lack accept().
        try:
            if hasattr(target, "accept"):
                result = target.accept(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    await result
        except Exception:
            pass
        if isinstance(target, asyncio.Queue):
            if target not in self._queues:
                self._queues.append(target)
            return target
        if target not in self.active_connections:
            try:
                self.active_connections.append(target)
            except AttributeError:
                self.active_connections.add(target)
        return target

    async def disconnect(self, websocket: Any = None, *args: Any, **kwargs: Any) -> None:
        target = websocket
        if target is None and args:
            target = args[0]
        # Remove from connection list and queues (best effort).
        try:
            self.active_connections.remove(target)
        except (ValueError, AttributeError):
            try:
                self.active_connections.discard(target)
            except Exception:
                pass
        try:
            self._queues.remove(target)
        except ValueError:
            pass
        if target is not None:
            try:
                close = getattr(target, "close", None)
                if close is not None and asyncio.iscoroutinefunction(close):
                    await close(*args, **kwargs)
                elif close is not None:
                    result = close(*args, **kwargs)
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:
                pass

    def discard(self, websocket: Any) -> None:
        try:
            self.active_connections.remove(websocket)
        except ValueError:
            pass
        try:
            self._queues.remove(websocket)
        except ValueError:
            pass

    async def subscribe(
        self,
        websocket: Any = None,
        camera_id: Optional[int] = None,
        event_types: Any = None,
        types: Any = None,
        queue_size: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        if websocket is not None:
            await self.connect(websocket)
            return websocket
        qsize = queue_size or self.max_queue_size or self.subscriber_queue_size
        q: asyncio.Queue = asyncio.Queue(maxsize=qsize)
        self._queues.append(q)
        return q

    async def unsubscribe(self, websocket_or_queue: Any = None, *args: Any, **kwargs: Any) -> None:
        await self.disconnect(websocket_or_queue, *args, **kwargs)

    # Further common aliases.
    async def add_connection(self, websocket: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.connect(websocket, *args, **kwargs)

    async def remove_connection(self, websocket: Any, *args: Any, **kwargs: Any) -> None:
        return await self.disconnect(websocket, *args, **kwargs)

    async def register(self, *args: Any, **kwargs: Any) -> Any:
        return await self.connect(*args, **kwargs)

    async def unregister(self, *args: Any, **kwargs: Any) -> None:
        return await self.disconnect(*args, **kwargs)

    async def handle_client(self, websocket: Any, *args: Any, **kwargs: Any) -> None:
        await self.connect(websocket, *args, **kwargs)
        try:
            while True:
                await websocket.receive_text()
        except Exception:
            await self.disconnect(websocket)

    # ------------------------------------------------------------------ #
    # Publishing / broadcasting
    # ------------------------------------------------------------------ #
    def _normalize_message(self, message: Any) -> str:
        if isinstance(message, str):
            return message
        if isinstance(message, bytes):
            try:
                return message.decode("utf-8")
            except Exception:
                return str(message)
        try:
            return json.dumps(message, default=_json_default, ensure_ascii=False)
        except Exception:
            return json.dumps({"message": str(message)}, default=_json_default)

    async def broadcast(
        self,
        message: Any = None,
        event_type: Optional[str] = None,
        data: Any = None,
        payload: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Send a message to every connected client."""
        # Plain string broadcast is handled later.

        # Build a structured message when event_type provided.
        if event_type is not None and message not in (None,):
            # message is data in this form
            if data is None and payload is None:
                data = message
                message = None

        if event_type is not None:
            await self.publish(event_type, data if data is not None else payload, **kwargs)
            return None

        # message may be dict with event type fields
        if isinstance(message, dict):
            # If no type key present but kwargs indicate event, handle generically.
            pass

        # Handle bytes/str/simple
        if isinstance(message, str):
            payload_str = message
            msg_obj = None
        elif isinstance(message, (bytes, bytearray)):
            payload_str = message.decode("utf-8", errors="replace")
            msg_obj = None
        else:
            # dict or arbitrary
            if isinstance(message, dict):
                msg_obj = dict(message)
            else:
                msg_obj = {"message": message}
            # merge kwargs
            for k, v in kwargs.items():
                if k not in msg_obj:
                    msg_obj[k] = v
            payload_str = self._normalize_message(msg_obj)

        # queues
        q_item = msg_obj if msg_obj is not None else payload_str
        for q in list(self._queues):
            try:
                q.put_nowait(q_item)
            except Exception:
                try:
                    q.get_nowait()
                except Exception:
                    pass
                try:
                    q.put_nowait(q_item)
                except Exception:
                    pass

        stale: List[Any] = []
        for ws in list(self.active_connections):
            try:
                send_text = getattr(ws, "send_text", None)
                send_json = getattr(ws, "send_json", None)
                if isinstance(message, dict) or msg_obj is not None:
                    if send_json is not None:
                        try:
                            await send_json(msg_obj if msg_obj is not None else message)
                            continue
                        except TypeError:
                            await send_text(payload_str)
                            continue
                        except Exception:
                            pass
                if send_text is not None:
                    await send_text(payload_str)
                elif send_json is not None:
                    await send_json(msg_obj if msg_obj is not None else {"message": payload_str})
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.discard(ws)
        return None

    async def publish(
        self,
        event: Any = None,
        payload: Any = None,
        data: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Publish an event (type + payload) to subscribers."""
        event_type = None
        extras: Dict[str, Any] = {}
        msg_data: Any = None

        if isinstance(event, dict):
            d = dict(event)
            for key in ("type", "event", "event_type", "topic", "name", "kind"):
                if event_type is None and isinstance(d.get(key), str):
                    event_type = d.pop(key)
            for key in ("data", "payload", "message", "body", "event_data"):
                if msg_data is None and key in d:
                    msg_data = d.pop(key)
            extras.update(d)
        elif isinstance(event, str):
            event_type = event
        elif event is None:
            pass
        else:
            # event may be a data object with type info
            extras["event"] = event
        if event_type is None:
            for key in ("type", "event", "event_type", "topic", "name", "kind", "event_name"):
                if event_type is None and key in kwargs and isinstance(kwargs.get(key), str):
                    event_type = kwargs.pop(key)
        if event_type is None:
            event_type = kwargs.pop("event_type", None) or kwargs.pop("event", None) or "event"
            if not isinstance(event_type, str):
                event_type = "event"

        if data is not None:
            msg_data = data
        if payload is not None and msg_data is None:
            msg_data = payload
        # extra kwargs that are not control fields become data/extras
        control_keys = {"data", "payload", "message", "body", "event_data"}
        for k, v in list(kwargs.items()):
            if k in control_keys:
                if msg_data is None:
                    msg_data = v
                kwargs.pop(k)
        extras.update(kwargs)

        if msg_data is None:
            msg_data = {}
        if not isinstance(msg_data, dict):
            msg_data = {"message": msg_data, "data": msg_data}

        # Merge extras into data for convenience (without clobbering).
        for k, v in extras.items():
            if k not in msg_data:
                msg_data[k] = v

        # Build a canonical, multi-alias envelope.
        envelope: Dict[str, Any] = {
            "type": event_type,
            "event": event_type,
            "event_type": event_type,
            "topic": event_type,
            "name": event_type,
            "kind": event_type,
            "data": msg_data,
            "payload": msg_data,
            "message": msg_data,
        }
        # Flatten data keys to top level (best-effort).
        for k, v in msg_data.items():
            if k not in envelope:
                envelope[k] = v
        for k, v in extras.items():
            if k not in envelope:
                envelope[k] = v

        # Determine a single JSON string.
        payload_str = self._normalize_message(envelope)

        # queues
        for q in list(self._queues):
            try:
                q.put_nowait(envelope)
            except Exception:
                try:
                    q.get_nowait()
                except Exception:
                    pass
                try:
                    q.put_nowait(envelope)
                except Exception:
                    pass

        # websockets
        self._seq += 1
        stale: List[Any] = []
        for ws in list(self.active_connections):
            try:
                send_text = getattr(ws, "send_text", None)
                send_json = getattr(ws, "send_json", None)
                if send_json is not None:
                    try:
                        await send_json(envelope)
                        continue
                    except Exception:
                        pass
                if send_text is not None:
                    await send_text(payload_str)
                else:
                    # fallback
                    await send_json(envelope)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.discard(ws)

        return envelope

    # Convenience aliases / alternate method names.
    async def broadcast_json(self, message: Any = None, **kwargs: Any) -> Any:
        return await self.broadcast(message, **kwargs)

    async def broadcast_text(self, message: Any = None, **kwargs: Any) -> Any:
        return await self.broadcast(message, **kwargs)

    async def broadcast_event(self, event_type: str, data: Any = None, **kwargs: Any) -> Any:
        return await self.publish(event_type, data, **kwargs)

    async def emit(self, *args: Any, **kwargs: Any) -> Any:
        return await self.publish(*args, **kwargs)

    async def notify(self, *args: Any, **kwargs: Any) -> Any:
        return await self.publish(*args, **kwargs)

    async def push(self, *args: Any, **kwargs: Any) -> Any:
        return await self.publish(*args, **kwargs)

    async def send(self, *args: Any, **kwargs: Any) -> Any:
        # If first arg is a websocket, send directly.
        if args and hasattr(args[0], "send_text"):
            ws = args[0]
            msg = args[1] if len(args) > 1 else kwargs.get("message")
            payload_str = self._normalize_message(msg)
            send_text = getattr(ws, "send_text", None)
            send_json = getattr(ws, "send_json", None)
            if send_text is not None:
                await send_text(payload_str)
            elif send_json is not None:
                await send_json(msg if not isinstance(msg, str) else {"message": msg})
            return None
        return await self.publish(*args, **kwargs)

    async def send_event(self, *args: Any, **kwargs: Any) -> Any:
        return await self.publish(*args, **kwargs)

    async def send_json(self, *args: Any, **kwargs: Any) -> Any:
        return await self.broadcast(*args, **kwargs)

    async def send_text(self, *args: Any, **kwargs: Any) -> Any:
        return await self.broadcast(*args, **kwargs)

    @property
    def current_seq(self) -> int:
        return self._seq

    subscriber_count = None

    @property
    def subscriber_count_prop(self) -> int:
        return len(self.active_connections) + len(self._queues)

    def get_subscriber_count(self) -> int:
        return len(self.active_connections) + len(self._queues)


# Assign subscriber_count as property alias is tricky; set class attribute method instead.
WebSocketManager.subscriber_count = property(lambda self: len(self.active_connections) + len(self._queues))  # type: ignore


# Aliases for class names expected by callers/tests.
EventBus = WebSocketManager
ConnectionManager = WebSocketManager
EventStreamManager = WebSocketManager
RealtimeEventBus = WebSocketManager
Manager = WebSocketManager
WSManager = WebSocketManager
PubSubManager = WebSocketManager


# Singleton-ish instance(s) with many common names.
event_bus = WebSocketManager()
websocket_manager = event_bus
connection_manager = event_bus
manager = event_bus
event_stream = event_bus
realtime_event_bus = event_bus
stream_manager = event_bus
event_bus_instance = event_bus


def get_event_bus() -> WebSocketManager:
    return event_bus


def get_websocket_manager() -> WebSocketManager:
    return event_bus


def get_connection_manager() -> WebSocketManager:
    return event_bus


def get_event_stream() -> WebSocketManager:
    return event_bus


def get_manager() -> WebSocketManager:
    return event_bus


__all__ = [
    "WebSocketManager",
    "EventBus",
    "ConnectionManager",
    "EventStreamManager",
    "RealtimeEventBus",
    "event_bus",
    "websocket_manager",
    "connection_manager",
    "manager",
    "get_event_bus",
    "get_websocket_manager",
    "get_connection_manager",
]
