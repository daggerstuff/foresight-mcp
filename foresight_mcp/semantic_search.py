"""
Semantic Vector Search for Memories (MEM-5).

Privacy-first embedding pipeline with a zero-dependency local hash-based
embedder as the default provider. Stores vectors in a `memory_embeddings`
table keyed by memory_id and computes cosine similarity for retrieval.

Providers:
- LocalHashEmbedder: deterministic 384-dim feature-hashing embedder.
  No external API calls, no ML model downloads. Suitable for clinical /
  HIPAA-sensitive deployments where data must never leave the process.

Embeddings are dimension-validated against `embedding_validation.py` so
that swapping in a real model (e.g. all-MiniLM-L6-v2) is a drop-in change.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import sqlite3
import struct
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .config import DB_PATH
from .connection_pool import get_pool
from .embedding_validation import (
    EmbeddingDimensionError,
    validate_embedding_dimension,
)
from .tenant_context import get_current_tenant_id

logger = logging.getLogger("foresight_semantic_search")

LOCAL_HASH_DIM = 384
DEFAULT_PROVIDER = "local-hash"
VALID_PROVIDERS: frozenset[str] = frozenset({DEFAULT_PROVIDER})

MAX_TEXT_LENGTH = 100_000
MAX_USER_ID_LENGTH = 128
MAX_TENANT_ID_LENGTH = 64
MAX_MEMORY_ID_LENGTH = 128

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class SemanticSearchError(ValueError):
    """Raised on invalid input or constraint violations."""


class Embedder(Protocol):
    """Protocol for embedding providers."""

    provider_name: str
    dimension: int

    def embed(self, text: str) -> list[float]: ...


class LocalHashEmbedder:
    """Deterministic 384-dim feature-hashing embedder.

    Tokenizes text into lowercase alnum tokens, applies signed feature
    hashing with murmurhash3-style mixing, and L2-normalizes the result.
    No external dependencies, no network calls, no model downloads.

    Quality: discriminative for short clinical/factual text; not a
    replacement for a learned model. Intended as a privacy-first default
    that can be swapped for a real model via the Embedder protocol.
    """

    provider_name = DEFAULT_PROVIDER
    dimension = LOCAL_HASH_DIM

    _NORM_EPS = 1e-12

    def embed(self, text: str) -> list[float]:
        """Produce a 384-dim unit vector for the given text."""
        if not isinstance(text, str):
            raise SemanticSearchError("text must be a string")
        if len(text) > MAX_TEXT_LENGTH:
            raise SemanticSearchError(f"text exceeds {MAX_TEXT_LENGTH} chars")

        vec = [0.0] * self.dimension
        for token, count in self._token_counts(text).items():
            h1, h2 = self._hash_token(token)
            idx = h1 % self.dimension
            sign = 1.0 if (h2 & 1) == 0 else -1.0
            vec[idx] += sign * (1.0 + math.log1p(count - 1))

        return self._l2_normalize(vec)

    @staticmethod
    def _token_counts(text: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for token in _TOKEN_RE.findall(text.lower()):
            counts[token] = counts.get(token, 0) + 1
        return counts

    @staticmethod
    def _hash_token(token: str) -> tuple[int, int]:
        """Two independent 32-bit hashes for index and sign."""
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        a, b = struct.unpack("<II", digest)
        return a, b

    @staticmethod
    def _l2_normalize(vec: list[float]) -> list[float]:
        norm = math.sqrt(sum(v * v for v in vec))
        if norm < LocalHashEmbedder._NORM_EPS:
            return vec
        return [v / norm for v in vec]


def get_embedder(provider: str = DEFAULT_PROVIDER) -> Embedder:
    """Return an embedder instance for the given provider name."""
    if provider != DEFAULT_PROVIDER:
        raise SemanticSearchError(f"unknown embedder provider {provider!r}; valid: {sorted(VALID_PROVIDERS)}")
    return LocalHashEmbedder()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity for two equal-length vectors."""
    if len(a) != len(b):
        raise SemanticSearchError(f"vector length mismatch: {len(a)} vs {len(b)}")
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def serialize_vector(vec: list[float]) -> bytes:
    """Pack a float vector into compact little-endian float32 bytes."""
    if len(vec) > 65_535:
        raise SemanticSearchError("vector too large to serialize")
    return struct.pack(f"<{len(vec)}f", *vec)


