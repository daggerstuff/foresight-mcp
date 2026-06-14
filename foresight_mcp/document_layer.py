"""
Document Layer for MEM-7.

Separates raw source content from extracted memories. A `documents` row
holds the canonical source text (transcript, article, journal entry);
a `document_chunks` row references a memory id and records the
character offset span within the document that produced it.

This lets us:
- Re-derive memories from source if the extraction algorithm improves
- Audit which memories came from which document
- Skip re-extraction when the source is unchanged (content_hash)
- Async/background re-extraction when the algorithm changes

Extraction is deliberately synchronous and minimal: paragraph-based
chunking with a soft character budget per chunk. The function is the
integration seam for a future async/LLM-based extractor (marked TODO).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import DB_PATH
from .connection_pool import get_pool
from .tenant_context import get_current_tenant_id


@dataclass
class DocumentCreateOptions:
    """Options for creating a document."""

    source: str = "note"
    tenant_id: str | None = None
    metadata: dict[str, Any] | None = None
    char_budget: int = 800
    memory_id_for_chunk: Any = None


logger = logging.getLogger("foresight_document_layer")

DEFAULT_CHUNK_CHAR_BUDGET = 800
MIN_CHUNK_CHAR_BUDGET = 100
MAX_CHUNK_CHAR_BUDGET = 8_000
MAX_TEXT_LENGTH = 200_000
MAX_TITLE_LENGTH = 500
MAX_USER_ID_LENGTH = 128
MAX_TENANT_ID_LENGTH = 64
MAX_MEMORY_ID_LENGTH = 128
MAX_DOCUMENT_ID_LENGTH = 128

VALID_DOCUMENT_SOURCES: frozenset[str] = frozenset({"transcript", "article", "journal", "note", "email", "other"})


class DocumentLayerError(ValueError):
    """Raised on invalid document input or constraint violations."""


def _validate_user_tenant(user_id: str, tenant_id: str) -> None:
    if not user_id or len(user_id) > MAX_USER_ID_LENGTH:
        raise DocumentLayerError(f"user_id must be 1-{MAX_USER_ID_LENGTH} chars")
    if not tenant_id or len(tenant_id) > MAX_TENANT_ID_LENGTH:
        raise DocumentLayerError(f"tenant_id must be 1-{MAX_TENANT_ID_LENGTH} chars")


def _validate_memory_id(memory_id: str) -> None:
    if not memory_id or len(memory_id) > MAX_MEMORY_ID_LENGTH:
        raise DocumentLayerError(f"memory_id must be 1-{MAX_MEMORY_ID_LENGTH} chars")


def _validate_source(source: str) -> None:
    if source not in VALID_DOCUMENT_SOURCES:
        raise DocumentLayerError(f"source must be one of {sorted(VALID_DOCUMENT_SOURCES)}, got {source!r}")


def _validate_budget(budget: int) -> None:
    if budget < MIN_CHUNK_CHAR_BUDGET or budget > MAX_CHUNK_CHAR_BUDGET:
        raise DocumentLayerError(f"chunk_char_budget must be in [{MIN_CHUNK_CHAR_BUDGET}, {MAX_CHUNK_CHAR_BUDGET}]")


def content_hash(text: str) -> str:
    """Stable SHA-256 hex digest of the document's raw text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class DocumentChunk:
    """A single chunk produced from a document."""

    document_id: str
    memory_id: str
    start_offset: int
    end_offset: int
    text: str
    chunk_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "memory_id": self.memory_id,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "text": self.text,
            "chunk_index": self.chunk_index,
        }


@dataclass
class Document:
    """A raw source document."""

    id: str
    tenant_id: str
    user_id: str
    title: str
    source: str
    content: str
    content_hash: str
    char_count: int
    chunk_count: int
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "title": self.title,
            "source": self.source,
            "content_hash": self.content_hash,
            "char_count": self.char_count,
            "chunk_count": self.chunk_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }


def chunk_text(text: str, char_budget: int = DEFAULT_CHUNK_CHAR_BUDGET) -> list[tuple[int, int, str]]:
    """Paragraph-based chunking with a soft character budget.

    Splits on blank lines, then greedily packs paragraphs into chunks
    that fit within `char_budget` characters. Hard-overflow chunks are
    emitted as-is when a single paragraph exceeds the budget.

    Returns a list of (start_offset, end_offset, chunk_text) tuples
    with offsets in the original `text` (end is exclusive, character-based).
    """
    _validate_budget(char_budget)
    if not text:
        return []

    paragraphs: list[tuple[int, int, str]] = []
    cursor = 0
    n = len(text)
    while cursor < n:
        while cursor < n and text[cursor] == "\n":
            cursor += 1
        if cursor >= n:
            break
        para_break = text.find("\n\n", cursor)
        if para_break == -1:
            para_break = n
        chunk_str = text[cursor:para_break].rstrip("\n")
        if chunk_str:
            paragraphs.append((cursor, para_break, chunk_str))
        cursor = para_break
        while cursor < n and text[cursor] == "\n":
            cursor += 1

    chunks: list[tuple[int, int, str]] = []
    buf_paras: list[str] = []
    buf_start: int = 0
    buf_end: int = 0
    buf_len: int = 0

    def _flush() -> None:
        nonlocal buf_paras, buf_start, buf_end, buf_len
        if buf_paras:
            chunks.append((buf_start, buf_end, "\n\n".join(buf_paras)))
        buf_paras = []
        buf_start = 0
        buf_end = 0
        buf_len = 0

    for start, end, para in paragraphs:
        para_len = len(para)
        if para_len > char_budget:
            _flush()
            chunks.append((start, end, para))
            continue
        separator = 2 if buf_paras else 0
        if buf_paras and buf_len + separator + para_len > char_budget:
            _flush()
        if not buf_paras:
            buf_start = start
        buf_paras.append(para)
        buf_end = end
        buf_len += separator + para_len

    _flush()
    return chunks


def extract_memories_from_text(
    text: str,
    char_budget: int = DEFAULT_CHUNK_CHAR_BUDGET,
) -> list[DocumentChunk]:
    """Produce chunks from raw text without persisting anything.

    Synthesizes a placeholder document_id ("pending") and memory_id
    per chunk; callers that want to persist should call
    `DocumentStore.create_document` with the original text instead.

    TODO: Replace the chunk-text-as-memory heuristic with a real
    LLM-based extractor behind an async boundary.
    """
    raw = chunk_text(text, char_budget=char_budget)
    return [
        DocumentChunk(
            document_id="pending",
            memory_id="pending",
            start_offset=start,
            end_offset=end,
            text=chunk,
            chunk_index=i,
        )
        for i, (start, end, chunk) in enumerate(raw)
    ]


