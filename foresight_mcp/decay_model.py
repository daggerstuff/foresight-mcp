"""Memory Strength Decay Model.

Computes and applies per-memory strength decay based on the Ebbinghaus
forgetting curve, configurable per (tenant, user, category) via the
existing ``decay_config`` table.

Design points:
- ``current_strength`` is a *separate* column from creator-set
  ``importance`` so the original importance signal is never overwritten
  by decay math.
- All decay applications are recorded in ``memory_decay_events`` for
  audit and debugging.
- The strength trend (``stable`` / ``strengthening`` / ``weakening`` /
  ``stale``) is derived at compute time from the new strength, the
  activation count, and the time since the last decay.
- The pre-existing ``TemporalService`` (``temporal_service.py``) still
  drives background decay sweeps that overwrite ``importance``; this
  module is the *strength-focused* interface that coexists with it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import DB_PATH
from .connection_pool import get_pool
from .tenant_context import get_current_tenant_id

logger = logging.getLogger("foresight_decay_model")

MAX_USER_ID_LENGTH = 128
MAX_TENANT_ID_LENGTH = 64

DEFAULT_HALF_LIFE_HOURS = 168.0
DEFAULT_MIN_IMPORTANCE = 0.1
DEFAULT_ACTIVATION_BOOST = 1.2
DEFAULT_STRENGTHENING_THRESHOLD = 5
DEFAULT_STALE_THRESHOLD = 0.2

# A memory whose last_decay_at is within this many hours of ``now`` is
# considered "fresh" and is skipped by apply_decay_batch. This handles
# clock drift between the moment a memory is written and the moment a
# decay sweep runs against it.
FRESHNESS_EPSILON_HOURS = 1.0 / 3600.0


class DecayModelError(ValueError):
    """Raised for invalid decay-model inputs."""


@dataclass(frozen=True)
class DecayConfig:
    """Decay configuration for a (tenant, user, category) triple."""

    tenant_id: str
    user_id: str
    category: str
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS
    min_importance: float = DEFAULT_MIN_IMPORTANCE
    activation_boost: float = DEFAULT_ACTIVATION_BOOST
    strengthening_threshold: int = DEFAULT_STRENGTHENING_THRESHOLD
    stale_threshold: float = DEFAULT_STALE_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "category": self.category,
            "half_life_hours": self.half_life_hours,
            "min_importance": self.min_importance,
            "activation_boost": self.activation_boost,
            "strengthening_threshold": self.strengthening_threshold,
            "stale_threshold": self.stale_threshold,
        }

    @classmethod
    def defaults(cls, tenant_id: str, user_id: str, category: str) -> DecayConfig:
        return cls(tenant_id=tenant_id, user_id=user_id, category=category)


@dataclass
class StrengthEvent:
    """A single audit-log entry for a decay/reinforce event."""

    id: int
    tenant_id: str
    user_id: str
    memory_id: str
    event_type: str
    old_strength: float | None
    new_strength: float | None
    decay_factor: float | None
    reason: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "memory_id": self.memory_id,
            "event_type": self.event_type,
            "old_strength": self.old_strength,
            "new_strength": self.new_strength,
            "decay_factor": self.decay_factor,
            "reason": self.reason,
            "created_at": self.created_at,
        }


@dataclass
class DecayStats:
    """Aggregate counters returned by batch decay operations."""

    processed: int = 0
    updated: int = 0
    skipped: int = 0
    reinforced: int = 0
    avg_decay_factor: float = 1.0
    trend_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "updated": self.updated,
            "skipped": self.skipped,
            "reinforced": self.reinforced,
            "avg_decay_factor": self.avg_decay_factor,
            "trend_counts": dict(self.trend_counts),
        }


@dataclass
class DecayConfigOptions:
    """Options for decay configuration updates."""

    half_life_hours: float | None = None
    min_importance: float | None = None
    activation_boost: float | None = None
    strengthening_threshold: int | None = None
    stale_threshold: float | None = None


@dataclass
class DecayRowContext:
    """Context for applying decay to a single memory row."""

    conn: sqlite3.Connection
    row: sqlite3.Row
    user_id: str
    tenant_id: str
    now: datetime
    now_iso: str
    stats: DecayStats


@dataclass
class DecayEventParams:
    """Parameters for recording a decay/reinforce event."""

    tenant_id: str
    user_id: str
    memory_id: str
    event_type: str
    old_strength: float | None
    new_strength: float | None
    decay_factor: float | None
    reason: str


class MemoryDecayService:
    """Per-memory strength decay operations.

    Reads decay parameters from the existing ``decay_config`` table and
    writes the resulting ``current_strength`` and ``last_decay_at`` back
    to the ``memories`` table. All events are logged to
    ``memory_decay_events``.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._ensure_tables()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> Any:
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_decay_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    user_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    old_strength REAL,
                    new_strength REAL,
                    decay_factor REAL,
                    reason TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_decay_events_lookup
                    ON memory_decay_events (tenant_id, user_id, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_decay_events_memory
                    ON memory_decay_events (memory_id, created_at DESC)
                """
            )
            conn.commit()
        finally:
            pool = get_pool(self.db_path)
            pool.release(conn)

    @staticmethod
    def _validate_ids(user_id: str, tenant_id: str) -> None:
        if not user_id or len(user_id) > MAX_USER_ID_LENGTH:
            raise DecayModelError(f"user_id must be 1-{MAX_USER_ID_LENGTH} chars")
        if not tenant_id or len(tenant_id) > MAX_TENANT_ID_LENGTH:
            raise DecayModelError(f"tenant_id must be 1-{MAX_TENANT_ID_LENGTH} chars")

    @staticmethod
    def _parse_iso(ts: str) -> datetime:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))

    @staticmethod
    def _compute_trend(
        new_strength: float,
        activation_count: int,
        hours_since_decay: float,
        config: DecayConfig,
    ) -> str:
        if new_strength <= config.stale_threshold:
            return "stale"
        if activation_count >= config.strengthening_threshold:
            return "strengthening"
        if activation_count < 2 and hours_since_decay > config.half_life_hours:
            return "weakening"
        return "stable"

    @staticmethod
    def _compute_decay_factor(hours_elapsed: float, half_life_hours: float) -> float:
        if half_life_hours <= 0:
            return 0.0
        return pow(0.5, max(0.0, hours_elapsed) / half_life_hours)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_decay_config(
        self,
        user_id: str,
        tenant_id: str | None = None,
        category: str = "general",
    ) -> DecayConfig:
        """Return the decay config for (tenant, user, category) or defaults."""
        tid = tenant_id or get_current_tenant_id()
        self._validate_ids(user_id, tid)
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT half_life_hours, min_importance, activation_boost,
                       strengthening_threshold, stale_threshold
                FROM decay_config
                WHERE tenant_id = ? AND user_id = ? AND category = ?
                """,
                (tid, user_id, category),
            ).fetchone()
            if row is None:
                return DecayConfig.defaults(tid, user_id, category)
            return DecayConfig(
                tenant_id=tid,
                user_id=user_id,
                category=category,
                half_life_hours=row["half_life_hours"],
                min_importance=row["min_importance"],
                activation_boost=row["activation_boost"],
                strengthening_threshold=row["strengthening_threshold"],
                stale_threshold=row["stale_threshold"],
            )
        finally:
            pool = get_pool(self.db_path)
            pool.release(conn)

    def set_decay_config(
        self,
        user_id: str,
        tenant_id: str | None = None,
        category: str = "general",
        *,
        half_life_hours: float | None = None,
        min_importance: float | None = None,
        activation_boost: float | None = None,
        strengthening_threshold: int | None = None,
        stale_threshold: float | None = None,
        options: DecayConfigOptions | None = None,
    ) -> DecayConfig:
        """Upsert a decay config. None values fall back to defaults."""
        # Backward compatibility: if options is provided, use it; otherwise build from individual params
        if options is not None:
            half_life_hours = options.half_life_hours
            min_importance = options.min_importance
            activation_boost = options.activation_boost
            strengthening_threshold = options.strengthening_threshold
            stale_threshold = options.stale_threshold

        tid = tenant_id or get_current_tenant_id()
        self._validate_ids(user_id, tid)
        cfg = DecayConfig(
            tenant_id=tid,
            user_id=user_id,
            category=category,
            half_life_hours=(half_life_hours if half_life_hours is not None else DEFAULT_HALF_LIFE_HOURS),
            min_importance=(min_importance if min_importance is not None else DEFAULT_MIN_IMPORTANCE),
            activation_boost=(activation_boost if activation_boost is not None else DEFAULT_ACTIVATION_BOOST),
            strengthening_threshold=(
                strengthening_threshold if strengthening_threshold is not None else DEFAULT_STRENGTHENING_THRESHOLD
            ),
            stale_threshold=(stale_threshold if stale_threshold is not None else DEFAULT_STALE_THRESHOLD),
        )
        if cfg.half_life_hours <= 0:
            raise DecayModelError("half_life_hours must be > 0")
        if not 0.0 <= cfg.min_importance <= 1.0:
            raise DecayModelError("min_importance must be in [0, 1]")
        if not 0.0 <= cfg.activation_boost <= 10.0:
            raise DecayModelError("activation_boost must be in [0, 10]")
        if cfg.strengthening_threshold < 0:
            raise DecayModelError("strengthening_threshold must be >= 0")
        if not 0.0 <= cfg.stale_threshold <= 1.0:
            raise DecayModelError("stale_threshold must be in [0, 1]")

        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO decay_config (
                    tenant_id, user_id, category, half_life_hours,
                    min_importance, activation_boost,
                    strengthening_threshold, stale_threshold
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, user_id, category) DO UPDATE SET
                    half_life_hours = excluded.half_life_hours,
                    min_importance = excluded.min_importance,
                    activation_boost = excluded.activation_boost,
                    strengthening_threshold = excluded.strengthening_threshold,
                    stale_threshold = excluded.stale_threshold
                """,
                (
                    cfg.tenant_id,
                    cfg.user_id,
                    cfg.category,
                    cfg.half_life_hours,
                    cfg.min_importance,
                    cfg.activation_boost,
                    cfg.strengthening_threshold,
                    cfg.stale_threshold,
                ),
            )
            conn.commit()
        finally:
            pool = get_pool(self.db_path)
            pool.release(conn)
        return cfg

    def get_memory_strength(
        self,
        memory_id: str,
        user_id: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the current strength + trend for a single memory."""
        tid = tenant_id or get_current_tenant_id()
        self._validate_ids(user_id, tid)
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT id, importance, current_strength, strength_trend,
                       activation_count, last_decay_at, accessed_at,
                       COALESCE(category, 'general') AS category,
                       created_at
                FROM memories
                WHERE id = ? AND user_id = ? AND tenant_id = ?
                """,
                (memory_id, user_id, tid),
            ).fetchone()
            if row is None:
                return None
            return {
                "memory_id": row["id"],
                "tenant_id": tid,
                "user_id": user_id,
                "importance": row["importance"],
                "current_strength": row["current_strength"],
                "strength_trend": row["strength_trend"],
                "activation_count": row["activation_count"],
                "last_decay_at": row["last_decay_at"],
                "accessed_at": row["accessed_at"],
                "category": row["category"],
                "created_at": row["created_at"],
            }
        finally:
            pool = get_pool(self.db_path)
            pool.release(conn)

    def reinforce_memory(
        self,
        memory_id: str,
        user_id: str,
        tenant_id: str | None = None,
        activation_boost: float | None = None,
    ) -> dict[str, Any] | None:
        """Boost a memory's strength on access. Returns the new state or None."""
        tid = tenant_id or get_current_tenant_id()
        self._validate_ids(user_id, tid)
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT importance, current_strength, activation_count,
                       COALESCE(category, 'general') AS category
                FROM memories
                WHERE id = ? AND user_id = ? AND tenant_id = ?
                """,
                (memory_id, user_id, tid),
            ).fetchone()
            if row is None:
                return None

            config = self.get_decay_config(user_id=user_id, tenant_id=tid, category=row["category"])
            boost = activation_boost or config.activation_boost
            old_strength = row["current_strength"] if row["current_strength"] is not None else row["importance"]
            new_strength = min(1.0, old_strength * boost)
            new_strength = max(config.min_importance, new_strength)
            new_activation_count = row["activation_count"] + 1
            now_iso = datetime.now(timezone.utc).isoformat()
            trend = self._compute_trend(
                new_strength=new_strength,
                activation_count=new_activation_count,
                hours_since_decay=0.0,
                config=config,
            )
            conn.execute(
                """
                UPDATE memories
                SET current_strength = ?,
                    strength_trend = ?,
                    activation_count = ?,
                    last_decay_at = ?,
                    accessed_at = ?,
                    last_retrieved_at = ?
                WHERE id = ? AND user_id = ? AND tenant_id = ?
                """,
                (
                    new_strength,
                    trend,
                    new_activation_count,
                    now_iso,
                    now_iso,
                    now_iso,
                    memory_id,
                    user_id,
                    tid,
                ),
            )
            self._insert_event(
                conn,
                params=DecayEventParams(
                    tenant_id=tid,
                    user_id=user_id,
                    memory_id=memory_id,
                    event_type="reinforce",
                    old_strength=old_strength,
                    new_strength=new_strength,
                    decay_factor=boost,
                    reason=f"reinforce (boost={boost:.3f})",
                ),
            )
            conn.commit()
            return {
                "memory_id": memory_id,
                "tenant_id": tid,
                "user_id": user_id,
                "old_strength": old_strength,
                "current_strength": new_strength,
                "activation_count": new_activation_count,
                "strength_trend": trend,
                "last_decay_at": now_iso,
            }
        finally:
            pool = get_pool(self.db_path)
            pool.release(conn)

    def apply_decay_batch(
        self,
        user_id: str,
        tenant_id: str | None = None,
        batch_size: int = 500,
        now: datetime | None = None,
    ) -> DecayStats:
        """Apply time-based decay to all memories for a user."""
        tid = tenant_id or get_current_tenant_id()
        self._validate_ids(user_id, tid)
        if batch_size <= 0:
            raise DecayModelError("batch_size must be > 0")
        now = now or datetime.now(timezone.utc)
        now_iso = now.isoformat()
        stats = DecayStats()
        conn = self._connect()
        try:
            offset = 0
            while True:
                rows = conn.execute(
                    """
                    SELECT id, importance, current_strength, activation_count,
                           last_decay_at, COALESCE(category, 'general') AS category
                    FROM memories
                    WHERE user_id = ? AND tenant_id = ?
                    ORDER BY id
                    LIMIT ? OFFSET ?
                    """,
                    (user_id, tid, batch_size, offset),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    self._apply_decay_to_row(
                        DecayRowContext(
                            conn=conn,
                            row=row,
                            user_id=user_id,
                            tenant_id=tid,
                            now=now,
                            now_iso=now_iso,
                            stats=stats,
                        )
                    )
                conn.commit()
                offset += batch_size
                if len(rows) < batch_size:
                    break
            if stats.updated > 0:
                stats.avg_decay_factor = stats.avg_decay_factor / stats.updated
            return stats
        finally:
            pool = get_pool(self.db_path)
            pool.release(conn)

    def _apply_decay_to_row(self, ctx: DecayRowContext) -> None:
        ctx.stats.processed += 1
        config = self.get_decay_config(user_id=ctx.user_id, tenant_id=ctx.tenant_id, category=ctx.row["category"])
        last_decay_at = ctx.row["last_decay_at"]
        if last_decay_at:
            try:
                hours_elapsed = (ctx.now - self._parse_iso(last_decay_at)).total_seconds() / 3600
            except ValueError:
                hours_elapsed = 0.0
        else:
            hours_elapsed = 0.0
        if hours_elapsed <= FRESHNESS_EPSILON_HOURS:
            ctx.stats.skipped += 1
            return

        decay_factor = self._compute_decay_factor(hours_elapsed, config.half_life_hours)
        old_strength = ctx.row["current_strength"] if ctx.row["current_strength"] is not None else ctx.row["importance"]
        new_strength = max(config.min_importance, old_strength * decay_factor)
        trend = self._compute_trend(
            new_strength=new_strength,
            activation_count=ctx.row["activation_count"],
            hours_since_decay=hours_elapsed,
            config=config,
        )
        ctx.conn.execute(
            """
            UPDATE memories
            SET current_strength = ?,
                strength_trend = ?,
                last_decay_at = ?
            WHERE id = ? AND user_id = ? AND tenant_id = ?
            """,
            (
                new_strength,
                trend,
                ctx.now_iso,
                ctx.row["id"],
                ctx.user_id,
                ctx.tenant_id,
            ),
        )
        self._insert_event(
            ctx.conn,
            params=DecayEventParams(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                memory_id=ctx.row["id"],
                event_type="decay",
                old_strength=old_strength,
                new_strength=new_strength,
                decay_factor=decay_factor,
                reason=(f"decay (hours={hours_elapsed:.2f}, half_life={config.half_life_hours:.1f})"),
            ),
        )
        ctx.stats.updated += 1
        ctx.stats.avg_decay_factor += decay_factor
        ctx.stats.trend_counts[trend] = ctx.stats.trend_counts.get(trend, 0) + 1

    @staticmethod
    def _insert_event(conn: sqlite3.Connection, *, params: DecayEventParams) -> None:
        conn.execute(
            """
            INSERT INTO memory_decay_events (
                tenant_id, user_id, memory_id, event_type,
                old_strength, new_strength, decay_factor, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                params.tenant_id,
                params.user_id,
                params.memory_id,
                params.event_type,
                params.old_strength,
                params.new_strength,
                params.decay_factor,
                params.reason,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def get_decay_events(
        self,
        user_id: str,
        tenant_id: str | None = None,
        memory_id: str | None = None,
        limit: int = 50,
    ) -> list[StrengthEvent]:
        """Return recent decay events for a user (optionally filtered by memory)."""
        tid = tenant_id or get_current_tenant_id()
        self._validate_ids(user_id, tid)
        if limit <= 0:
            raise DecayModelError("limit must be > 0")
        conn = self._connect()
        try:
            if memory_id is not None:
                rows = conn.execute(
                    """
                    SELECT id, tenant_id, user_id, memory_id, event_type,
                           old_strength, new_strength, decay_factor,
                           reason, created_at
                    FROM memory_decay_events
                    WHERE tenant_id = ? AND user_id = ? AND memory_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (tid, user_id, memory_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, tenant_id, user_id, memory_id, event_type,
                           old_strength, new_strength, decay_factor,
                           reason, created_at
                    FROM memory_decay_events
                    WHERE tenant_id = ? AND user_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (tid, user_id, limit),
                ).fetchall()
            return [
                StrengthEvent(
                    id=row["id"],
                    tenant_id=row["tenant_id"],
                    user_id=row["user_id"],
                    memory_id=row["memory_id"],
                    event_type=row["event_type"],
                    old_strength=row["old_strength"],
                    new_strength=row["new_strength"],
                    decay_factor=row["decay_factor"],
                    reason=row["reason"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]
        finally:
            pool = get_pool(self.db_path)
            pool.release(conn)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class _DecayModelSingleton:
    """Module-level singleton for MemoryDecayService."""

    _instance: MemoryDecayService | None = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, db_path: str | None = None) -> MemoryDecayService:
        """Return a process-wide MemoryDecayService."""
        with cls._lock:
            if cls._instance is None:
                if db_path is None:
                    db_path = DB_PATH
                cls._instance = MemoryDecayService(db_path)
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (for tests)."""
        with cls._lock:
            cls._instance = None


def get_decay_model(
    db_path: str | None = None,
) -> MemoryDecayService:
    """Return a process-wide MemoryDecayService.

    ``db_path`` is resolved at call time from the module-level ``DB_PATH``
    binding, which means tests can use ``patch.object(mod, "DB_PATH", ...)``
    to redirect the singleton to a temp database.
    """
    return _DecayModelSingleton.get_instance(db_path)


def reset_decay_model() -> None:
    """Reset the singleton (for tests)."""
    _DecayModelSingleton.reset()
