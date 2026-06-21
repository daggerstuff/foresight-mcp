"""One-off script to add tenant_id filters to all remaining query gaps."""
import re

FILES = {}

# ── temporal_service.py ──────────────────────────────────────────────
with open("foresight_mcp/temporal_service.py") as f:
    s = f.read()

# _get_decay_config: add tenant_id param + filter
s = s.replace(
    'def _get_decay_config(self, user_id: str, category: str = "general") -> DecayConfig:',
    'def _get_decay_config(self, user_id: str, category: str = "general", tenant_id: str = "default") -> DecayConfig:',
)
s = s.replace(
    "WHERE user_id = ? AND category = ?\n            \"\"\", (user_id, category))",
    "WHERE user_id = ? AND tenant_id = ? AND category = ?\n            \"\"\", (user_id, tenant_id, category))",
)

# on_memory_retrieved: add tenant_id param + SELECT/UPDATE filters
s = s.replace(
    "def on_memory_retrieved(\n        self,\n        memory_id: str,\n        user_id: str,\n        importance: float = 1.0,\n        activation_boost: float | None = None\n    ) -> tuple[float, FreshnessTrend]:",
    "def on_memory_retrieved(\n        self,\n        memory_id: str,\n        user_id: str,\n        importance: float = 1.0,\n        activation_boost: float | None = None,\n        tenant_id: str = \"default\"\n    ) -> tuple[float, FreshnessTrend]:",
)
s = s.replace(
    "FROM memories WHERE id = ? AND user_id = ?\n                \"\"\", (memory_id, user_id))",
    "FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?\n                \"\"\", (memory_id, user_id, tenant_id))",
)
s = s.replace(
    "WHERE id = ? AND user_id = ? AND tenant_id = ?\n                \"\"\", (\n                datetime.now(timezone.utc).isoformat(),\n                new_activation_count,\n                new_importance,\n                config.min_importance,\n                trend,\n                datetime.now(timezone.utc).isoformat(),\n                memory_id,\n                user_id,\n                tenant_id\n            ))",
    "WHERE id = ? AND user_id = ? AND tenant_id = ?\n                \"\"\", (\n                datetime.now(timezone.utc).isoformat(),\n                new_activation_count,\n                new_importance,\n                config.min_importance,\n                trend,\n                datetime.now(timezone.utc).isoformat(),\n                memory_id,\n                user_id,\n                tenant_id\n            ))",
)

# batch_update_decay: add tenant_id to SELECT and UPDATE
s = s.replace(
    "FROM memories\n                WHERE user_id = ?\n                \"\"\", (user_id,))",
    "FROM memories\n                WHERE user_id = ? AND tenant_id = ?\n                \"\"\", (user_id, tenant_id))",
)
s = s.replace(
    "WHERE id = ? AND user_id = ?\n                \"\"\", (\n                    new_importance,\n                    trend,\n                    datetime.now(timezone.utc).isoformat(),\n                    memory_id,\n                    user_id\n                ))",
    "WHERE id = ? AND user_id = ? AND tenant_id = ?\n                \"\"\", (\n                    new_importance,\n                    trend,\n                    datetime.now(timezone.utc).isoformat(),\n                    memory_id,\n                    user_id,\n                    tenant_id\n                ))",
)

# get_memory_stats: add tenant_id
s = s.replace(
    "FROM memories\n                WHERE user_id = ?\n                \"\"\", (user_id,))",
    "FROM memories\n                WHERE user_id = ? AND tenant_id = ?\n                \"\"\", (user_id, tenant_id))",
)

FILES["foresight_mcp/temporal_service.py"] = s

# ── hybrid_retriever.py: _graph_search entity lookup ────────────────
with open("foresight_mcp/hybrid_retriever.py") as f:
    s = f.read()

s = s.replace(
    "params = [user_id] + [f\"%{t}%\" for t in escaped_terms]",
    "params = [user_id, tenant_id] + [f\"%{t}%\" for t in escaped_terms]",
)
s = s.replace(
    "WHERE e.user_id = ?\n            AND ({like_clauses})",
    "WHERE e.user_id = ? AND e.tenant_id = ?\n            AND ({like_clauses})",
)

FILES["foresight_mcp/hybrid_retriever.py"] = s

# ── reflection_engine.py: _build_entity_summary ──────────────────────
with open("foresight_mcp/reflection_engine.py") as f:
    s = f.read()

# Add tenant_id param
s = s.replace(
    "def _build_entity_summary(self, user_id: str",
    "def _build_entity_summary(self, user_id: str, tenant_id: str = \"default\"",
)
# Add tenant_id filter to entity query
s = s.replace(
    "FROM memory_entities WHERE user_id = ?",
    "FROM memory_entities WHERE user_id = ? AND tenant_id = ?",
)
# Add tenant_id filter to relationship query
s = s.replace(
    "FROM entity_relationships WHERE user_id = ?",
    "FROM entity_relationships WHERE user_id = ? AND tenant_id = ?",
)
# Fix params for both queries (need to find the execute calls)
s = s.replace(
    "\"\"\", (user_id, limit))\n\n    # Get relationships",
    "\"\"\", (user_id, tenant_id, limit))\n\n    # Get relationships",
)
s = s.replace(
    "\"\"\", (user_id, rel_limit))\n\n    return {",
    "\"\"\", (user_id, tenant_id, rel_limit))\n\n    return {",
)

FILES["foresight_mcp/reflection_engine.py"] = s

# ── subconscious.py: key singleton on tenant_id+user_id ─────────────
with open("foresight_mcp/subconscious.py") as f:
    s = f.read()

