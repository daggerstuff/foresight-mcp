"""
WebSocket Server implementation for Foresight

Handles WebSocket connections with:
- Authentication on connect
- Heartbeat/ping-pong for connection health
- Reconnection with last event ID
- Subscription management
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from foresight_mcp.event_bus import EventType

from .subscriptions import get_subscription_manager

logger = logging.getLogger("foresight_websocket")


class ConnectionState(StrEnum):
    """WebSocket connection state."""

    CONNECTING = "connecting"
    CONNECTED = "connected"
    AUTHENTICATED = "authenticated"
    DISCONNECTED = "disconnected"


@dataclass
class Connection:
    """Represents a WebSocket connection."""

    id: str
    send: Callable[[str], Any]
    close: Callable[[], Any]
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    state: ConnectionState = ConnectionState.CONNECTING
    user_id: str | None = None
    tenant_id: str | None = None
    last_event_id: str | None = None
    subscriptions: set[str] = field(default_factory=set)

    def send_message(self, message: dict[str, Any]) -> None:
        """Send a message to the connected client."""
        try:
            message_str = json.dumps(message)
            self.send(message_str)
        except Exception as e:
            logger.error(f"Error sending message to connection {self.id}: {e}")


class WebSocketHandler:
    """
    Handles WebSocket connections and message routing.

    Features:
    - Connection lifecycle management
    - Message parsing and routing
    - Heartbeat/ping-pong
    - Reconnection support
    """

    def __init__(
        self,
        auth_callback: Callable[[str], tuple[str, str] | None] | None = None,
        heartbeat_interval: float = 30.0,
        heartbeat_timeout: float = 90.0,
    ):
        self._connections: dict[str, Connection] = {}
        self._auth_callback = auth_callback
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_timeout = heartbeat_timeout

    async def connect(
        self,
        connection_id: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        last_event_id: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """
        Handle new WebSocket connection.

        Args:
            connection_id: Optional reconnection ID
            user_id: Optional user ID for authentication
            tenant_id: Optional tenant ID
            last_event_id: Last event ID for reconnection sync

        Returns:
            Tuple of (connection_id, welcome_message)
        """
        conn_id = connection_id or str(uuid.uuid4())
        is_reconnect = connection_id in self._connections

        if is_reconnect:
            # Restore existing connection
            conn = self._connections[connection_id]
            conn.state = ConnectionState.CONNECTED
            conn.last_heartbeat = datetime.now(timezone.utc)
            logger.info(f"Reconnected connection: {connection_id}")
            return connection_id, {
                "type": "reconnected",
                "connection_id": connection_id,
                "message": "Reconnected to previous session",
                "last_event_id": conn.last_event_id,
            }

        # New connection
        connection = Connection(
            id=conn_id,
            send=lambda _msg: None,  # Will be set by WebSocket server
            close=lambda: None,
            user_id=user_id,
            tenant_id=tenant_id,
            last_event_id=last_event_id,
        )
        connection.state = ConnectionState.CONNECTED

        self._connections[conn_id] = connection
        logger.info(f"New WebSocket connection: {conn_id}")

        return conn_id, {
            "type": "connection_accepted",
            "connection_id": conn_id,
            "message": "Connected to Foresight WebSocket server",
            "heartbeat_interval": int(self._heartbeat_interval),
            "user_id": user_id,
            "tenant_id": tenant_id,
        }

    async def disconnect(self, connection_id: str) -> None:
        """Handle connection disconnect."""
        if connection_id in self._connections:
            conn = self._connections[connection_id]
            conn.state = ConnectionState.DISCONNECTED
            # Don't remove - keep for potential reconnection
            logger.info(f"WebSocket disconnect: {connection_id}")

    async def authenticate(self, connection_id: str, token: str) -> dict[str, Any]:
        """
        Authenticate a connection.

        Args:
            connection_id: Connection to authenticate
            token: Authentication token

        Returns:
            Authentication result message
        """
        connection = self._connections.get(connection_id)
        if not connection:
            return {"type": "error", "message": "Connection not found"}

        if self._auth_callback:
            try:
                if asyncio.iscoroutinefunction(self._auth_callback):
                    result = await self._auth_callback(token)
                else:
                    result = self._auth_callback(token)

                if result:
                    user_id, tenant_id = result
                    connection.user_id = user_id
                    connection.tenant_id = tenant_id
                    connection.state = ConnectionState.AUTHENTICATED
                    logger.info(f"Connection {connection_id} authenticated as {user_id} in tenant {tenant_id}")
                    return {
                        "type": "authenticated",
                        "connection_id": connection_id,
                        "user_id": user_id,
                        "tenant_id": tenant_id,
                    }
            except Exception as e:
                logger.error(f"Authentication error: {e}")

        return {"type": "error", "message": "Authentication failed"}

    async def receive(self, connection_id: str, message: dict[str, Any]) -> dict[str, Any] | None:
        """
        Handle incoming message from client.

        Message types:
        - ping: Heartbeat check
        - subscribe: Subscribe to event types
        - unsubscribe: Unsubscribe from event types
        - auth: Authenticate connection

        Returns:
            Response message or None
        """
        action = message.get("action")

        if action == "ping":
            connection = self._connections.get(connection_id)
            if connection:
                connection.last_heartbeat = datetime.now(timezone.utc)
            return {
                "type": "pong",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "connection_id": connection_id,
            }

        if action == "subscribe":
            sub_manager = get_subscription_manager()

            subscription_id = message.get("subscription_id", str(uuid.uuid4()))
            event_types = message.get("event_types", [])
            entity_filter = message.get("entity_filter")

            await sub_manager.subscribe(
                subscription_id=subscription_id,
                connection_id=connection_id,
                event_types=event_types,
                entity_filter=entity_filter,
            )

            return {
                "type": "subscribed",
                "subscription_id": subscription_id,
                "event_types": event_types,
                "entity_filter": entity_filter,
            }

        if action == "unsubscribe":
            sub_manager = get_subscription_manager()
            subscription_id = message.get("subscription_id")

            if subscription_id:
                await sub_manager.unsubscribe(subscription_id)
                return {
                    "type": "unsubscribed",
                    "subscription_id": subscription_id,
                }

        if action == "auth":
            token: str = message.get("token", "")
            return await self.authenticate(connection_id, token)

        return {"type": "error", "message": f"Unknown action: {action}"}

    async def cleanup_stale_connections(self, timeout_seconds: int = 300) -> list[str]:
        """Remove connections that haven't sent heartbeat."""
        removed = []
        now = datetime.now(timezone.utc)

        for conn_id, conn in list(self._connections.items()):
            if conn.state == ConnectionState.DISCONNECTED:
                age = (now - conn.connected_at).total_seconds()
                if age > timeout_seconds:
                    del self._connections[conn_id]
                    removed.append(conn_id)
            elif conn.state in (ConnectionState.CONNECTED, ConnectionState.AUTHENTICATED):
                heartbeat_age = (now - conn.last_heartbeat).total_seconds()
                if heartbeat_age > self._heartbeat_timeout:
                    logger.warning(
                        "Closing connection %s: no heartbeat for %.0fs (timeout %.0fs)",
                        conn_id,
                        heartbeat_age,
                        self._heartbeat_timeout,
                    )
                    try:
                        conn.close()
                    except Exception:
                        logger.debug("Failed to close stale connection %s", conn_id, exc_info=True)
                    del self._connections[conn_id]
                    removed.append(conn_id)

        return removed

    def get_connection(self, connection_id: str) -> Connection | None:
        """Get connection by ID."""
        return self._connections.get(connection_id)

    def get_stats(self) -> dict[str, Any]:
        """Get connection statistics."""
        states = {}
        for conn in self._connections.values():
            state = conn.state.value
            states[state] = states.get(state, 0) + 1

        return {
            "total_connections": len(self._connections),
            "by_state": states,
        }


