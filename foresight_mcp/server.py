#!/usr/bin/env python3
"""Foresight MCP Server - Simple, persistent memory for Claude Code."""
from __future__ import annotations

import os
import sqlite3
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

# Configuration
DEFAULT_DB_PATH = str(Path.home() / ".foresight" / "memory.db")
DEFAULT_USER_ID = os.environ.get("USER", "user")
DEFAULT_BANK_ID = "default"

DB_PATH = os.environ.get("FORESIGHT_DB_PATH", DEFAULT_DB_PATH)
USER_ID = os.environ.get("FORESIGHT_USER_ID", DEFAULT_USER_ID)
BANK_ID = os.environ.get("FORESIGHT_BANK_ID", DEFAULT_BANK_ID)

def get_db_connection():
    conn = sqlite3.connect(str(Path(DB_PATH)))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            category TEXT DEFAULT 'fact',
            user_id TEXT DEFAULT 'default',
            bank_id TEXT DEFAULT 'default',
            created_at TEXT NOT NULL,
            updated_at TEXT,
            metadata TEXT DEFAULT '{}'
        )
    """)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_memories_content ON memories(content)')
    conn.commit()
    conn.close()

init_db()

mcp = FastMCP("Foresight")

@mcp.tool()
def store_memory(content: str, category: str = "fact", user_id: Optional[str] = None) -> str:
    memory_id = hashlib.sha256(f"{content}{datetime.now().isoformat()}".encode()).hexdigest()[:16]
    uid = user_id or USER_ID
    conn = get_db_connection()
    conn.execute("INSERT INTO memories (id, content, category, user_id, bank_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (memory_id, content, category, uid, BANK_ID, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    return f"Stored memory {memory_id}: {content[:50]}..."

@mcp.tool()
def query_memories(query: str, user_id: Optional[str] = None, limit: int = 5, offset: int = 0) -> str:
    uid = user_id or USER_ID
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM memories WHERE user_id = ? AND content LIKE ? LIMIT ? OFFSET ?",
                        (uid, f"%{query}%", limit, offset)).fetchall()
    conn.close()
    if not rows: return f"No memories found matching '{query}'"
    results = [f"- [{r['id']}] ({r['category']}) {r['content']}" for r in rows]
    return f"Found {len(results)} memories:\n" + "\n".join(results)

@mcp.tool()
def list_memories(user_id: Optional[str] = None, limit: int = 10, offset: int = 0) -> str:
    uid = user_id or USER_ID
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM memories WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                        (uid, limit, offset)).fetchall()
    conn.close()
    if not rows: return "No memories found."
    results = [f"- [{r['id']}] ({r['category']}) {r['content'][:100]}..." for r in rows]
    return f"Memories ({len(results)} shown):\n" + "\n".join(results)

@mcp.tool()
def get_memory(memory_id: str, user_id: Optional[str] = None) -> str:
    uid = user_id or USER_ID
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM memories WHERE id = ? AND user_id = ?", (memory_id, uid)).fetchone()
    conn.close()
    if not row: return f"Memory {memory_id} not found."
    return f"[{row['id']}] ({row['category']}) {row['content']}"

@mcp.tool()
def update_memory(memory_id: str, content: Optional[str] = None, category: Optional[str] = None, user_id: Optional[str] = None) -> str:
    uid = user_id or USER_ID
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM memories WHERE id = ? AND user_id = ?", (memory_id, uid)).fetchone()
    if not row: return f"Memory {memory_id} not found."
    updates, values = [], []
    if content: updates.append("content = ?"); values.append(content)
    if category: updates.append("category = ?"); values.append(category)
    if updates:
        updates.append("updated_at = ?"); values.append(datetime.now(timezone.utc).isoformat())
        values.extend([memory_id, uid])
        conn.execute(f"UPDATE memories SET {', '.join(updates)} WHERE id = ? AND user_id = ?", values)
        conn.commit()
    conn.close()
    return f"Updated memory {memory_id}"

@mcp.tool()
def delete_memory(memory_id: str, user_id: Optional[str] = None) -> str:
    uid = user_id or USER_ID
    conn = get_db_connection()
    row = conn.execute("SELECT id FROM memories WHERE id = ? AND user_id = ?", (memory_id, uid)).fetchone()
    if not row: return f"Memory {memory_id} not found."
    conn.execute("DELETE FROM memories WHERE id = ? AND user_id = ?", (memory_id, uid))
    conn.commit()
    conn.close()
    return f"Deleted memory {memory_id}"

@mcp.tool()
def memory_status() -> str:
    conn = get_db_connection()
    count = conn.execute("SELECT COUNT(*) FROM memories WHERE user_id = ?", (USER_ID,)).fetchone()[0]
    conn.close()
    return json.dumps({"status": "healthy", "database": DB_PATH, "bank_id": BANK_ID, "user_id": USER_ID, "memory_count": count}, indent=2)

def main():
    mcp.run()

if __name__ == "__main__":
    main()
