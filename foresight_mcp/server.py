#!/usr/bin/env python3
"""
Foresight MCP Server - Full memory system with psychological safety features.
Restored from src/lib/ai/memory/ architecture.
"""
from __future__ import annotations

import os
import sqlite3
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

from fastmcp import FastMCP

# Import restored memory system components
from .memory_types import (
    MemoryObject, EmotionalMetadata,
    EmpathyMetrics
)
from .memory_components import (
    MemoryCrisisTagger, SocraticGate, MemorySynthesizer, MemoryLinker
)
from .crisis_detection import get_crisis_service
from .subconscious import get_subconscious_agent
from .event_bus import get_event_bus, memory_stored, memory_retrieved, memory_updated, memory_deleted

# Configuration
DEFAULT_DB_PATH = str(Path.home() / ".foresight" / "memory.db")
DEFAULT_USER_ID = os.environ.get("USER", "user")
DEFAULT_BANK_ID = "default"

DB_PATH = os.environ.get("FORESIGHT_DB_PATH", DEFAULT_DB_PATH)
USER_ID = os.environ.get("FORESIGHT_USER_ID", DEFAULT_USER_ID)
BANK_ID = os.environ.get("FORESIGHT_BANK_ID", DEFAULT_BANK_ID)


def get_db_connection():
    """Get a database connection with row factory."""
    conn = sqlite3.connect(str(Path(DB_PATH)))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database schema with full memory support."""
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db_connection()

    # Drop existing table if schema needs to change (migration)
    conn.execute("DROP TABLE IF EXISTS memories")

    # Main memories table with extended fields
    conn.execute("""
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
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
        synthesized_from TEXT DEFAULT '[]'
    )
    """)

    # Indexes for common queries
    conn.execute('CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_memories_content ON memories(content)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_memories_tags ON memories(tags)')

    conn.commit()
    conn.close()


# Initialize database on module load
init_db()

# Initialize memory system components
_memory_system_initialized = False


def get_memory_system():
    """Get or initialize the memory system components."""
    global _memory_system_initialized
    if not _memory_system_initialized:
        _memory_system_initialized = True
    return {
        'tagger': MemoryCrisisTagger(get_crisis_service('high')),
        'gate': None,  # Created per-evaluate to get fresh tagger
        'synthesizer': MemorySynthesizer(),
        'linker': MemoryLinker(),
    }


mcp = FastMCP("Foresight")


@mcp.tool()
def store_memory(content: str, category: str = "fact",
                 scope: str = "session", retention: str = "short_term",
                 emotional_context: Optional[dict] = None,
                 metrics: Optional[dict] = None,
                 user_id: Optional[str] = None) -> str:
    """
    Store a new memory with full psychological safety features.

    Args:
        content: The memory content to store
        category: Category label (default: "fact")
        scope: Memory scope - session, arc, trait, or fact
        retention: Retention policy - ephemeral, short_term, long_term, or permanent
        emotional_context: Emotional metadata (valence, arousal, dominance, primary_emotion, intensity)
        metrics: Empathy metrics (reciprocity, validation_accuracy, resistance_level)
        user_id: Optional user ID override

    Returns:
        Confirmation with memory ID and gate decision
    """
    memory_id = hashlib.sha256(
        f"{content}{datetime.now().isoformat()}".encode()
    ).hexdigest()[:16]

    uid = user_id or USER_ID

    # Parse emotional context if provided
    emo_ctx = None
    if emotional_context:
        emo_ctx = EmotionalMetadata(**emotional_context)

    # Parse metrics if provided
    met = None
    if metrics:
        met = EmpathyMetrics(**metrics)

    # Create memory object
    memory = MemoryObject.create(
        content=content,
        scope=scope,
        retention=retention,
        emotional_context=emo_ctx,
        metrics=met
    )
    memory.id = memory_id

    # Run through Socratic Gate
    ms = get_memory_system()
    gate = SocraticGate(ms['tagger'])

    import asyncio
    gate_result = asyncio.run(gate.evaluate(memory, uid))

    # Apply tags from gate
    memory.tags = gate_result.suggested_tags

    # Store in database
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO memories (
            id, content, scope, retention, category, user_id, bank_id,
            created_at, tags, emotional_context, metrics, is_ghost, synthesized_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        memory_id, content, scope, retention, category, uid, BANK_ID,
        datetime.now(timezone.utc).isoformat(),
        json.dumps(memory.tags),
        json.dumps(emotional_context or {}),
        json.dumps(metrics or {}),
        0,
        json.dumps([])
    ))
    conn.commit()
    conn.close()

    # Emit event
    event_bus = get_event_bus()
    event_bus.publish(memory_stored(memory_id=memory_id, content=content, actor=uid))

    # Build response
    response = f"Stored memory {memory_id}: {content[:50]}..."
    response += f"\nGate Decision: {gate_result.decision}"
    response += f"\nReason: {gate_result.reason}"
    if gate_result.suggested_tags:
        response += f"\nTags: {', '.join(gate_result.suggested_tags)}"
    if gate_result.anomaly_detected:
        response += "\n⚠️  ANOMALY DETECTED - Review required"

    return response


@mcp.tool()
def query_memories(query: str, user_id: Optional[str] = None,
                   limit: int = 5, offset: int = 0) -> str:
    """Search memories by content using a query string."""
    uid = user_id or USER_ID
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM memories WHERE user_id = ? AND content LIKE ? LIMIT ? OFFSET ?",
        (uid, f"%{query}%", limit, offset)
    ).fetchall()
    conn.close()

    if not rows:
        return f"No memories found matching '{query}'"

    # Emit events for retrieved memories
    event_bus = get_event_bus()
    for r in rows:
        event_bus.publish(memory_retrieved(memory_id=r['id'], query_context=query, actor=uid))

    results = [f"- [{r['id']}] ({r['scope']}/{r['retention']}) {r['content']}" for r in rows]
    return f"Found {len(results)} memories:\n" + "\n".join(results)


@mcp.tool()
def list_memories(user_id: Optional[str] = None,
                  limit: int = 10, offset: int = 0) -> str:
    """List all memories for a user, ordered by creation date."""
    uid = user_id or USER_ID
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM memories WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (uid, limit, offset)
    ).fetchall()
    conn.close()

    if not rows:
        return "No memories found."

    results = [f"- [{r['id']}] ({r['scope']}/{r['retention']}) {r['content'][:80]}..." for r in rows]
    return f"Memories ({len(results)} shown):\n" + "\n".join(results)


@mcp.tool()
def get_memory(memory_id: str, user_id: Optional[str] = None) -> str:
    """Retrieve a specific memory by its ID with full metadata."""
    uid = user_id or USER_ID
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ?",
        (memory_id, uid)
    ).fetchone()
    conn.close()

    if not row:
        return f"Memory {memory_id} not found."

    # Emit event
    event_bus = get_event_bus()
    event_bus.publish(memory_retrieved(memory_id=memory_id, query_context="", actor=uid))

    # Parse JSON fields
    tags = json.loads(row['tags'])
    emotional_context = json.loads(row['emotional_context'])
    metrics = json.loads(row['metrics'])
    synthesized_from = json.loads(row['synthesized_from'])

    result = f"[{row['id']}] ({row['scope']}/{row['retention']})\n"
    result += f"Content: {row['content']}\n"
    result += f"Tags: {', '.join(tags) if tags else 'none'}\n"
    if emotional_context:
        result += f"Emotional Context: {emotional_context}\n"
    if metrics:
        result += f"Metrics: {metrics}\n"
    if row['vector_id']:
        result += f"Vector ID: {row['vector_id']}\n"
    if row['gist']:
        result += f"Gist: {row['gist']}\n"
    if row['is_ghost']:
        result += "[GHOST NODE - Content archived]"

    return result


@mcp.tool()
def update_memory(memory_id: str, content: Optional[str] = None,
                  category: Optional[str] = None,
                  scope: Optional[str] = None,
                  retention: Optional[str] = None,
                  tags: Optional[List[str]] = None,
                  user_id: Optional[str] = None) -> str:
    """Update an existing memory's content or metadata."""
    uid = user_id or USER_ID
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ?",
        (memory_id, uid)
    ).fetchone()

    if not row:
        conn.close()
        return f"Memory {memory_id} not found."

    updates = []
    values = []

    if content:
        updates.append("content = ?")
        values.append(content)
    if category:
        updates.append("category = ?")
        values.append(category)
    if scope:
        updates.append("scope = ?")
        values.append(scope)
    if retention:
        updates.append("retention = ?")
        values.append(retention)
    if tags:
        updates.append("tags = ?")
        values.append(json.dumps(tags))

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.extend([memory_id, uid])
        conn.execute(
            f"UPDATE memories SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            values
        )
        conn.commit()

    conn.close()

    # Emit event
    event_bus = get_event_bus()
    event_bus.publish(memory_updated(memory_id=memory_id, old_content=row['content'], new_content=content or row['content'], actor=uid))

    return f"Updated memory {memory_id}"


