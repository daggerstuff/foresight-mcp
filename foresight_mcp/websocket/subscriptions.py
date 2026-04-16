"""
Subscription Manager for WebSocket connections
Handles subscribe/unsubscribe and event routing
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set
from enum import Enum
import json

from ..event_bus import EventType

logger = logging.getLogger("foresight_websocket")


class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    ERROR = "error"


@dataclass
class Subscription:
    """
    Represents a client subscription.

    Attributes:
        id: Unique subscription identifier
        connection_id: WebSocket connection ID
        event_types: Set of event types to subscribe to
        entity_filter: Optional filter by entity ID pattern
        created_at: Subscription creation timestamp
        status: Current subscription status
    """
    id: str
    connection_id: str
    event_types: Set[EventType] = field(default_factory=set)
    entity_filter: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE

    def matches_event(self, event_type: EventType, entity_id: Optional[str] = None) -> bool:
        """Check if an event matches this subscription."""
        if self.status != SubscriptionStatus.ACTIVE:
            return False

        if event_type not in self.event_types:
            return False

        if self.entity_filter and entity_id and not self._entity_matches(entity_id):
            return False

        return True

    def _entity_matches(self, entity_id: str) -> bool:
        """Check if entity matches filter (supports wildcards)."""
        if not self.entity_filter:
            return True
        if self.entity_filter == "*":
            return True
        if self.entity_filter.endswith(":*"):
            # e.g., "memory:*" matches "memory:123"
            prefix = self.entity_filter[:-1]  # "memory:"
            return entity_id.startswith(prefix)
        return entity_id == self.entity_filter


class SubscriptionManager:
    """
    Manages WebSocket subscriptions.

    Features:
    - Subscribe/unsubscribe connections
    - Route events to matching subscriptions
    - Handle connection cleanup
    """

    def __init__(self):
        self._subscriptions: Dict[str, Subscription] = {}
        self._connection_subscriptions: Dict[str, Set[str]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        subscription_id: str,
        connection_id: str,
        event_types: List[str],
        entity_filter: Optional[str] = None,
    ) -> Subscription:
        """Create a new subscription."""
        # Parse event types
        parsed_types: Set[EventType] = set()
        for et in event_types:
            try:
                parsed_types.add(EventType(et))
            except ValueError:
                logger.warning(f"Unknown event type: {et}")

        subscription = Subscription(
            id=subscription_id,
            connection_id=connection_id,
            event_types=parsed_types,
            entity_filter=entity_filter,
        )

        async with self._lock:
            self._subscriptions[subscription_id] = subscription

            # Track by connection
            if connection_id not in self._connection_subscriptions:
                self._connection_subscriptions[connection_id] = set()
            self._connection_subscriptions[connection_id].add(subscription_id)

        logger.info(f"Created subscription {subscription_id} for connection {connection_id}")
        return subscription

    async def unsubscribe(self, subscription_id: str) -> bool:
        """Remove a subscription."""
        async with self._lock:
            if subscription_id not in self._subscriptions:
                return False

            subscription = self._subscriptions[subscription_id]

            # Remove from connection tracking
            conn_subs = self._connection_subscriptions.get(subscription.connection_id, set())
            conn_subs.discard(subscription_id)

            # Remove subscription
            del self._subscriptions[subscription_id]

        logger.info(f"Removed subscription {subscription_id}")
        return True

    async def unsubscribe_all(self, connection_id: str) -> List[str]:
        """Remove all subscriptions for a connection."""
        removed = []

        async with self._lock:
            subscription_ids = list(self._connection_subscriptions.get(connection_id, []))

            for sub_id in subscription_ids:
                if sub_id in self._subscriptions:
                    del self._subscriptions[sub_id]
                    removed.append(sub_id)

            self._connection_subscriptions.pop(connection_id, None)

        logger.info(f"Removed {len(removed)} subscriptions for connection {connection_id}")
        return removed

    def get_matching_subscriptions(self, event_type: EventType, entity_id: Optional[str] = None) -> List[Subscription]:
        """Get all subscriptions matching an event."""
        return [
            sub for sub in self._subscriptions.values()
            if sub.matches_event(event_type, entity_id)
        ]

    async def send_to_subscribers(
        self,
        event_type: EventType,
        payload: Dict[str, Any],
        entity_id: Optional[str] = None,
        send_func: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ) -> int:
        """
        Send event to all matching subscriptions.

        Args:
            event_type: Type of event
            payload: Event payload
            entity_id: Optional entity ID for filtering
            send_func: Async function to send message (connection_id, message)

        Returns:
            Number of messages sent
        """
        if not send_func:
            return 0

        matching = self.get_matching_subscriptions(event_type, entity_id)
        sent_count = 0

        for subscription in matching:
            try:
                message = {
                    "type": "event",
                    "subscription_id": subscription.id,
                    "event_type": event_type.value,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "payload": payload,
                }
                await send_func(subscription.connection_id, message)
                sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send to {subscription.id}: {e}")

        return sent_count

    def get_stats(self) -> Dict[str, Any]:
        """Get subscription statistics."""
        by_status: Dict[str, int] = {}
        for sub in self._subscriptions.values():
            status = sub.status.value
            by_status[status] = by_status.get(status, 0) + 1

        return {
            "total_subscriptions": len(self._subscriptions),
            "unique_connections": len(self._connection_subscriptions),
            "by_status": by_status,
        }


# =============================================================================
# Global Subscription Manager
# =============================================================================

_subscription_manager: Optional[SubscriptionManager] = None


def get_subscription_manager() -> SubscriptionManager:
    """Get the global subscription manager instance."""
    global _subscription_manager
    if _subscription_manager is None:
        _subscription_manager = SubscriptionManager()
    return _subscription_manager


def reset_subscription_manager() -> None:
    """Reset the global subscription manager (for testing)."""
    global _subscription_manager
    _subscription_manager = None
