"""
Event Hook System for Foresight MCP
Allows registering custom handlers for events with support for:
- Python callables
- HTTP webhooks
- Async handlers
- Conditional execution
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union, Coroutine
from enum import Enum
import sqlite3
import httpx

from .event_bus import Event, EventType, get_event_bus

logger = logging.getLogger("foresight_hooks")


# =============================================================================
# Hook Types
# =============================================================================

class HookType(str, Enum):
    """Types of hooks supported."""
    CALLABLE = "callable"  # Python function
    HTTP = "http"  # HTTP webhook
    ASYNC = "async"  # Async Python function


@dataclass
class HookRegistration:
    """
    Registered hook configuration.

    Attributes:
        id: Unique identifier for the hook
        name: Human-readable name
        event_type: Event type to listen for
        hook_type: Type of hook (callable, http, async)
        handler: Callable or HTTP URL
        condition: Optional condition function for filtering
        retry_count: Number of retries on failure
        timeout: Timeout in seconds for HTTP hooks
        metadata: Additional configuration
        enabled: Whether hook is active
        created_at: Registration timestamp
    """
    id: str
    name: str
    event_type: EventType
    hook_type: HookType
    handler: Union[Callable, str]  # str = URL
    condition: Optional[Callable[[Event], bool]] = None
    retry_count: int = 3
    timeout: int = 30
    metadata: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            "id": self.id,
            "name": self.name,
            "event_type": self.event_type.value,
            "hook_type": self.hook_type.value,
            "handler": self.handler if self.hook_type == HookType.HTTP else "<callable>",
            "condition": self.condition.__name__ if self.condition else None,
            "retry_count": self.retry_count,
            "timeout": self.timeout,
            "metadata": self.metadata,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
        }


# =============================================================================
# Hook Registry (SQLite-backed)
# =============================================================================

class HookRegistry:
    """
    Persistent registry for hook configurations.

    Stores hook registrations in SQLite for durability across sessions.
    """

    def __init__(self, db_path: Optional[str] = None):
        """Initialize hook registry.

        Args:
            db_path: Path to SQLite database (default: ~/.foresight/hooks.db)
        """
        if db_path is None:
            db_path = str(Path.home() / ".foresight" / "hooks.db")

        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        db_path = Path(self.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(db_path))
        conn.execute("""
        CREATE TABLE IF NOT EXISTS hooks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            event_type TEXT NOT NULL,
            hook_type TEXT NOT NULL,
            handler TEXT NOT NULL,
            condition_name TEXT,
            retry_count INTEGER DEFAULT 3,
            timeout INTEGER DEFAULT 30,
            metadata TEXT DEFAULT '{}',
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hooks_event_type ON hooks(event_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hooks_enabled ON hooks(enabled)")
        conn.commit()
        conn.close()

    def register(self, hook: HookRegistration) -> None:
        """Register a new hook."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
        INSERT OR REPLACE INTO hooks
        (id, name, event_type, hook_type, handler, condition_name, retry_count, timeout, metadata, enabled, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            hook.id,
            hook.name,
            hook.event_type.value,
            hook.hook_type.value,
            hook.handler,
            hook.condition.__name__ if hook.condition else None,
            hook.retry_count,
            hook.timeout,
            json.dumps(hook.metadata),
            1 if hook.enabled else 0,
            hook.created_at.isoformat()
        ))
        conn.commit()
        conn.close()

    def unregister(self, hook_id: str) -> bool:
        """Remove a hook registration."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("DELETE FROM hooks WHERE id = ?", (hook_id,))
        conn.commit()
        conn.close()
        return cursor.rowcount > 0

    def get_by_event_type(self, event_type: EventType) -> List[HookRegistration]:
        """Get all registered hooks for an event type."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT * FROM hooks WHERE event_type = ? AND enabled = 1",
            (event_type.value,)
        ).fetchall()
        conn.close()
        return [self._row_to_hook(row) for row in rows]

    def get_all(self) -> List[HookRegistration]:
        """Get all registered hooks."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT * FROM hooks").fetchall()
        conn.close()
        return [self._row_to_hook(row) for row in rows]

    def _row_to_hook(self, row: tuple) -> HookRegistration:
        """Convert database row to HookRegistration."""
        return HookRegistration(
            id=row[0],
            name=row[1],
            event_type=EventType(row[2]),
            hook_type=HookType(row[3]),
            handler=row[4],
            condition=None,  # Conditions can't be serialized
            retry_count=row[6],
            timeout=row[7],
            metadata=json.loads(row[8]),
            enabled=bool(row[9]),
            created_at=datetime.fromisoformat(row[10])
        )


