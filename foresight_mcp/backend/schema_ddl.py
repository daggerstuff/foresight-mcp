"""Portable schema DDL for Foresight MCP.

Single source of truth for all CREATE TABLE / ALTER TABLE / CREATE INDEX
statements that Foresight needs to bootstrap a fresh database. The same
set is used by both the SQLite default and the PostgreSQL (psycopg v3)
deployment; ``PostgresBackend._translate_sql`` then performs the dialect
rewrite (``?`` → ``%s``, ``INTEGER PRIMARY KEY AUTOINCREMENT`` → ``SERIAL``,
``BLOB`` → ``BYTEA``) so we do not need two parallel DDL trees.

The dictionary is intentionally keyed by an integer version so the
migration runner (``backend_migrations.run_migrations``) can apply
versions sequentially and record each in ``schema_migrations``.

Anything database-portability-sensitive lives here; nothing else in the
codebase should hard-code per-table CREATE TABLE strings.
"""

from __future__ import annotations

# Ordered by version (Phase N → SQL list). Each list is executed in a
# transaction by ``backend_migrations.run_migrations``; statements that are
# already applied (e.g. duplicate column) are skipped.
MIGRATIONS: dict[int, list[str]] = {
    1: [
        """CREATE TABLE IF NOT EXISTS tenants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            rate_limit INTEGER DEFAULT 100,
            burst_limit INTEGER DEFAULT 20,
            created_at TEXT NOT NULL,
            config TEXT DEFAULT '{}'
        )""",
        """CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            scope TEXT DEFAULT 'session',
            retention TEXT DEFAULT 'short_term',
            category TEXT DEFAULT 'fact',
            user_id TEXT DEFAULT 'default',
            bank_id TEXT DEFAULT 'default',
            created_at TEXT NOT NULL,
            updated_at TEXT,
            tags TEXT DEFAULT '[]',
            emotional_context TEXT DEFAULT '{}',
            metrics TEXT DEFAULT '{}',
            vector_id TEXT,
            gist TEXT,
            is_ghost INTEGER DEFAULT 0,
            synthesized_from TEXT DEFAULT '[]',
            version INTEGER DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS memory_versions (
            id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            content TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            tags TEXT DEFAULT '[]',
            emotional_context TEXT DEFAULT '{}',
            metrics TEXT DEFAULT '{}',
            rollback_of TEXT DEFAULT NULL,
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_memories_content ON memories(content)",
        "CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope)",
        "CREATE INDEX IF NOT EXISTS idx_memories_tags ON memories(tags)",
        "CREATE INDEX IF NOT EXISTS idx_versions_memory ON memory_versions(memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_versions_tenant ON memory_versions(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_versions_created ON memory_versions(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_tenants_id ON tenants(id)",
    ],
    2: [
        "ALTER TABLE memories ADD COLUMN accessed_at TEXT DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE memories ADD COLUMN importance REAL DEFAULT 1.0",
        "ALTER TABLE memories ADD COLUMN decay_rate REAL DEFAULT 0.01",
        "ALTER TABLE memories ADD COLUMN activation_count INTEGER DEFAULT 0",
        "ALTER TABLE memories ADD COLUMN retrieval_count INTEGER DEFAULT 0",
        "ALTER TABLE memories ADD COLUMN strength_trend TEXT DEFAULT 'stable'",
        "ALTER TABLE memories ADD COLUMN last_retrieved_at TEXT",
        # NOTE: SQLite allows ADD COLUMN for category twice (idempotent if the
        # original table already declared it).  Postgres will error on duplicate
        # column adds — the runner catches that and skips.
        "ALTER TABLE memories ADD COLUMN category TEXT DEFAULT 'general'",
        # Clinical/safety/privacy gating — see PIX-3956. is_sensitive is
        # opt-in, default 0 (false).  Per-tenant because every read filters
        # by (user_id, tenant_id).  Maintenance gates read this column to
        # exclude sensitive memories from auto-consolidation and to forbid
        # any auto-archival of sensitive rows.
        "ALTER TABLE memories ADD COLUMN is_sensitive INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE memories ADD COLUMN sensitivity_reason TEXT",
        "CREATE INDEX IF NOT EXISTS idx_memories_user_created ON memories(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_memories_user_accessed ON memories(user_id, accessed_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(user_id, importance DESC, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_strength_trend ON memories(user_id, strength_trend, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(user_id, category, created_at DESC)",
        """CREATE TABLE IF NOT EXISTS decay_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            half_life_hours REAL DEFAULT 168.0,
            min_importance REAL DEFAULT 0.1,
            activation_boost REAL DEFAULT 1.2,
            strengthening_threshold INTEGER DEFAULT 5,
            stale_threshold REAL DEFAULT 0.2,
            UNIQUE(tenant_id, user_id, category)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_decay_config_tenant ON decay_config(tenant_id)",
    ],
    3: [
        """CREATE TABLE IF NOT EXISTS curation_runs (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            source_bank_id TEXT NOT NULL,
            output_bank_id TEXT NOT NULL,
            policy_mode TEXT NOT NULL,
            tool_access TEXT NOT NULL,
            output_mode TEXT NOT NULL,
            status TEXT NOT NULL,
            instructions TEXT,
            summary_json TEXT DEFAULT '{}',
            error_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT,
            archived_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_curation_runs_tenant_user ON curation_runs(tenant_id, user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_curation_runs_status ON curation_runs(tenant_id, user_id, status, created_at DESC)",
    ],
    4: [
        """CREATE TABLE IF NOT EXISTS context_blocks (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            label TEXT NOT NULL,
            content TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, label)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_context_blocks_lookup ON context_blocks(tenant_id, user_id, updated_at DESC)",
    ],
    5: [
        "ALTER TABLE curation_runs ADD COLUMN transcript_bundle_json TEXT",
        "ALTER TABLE curation_runs ADD COLUMN session_id TEXT",
        "ALTER TABLE curation_runs ADD COLUMN project_path TEXT",
    ],
    6: [
        """CREATE TABLE IF NOT EXISTS memory_relationships (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            source_memory_id TEXT NOT NULL,
            target_memory_id TEXT NOT NULL,
            relationship_type TEXT NOT NULL
                CHECK(relationship_type IN (
                    'updates', 'extends', 'derives',
                    'contradicts', 'supports', 'related'
                )),
            confidence REAL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1),
            metadata TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(tenant_id, user_id, source_memory_id, target_memory_id, relationship_type),
            FOREIGN KEY (source_memory_id) REFERENCES memories(id) ON DELETE CASCADE,
            FOREIGN KEY (target_memory_id) REFERENCES memories(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_memory_relationships_source ON memory_relationships(tenant_id, user_id, source_memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_memory_relationships_target ON memory_relationships(tenant_id, user_id, target_memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_memory_relationships_type ON memory_relationships(tenant_id, user_id, relationship_type)",
        "ALTER TABLE memories ADD COLUMN relation_type TEXT",
        "ALTER TABLE memories ADD COLUMN related_memory_id TEXT",
        "CREATE INDEX IF NOT EXISTS idx_memories_relation ON memories(tenant_id, user_id, relation_type)",
    ],
    7: [
        """CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            vector BLOB NOT NULL,
            model_version TEXT DEFAULT '1',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, memory_id, provider)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_user ON memory_embeddings(tenant_id, user_id, provider)",
    ],
    8: [
        """CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            char_count INTEGER NOT NULL,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            UNIQUE(tenant_id, user_id, content_hash)
        )""",
        """CREATE TABLE IF NOT EXISTS document_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            memory_id TEXT,
            chunk_index INTEGER NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(document_id, chunk_index),
            FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(tenant_id, user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(tenant_id, user_id, content_hash)",
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_doc ON document_chunks(document_id, chunk_index)",
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_memory ON document_chunks(memory_id)",
    ],
    9: [
        "ALTER TABLE memories ADD COLUMN current_strength REAL",
        "ALTER TABLE memories ADD COLUMN last_decay_at TEXT",
        "CREATE INDEX IF NOT EXISTS idx_memories_strength ON memories(tenant_id, user_id, current_strength)",
        """CREATE TABLE IF NOT EXISTS memory_decay_events (
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
        )""",
        "CREATE INDEX IF NOT EXISTS idx_memory_decay_events_lookup ON memory_decay_events (tenant_id, user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_memory_decay_events_memory ON memory_decay_events (memory_id, created_at DESC)",
    ],
    10: [
        "ALTER TABLE memories ADD COLUMN content_hash TEXT",
        "CREATE INDEX IF NOT EXISTS idx_memories_content_hash ON memories(tenant_id, user_id, content_hash)",
    ],
    11: [
        """CREATE TABLE IF NOT EXISTS injection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            query_length INTEGER NOT NULL,
            latency_ms REAL NOT NULL,
            memories_fetched INTEGER NOT NULL,
            memories_returned INTEGER NOT NULL,
            fast_path INTEGER,
            signal_counts TEXT NOT NULL DEFAULT '{}',
            max_memories_requested INTEGER NOT NULL,
            min_relevance REAL NOT NULL,
            max_chars INTEGER
        )""",
        "CREATE INDEX IF NOT EXISTS idx_injection_runs_lookup ON injection_runs(tenant_id, user_id, created_at DESC)",
    ],
}


__all__ = ["MIGRATIONS"]
