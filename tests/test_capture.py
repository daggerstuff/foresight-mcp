"""Tests for PIX-3954 capture pipeline (SessionClassifier, MemoryExtractor, DedupeEngine, CapturePipeline)."""

import hashlib
from datetime import datetime, timezone

import pytest
from foresight_mcp.capture import (
    CapturedMemory,
    DedupeEngine,
    MemoryExtractor,
    SessionClassifier,
    get_capture_pipeline,
    reset_capture_pipeline,
)
from foresight_mcp.document_layer import content_hash as _content_hash
from foresight_mcp.memory_relationships import reset_memory_relationship_store

# ====== Fixtures ======


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Isolate DB per test — same pattern as test_server.py."""
    db_file = tmp_path / "test_capture.db"
    monkeypatch.setenv("FORESIGHT_DB_PATH", str(db_file))

    import foresight_mcp.config as config_module
    import foresight_mcp.connection_pool as conn_pool_module
    from foresight_mcp.connection_pool import reset_pool
    from foresight_mcp.server import init_db

    monkeypatch.setattr(config_module, "DB_PATH", str(db_file))
    monkeypatch.setattr(conn_pool_module, "DB_PATH", str(db_file))
    reset_pool()

    from foresight_mcp.tenant_context import set_current_account_id, set_current_user_id

    set_current_user_id("_test_user_")
    set_current_account_id("_test_")

    init_db()
    reset_capture_pipeline()
    reset_memory_relationship_store()
    yield
    reset_pool()
    from foresight_mcp.tenant_context import reset_tenant_context

    reset_tenant_context()


# ====== SessionClassifier ======


class TestSessionClassifier:
    def test_skip_no_messages(self):
        skip, reason = SessionClassifier.should_skip([])
        assert skip is True
        assert "no messages" in reason

    def test_skip_too_few_messages(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        skip, reason = SessionClassifier.should_skip(msgs)
        assert skip is True
        assert "2 messages" in reason

    def test_skip_no_user_messages(self):
        msgs = [
            {"role": "assistant", "content": "hello"},
            {"role": "assistant", "content": "how can I help"},
            {"role": "assistant", "content": "let me know"},
        ]
        skip, reason = SessionClassifier.should_skip(msgs)
        assert skip is True
        assert "no user messages" in reason

    def test_skip_avg_chars_too_low(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "yes"},
        ]
        skip, reason = SessionClassifier.should_skip(msgs)
        assert skip is True
        assert "avg message length" in reason

    def test_skip_no_technical_content(self):
        msgs = [
            {"role": "user", "content": "I had a good day today, the weather was nice."},
            {"role": "assistant", "content": "That sounds wonderful, tell me more."},
            {"role": "user", "content": "I went for a walk and saw some birds."},
        ]
        skip, reason = SessionClassifier.should_skip(msgs)
        assert skip is True
        assert "no technical content" in reason

    def test_pass_technical_content(self):
        msgs = [
            {"role": "user", "content": "Can you help me install the API client from the npm registry?"},
            {"role": "assistant", "content": "Sure, run `npm install my-api-client --save` to add it to your project."},
            {"role": "user", "content": "I prefer using pnpm for package management because it's faster."},
        ]
        skip, reason = SessionClassifier.should_skip(msgs)
        assert skip is False, f"unexpected skip: {reason}"

    def test_pass_code_block(self):
        msgs = [
            {"role": "user", "content": "Here's my Python code for the handler function"},
            {
                "role": "assistant",
                "content": "```python\ndef hello(name: str) -> str:\n    return f'Hello, {name}!'\n```",
            },
            {"role": "user", "content": "Looks good, let's deploy it to production now."},
        ]
        skip, reason = SessionClassifier.should_skip(msgs)
        assert skip is False, f"unexpected skip: {reason}"

    def test_pass_file_path(self):
        msgs = [
            {"role": "user", "content": "The config file is located at /etc/app/config.yaml in the server directory"},
            {"role": "assistant", "content": "Let me check that file path for you and read its contents."},
            {"role": "user", "content": "Found it, it's in the src/utils/helpers/config.yaml subdirectory."},
        ]
        skip, reason = SessionClassifier.should_skip(msgs)
        assert skip is False, f"unexpected skip: {reason}"


# ====== MemoryExtractor ======


class TestMemoryExtractor:
    def test_extract_decision(self):
        msgs = [
            {"role": "user", "content": "Let's use PostgreSQL for the database. It's more reliable."},
            {"role": "assistant", "content": "Good choice. I'll set up the schema."},
        ]
        candidates = MemoryExtractor.extract(msgs)
        assert len(candidates) >= 1
        decisions = [c for c in candidates if c.category == "decision"]
        assert len(decisions) >= 1
        assert decisions[0].is_immutable is True
        assert decisions[0].scope == "arc"
        assert decisions[0].importance == 0.7

    def test_extract_preference(self):
        msgs = [
            {"role": "user", "content": "I always use type hints in Python. They make code clearer."},
            {"role": "assistant", "content": "That's a good habit for maintainability."},
        ]
        candidates = MemoryExtractor.extract(msgs)
        preferences = [c for c in candidates if c.category == "preference"]
        assert len(preferences) >= 1
        assert preferences[0].scope == "trait"

    def test_extract_tool_recipe(self):
        msgs = [
            {"role": "user", "content": "I solved it by running `npm install && npm run build`"},
            {"role": "assistant", "content": "Great, that should work for the CI pipeline too."},
        ]
        candidates = MemoryExtractor.extract(msgs)
        recipes = [c for c in candidates if c.category == "tool_recipe"]
        assert len(recipes) >= 1

    def test_extract_tool_recipe_code_block(self):
        msgs = [
            {"role": "user", "content": "Here's what worked for me:\n```bash\nuvicorn main:app --reload\n```"},
            {"role": "assistant", "content": "Good, that's the dev server command."},
        ]
        candidates = MemoryExtractor.extract(msgs)
        recipes = [c for c in candidates if c.category == "tool_recipe"]
        assert len(recipes) >= 1

    def test_extract_pattern(self):
        msgs = [
            {"role": "user", "content": "This is the same pattern as our auth module. We should reuse it."},
            {"role": "assistant", "content": "Yes, it follows the same approach as the existing service layer."},
        ]
        candidates = MemoryExtractor.extract(msgs)
        patterns = [c for c in candidates if c.category == "pattern"]
        assert len(patterns) >= 1

    def test_extract_pending_item(self):
        msgs = [
            {"role": "user", "content": "TODO: add input validation for the user registration endpoint"},
            {"role": "assistant", "content": "I'll create a validation middleware for that."},
            {"role": "user", "content": "We need to follow up on the deployment next week."},
        ]
        candidates = MemoryExtractor.extract(msgs)
        pending = [c for c in candidates if c.category == "pending_item"]
        assert len(pending) >= 1
        assert pending[0].scope == "session"

    def test_no_candidates_for_trivial(self):
        msgs = [
            {"role": "user", "content": "Hello, how are you?"},
            {"role": "assistant", "content": "I'm doing well, thanks!"},
        ]
        candidates = MemoryExtractor.extract(msgs)
        assert len(candidates) == 0

    def test_extract_all_categories(self):
        msgs = [
            {"role": "user", "content": "Let's use FastAPI for the new service."},
            {"role": "user", "content": "I always prefer async handlers for I/O bound tasks."},
            {"role": "user", "content": "I solved it by calling await client.query()"},
            {"role": "user", "content": "This is the same pattern as our message queue handler."},
            {"role": "user", "content": "TODO: add retry logic with exponential backoff"},
        ]
        candidates = MemoryExtractor.extract(msgs)
        categories = {c.category for c in candidates}
        assert categories == {"decision", "preference", "tool_recipe", "pattern", "pending_item"}


# ====== DedupeEngine ======


class TestDedupeEngine:
    def _seed_memory(
        self, content: str, category: str = "decision", user_id: str = "_test_user_", tenant_id: str = "_test_"
    ):
        """Insert a memory directly so the engine can find it."""
        from foresight_mcp.connection_pool import get_pool

        pool = get_pool()
        conn = pool.acquire()
        try:
            now = datetime.now(timezone.utc).isoformat()
            stored_content = f"[auto-captured/{category}] {content}"
            h = _content_hash(stored_content)
            mid = hashlib.sha256(f"{content}{now}".encode()).hexdigest()[:16]
            conn.execute(
                """INSERT OR IGNORE INTO memories
                   (id, content, content_hash, scope, retention, category, user_id, bank_id, tenant_id,
                    created_at, updated_at, tags, emotional_context, metrics, is_ghost, synthesized_from, importance)
                   VALUES (?, ?, ?, 'arc', 'long_term', ?, ?, 'test_bank', ?, ?, ?, '[]', '{}', '{}', 0, '[]', 0.7)""",
                (mid, stored_content, h, category, user_id, tenant_id, now, now),
            )
            conn.commit()
            return mid
        finally:
            pool.release(conn)
            conn.close()

    def test_unique(self):
        c = CapturedMemory(
            content="Let's use Redis for caching",
            category="decision",
            scope="arc",
            retention="long_term",
            importance=0.7,
        )
        result = DedupeEngine.check(c, "_test_user_", "_test_")
        assert result.status == "UNIQUE"
        assert result.existing_id is None

    def test_duplicate_exact_match(self):
        content = "Let's use PostgreSQL for persistence"
        self._seed_memory(content, category="decision")
        c = CapturedMemory(content=content, category="decision", scope="arc", retention="long_term", importance=0.7)
        result = DedupeEngine.check(c, "_test_user_", "_test_")
        assert result.status == "DUPLICATE"
        assert result.existing_id is not None
        assert result.similarity == 1.0

    def test_near_duplicate_high_overlap(self):
        content = "Let's use PostgreSQL for persistence because it's reliable and performant"
        self._seed_memory(content, category="decision")
        similar = "Let's use PostgreSQL for persistence since it's reliable and performant for our use case"
        c = CapturedMemory(content=similar, category="decision", scope="arc", retention="long_term", importance=0.7)
        result = DedupeEngine.check(c, "_test_user_", "_test_")
        # Jaccard should be > 0.55
        assert result.status in ("NEAR_DUPLICATE", "DUPLICATE"), f"got {result.status}"

    def test_near_duplicate_by_same_user(self):
        content1 = "I prefer using FastAPI for building REST APIs and web services"
        self._seed_memory(content1, category="preference")
        content2 = "I always prefer FastAPI for building REST APIs and web services in Python"
        c = CapturedMemory(
            content=content2, category="preference", scope="trait", retention="long_term", importance=0.6
        )
        result = DedupeEngine.check(c, "_test_user_", "_test_")
        assert result.status in ("NEAR_DUPLICATE", "DUPLICATE"), f"got {result.status}"

    def test_different_content_unique(self):
        self._seed_memory("Let's use MongoDB for documents", category="decision")
        c = CapturedMemory(
            content="Let's use Redis for caching",
            category="decision",
            scope="arc",
            retention="long_term",
            importance=0.7,
        )
        result = DedupeEngine.check(c, "_test_user_", "_test_")
        assert result.status == "UNIQUE"

    def test_different_category_no_false_dedup(self):
        """Same words but different category should not collide."""
        self._seed_memory("Let's use FastAPI for the service", category="decision")
        c = CapturedMemory(
            content="Let's use FastAPI for the service",
            category="preference",
            scope="trait",
            retention="long_term",
            importance=0.6,
        )
        result = DedupeEngine.check(c, "_test_user_", "_test_")
        # Content hash will differ since it includes the category prefix
        assert result.status == "UNIQUE"

    def test_tokenize(self):
        tokens = DedupeEngine._tokenize("Hello World! This is a test_123.")
        assert "hello" in tokens
        assert "world" in tokens
        assert "test_123" in tokens


# ====== CapturePipeline — integration tests ======


class TestCapturePipeline:
    def _count_memories(self):
        from foresight_mcp.connection_pool import get_pool

        pool = get_pool()
        conn = pool.acquire()
        try:
            return conn.execute("SELECT COUNT(*) as cnt FROM memories WHERE user_id = '_test_user_'").fetchone()["cnt"]
        finally:
            pool.release(conn)
            conn.close()

    def test_full_pipeline_stores_decisions(self):
        pipeline = get_capture_pipeline()
        msgs = [
            {"role": "user", "content": "Let's use PostgreSQL for the database backend running on AWS RDS."},
            {
                "role": "assistant",
                "content": "Good choice, I'll configure the connection pool with SSL and autocommit.",
            },
            {"role": "user", "content": "We should also use Redis for caching to improve the response times."},
        ]
        stats = pipeline.run("sess_1", msgs, "_test_user_")
        assert stats.skipped is False
        assert stats.stored >= 1

    def test_pipeline_skips_trivial(self):
        pipeline = get_capture_pipeline()
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "bye"},
        ]
        stats = pipeline.run("sess_2", msgs, "_test_user_")
        assert stats.skipped is True
        assert stats.stored == 0

    def test_pipeline_skips_no_user_content(self):
        pipeline = get_capture_pipeline()
        msgs = [
            {"role": "assistant", "content": "Hello, how can I help?"},
            {"role": "assistant", "content": "I can assist with code review."},
            {"role": "assistant", "content": "Please let me know what you need."},
        ]
        stats = pipeline.run("sess_3", msgs, "_test_user_")
        assert stats.skipped is True

    def test_pipeline_dedup_exact(self):
        """Running same transcript twice should dedup on the second call."""
        pipeline = get_capture_pipeline()
        msgs = [
            {"role": "user", "content": "Let's use type hints everywhere in our Python codebase going forward."},
            {"role": "assistant", "content": "Agreed, that will improve code maintainability and readability."},
            {"role": "user", "content": "I always prefer explicit return types in function signatures."},
        ]
        stats1 = pipeline.run("sess_4", msgs, "_test_user_")
        assert stats1.stored >= 1
        stats2 = pipeline.run("sess_4", msgs, "_test_user_")
        # Second run should find duplicates
        assert stats2.duplicates >= 1
        assert stats2.stored == 0  # all should be duplicates now

    def test_pipeline_relationships_on_near_dup(self):
        """Near duplicates should be linked via derives relationships."""
        pipeline = get_capture_pipeline()
        msgs1 = [
            {"role": "user", "content": "I prefer using FastAPI for building web APIs and backend services."},
            {"role": "assistant", "content": "Good choice for async Python applications with high throughput."},
            {"role": "user", "content": "It has great documentation and auto-generated OpenAPI schemas."},
        ]
        stats1 = pipeline.run("sess_5", msgs1, "_test_user_")
        assert stats1.stored >= 1

        # Similar but not identical preference
        msgs2 = [
            {"role": "user", "content": "I always prefer FastAPI for building REST APIs and web services."},
            {"role": "assistant", "content": "Good, it has excellent async support."},
            {"role": "user", "content": "The auto-generated docs are a big plus."},
        ]
        stats2 = pipeline.run("sess_6", msgs2, "_test_user_")
        # Should still store (add-only), but detect near-dup
        assert stats2.stored >= 1

    def test_pipeline_extracts_all_categories(self):
        pipeline = get_capture_pipeline()
        msgs = [
            {"role": "user", "content": "Let's use FastAPI for the new microservice."},
            {"role": "user", "content": "I always prefer async database drivers."},
            {"role": "user", "content": "I solved it with `await redis.get(key)` in the handler."},
            {"role": "user", "content": "This follows the same pattern as our event bus."},
            {"role": "user", "content": "TODO: add monitoring and alerting for the deployment."},
        ]
        stats = pipeline.run("sess_7", msgs, "_test_user_")
        assert stats.candidates_found >= 5
        assert stats.stored >= 5

    def test_pipeline_empty_messages(self):
        pipeline = get_capture_pipeline()
        stats = pipeline.run("sess_8", [], "_test_user_")
        assert stats.skipped is True
        assert "no messages" in stats.skip_reason

    def test_pipeline_skips_after_dedup_exhaustion(self):
        """A session with only content already stored should result in zero new stores."""
        pipeline = get_capture_pipeline()
        msgs = [
            {"role": "user", "content": "I always prefer type hints in Python for code clarity and readability."},
            {"role": "assistant", "content": "That's a great practice for large codebases with multiple contributors."},
            {"role": "user", "content": "I always prefer explicit code over implicit patterns for maintainability."},
        ]
        stats1 = pipeline.run("sess_9", msgs, "_test_user_")
        assert stats1.stored >= 1

        # Replay exact same messages
        stats2 = pipeline.run("sess_10", msgs, "_test_user_")
        assert stats2.duplicates >= 1
        assert stats2.stored == 0


# ====== CapturePipeline — Server integration ======


class TestServerIntegration:
    """Verify the pipeline is called from process_session_transcript."""

    def test_process_session_transcript_includes_capture(self):
        """Verify the return message includes memory count from capture pipeline."""
        from foresight_mcp.server import process_session_transcript

        msgs = [
            {"role": "user", "content": "Let's use Redis for caching"},
            {"role": "assistant", "content": "Good idea, I'll configure it"},
            {"role": "user", "content": "I prefer using sentinel for high availability"},
        ]
        result = process_session_transcript("sess_integration", msgs, project_path="/tmp", user_id="_test_user_")
        # The result should mention memories were stored
        assert "new memories" in result

    def test_process_session_transcript_skips_trivial(self):
        """Trivial sessions should still report zero new memories."""
        from foresight_mcp.server import process_session_transcript

        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "bye now"},
        ]
        result = process_session_transcript("sess_trivial", msgs, user_id="_test_user_")
        assert "0 new memories" in result

    def test_process_session_transcript_with_existing_bridge(self):
        """Pipeline should coexist with existing _bridge_context_blocks_to_memories."""
        from foresight_mcp.server import process_session_transcript

        msgs = [
            {"role": "user", "content": "TODO: Add retry logic for the API client."},
            {"role": "assistant", "content": "I'll create a retry decorator with exponential backoff."},
            {"role": "user", "content": "I always prefer tenacity for retry logic in Python."},
        ]
        result = process_session_transcript("sess_bridge_coexist", msgs, user_id="_test_user_")
        # Both the bridge and the pipeline should have stored memories
        assert "new memories" in result
        count_str = result.split("(")[1].split(" ")[0] if "(" in result else "0"
        assert int(count_str) > 0, f"Expected new memories, got: {result}"

    def test_pipeline_singleton_consistency(self):
        """get_capture_pipeline should return the same instance."""
        p1 = get_capture_pipeline()
        p2 = get_capture_pipeline()
        assert p1 is p2

    def test_reset_capture_pipeline(self):
        p1 = get_capture_pipeline()
        reset_capture_pipeline()
        p2 = get_capture_pipeline()
        assert p1 is not p2