@mcp.tool()
def delete_memory(memory_id: str, user_id: Optional[str] = None) -> str:
    """Delete a memory by its ID."""
    uid = user_id or USER_ID
    conn = get_db_connection()
    row = conn.execute(
        "SELECT id FROM memories WHERE id = ? AND user_id = ?",
        (memory_id, uid)
    ).fetchone()

    if not row:
        conn.close()
        return f"Memory {memory_id} not found."

    # Emit event before deletion
    event_bus = get_event_bus()
    event_bus.publish(memory_deleted(memory_id=memory_id, actor=uid))

    conn.execute("DELETE FROM memories WHERE id = ? AND user_id = ?", (memory_id, uid))
    conn.commit()
    conn.close()
    return f"Deleted memory {memory_id}"


@mcp.tool()
def synthesize_memories(user_id: Optional[str] = None) -> str:
    """
    Run synthesis on all memories to detect stance shifts and merge candidates.

    Returns:
        Synthesis results including merged IDs and detected shifts
    """
    uid = user_id or USER_ID
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM memories WHERE user_id = ? ORDER BY created_at",
        (uid,)
    ).fetchall()
    conn.close()

    if len(rows) < 5:
        return "Not enough memories for synthesis (need 5+, have %d)" % len(rows)

    # Convert to MemoryObject list
    memories = []
    for row in rows:
        emo = json.loads(row['emotional_context']) if row['emotional_context'] else None
        met = json.loads(row['metrics']) if row['metrics'] else None
        emo_obj = EmotionalMetadata(**emo) if emo else None
        met_obj = EmpathyMetrics(**met) if met else None

        mem = MemoryObject(
            id=row['id'],
            timestamp=row['created_at'],
            scope=row['scope'],
            retention=row['retention'],
            content=row['content'],
            tags=json.loads(row['tags']) or [],
            synthesized_from=json.loads(row['synthesized_from']) or [],
            is_ghost=bool(row.get('is_ghost', 0)),
            emotional_context=emo_obj,
            metrics=met_obj,
            vector_id=row.get('vector_id'),
            gist=row.get('gist')
        )
        memories.append(mem)

    # Run synthesis
    ms = get_memory_system()
    import asyncio
    result = asyncio.run(ms['synthesizer'].synthesize(memories))

    if not result:
        return "Synthesis returned no results."

    output = [
        "=== Synthesis Results ===",
        f"Merged memories: {len(result.merged_ids)}",
        f"New memory ID: {result.new_memory_id}",
        f"Compression ratio: {result.compression_ratio:.2f}",
        f"Stance shifts detected: {len(result.stance_shifts)}"
    ]

    if result.stance_shifts:
        output.append("\n--- Stance Shifts ---")
        for shift in result.stance_shifts:
            output.append(
                f"  {shift.attribute}: {shift.old_value:.2f} → {shift.new_value:.2f} "
                f"(Δ={shift.delta:+.2f}, confidence={shift.confidence:.2f})"
            )

    return "\n".join(output)