class WebSocketServer:
    """
    WebSocket server for real-time event subscriptions.

    Usage:
        server = WebSocketServer(event_bus)
        await server.start()
    """

    def __init__(
        self,
        event_bus=None,
        auth_callback: Callable[[str], tuple[str, str] | None] | None = None,
        heartbeat_interval: float = 30.0,
        heartbeat_timeout: float = 90.0,
    ):
        """Initialize WebSocket server.

        Args:
            event_bus: Event bus for subscribing to events
            auth_callback: Callback to authenticate tokens
            heartbeat_interval: Interval for sending ping
            heartbeat_timeout: Timeout for considering connection dead
        """

        self.handler = WebSocketHandler(
            auth_callback,
            heartbeat_interval=heartbeat_interval,
            heartbeat_timeout=heartbeat_timeout,
        )
        self._event_bus = event_bus
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_timeout = heartbeat_timeout
        self._running = False
        self._task: asyncio.Task | None = None
        self._event_buffer: list[dict[str, Any]] = []
        self._max_buffer_size = 1000

    async def start(self, host: str = "0.0.0.0", port: int = 8765) -> None:
        """Start the WebSocket server."""
        self._running = True
        logger.info(f"Starting WebSocket server on {host}:{port}")

        # Start background tasks
        self._task = asyncio.create_task(self._server_loop())

        # Subscribe to event bus
        if self._event_bus:
            self._subscribe_to_events()

    async def _server_loop(self) -> None:
        """
        Main server loop for cleanup tasks.

        Uses adaptive sleep: backs off up to _MAX_CLEANUP_INTERVAL when there
        are few/no stale connections, and resets to _MIN_CLEANUP_INTERVAL when
        work is found. This avoids burning CPU on a fixed 60-second tick when
        the server is idle, while staying responsive under load.
        """
        _min_cleanup_interval = 10.0  # seconds — floor when connections are stale
        _max_cleanup_interval = 300.0  # seconds — ceiling when server is quiet
        _backoff_factor = 2.0

        interval = _min_cleanup_interval
        while self._running:
            try:
                removed = await self.handler.cleanup_stale_connections()
                if removed:
                    # Work was found — reset to minimum interval
                    interval = _min_cleanup_interval
                    logger.debug("Cleaned up %d stale WebSocket connection(s)", len(removed))
                else:
                    # Nothing to do — back off exponentially up to the ceiling
                    interval = min(interval * _backoff_factor, _max_cleanup_interval)

                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Server loop error: %s", exc)
                await asyncio.sleep(_min_cleanup_interval)

    async def stop(self) -> None:
        """Stop the WebSocket server."""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def _subscribe_to_events(self) -> None:
        """Subscribe to all event bus events."""
        if not self._event_bus:
            logger.warning("No event bus available — skipping event subscriptions")
            return

        def on_event(event):
            """Callback for all events - broadcast to subscribers."""
            payload = {
                "id": event.id,
                "event_type": event.event_type.value,
                "timestamp": event.timestamp.isoformat(),
                "actor": event.actor,
                "entity_id": event.entity_id,
                "payload": event.payload,
                "metadata": event.metadata,
            }
            self._broadcast_event(event.event_type.value, payload)

            # Add to buffer
            self._event_buffer.append(payload)
            if len(self._event_buffer) > self._max_buffer_size:
                self._event_buffer.pop(0)

        # Subscribe to all event types
        for event_type in EventType:
            self._event_bus.subscribe(event_type, on_event)

    def _broadcast_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Broadcast event to all subscribed connections."""

        message = {
            "type": "event",
            "event_type": event_type,
            "payload": payload,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        for connection in self.handler._connections.values():
            try:
                connection.send_message(message)
            except Exception as e:
                logger.error(f"Error broadcasting event to connection {connection.id}: {e}")

    def get_buffered_events(self, since_event_id: str | None = None) -> list[dict[str, Any]]:
        """Get buffered events for reconnection sync."""
        if not since_event_id:
            return self._event_buffer[-100:]  # Last 100 events

        # Find events since the given ID
        found = False
        result = []
        for event in self._event_buffer:
            if event.get("id") == since_event_id:
                found = True
            if found:
                result.append(event)

        return result if found else self._event_buffer


# =============================================================================
# Integration with Event Bus
# =============================================================================


def setup_event_bus_websocket_integration(event_bus, websocket_server: WebSocketServer) -> None:
    """
    Set up event bus to broadcast events via WebSocket.

    This connects the event bus event stream to the WebSocket server,
    broadcasting all events to subscribed clients.
    """

    def on_event(event):
        """Callback for all events."""
        payload = {
            "id": event.id,
            "event_type": event.event_type.value,
            "timestamp": event.timestamp.isoformat(),
            "actor": event.actor,
            "entity_id": event.entity_id,
            "payload": event.payload,
            "metadata": event.metadata,
        }
        websocket_server._broadcast_event(event.event_type.value, payload)

    # Subscribe to all event types
    for event_type in EventType:
        event_bus.subscribe(event_type, on_event)
