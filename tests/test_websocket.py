"""Tests for WebSocket server and subscriptions."""
import pytest
import asyncio
from foresight_mcp.websocket.server import (
    WebSocketServer,
    WebSocketHandler,
    ConnectionState,
)
from foresight_mcp.websocket.subscriptions import (
    get_subscription_manager,
    reset_subscription_manager,
)
from foresight_mcp.event_bus import EventType


@pytest.fixture
def event_loop():
    """Create event loop for tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def reset_subscriptions():
    """Reset subscription manager before each test."""
    reset_subscription_manager()
    yield


class TestWebSocketHandler:
    """Test WebSocket handler."""

    @pytest.mark.asyncio
    async def test_connect_new_connection(self):
        """Test connecting a new client."""
        handler = WebSocketHandler()
        conn_id, response = await handler.connect(tenant_id="tenant-1", user_id="user-1")

        assert conn_id is not None
        assert response["type"] == "connection_accepted"
        assert response["connection_id"] == conn_id
        assert response["heartbeat_interval"] == 30

    @pytest.mark.asyncio
    async def test_reconnect_connection(self):
        """Test reconnecting with existing connection ID."""
        handler = WebSocketHandler()

        # First connection
        conn_id, _ = await handler.connect(connection_id="conn-123")
        assert conn_id == "conn-123"

        # Reconnection
        _, response = await handler.connect(connection_id="conn-123")
        assert response["type"] == "reconnected"
        assert response["connection_id"] == "conn-123"

    @pytest.mark.asyncio
    async def test_disconnect_connection(self):
        """Test disconnecting a connection."""
        handler = WebSocketHandler()

        conn_id, _ = await handler.connect()
        await handler.disconnect(conn_id)

        conn = handler.get_connection(conn_id)
        assert conn is not None
        assert conn.state == ConnectionState.DISCONNECTED


class TestSubscriptionManager:
    """Test subscription manager."""

    @pytest.mark.asyncio
    async def test_subscribe(self):
        """Test creating a subscription."""
        sub_manager = get_subscription_manager()

        result = await sub_manager.subscribe(
            subscription_id="sub-1",
            connection_id="conn-1",
            event_types=["memory.stored", "memory.updated"],
            entity_filter="memory:*",
        )

        assert result.id == "sub-1"
        assert result.connection_id == "conn-1"
        assert len(result.event_types) == 2

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        """Test removing a subscription."""
        sub_manager = get_subscription_manager()

        await sub_manager.subscribe(
            subscription_id="sub-1",
            connection_id="conn-1",
            event_types=["memory.stored"],
        )

        result = await sub_manager.unsubscribe("sub-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_subscription_matches_event(self):
        """Test subscription event matching."""
        sub_manager = get_subscription_manager()

        await sub_manager.subscribe(
            subscription_id="sub-1",
            connection_id="conn-1",
            event_types=["memory.stored", "memory.updated"],
        )

        # Should match
        subs = sub_manager.get_matching_subscriptions(EventType.MEMORY_STORED)
        assert len(subs) == 1

        # Should not match
        subs = sub_manager.get_matching_subscriptions(EventType.MEMORY_DELETED)
        assert len(subs) == 0


class TestWebSocketServer:
    """Test WebSocket server integration."""

    @pytest.mark.asyncio
    async def test_server_initialization(self):
        """Test server initializes correctly."""
        server = WebSocketServer()
        assert server.handler is not None
        assert server._heartbeat_interval == 30.0
        assert server._heartbeat_timeout == 90.0

    @pytest.mark.asyncio
    async def test_server_with_auth_callback(self):
        """Test server with authentication callback."""
        def auth_func(token):
            return token == "valid-token"

        server = WebSocketServer(auth_callback=auth_func)
        assert server.handler._auth_callback == auth_func


class TestConnectionState:
    """Test connection state management."""

    @pytest.mark.asyncio
    async def test_connection_state_transitions(self):
        """Test connection state transitions."""
        handler = WebSocketHandler()

        # Connect
        conn_id, _ = await handler.connect()
        conn = handler.get_connection(conn_id)
        assert conn.state == ConnectionState.CONNECTED

        # Disconnect
        await handler.disconnect(conn_id)
        assert conn.state == ConnectionState.DISCONNECTED