def deserialize_vector(blob: bytes, expected_dim: int) -> list[float]:
    """Unpack a float32 blob into a list, validating dimension."""
    if len(blob) != expected_dim * 4:
        raise SemanticSearchError(f"vector blob size {len(blob)} != expected {expected_dim * 4}")
    return list(struct.unpack(f"<{expected_dim}f", blob))


@dataclass
class SemanticMatch:
    """A single semantic search match."""

    memory_id: str
    score: float
    provider: str
    dimension: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "score": round(self.score, 6),
            "provider": self.provider,
            "dimension": self.dimension,
        }


@dataclass
class SemanticSearchResult:
    """Result of a semantic vector search."""

    query: str
    provider: str
    dimension: int
    matches: list[SemanticMatch] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "provider": self.provider,
            "dimension": self.dimension,
            "matches": [m.to_dict() for m in self.matches],
        }


def _validate_user_tenant(user_id: str, tenant_id: str) -> None:
    if not user_id or len(user_id) > MAX_USER_ID_LENGTH:
        raise SemanticSearchError(f"user_id must be 1-{MAX_USER_ID_LENGTH} chars")
    if not tenant_id or len(tenant_id) > MAX_TENANT_ID_LENGTH:
        raise SemanticSearchError(f"tenant_id must be 1-{MAX_TENANT_ID_LENGTH} chars")


def _validate_memory_id(memory_id: str) -> None:
    if not memory_id or len(memory_id) > MAX_MEMORY_ID_LENGTH:
        raise SemanticSearchError(f"memory_id must be 1-{MAX_MEMORY_ID_LENGTH} chars")