@mcp.tool()
def archive_memory(memory_id: str, user_id: Optional[str] = None) -> str:
    """
    Archive a memory to a ghost node.
    Requires the memory to have a vector_id.
    """
    uid = user_id or USER_ID
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ?",
        (memory_id, uid)
    ).fetchone()

    if not row:
        conn.close()
        return f"Memory {memory_id} not found."

    if not row.get('vector_id'):
        conn.close()
        return f"Cannot archive memory without vector_id. Embed first."

    # Create ghost node
    ms = get_memory_system()
    ghost = ms['linker'].to_ghost(
        MemoryObject(
            id=row['id'],
            timestamp=row['created_at'],
            scope=row['scope'],
            retention=row['retention'],
            content=row['content'],
            tags=json.loads(row['tags']) or [],
            synthesized_from=json.loads(row['synthesized_from']) or [],
            is_ghost=bool(row.get('is_ghost', 0)),
            vector_id=row['vector_id'],
            gist=row.get('gist')
        )
    )

    # Update database
    conn.execute("""
        UPDATE memories SET content = ?, is_ghost = 1, gist = ?
        WHERE id = ? AND user_id = ?
    """, (ghost.content, ghost.gist, memory_id, uid))
    conn.commit()
    conn.close()

    return f"Archived memory {memory_id} to ghost node. Gist: {ghost.gist}"


