# Foresight MCP — Deployment Guide

Companion to `INSTALL.md` and `README.md`. This document records the **deploy-time** concerns of a Foresight MCP deployment: environment variables, backend selection, Neon specifics, surgical patches, and known caveats discovered while closing PIX-3996 (Multi-Agent Deployment Verification, Phase 7).

> **Audience**: operators bringing a Foresight MCP instance online against Neon Postgres (or SQLite as a fallback).

---

## 1. Quick Start

```bash
# 1. Fetch the source
git submodule update --init foresight-mcp

# 2. Install runtime + Postgres + Redis deps in the submodule's own .venv
cd foresight-mcp
uv sync --extra postgres              # pulls psycopg, psycopg-binary, psycopg-pool
uv add redis                          # optional: sibling-infrastructure compat

# 3. Export identity + DB URL (NEVER commit these)
export FORESIGHT_DB_URL="postgresql://neondb_owner:<REDACTED>@ep-falling-dew-a8eovkvn-pooler.eastus2.azure.neon.tech/foresight?sslmode=require"
export FORESIGHT_IDENTITY=foresight-prod
export FORESIGHT_BANK_ID=pixelated

# 4. Smoke the backend factory
cd ..
set -a; source .env.local; set +a
( cd foresight-mcp && uv run python -c "from foresight_mcp.backend import create_backend; b=create_backend(); print(type(b).__name__)" )
# expect: PostgresBackend
```

If `FORESIGHT_DB_URL` is unset, the factory falls back to `SqliteBackend()` — see §6 for the SQLite caveat.

---

## 2. Required Environment Variables

| Variable              | Required?   | Purpose                                                                                                                           | Default                         |
| --------------------- | ----------- | --------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| `FORESIGHT_DB_URL`    | No          | Postgres DSN. If unset → SQLite fallback (per §6).                                                                                | _(empty → SQLite)_              |
| `FORESIGHT_DB_PATH`   | No          | Override SQLite file path.                                                                                                        | `~/.foresight/memory.db`        |
| `FORESIGHT_IDENTITY`  | **Yes**     | Logical agent identity propagated to MCP.                                                                                         | _(none — must set)_             |
| `FORESIGHT_BANK_ID`   | Recommended | Tenant/bank namespace for cross-tenant isolation.                                                                                 | _(empty → single-tenant)_       |
| `FORESIGHT_USER_ID`   | Recommended | Stable internal user identifier.                                                                                                  | _(empty → process pid)_         |
| `FORESIGHT_API_URL`   | Recommended | Upstream MCP API base.                                                                                                            | `http://127.0.0.1:54321`        |
| `FORESIGHT_REDIS_URL` | _Optional_  | Redis companion cache URL loaded by `RedisCache` (see §7). Set this directly OR mirror `REDIS_URL` / `REDIS_URL_REMOTE` upstream. | _empty → in-process dict cache_ |
| `REDIS_URL`           | _Optional_  | Local Docker Redis URL (`redis://[:pw]@127.0.0.1:6379`). Smoke source for `FORESIGHT_REDIS_URL`.                                  | _none_                          |
| `REDIS_URL_REMOTE`    | _Optional_  | Upstash Redis URL (`rediss://default:[pw]@host:6379`). Cross-process smoke source for `FORESIGHT_REDIS_URL`.                      | _none_                          |

> **Security**: keep `FORESIGHT_DB_URL` and any Upstash credentials out of git. They belong in `~/.env` for shared team deployment of Foresight→Neon, with optional per-developer override in `~/.env.local` (last-source-wins via `foresight-mcp-server.sh`), or in the deployment platform's secret manager. Both files are gitignored via the `~/.gitignore` rule `/.env*`.

---

## 3. Backend Selection

Selection is purely string-prefix based, executed in `foresight_mcp/backend/__init__.py:create_backend()`:

```python
def create_backend() -> DatabaseBackend:
    db_url = os.environ.get("FORESIGHT_DB_URL", "").strip()
    if db_url.startswith(("postgresql://", "postgres://")):
        return PostgresBackend(dsn=db_url)
    return SqliteBackend()
```

