"""
CRDT (Conflict-Free Replicated Data Type) Implementations

Provides conflict-free data types for offline-first synchronization:
- LWW-Register: Last-Writer-Wins Register for scalar values
- OR-Set: Observed-Remove Set for collections
- Vector Clock: Causal ordering of events
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


# =============================================================================
# Vector Clock
# =============================================================================


@dataclass
class VectorClock:
    """
    Vector clock for causal ordering.

    Tracks logical timestamps across distributed nodes.
    Each node increments its own counter when performing operations.

    Attributes:
        clock: Dict mapping node_id to counter
    """

    clock: dict[str, int] = field(default_factory=dict)

    def increment(self, node_id: str) -> None:
        """Increment clock for a node."""
        self.clock[node_id] = self.clock.get(node_id, 0) + 1

    def merge(self, other: VectorClock) -> None:
        """Merge with another vector clock (take max of each component)."""
        all_nodes = set(self.clock.keys()) | set(other.clock.keys())
        for node_id in all_nodes:
            self.clock[node_id] = max(self.clock.get(node_id, 0), other.clock.get(node_id, 0))

    def happens_before(self, other: VectorClock) -> bool:
        """
        Check if this clock happens-before another.

        A happens-before B if:
        - All components of A <= corresponding components of B
        - At least one component of A < corresponding component of B
        """
        all_nodes = set(self.clock.keys()) | set(other.clock.keys())

        at_least_one_less = False
        for node_id in all_nodes:
            self_val = self.clock.get(node_id, 0)
            other_val = other.clock.get(node_id, 0)

            if self_val > other_val:
                return False
            if self_val < other_val:
                at_least_one_less = True

        return at_least_one_less

    def concurrent_with(self, other: VectorClock) -> bool:
        """Check if this clock is concurrent with another (neither happens-before)."""
        return not self.happens_before(other) and not other.happens_before(self)

    def copy(self) -> VectorClock:
        """Create a copy of this vector clock."""
        new_clock = VectorClock()
        new_clock.clock = self.clock.copy()
        return new_clock

    def to_dict(self) -> dict[str, int]:
        """Convert to dictionary."""
        return self.clock.copy()

    @classmethod
    def from_dict(cls, data: dict[str, int]) -> VectorClock:
        """Create from dictionary."""
        clock = VectorClock()
        clock.clock = data
        return clock


# =============================================================================
# LWW-Register (Last-Writer-Wins Register)
# =============================================================================


@dataclass
class LWWRegister[T]:
    """
    Last-Writer-Wins Register CRDT.

    Resolves conflicts by choosing the value with the latest timestamp.
    Ties are broken by node_id (lexicographically higher wins).

    Attributes:
        value: Current value
        timestamp: Logical timestamp (Unix timestamp)
        node_id: ID of the node that wrote the value
        vector_clock: Causal ordering
    """

    value: T | None = None
    timestamp: float = 0.0
    node_id: str = ""
    vector_clock: VectorClock = field(default_factory=VectorClock)

    def set(self, new_value: T, node_id: str, timestamp: float | None = None) -> None:
        """
        Set a new value.

        Args:
            new_value: Value to set
            node_id: ID of the node performing the operation
            timestamp: Optional timestamp (defaults to current time)
        """
        ts = timestamp or time.time()

        # Always accept if timestamp is newer, or same timestamp and node_id is higher
        if ts > self.timestamp or (ts == self.timestamp and node_id > self.node_id):
            self.value = new_value
            self.timestamp = ts
            self.node_id = node_id
            self.vector_clock.increment(node_id)

    def merge(self, other: LWWRegister[T]) -> None:
        """Merge with another LWW-Register (last writer wins)."""
        if other.timestamp > self.timestamp:
            self.value = other.value
            self.timestamp = other.timestamp
            self.node_id = other.node_id
            self.vector_clock.merge(other.vector_clock)
        elif other.timestamp == self.timestamp:
            if other.node_id > self.node_id:
                self.value = other.value
                self.node_id = other.node_id
            self.vector_clock.merge(other.vector_clock)
        else:
            self.vector_clock.merge(other.vector_clock)

    def get(self) -> T | None:
        """Get the current value."""
        return self.value

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "value": self.value,
            "timestamp": self.timestamp,
            "node_id": self.node_id,
            "vector_clock": self.vector_clock.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LWWRegister:
        """Create from dictionary."""
        register = cls()
        register.value = data.get("value")
        register.timestamp = data.get("timestamp", 0.0)
        register.node_id = data.get("node_id", "")
        if "vector_clock" in data:
            register.vector_clock = VectorClock.from_dict(data["vector_clock"])
        return register


# =============================================================================
# OR-Set (Observed-Remove Set)
# =============================================================================


@dataclass
class ORSet[T]:
    """
    Observed-Remove Set CRDT.

    Elements can be added and removed concurrently without conflicts.
    An element is in the set if it has been added and not subsequently removed.

    Attributes:
        _adds: Dict mapping element hash to set of (timestamp, node_id) pairs
        _removes: Dict mapping element hash to set of (timestamp, node_id) pairs
        _values: Dict mapping element hash to the most recent element value
        _cache: Cache of current set contents
    """

    _adds: dict[str, set[tuple]] = field(default_factory=dict)
    _removes: dict[str, set[tuple]] = field(default_factory=dict)
    _values: dict[str, T] = field(default_factory=dict)
    _cache: set[T] | None = None
    _node_id: str = "default"
    vector_clock: VectorClock = field(default_factory=VectorClock)

    def __post_init__(self):
        if self._adds is None:
            self._adds = {}
        if self._removes is None:
            self._removes = {}
        if self._values is None:
            self._values = {}

    def set_node_id(self, node_id: str) -> None:
        """Set the node ID for this replica."""
        self._node_id = node_id

    def _get_hash(self, element: T) -> str:
        """Get hash of an element."""
        return hashlib.sha256(str(element).encode()).hexdigest()[:16]

    def add(self, element: T) -> None:
        """Add an element to the set."""
        ts = time.time()
        element_hash = self._get_hash(element)
        tag = (ts, self._node_id)

        if element_hash not in self._adds:
            self._adds[element_hash] = set()
        self._adds[element_hash].add(tag)
        self._values[element_hash] = element

        self.vector_clock.increment(self._node_id)
        self._cache = None

    def remove(self, element: T) -> None:
        """Remove an element from the set."""
        element_hash = self._get_hash(element)

        # Mark all current adds as removed
        if element_hash in self._adds:
            if element_hash not in self._removes:
                self._removes[element_hash] = set()
            # Copy adds to removes
            for add_tag in self._adds[element_hash]:
                self._removes[element_hash].add(add_tag)

        self.vector_clock.increment(self._node_id)
        self._cache = None

    def contains(self, element: T) -> bool:
        """Check if element is in the set."""
        element_hash = self._get_hash(element)

        if element_hash not in self._adds:
            return False

        # Element is in set if there's at least one add not matched by remove
        adds = self._adds.get(element_hash, set())
        removes = self._removes.get(element_hash, set())

        return len(adds - removes) > 0

    def get_elements(self) -> set[T]:
        """Get all elements in the set."""
        if self._cache is not None:
            return self._cache

        self._cache = {
            self._values[element_hash]
            for element_hash, adds in self._adds.items()
            if element_hash in self._values and adds - self._removes.get(element_hash, set())
        }
        return self._cache

    def merge(self, other: ORSet[T]) -> None:
        """Merge with another OR-Set."""
        # Merge adds
        for element_hash, tags in other._adds.items():
            if element_hash not in self._adds:
                self._adds[element_hash] = set()
            self._adds[element_hash].update(tags)
            if element_hash in other._values:
                self._values[element_hash] = other._values[element_hash]

        # Merge removes
        for element_hash, tags in other._removes.items():
            if element_hash not in self._removes:
                self._removes[element_hash] = set()
            self._removes[element_hash].update(tags)

        self.vector_clock.merge(other.vector_clock)
        self._cache = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "adds": {k: list(v) for k, v in self._adds.items()},
            "removes": {k: list(v) for k, v in self._removes.items()},
            "values": self._values.copy(),
            "vector_clock": self.vector_clock.to_dict(),
            "node_id": self._node_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ORSet:
        """Create from dictionary."""
        orset = cls()
        orset._adds = {k: set(v) for k, v in data.get("adds", {}).items()}
        orset._removes = {k: set(v) for k, v in data.get("removes", {}).items()}
        orset._values = data.get("values", {}).copy()
        orset._node_id = data.get("node_id", "default")
        if "vector_clock" in data:
            orset.vector_clock = VectorClock.from_dict(data["vector_clock"])
        return orset


# =============================================================================
# LWW-Map (Last-Writer-Wins Map)
# =============================================================================


@dataclass
class LWWMap[T]:
    """
    Last-Writer-Wins Map CRDT.

    A map where each key is an LWW-Register.
    Concurrent writes to the same key are resolved by LWW.

    Attributes:
        _entries: Dict mapping keys to LWW-Registers
    """

    _entries: dict[str, LWWRegister] = field(default_factory=dict)
    _node_id: str = "default"
    vector_clock: VectorClock = field(default_factory=VectorClock)

    def set_node_id(self, node_id: str) -> None:
        """Set the node ID for this replica."""
        self._node_id = node_id

    def set(self, key: str, value: T) -> None:
        """Set a key-value pair."""
        if key not in self._entries:
            self._entries[key] = LWWRegister()
        self._entries[key].set(value, self._node_id)
        self.vector_clock.increment(self._node_id)

    def get(self, key: str) -> T | None:
        """Get value for a key."""
        if key not in self._entries:
            return None
        return self._entries[key].get()

    def delete(self, key: str) -> None:
        """Delete a key (tombstone)."""
        if key in self._entries:
            # Set to None with new timestamp
            self._entries[key].set(None, self._node_id)
            self.vector_clock.increment(self._node_id)

    def keys(self) -> list[str]:
        """Get all keys."""
        return list(self._entries.keys())

    def merge(self, other: LWWMap[T]) -> None:
        """Merge with another LWW-Map."""
        for key, other_register in other._entries.items():
            if key not in self._entries:
                self._entries[key] = LWWRegister.from_dict(other_register.to_dict())
            else:
                self._entries[key].merge(other_register)
        self.vector_clock.merge(other.vector_clock)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "entries": {k: v.to_dict() for k, v in self._entries.items()},
            "vector_clock": self.vector_clock.to_dict(),
            "node_id": self._node_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LWWMap:
        """Create from dictionary."""
        lww_map = cls()
        entries = data.get("entries", {})
        lww_map._entries = {k: LWWRegister.from_dict(v) for k, v in entries.items()}
        lww_map._node_id = data.get("node_id", "default")
        if "vector_clock" in data:
            lww_map.vector_clock = VectorClock.from_dict(data["vector_clock"])
        return lww_map