@mcp.tool()
def memory_status() -> str:
    """Get the current status of the memory system."""
    conn = get_db_connection()
    count = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE user_id = ?",
        (USER_ID,)
    ).fetchone()[0]

    # Count by scope
    scope_counts = conn.execute(
        "SELECT scope, COUNT(*) FROM memories WHERE user_id = ? GROUP BY scope",
        (USER_ID,)
    ).fetchall()

    # Count crisis signals
    crisis_count = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE user_id = ? AND tags LIKE '%CRISIS%'",
        (USER_ID,)
    ).fetchone()[0]

    conn.close()

    status = {
        "status": "healthy",
        "database": DB_PATH,
        "bank_id": BANK_ID,
        "user_id": USER_ID,
        "memory_count": count,
        "crisis_signals": crisis_count,
        "by_scope": {r[0]: r[1] for r in scope_counts}
    }

    return json.dumps(status, indent=2)


# =============================================================================
# Subconscious Memory Block Tools
# =============================================================================

@mcp.tool()
def get_subconscious_blocks(user_id: Optional[str] = None) -> str:
    """
    Get all subconscious memory blocks.

    Returns:
        JSON list of all non-empty memory blocks
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    blocks = agent.get_all_blocks()
    return json.dumps(blocks, indent=2)


@mcp.tool()
def get_subconscious_block(label: str, user_id: Optional[str] = None) -> str:
    """
    Get a specific subconscious memory block.

    Args:
        label: Block label (guidance, pending_items, project_context,
               session_patterns, user_preferences, self_improvement, tool_guidelines)
        user_id: Optional user ID override

    Returns:
        Block content or not found message
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    content = agent.get_block(label)
    if content:
        return f"[{label}]\n{content}"
    return f"Block '{label}' not found."


@mcp.tool()
def update_subconscious_block(
    label: str,
    content: str,
    user_id: Optional[str] = None
) -> str:
    """
    Update a subconscious memory block.

    Args:
        label: Block label to update
        content: New content for the block
        user_id: Optional user ID override

    Returns:
        Confirmation message
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    agent.update_guidance(content) if label == "guidance" else None
    if label != "guidance":
        agent.state.update_block(label, content)
    return f"Updated block '{label}'"


@mcp.tool()
def add_subconscious_guidance(line: str, user_id: Optional[str] = None) -> str:
    """
    Add a line to the guidance block.

    Args:
        line: Line to append to guidance
        user_id: Optional user ID override

    Returns:
        Confirmation message
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    agent.add_guidance_line(line)
    return f"Added guidance line: {line[:50]}..."


@mcp.tool()
def get_subconscious_whisper(user_id: Optional[str] = None) -> str:
    """
    Get the current whisper injection (guidance in XML format).

    Args:
        user_id: Optional user ID override

    Returns:
        XML formatted whisper message or empty if no guidance
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    whisper = agent.get_whisper()
    if not whisper:
        return "(No active guidance - whisper is empty)"
    return whisper


@mcp.tool()
def get_subconscious_context(user_id: Optional[str] = None) -> str:
    """
    Get all subconscious memory blocks as XML context.

    Args:
        user_id: Optional user ID override

    Returns:
        XML formatted memory blocks
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    return agent.get_full_context()


@mcp.tool()
def reset_subconscious_block(label: str, user_id: Optional[str] = None) -> str:
    """
    Reset a subconscious memory block to default.

    Args:
        label: Block label to reset
        user_id: Optional user ID override

    Returns:
        Confirmation message
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    agent.reset_block(label)
    return f"Reset block '{label}' to default"


@mcp.tool()
def clear_subconscious_block(label: str, user_id: Optional[str] = None) -> str:
    """
    Clear a subconscious memory block.

    Args:
        label: Block label to clear
        user_id: Optional user ID override

    Returns:
        Confirmation message
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    agent.clear_block(label)
    return f"Cleared block '{label}'"


@mcp.tool()
def process_session_transcript(
    session_id: str,
    messages: List[dict],
    project_path: Optional[str] = None,
    user_id: Optional[str] = None
) -> str:
    """
    Process a session transcript and extract memories.

    Args:
        session_id: Unique session identifier
        messages: List of message dicts with role/content
        project_path: Optional project path for context
        user_id: Optional user ID override

    Returns:
        Confirmation message
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)

    import asyncio
    asyncio.run(agent.process_transcript(
        session_id=session_id,
        messages=messages,
        project_path=project_path
    ))

    return f"Processed transcript for session {session_id}"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