class SemanticSearch:
    """SQLite-backed semantic vector store with pluggable embedder."""

    def __init__(
        self,
        db_path: str,
        embedder: Embedder | None = None,
        provider: str = DEFAULT_PROVIDER,
    ) -> None:
        self.db_path = db_path
        self.provider = provider
        self.embedder: Embedder = embedder or get_embedder(provider)
        if self.embedder.provider_name != provider:
            raise SemanticSearchError(
                f"embedder.provider_name {self.embedder.provider_name!r} does not match requested provider {provider!r}"
            )
        self.dimension = self.embedder.dimension
        self._lock = threading.Lock()
        self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_embeddings (
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
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_user "
                "ON memory_embeddings(tenant_id, user_id, provider)"
            )
            conn.commit()
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()

    def index_memory(
        self,
        memory_id: str,
        text: str,
        user_id: str,
        tenant_id: str | None = None,
        provider: str | None = None,
    ) -> int:
        """Compute and store (or replace) the embedding for a memory."""
        _validate_memory_id(memory_id)
        if text is None or not text.strip():
            raise SemanticSearchError("text must be a non-empty string")
        tid = tenant_id or get_current_tenant_id()
        _validate_user_tenant(user_id, tid)
        prov = provider or self.provider
        embedder = self.embedder if prov == self.provider else get_embedder(prov)
        vec = embedder.embed(text)
        try:
            validate_embedding_dimension(vec, expected_dimension=embedder.dimension)
        except EmbeddingDimensionError as exc:
            raise SemanticSearchError(str(exc)) from exc

        blob = serialize_vector(vec)
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            with self._lock:
                conn.execute(
                    """
                    INSERT INTO memory_embeddings (
                        memory_id, tenant_id, user_id,
                        provider, dimension, vector,
                        model_version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, '1', ?, ?)
                    ON CONFLICT(tenant_id, user_id, memory_id, provider)
                    DO UPDATE SET
                        vector = excluded.vector,
                        dimension = excluded.dimension,
                        updated_at = excluded.updated_at
                    """,
                    (
                        memory_id,
                        tid,
                        user_id,
                        prov,
                        embedder.dimension,
                        blob,
                        now,
                        now,
                    ),
                )
                conn.commit()
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()

        return embedder.dimension

    def delete_memory_embedding(
        self,
        memory_id: str,
        user_id: str,
        tenant_id: str | None = None,
        provider: str | None = None,
    ) -> int:
        """Remove the embedding for a memory. Returns rows deleted."""
        _validate_memory_id(memory_id)
        tid = tenant_id or get_current_tenant_id()
        _validate_user_tenant(user_id, tid)
        prov = provider or self.provider
        conn = self._connect()
        try:
            with self._lock:
                cur = conn.execute(
                    """
                    DELETE FROM memory_embeddings
                    WHERE tenant_id = ? AND user_id = ?
                      AND memory_id = ? AND provider = ?
                    """,
                    (tid, user_id, memory_id, prov),
                )
                conn.commit()
                return cur.rowcount
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()

    def search(  # noqa: PLR0913
        self,
        query: str,
        user_id: str,
        tenant_id: str | None = None,
        limit: int = 10,
        min_score: float = 0.0,
        provider: str | None = None,
    ) -> SemanticSearchResult:
        """Semantic search by cosine similarity over stored vectors."""
        if not query or not query.strip():
            raise SemanticSearchError("query must be a non-empty string")
        if limit < 1 or limit > 1000:
            raise SemanticSearchError("limit must be in [1, 1000]")
        if min_score < -1.0 or min_score > 1.0:
            raise SemanticSearchError("min_score must be in [-1.0, 1.0]")
        tid = tenant_id or get_current_tenant_id()
        _validate_user_tenant(user_id, tid)
        prov = provider or self.provider
        embedder = self.embedder if prov == self.provider else get_embedder(prov)

        query_vec = embedder.embed(query)
        try:
            validate_embedding_dimension(query_vec, expected_dimension=embedder.dimension)
        except EmbeddingDimensionError as exc:
            raise SemanticSearchError(str(exc)) from exc

        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT memory_id, vector, dimension
                FROM memory_embeddings
                WHERE tenant_id = ? AND user_id = ? AND provider = ?
                """,
                (tid, user_id, prov),
            ).fetchall()
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()

        matches: list[SemanticMatch] = []
        for r in rows:
            dim = int(r["dimension"])
            if dim != embedder.dimension:
                logger.warning(
                    "Skipping memory %s: dim %d != embedder dim %d",
                    r["memory_id"],
                    dim,
                    embedder.dimension,
                )
                continue
            vec = deserialize_vector(bytes(r["vector"]), dim)
            score = cosine_similarity(query_vec, vec)
            if score >= min_score:
                matches.append(
                    SemanticMatch(
                        memory_id=r["memory_id"],
                        score=score,
                        provider=prov,
                        dimension=dim,
                    )
                )

        matches.sort(key=lambda m: m.score, reverse=True)
        return SemanticSearchResult(
            query=query,
            provider=prov,
            dimension=embedder.dimension,
            matches=matches[:limit],
        )


class _SemanticSearchSingleton:
    """Module-level singleton for SemanticSearch."""

    _instance: SemanticSearch | None = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, provider: str = DEFAULT_PROVIDER) -> SemanticSearch:
        """Return the process-singleton SemanticSearch, initializing lazily."""
        with cls._lock:
            if cls._instance is None or cls._instance.provider != provider:
                cls._instance = SemanticSearch(DB_PATH, provider=provider)
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (test-only helper)."""
        with cls._lock:
            cls._instance = None


def get_semantic_search(provider: str = DEFAULT_PROVIDER) -> SemanticSearch:
    """Return the process-singleton SemanticSearch, initializing lazily."""
    return _SemanticSearchSingleton.get_instance(provider)


def reset_semantic_search() -> None:
    """Reset the singleton (test-only helper)."""
    _SemanticSearchSingleton.reset()