# Change get_subconscious_agent to accept and use tenant_id
s = s.replace(
    "def get_subconscious_agent(user_id: str",
    "def get_subconscious_agent(user_id: str, tenant_id: str = \"default\"",
)
# Key the cache on (tenant_id, user_id) instead of just user_id
s = s.replace(
    "_key = user_id",
    "_key = (tenant_id, user_id)",
)
s = s.replace(
    "if _key in _subconscious_agents:",
    "if _key in _subconscious_agents:",
)
s = s.replace(
    "agent = _subconscious_agents.get(user_id)",
    "agent = _subconscious_agents.get(_key)",
)
s = s.replace(
    "_subconscious_agents[user_id] = agent",
    "_subconscious_agents[_key] = agent",
)

FILES["foresight_mcp/subconscious.py"] = s

# ── server.py: multiple missing tenant_id filters ───────────────────
with open("foresight_mcp/server.py") as f:
    s = f.read()

# Ensure get_current_tenant_id is imported
if "from .tenant_context import get_current_tenant_id" not in s:
    s = s.replace(
        "from .tenant_context import",
        "from .tenant_context import get_current_tenant_id,",
    ) if "from .tenant_context import" in s else None
    if "from .tenant_context import" not in s:
        # Add import near other tenant imports
        s = s.replace(
            "from .tenant_context",
            "from .tenant_context import get_current_tenant_id\nfrom .tenant_context",
        )

# memory_status: add tenant_id to all 3 queries
s = s.replace(
    '"SELECT COUNT(*) FROM memories WHERE user_id = ?"\n        ).fetchone()[0]',
    '"SELECT COUNT(*) FROM memories WHERE user_id = ? AND tenant_id = ?"\n        ).fetchone()[0]',
)
s = s.replace(
    '"SELECT scope, COUNT(*) FROM memories WHERE user_id = ? GROUP BY scope"\n        ).fetchall()',
    '"SELECT scope, COUNT(*) FROM memories WHERE user_id = ? AND tenant_id = ? GROUP BY scope"\n        ).fetchall()',
)
s = s.replace(
    "\"SELECT COUNT(*) FROM memories WHERE user_id = ? AND tags LIKE '%CRISIS%'\"\n        ).fetchone()[0]",
    "\"SELECT COUNT(*) FROM memories WHERE user_id = ? AND tenant_id = ? AND tags LIKE '%CRISIS%'\"\n        ).fetchone()[0]",
)

# memory_status params: add get_current_tenant_id() to each
# This is tricky - need to replace (USER_ID,) with (USER_ID, get_current_tenant_id())
# but only in the memory_status function area
# Find the three occurrences near memory_status
lines = s.split("\n")
in_status = False
status_start = None
for i, line in enumerate(lines):
    if "def memory_status" in line:
        in_status = True
        status_start = i
    if in_status and "return json.dumps" in line:
        in_status = False
    if in_status and "(USER_ID,)" in line:
        lines[i] = lines[i].replace("(USER_ID,)", "(USER_ID, get_current_tenant_id(),)")
s = "\n".join(lines)

# _bridge_subconscious_to_memories: add tenant_id to dedup SELECT
s = s.replace(
    '"WHERE user_id = ? AND content = ? AND is_ghost = 0 "',
    '"WHERE user_id = ? AND tenant_id = ? AND content = ? AND is_ghost = 0 "',
)
s = s.replace(
    "(uid, content),\n        ).fetchone()",
    "(uid, get_current_tenant_id(), content),\n        ).fetchone()",
)

# _bridge INSERT: add tenant_id column and value
s = s.replace(
    '"(id, content, scope, retention, category, user_id, bank_id, "\n                "created_at',
    '"(id, content, scope, retention, category, user_id, bank_id, tenant_id, "\n                "created_at',
)
s = s.replace(
    "(mid, content, category, uid, BANK_ID, now, now),",
    "(mid, content, category, uid, BANK_ID, get_current_tenant_id(), now, now),",
)

# rollback_to_version: add tenant_id to UPDATE WHERE
s = s.replace(
    "WHERE id = ? AND user_id = ?\n                \"\"\", (\n                    version_row",
    "WHERE id = ? AND user_id = ? AND tenant_id = ?\n                \"\"\", (\n                    version_row",
)

# Find the rollback version update params and add tenant_id
# This one is the most complex - need to add get_current_tenant_id() to the values
lines = s.split("\n")
in_rollback = False
for i, line in enumerate(lines):
    if "def rollback_to_version" in line or "def rollback_memory" in line:
        in_rollback = True
    if in_rollback and "memory_id, uid" in line and "tenant_id" not in line:
        lines[i] = lines[i].replace("memory_id, uid", "memory_id, uid, get_current_tenant_id()")
        in_rollback = False
s = "\n".join(lines)

# update_memory: add tenant_id to dynamic UPDATE WHERE
s = s.replace(
    "WHERE id = ? AND user_id = ?\"",
    "WHERE id = ? AND user_id = ? AND tenant_id = ?\"",
)
s = s.replace(
    "values.extend([memory_id, uid])",
    "values.extend([memory_id, uid, get_current_tenant_id()])",
)

# archive_memory: add tenant_id to UPDATE WHERE
s = s.replace(
    "WHERE id = ? AND user_id = ?\n                \"\"\", (ghost.content, ghost.gist, memory_id, uid))",
    "WHERE id = ? AND user_id = ? AND tenant_id = ?\n                \"\"\", (ghost.content, ghost.gist, memory_id, uid, get_current_tenant_id()))",
)

FILES["foresight_mcp/server.py"] = s

# ── Write all files ──────────────────────────────────────────────────
for path, content in FILES.items():
    with open(path, "w") as f:
        f.write(content)
    print(f"  {path}: OK")

print("\nDone. All tenant_id gaps patched.")