There is no autodetection beyond the URL prefix. **If you set `FORESIGHT_DB_URL=postgresql+psycopg://...` or `pgbouncer://...`, you will silently land on the SQLite branch.** Always use `postgresql://` (the canonical libpq scheme that `psycopg` understands).

---

## 4. Neon Postgres Specifics

Neon's transaction-pooler endpoint wraps pgBouncer in front of the writer. The Flow:

```
your process ──► ep-…-pooler.eastus2.azure.neon.tech:5432 (pgBouncer) ──► writer Neon compute
```

Important properties verified during PIX-3996:

- **`sslmode=require` is mandatory.** Neon refuses non-TLS connections. The factory passes the URL verbatim to `psycopg_pool.ConnectionPool`, so the query string must carry the SSL directive.
- **Two endpoints exist — pick one and stick with it.** `-pooler.eastus2.azure.neon.tech` is the connection-pooler (pgBouncer-mode). Drop the `-pooler` segment to talk directly to the writer (long-lived sessions, e.g. for migrations). Mixing them in one process can yield inconsistent snapshot states.
- **Connection lifetime: ≤ 5 min recommended.** Neon idle-kills pooled connections. Configure your process to reconnect rather than hold sessions open. With `psycopg_pool`, this is automatic — the pool reopens dropped connections transparently.
- **Write/read separation**: not generally required. For high write throughput, prefer the direct writer endpoint.

---

## 5. The Four Surgical Corrections in `postgres_backend.py`

Recorded on `chad/sentry-fixes-round5 @ b445754`. These are corrections that the lineage of the file picked up against psycopg 3.3.x; before this commit they prevented `PostgresBackend` from starting at all.

| Where                  | Before                                                                         | After                                                                           | Why                                                                                                                                                                                             |
| ---------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| L4-5 (docstring)       | `psycopg.pool.ConnectionPool`, `psycopg.rows.DictRow`                          | `psycopg_pool.ConnectionPool`, `psycopg.rows.dict_row`                          | Cosmetic — references the correct packages by their current names.                                                                                                                              |
| L90 (type hint)        | `psycopg.pool.ConnectionPool \| None`                                          | `psycopg_pool.ConnectionPool \| None`                                           | Cosmetic — same reason.                                                                                                                                                                         |
| L92-93 (imports)       | `from psycopg.rows import DictRow` / `from psycopg.pool import ConnectionPool` | `from psycopg.rows import dict_row` / `from psycopg_pool import ConnectionPool` | **Critical**. `DictRow` is a type, not a callable, on psycopg 3.3.x — `pool_kwargs={"row_factory": DictRow}` makes `pool.open()` fail at runtime. `dict_row` is the lowercase factory function. |
| L113-114 (row factory) | `kwargs={"row_factory": DictRow}`                                              | `kwargs={"row_factory": dict_row}`                                              | Carries the import change into the pool's open configuration.                                                                                                                                   |
| L118 (close)           | `self._pool.close()`                                                           | `self._pool.close(timeout=10.0)`                                                | Cosmetic — silences the cosmetic "couldn't stop thread" warning by waiting up to 10s for pool workers to flush. Functionally identical (workers are daemon threads).                            |

If you upgrade `psycopg-pool` past 3.3.x in the future, re-verify these names — they may shift again.

---

## 6. SQLite Fallback — Fixed in this PR

Substep C of PIX-3996 originally failed because of a **pre-existing bug** in `foresight_mcp/backend/sqlite_backend.py`. Substep G (this PR, per user-authorized scope expansion in m0246) corrects it.

**The fix** — two-line surgical patch at `sqlite_backend.py:44-45`:

```diff
   self._pool = ConnectionPool(
       db_path=path,
-      max_size=max_size,
-      max_idle_seconds=max_idle_seconds,
+      max_size=self._max_size,
+      max_idle_seconds=self._max_idle_seconds,
   )
```