# =============================================================================
# Hook Executor
# =============================================================================

class HookExecutor:
    """
    Executes registered hooks when events are published.

    Handles:
    - Callable hooks (synchronous)
    - Async hooks
    - HTTP webhook hooks (with retry)
    - Conditional execution
    - Error handling with retries
    """

    def __init__(self, registry: Optional[HookRegistry] = None):
        """Initialize hook executor.

        Args:
            registry: Hook registry for persistence (default: global registry)
        """
        self._registry = registry or HookRegistry()
        self._callable_handlers: Dict[EventType, List[Callable[[Event], None]]] = {}
        self._async_handlers: Dict[EventType, List[Callable[[Event], Coroutine[Any, Any, Any]]]] = {}

        # Subscribe to event bus
        self._event_bus = get_event_bus()
        self._subscribe_to_events()

    def _subscribe_to_events(self) -> None:
        """Subscribe to all event types."""
        for event_type in EventType:
            self._event_bus.subscribe(event_type, self._execute_hooks)

    def _execute_hooks(self, event: Event) -> None:
        """Execute all hooks for an event."""
        event_type = event.event_type

        # Get registered hooks from registry
        hooks = self._registry.get_by_event_type(event_type)

        for hook in hooks:
            # Check condition
            if hook.condition and not hook.condition(event):
                continue

            # Execute based on hook type
            if hook.hook_type == HookType.CALLABLE:
                self._execute_callable(hook, event)
            elif hook.hook_type == HookType.ASYNC:
                asyncio.create_task(self._execute_async(hook, event))
            elif hook.hook_type == HookType.HTTP:
                asyncio.create_task(self._execute_http(hook, event))

        # Also execute in-memory handlers
        if event_type in self._callable_handlers:
            for handler in self._callable_handlers[event_type]:
                try:
                    handler(event)
                except Exception as e:
                    logger.error(f"Callable hook error: {e}")

        if event_type in self._async_handlers:
            for handler in self._async_handlers[event_type]:
                asyncio.create_task(handler(event))

    def _execute_callable(self, hook: HookRegistration, event: Event) -> None:
        """Execute a callable hook."""
        try:
            # Callable handlers are registered in-memory, not persisted
            # This is for hooks registered via register_callable()
            pass
        except Exception as e:
            logger.error(f"Callable hook {hook.name} failed: {e}")

    async def _execute_async(self, hook: HookRegistration, event: Event) -> None:
        """Execute an async hook."""
        try:
            # Async handlers are registered in-memory, not persisted
            pass
        except Exception as e:
            logger.error(f"Async hook {hook.name} failed: {e}")

    async def _execute_http(self, hook: HookRegistration, event: Event) -> None:
        """Execute an HTTP webhook hook with retry."""
        # hook.handler is a str (URL) for HTTP hooks
        url: str = hook.handler  # type: ignore
        payload = {
            "event_id": event.id,
            "event_type": event.event_type.value,
            "timestamp": event.timestamp.isoformat(),
            "actor": event.actor,
            "entity_id": event.entity_id,
            "payload": event.payload,
            "metadata": event.metadata,
        }

        last_error = None
        for attempt in range(hook.retry_count):
            try:
                async with httpx.AsyncClient(timeout=hook.timeout) as client:
                    response = await client.post(url, json=payload)
                    response.raise_for_status()
                    return
            except Exception as e:
                last_error = e
                if attempt < hook.retry_count - 1:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff

        logger.error(f"HTTP hook {hook.name} failed after {hook.retry_count} attempts: {last_error}")

    def register_callable(self, event_type: EventType, handler: Callable[[Event], None]) -> None:
        """Register an in-memory callable handler."""
        if event_type not in self._callable_handlers:
            self._callable_handlers[event_type] = []
        self._callable_handlers[event_type].append(handler)

    def register_async(self, event_type: EventType, handler: Callable[[Event], Coroutine[Any, Any, Any]]) -> None:
        """Register an in-memory async handler."""
        if event_type not in self._async_handlers:
            self._async_handlers[event_type] = []
        self._async_handlers[event_type].append(handler)

    def register_http_hook(
        self,
        name: str,
        event_type: EventType,
        url: str,
        retry_count: int = 3,
        timeout: int = 30,
        metadata: Optional[Dict[str, Any]] = None
    ) -> HookRegistration:
        """Register an HTTP webhook hook."""
        hook_id = hashlib.sha256(f"{name}:{url}".encode()).hexdigest()[:16]
        hook = HookRegistration(
            id=hook_id,
            name=name,
            event_type=event_type,
            hook_type=HookType.HTTP,
            handler=url,
            retry_count=retry_count,
            timeout=timeout,
            metadata=metadata or {},
        )
        self._registry.register(hook)
        return hook


