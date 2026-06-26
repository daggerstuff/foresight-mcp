"""Tests for SqliteBackend connection pooling and query execution."""
import pytest
from foresight_mcp.backend.sqlite_backend import SqliteBackend

@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "backend_test.db")

@pytest.fixture
def backend(db_path):
    b = SqliteBackend(db_path=db_path)
    b.connect()
    yield b
    b.close()

def test_connect_and_close(db_path):
    b = SqliteBackend(db_path=db_path)
    assert b._pool is None

    b.connect()
    assert b._pool is not None

    b.close()
    assert b._pool is None


def test_close_never_connected(db_path):
    """Test close() on a never-connected backend is a no-op."""
    b = SqliteBackend(db_path=db_path)
    assert b._pool is None
    # close() without connect() should not raise
    b.close()
    # close() again should also be a no-op
    b.close()


def test_unconnected_backend_raises(db_path):
    b = SqliteBackend(db_path=db_path)

    with pytest.raises(RuntimeError, match="SqliteBackend not connected"):
        with b.connection():
            pass

    with pytest.raises(RuntimeError, match="SqliteBackend not connected"):
        b.execute("SELECT 1")

    with pytest.raises(RuntimeError, match="SqliteBackend not connected"):
        b.execute_many("SELECT ?", [(1,)])

    with pytest.raises(RuntimeError, match="SqliteBackend not connected"):
        b.fetch("SELECT 1")

    with pytest.raises(RuntimeError, match="SqliteBackend not connected"):
        b.fetch_one("SELECT 1")

def test_execute_and_fetch(backend):
    backend.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
    backend.execute("INSERT INTO test (name) VALUES (?)", ("foo",))

    rows = backend.fetch("SELECT id, name FROM test")
    assert len(rows) == 1
    assert rows[0]["name"] == "foo"
    assert rows[0]["id"] == 1

def test_fetch_one(backend):
    backend.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
    backend.execute("INSERT INTO test (name) VALUES (?)", ("bar",))

    row = backend.fetch_one("SELECT name FROM test WHERE id = ?", (1,))
    assert row is not None
    assert row["name"] == "bar"

    row_none = backend.fetch_one("SELECT name FROM test WHERE id = ?", (99,))
    assert row_none is None

def test_execute_many(backend):
    backend.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
    backend.execute_many("INSERT INTO test (name) VALUES (?)", [("a",), ("b",), ("c",)])

    rows = backend.fetch("SELECT name FROM test ORDER BY name")
    assert len(rows) == 3
    assert rows[0]["name"] == "a"
    assert rows[1]["name"] == "b"
    assert rows[2]["name"] == "c"

def test_connection_context_manager(backend):
    backend.execute("CREATE TABLE test (val INTEGER)")

    with backend.connection() as conn:
        conn.execute("INSERT INTO test (val) VALUES (42)")
        conn.commit()

    row = backend.fetch_one("SELECT val FROM test")
    assert row["val"] == 42

def test_stats(backend):
    stats = backend.stats
    assert "idle" in stats
    assert "in_use" in stats
    assert "max_size" in stats


def test_execute_many_empty(backend):
    """Test execute_many with an empty parameter sequence is a no-op."""
    backend.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
    backend.execute_many("INSERT INTO test (name) VALUES (?)", [])

    rows = backend.fetch("SELECT name FROM test ORDER BY name")
    assert len(rows) == 0


def test_stats_unconnected(db_path):
    b = SqliteBackend(db_path=db_path)
    stats = b.stats
    assert stats == {"idle": 0, "in_use": 0, "max_size": 10}

def test_connection_exception_rollback(backend):
    backend.execute("CREATE TABLE test (val INTEGER)")

    with pytest.raises(ValueError):
        with backend.connection() as conn:
            conn.execute("INSERT INTO test (val) VALUES (42)")
            raise ValueError("Something went wrong")

    # The transaction should have been rolled back
    row = backend.fetch_one("SELECT val FROM test")
    assert row is None


def test_connect_default_db_path(monkeypatch, tmp_path):
    default_path = str(tmp_path / "default.db")
    import foresight_mcp.backend.sqlite_backend
    monkeypatch.setattr(foresight_mcp.backend.sqlite_backend, "DB_PATH", default_path)

    b = SqliteBackend()
    b.connect()
    assert b._pool is not None
    assert b._pool.db_path == default_path
    b.close()


def test_execute_dict_params(backend):
    backend.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
    backend.execute("INSERT INTO test (name) VALUES (:name)", {"name": "dict_foo"})

    rows = backend.fetch("SELECT id, name FROM test WHERE name = :name", {"name": "dict_foo"})
    assert len(rows) == 1
    assert rows[0]["name"] == "dict_foo"

    row = backend.fetch_one("SELECT id, name FROM test WHERE name = :name", {"name": "dict_foo"})
    assert row is not None
    assert row["name"] == "dict_foo"


def test_execute_no_params(backend):
    backend.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
    backend.execute("INSERT INTO test (name) VALUES ('no_params')")

    rows = backend.fetch("SELECT id, name FROM test")
    assert len(rows) == 1
    assert rows[0]["name"] == "no_params"

    row = backend.fetch_one("SELECT id, name FROM test")
    assert row is not None
    assert row["name"] == "no_params"

def test_fetch_one_multiple_results(backend):
    backend.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
    backend.execute_many("INSERT INTO test (name) VALUES (?)", [("a",), ("b",)])

    # fetch_one should just return the first row
    row = backend.fetch_one("SELECT name FROM test ORDER BY name")
    assert row is not None
    assert row["name"] == "a"