The `__init__` parameters `max_size` and `max_idle_seconds` go out of scope when `connect()` runs. They were stored as `self._max_size` / `self._max_idle_seconds` (L110-111), so `connect()` now reads the cached values. The constructor signature is unchanged.

**Verification** (substep G smoke, `FORESIGHT_DB_URL` unset):

```
create_backend() → SqliteBackend ✓
SqliteBackend.connect() → no NameError ✓
.execute() / .fetch() / .fetch_one() / .execute_many() roundtrip ✓
.close() graceful, no thread-stop warning ✓
```

**Fix 2 (this PR, per m0274 directive).** `sqlite_backend.py:58-68` `connection()` contextmanager landed with three pre-existing pyright errors (`reportUndefinedVariable "sql"` / `"params"` × 2 plus `reportReturnType Generator not satisfied`) that guaranteed `NameError` for any `with self.connection():` caller. Body rewritten to `with self._pool.acquire() as conn: yield conn` — matches the pool's `PooledConnection` shape (`_pool.acquire()` is itself a contextmanager; see `connection_pool.py:34`). Pyright post-fix: `No diagnostics found`. Smoke verifies `SQLITE_CONTEXTMANAGER_OK` (`SELECT COUNT(*)` inside `with self.connection()`) plus `SQLITE_CONTEXTMANAGER_INSERT_OK` (INSERT/COMMIT/SELECT roundtrip via the contextmanager). `execute()` / `fetch()` / `fetch_one()` / `execute_many()` paths regression-clean.

> If you bring up a fresh Neon-backed instance, this finding is irrelevant to you — the factory will route to `PostgresBackend`. It only matters if you were counting on SQLite for offline / disaster-recovery mode.

---

## 7. Redis Companion Cache — Implemented

Substeps B → F of PIX-3996 respond to the constraint that Foresight can't drop cross-process shared caching on a Redis-free broker. The class is `foresight_mcp/redis_cache.RedisCache`, instantiated by `reflection_narrative.generate_insight_narrative(...)` when the caller provides it via the `cache=` argument.

**Class surface** — mirrors `NarrativeCache` exactly:

| Method  | Args                                                                                   |
| ------- | -------------------------------------------------------------------------------------- |
| `get`   | `report_id`, `tenant_id=`, `user_id=`, `model_version=`, `insights_hash=`              |
| `put`   | `report_id`, `narrative`, `tenant_id=`, `user_id=`, `model_version=`, `insights_hash=` |
| `clear` | `tenant_id=None`                                                                       |
| `stats` | (no args)                                                                              |
| `close` | (no args)                                                                              |

**Key derivation** — identical SHA-256 hashes across NarrativeCache and RedisCache:

```
NarrativeCache._cache_key(...) → sha256 of (report_id, tenant_id, user_id, model_version, insights_hash)
RedisCache._key(...)            → "{prefix}:narrative:{tenant_id}:{user_id}:{cache_key}"
```

A `put` on one implementation guarantees a `get` hit on the other for the same logical row.

**Storage layout** (Redis):

- Value keys: `{prefix}:narrative:{tenant_id}:{user_id}:{cache_key}`
- Per-shard LRU sorted sets: `{prefix}:zset:{tenant_id}:{user_id}`

**TTL** — `DEFAULT_TTL_SECONDS = 604_800` (=7d). Native via `SETEX`. The smoke verifies `c._client.ttl(entry_key) == 604800` for live entries.

**LRU eviction** — `DEFAULT_MAX_ENTRIES = 10_000`. Per-tenant, per-user shard sorted set scored by epoch timestamp; when `ZCARD > max_entries`, the oldest `overflow` entries are pipelined-DEL'd along with their narrative keys (`ZRANGE 0 overflow-1 WITHSCORES`, then `ZREM`). `Eviction count` propagates through `stats()`.