# =============================================================================
# Global Hook Executor
# =============================================================================

_hook_executor: Optional[HookExecutor] = None


def get_hook_executor() -> HookExecutor:
    """Get the global hook executor instance."""
    global _hook_executor
    if _hook_executor is None:
        _hook_executor = HookExecutor()
    return _hook_executor


def reset_hook_executor() -> None:
    """Reset the global hook executor (for testing)."""
    global _hook_executor
    _hook_executor = None


# =============================================================================
# MCP Tools
# =============================================================================

from fastmcp import FastMCP

mcp = FastMCP("Foresight Hooks")


@mcp.tool()
def list_hooks() -> str:
    """List all registered hooks."""
    executor = get_hook_executor()
    hooks = executor._registry.get_all()

    if not hooks:
        return "No hooks registered."

    lines = ["Registered hooks:", ""]
    for hook in hooks:
        status = "enabled" if hook.enabled else "disabled"
        lines.append(f"- [{hook.id}] {hook.name}")
        lines.append(f"  Event: {hook.event_type.value} | Type: {hook.hook_type.value} | Status: {status}")
        if hook.hook_type == HookType.HTTP:
            lines.append(f"  URL: {hook.handler}")
        lines.append(f"  Retries: {hook.retry_count} | Timeout: {hook.timeout}s")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def register_hook(
    name: str,
    event_type: str,
    hook_type: str = "http",
    url: Optional[str] = None,
    retry_count: int = 3,
    timeout: int = 30
) -> str:
    """
    Register a new hook.

    Args:
        name: Human-readable name for the hook
        event_type: Event type to listen for (e.g., "memory.stored")
        hook_type: Type of hook ("http" supported)
        url: URL for HTTP hooks
        retry_count: Number of retries on failure
        timeout: Timeout in seconds
    """
    try:
        et = EventType(event_type)
    except ValueError:
        return f"Invalid event type: {event_type}. Valid types: {', '.join(e.value for e in EventType)}"

    executor = get_hook_executor()

    if hook_type == "http":
        if not url:
            return "URL required for HTTP hooks"
        hook = executor.register_http_hook(
            name=name,
            event_type=et,
            url=url,
            retry_count=retry_count,
            timeout=timeout
        )
        return f"Registered HTTP hook '{name}' (ID: {hook.id}) for event {et.value}"

    return f"Hook type '{hook_type}' not yet supported via MCP"


@mcp.tool()
def unregister_hook(hook_id: str) -> str:
    """
    Unregister a hook by ID.

    Args:
        hook_id: ID of hook to remove
    """
    executor = get_hook_executor()
    if executor._registry.unregister(hook_id):
        return f"Unregistered hook {hook_id}"
    return f"Hook {hook_id} not found"