class DocumentStore:
    """SQLite-backed document + chunk store."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._ensure_tables()

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
                CREATE TABLE IF NOT EXISTS documents (
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
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS document_chunks (
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
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(tenant_id, user_id, created_at DESC)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(tenant_id, user_id, content_hash)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_document_chunks_doc ON document_chunks(document_id, chunk_index)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_document_chunks_memory ON document_chunks(memory_id)")
            conn.commit()
        finally:
            pool = get_pool(self.db_path)
            pool.release(conn)
            conn.close()

    def _validate_create_params(self, title: str, content: str, options: DocumentCreateOptions) -> None:
        """Validate parameters for document creation."""
        if not title or len(title) > MAX_TITLE_LENGTH:
            raise DocumentLayerError(f"title must be 1-{MAX_TITLE_LENGTH} chars")
        if not isinstance(content, str) or not content:
            raise DocumentLayerError("content must be a non-empty string")
        if len(content) > MAX_TEXT_LENGTH:
            raise DocumentLayerError(f"content exceeds {MAX_TEXT_LENGTH} chars")
        _validate_source(options.source)
        _validate_budget(options.char_budget)

    def _create_chunks(
        self, doc_id: str, content: str, user_id: str, tenant_id: str, now: str, options: DocumentCreateOptions
    ) -> list[DocumentChunk]:
        """Create document chunks from content."""
        raw_chunks = chunk_text(content, char_budget=options.char_budget)
        chunks: list[DocumentChunk] = []
        for i, (start, end, text) in enumerate(raw_chunks):
            if callable(options.memory_id_for_chunk):
                mid = options.memory_id_for_chunk(i, text)
                if mid is not None:
                    _validate_memory_id(str(mid))
            elif isinstance(options.memory_id_for_chunk, str):
                mid = options.memory_id_for_chunk
            else:
                mid = None
            chunks.append(
                DocumentChunk(
                    document_id=doc_id,
                    memory_id=str(mid) if mid is not None else "",
                    start_offset=start,
                    end_offset=end,
                    text=text,
                    chunk_index=i,
                )
            )
        return chunks

    def create_document(
        self,
        title: str,
        content: str,
        user_id: str,
        source: str = "note",
        tenant_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        char_budget: int = DEFAULT_CHUNK_CHAR_BUDGET,
        memory_id_for_chunk: Any = None,
        *,
        options: DocumentCreateOptions | None = None,
    ) -> tuple[Document, list[DocumentChunk]]:
        """Persist a new document and its extracted chunks.

        `memory_id_for_chunk` is optional: if callable, it is invoked
        with (chunk_index, text) and must return a memory_id string.
        If a string, it is used for all chunks. If None, chunks are
        stored without a memory_id (extraction stub mode).
        """
        # Backward compatibility: if options is provided, use it; otherwise build from individual parameters
        if options is None:
            options = DocumentCreateOptions(
                source=source,
                tenant_id=tenant_id,
                metadata=metadata,
                char_budget=char_budget,
                memory_id_for_chunk=memory_id_for_chunk,
            )
        self._validate_create_params(title, content, options)
        tid = options.tenant_id or get_current_tenant_id()
        _validate_user_tenant(user_id, tid)
        meta = options.metadata or {}

        doc_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        h = content_hash(content)

        chunks = self._create_chunks(doc_id, content, user_id, tid, now, options)

        conn = self._connect()
        try:
            with self._lock:
                existing = conn.execute(
                    """
                    SELECT id FROM documents
                    WHERE tenant_id = ? AND user_id = ? AND content_hash = ?
                    """,
                    (tid, user_id, h),
                ).fetchone()
                if existing is not None:
                    raise DocumentLayerError(f"document with identical content already exists: {existing['id']}")

                conn.execute(
                    """
                    INSERT INTO documents (
                        id, tenant_id, user_id, title, source,
                        content, content_hash, char_count, chunk_count,
                        created_at, updated_at, metadata
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        tid,
                        user_id,
                        title,
                        options.source,
                        content,
                        h,
                        len(content),
                        len(chunks),
                        now,
                        now,
                        json.dumps(meta, ensure_ascii=False),
                    ),
                )

                for c in chunks:
                    conn.execute(
                        """
                        INSERT INTO document_chunks (
                            document_id, tenant_id, user_id,
                            memory_id, chunk_index, start_offset,
                            end_offset, text, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            doc_id,
                            tid,
                            user_id,
                            c.memory_id or None,
                            c.chunk_index,
                            c.start_offset,
                            c.end_offset,
                            c.text,
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

        doc = Document(
            id=doc_id,
            tenant_id=tid,
            user_id=user_id,
            title=title,
            source=options.source,
            content=content,
            content_hash=h,
            char_count=len(content),
            chunk_count=len(chunks),
            created_at=now,
            updated_at=now,
            metadata=meta,
        )
        return doc, chunks

    def get_document(
        self,
        document_id: str,
        user_id: str,
        tenant_id: str | None = None,
    ) -> Document | None:
        tid = tenant_id or get_current_tenant_id()
        _validate_user_tenant(user_id, tid)
        if not document_id or len(document_id) > MAX_DOCUMENT_ID_LENGTH:
            raise DocumentLayerError(f"document_id must be 1-{MAX_DOCUMENT_ID_LENGTH} chars")
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT id, tenant_id, user_id, title, source,
                       content, content_hash, char_count, chunk_count,
                       created_at, updated_at, metadata
                FROM documents
                WHERE id = ? AND tenant_id = ? AND user_id = ?
                """,
                (document_id, tid, user_id),
            ).fetchone()
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()
        if row is None:
            return None
        meta_raw = row["metadata"]
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        return Document(
            id=row["id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            title=row["title"],
            source=row["source"],
            content=row["content"],
            content_hash=row["content_hash"],
            char_count=int(row["char_count"]),
            chunk_count=int(row["chunk_count"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=meta,
        )

    def list_chunks(
        self,
        document_id: str,
        user_id: str,
        tenant_id: str | None = None,
    ) -> list[DocumentChunk]:
        tid = tenant_id or get_current_tenant_id()
        _validate_user_tenant(user_id, tid)
        if not document_id or len(document_id) > MAX_DOCUMENT_ID_LENGTH:
            raise DocumentLayerError(f"document_id must be 1-{MAX_DOCUMENT_ID_LENGTH} chars")
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT document_id, memory_id, chunk_index,
                       start_offset, end_offset, text
                FROM document_chunks
                WHERE document_id = ? AND tenant_id = ? AND user_id = ?
                ORDER BY chunk_index ASC
                """,
                (document_id, tid, user_id),
            ).fetchall()
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()
        return [
            DocumentChunk(
                document_id=r["document_id"],
                memory_id=r["memory_id"] or "",
                start_offset=int(r["start_offset"]),
                end_offset=int(r["end_offset"]),
                text=r["text"],
                chunk_index=int(r["chunk_index"]),
            )
            for r in rows
        ]

    def get_memory_source(
        self,
        memory_id: str,
        user_id: str,
        tenant_id: str | None = None,
    ) -> tuple[Document, DocumentChunk] | None:
        """Reverse-lookup: given a memory_id, return its source doc + chunk."""
        _validate_memory_id(memory_id)
        tid = tenant_id or get_current_tenant_id()
        _validate_user_tenant(user_id, tid)
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT d.id, d.tenant_id, d.user_id, d.title, d.source,
                       d.content, d.content_hash, d.char_count, d.chunk_count,
                       d.created_at, d.updated_at, d.metadata,
                       c.memory_id, c.chunk_index, c.start_offset,
                       c.end_offset, c.text
                FROM document_chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.memory_id = ? AND c.tenant_id = ? AND c.user_id = ?
                """,
                (memory_id, tid, user_id),
            ).fetchone()
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()
        if row is None:
            return None
        meta_raw = row["metadata"]
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        doc = Document(
            id=row["id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            title=row["title"],
            source=row["source"],
            content=row["content"],
            content_hash=row["content_hash"],
            char_count=int(row["char_count"]),
            chunk_count=int(row["chunk_count"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=meta,
        )
        chunk = DocumentChunk(
            document_id=row["id"],
            memory_id=row["memory_id"] or "",
            start_offset=int(row["start_offset"]),
            end_offset=int(row["end_offset"]),
            text=row["text"],
            chunk_index=int(row["chunk_index"]),
        )
        return doc, chunk

    def delete_document(
        self,
        document_id: str,
        user_id: str,
        tenant_id: str | None = None,
    ) -> int:
        tid = tenant_id or get_current_tenant_id()
        _validate_user_tenant(user_id, tid)
        if not document_id or len(document_id) > MAX_DOCUMENT_ID_LENGTH:
            raise DocumentLayerError(f"document_id must be 1-{MAX_DOCUMENT_ID_LENGTH} chars")
        conn = self._connect()
        try:
            with self._lock:
                cur = conn.execute(
                    """
                    DELETE FROM documents
                    WHERE id = ? AND tenant_id = ? AND user_id = ?
                    """,
                    (document_id, tid, user_id),
                )
                conn.commit()
                return cur.rowcount
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()


class _DocumentStoreSingleton:
    """Module-level singleton for DocumentStore."""

    _instance: DocumentStore | None = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> DocumentStore:
        """Return the process-singleton DocumentStore, initializing lazily."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = DocumentStore(DB_PATH)
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (test-only helper)."""
        with cls._lock:
            cls._instance = None


def get_document_store() -> DocumentStore:
    """Return the process-singleton DocumentStore, initializing lazily."""
    return _DocumentStoreSingleton.get_instance()


def reset_document_store() -> None:
    """Reset the singleton (test-only helper)."""
    _DocumentStoreSingleton.reset()