**HIPAA-grade log safety** — `_sanitize_url(url)` re-substitutes passwords via `re.sub(r":[^:@]*@", ":***@", url)`. Verified live: `stats()["url"]` returns `rediss://default:***@witty-buffalo-119990.upstash.io:6379` — credentials never leave the process boundary in plain text.

**Multi-process shared caching** — verified (substep F smoke, Upstash broker):

```
Writer PID 101011: put("rpid", "from-process-AAA-50718", tenant_id="t-remote", ...) → close(), prefix to /tmp/.pix3996_remote_prefix.txt
Reader PID 101373: get("rpid", tenant_id="t-remote", ...) → "from-process-AAA-50718"  ✓
Cross-process value matches. CROSS_PROCESS_UPSTASH_VERIFIED.
```

Plus local Docker smoke: `c.put(...)` → `c.get(...)` produces a HIT with TTL=604800 intact.

**Configuration** — see §2 (`FORESIGHT_REDIS_URL`). If `FORESIGHT_REDIS_URL` is empty, callers that explicitly construct `RedisCache(url, ...)` consume it on demand; Foresight's default cache is still the in-process dict in `reflection_narrative.py`. If you want Foresight to construct the cache automatically, see `foresight_mcp/reflection_narrative._get_default_cache()`.

---

## 8. The `--active` Flag Trap

`uv run` defaults to the closest `.venv`. The host repo (`pixelated/`) ships an outer `.venv` that **lacks** `psycopg`, `psycopg-pool`, and `redis`. If you accidentally run with `--active` from anywhere outside `foresight-mcp/`, you'll end up using the outer venv and getting `ModuleNotFoundError`.

```
VIRTUAL_ENV=/home/vivi/pixelated/.venv does not match ... ignore
```

That warning is log noise, not failure — but **only when you are already inside `foresight-mcp/`**. If you see it while `cwd` is somewhere else (e.g. `/home/vivi/pixelated/`), the launch silently falls back to the outer venv and will fail to `import psycopg` later.

Pattern that always works:

```bash
( cd foresight-mcp && uv run python -c "from foresight_mcp.backend import create_backend; print(type(create_backend()).__name__)" )
```

The parentheses matter — `cd` is scoped to the subshell. Don't `cd foresight-mcp && uv run ...` in the parent shell because the cd persists and contaminates subsequent commands.

---

## 9. Substep Verification Log (PIX-3996, Phase 7)

> Scope pivot (m0244): "If we can't find any Redis implementation, then we need to add it, along with the postgresql." Substeps F + G were added in-flight per user direction (m0246).

| Substep                                                                | Status                  | Evidence                                                                                                                                                                                                                                                                                       |
| ---------------------------------------------------------------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A — Multi-agent memory sharing E2E cross-process                       | ✅ PASS                 | Writer PID 83615 + Reader PID 83619; row visible across Postgres pooler; table `pix3996_multi_agent_ping` created and dropped.                                                                                                                                                                 |
| B — Redis companion shared caching (original)                          | ✅ NO-OP → covered by F | Original B concluded no Redis path inside Foresight; superseded by F implementation (in-place per user m0246).                                                                                                                                                                                 |
| C — SQLite fallback smoke                                              | ✅ PASS (post G+Fix 2)  | Two `sqlite_backend.py` fixes this PR: (i) `connect()` `max_size`/`max_idle_seconds` NameError at L44-45; (ii) `connection()` contextmanager unbound `sql`/`params` + missing `yield` at L58-68 (per m0274 directive). Substep A originally passed only because it was on the Postgres branch. |
| D — Deployment doc                                                     | ✅ PASS                 | This file.                                                                                                                                                                                                                                                                                     |
| E — Substep E placeholder                                              | ⏳ Re-routed to J       | Original E (Linear close) became J after F + G rolled in.                                                                                                                                                                                                                                      |
| F — RedisCache implementation + dual smokes                            | ✅ PASS                 | Local Docker (TTL=604800 SETEX, LRU ZSET verified); Upstash cross-process (writer 101011 → reader 101373 matched value across OS processes). See §7.                                                                                                                                           |
| G — SQLite backend NameError fix                                       | ✅ PASS                 | 2-line surgical fix at `sqlite_backend.py:44-45`. Smoke confirms no NameError, CRUD roundtrip (`fetch_one`, `execute_many`, INSERT/DELETE/CREATE TABLE).                                                                                                                                       |
| H — DEPLOYMENT.md update                                               | ✅ PASS                 | §2 (env vars) + §6 (SQLite fallback) + §7 (Redis companion) + §9 + §10 updated.                                                                                                                                                                                                                |
| I — Substep ledger                                                     | ✅ Re-routed to J       | (no separate code; tracked via E→J reroute)                                                                                                                                                                                                                                                    |
| J — Linear close                                                       | ✅ PASS                 | PIX-3996 transitioned In Progress → Done with substep verification comment.                                                                                                                                                                                                                    |
| K — `connection()` contextmanager fix (post-close per m0274 directive) | ✅ PASS                 | Body changed from `try: conn.execute(sql, params); conn.commit(); finally: self._pool.release(conn)` to `with self._pool.acquire() as conn: yield conn`. Pyright post-fix `No diagnostics found`. Smoke confirms SELECT roundtrip + INSERT/COMMIT/SELECT via `with self.connection()`.         |

All edits scoped to `chad/sentry-fixes-round5 @ b445754` (foresight-mcp submodule). **All five files are uncommitted at the time of writing.** Per AGENTS.md "Never commit without explicit request", the commit + push is held back pending user authorization.

**Files touched in this PR:**

| File                                        | Status   | Edits                                                                                                                                             |
| ------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `foresight_mcp/backend/postgres_backend.py` | Modified | 5 surgical namespace corrections (see §5).                                                                                                        |
| `foresight_mcp/redis_cache.py`              | **NEW**  | `RedisCache` class, ~225 lines. Mirrors `NarrativeCache` API. Pyright-clean.                                                                      |
| `foresight_mcp/backend/sqlite_backend.py`   | Modified | 2-line `self._` prefix fix at L44-45 (G substep) + `connection()` contextmanager body rewrite at L58-68 (Fix 2 / K substep, per m0274 directive). |
| `foresight_mcp/reflection_narrative.py`     | Modified | 4 surgical edits: import + typing + isinstance guard + dict-first dispatch reorder (L67, L271, L323, L329-340, L398-409).                         |
| `foresight_mcp/config.py`                   | Modified | `FORESIGHT_REDIS_URL` env var added (3 lines).                                                                                                    |

---

## 10. Troubleshooting

| Symptom                                                                  | Likely Cause                                                  | Fix                                                              |
| ------------------------------------------------------------------------ | ------------------------------------------------------------- | ---------------------------------------------------------------- |
| `factory returned SqliteBackend despite setting FORESIGHT_DB_URL`        | URL prefix mismatch (`postgresql+psycopg://`, `pgbouncer://`) | Use bare `postgresql://`.                                        |
| `ModuleNotFoundError: No module named 'psycopg'`                         | Outer `.venv` shadowed the submodule venv                     | Drop `--active`. `cd foresight-mcp && uv sync --extra postgres`. |
| `attribute 'row_factory' requires dict_row, not DictRow`                 | Stale import in `postgres_backend.py`                         | Apply the surgical correction from §5 (L92-93 + L113-114).       |
| `NameError: name 'max_size' is not defined` (SQLite path)                | Bug — fixed in this PR (§6)                                   | Already shipped; `uv sync` then retry.                           |
| `NameError: name 'sql' is not defined` (SQLite `with self.connection()`) | Fixed in this PR (§6 "Fix 2")                                 | Use `with self.connection() as conn:` — no workaround needed.    |
| Neon SSL handshake fails                                                 | Missing `sslmode=require`                                     | Append `?sslmode=require` to your DSN.                           |
| Cross-process row invisibility                                           | Hitting writer endpoint for read, pooler for write            | Pick one endpoint per process and stick with it.                 |
