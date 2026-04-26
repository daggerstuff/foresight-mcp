import os
import tempfile
import pytest

from foresight_mcp.auth import AuthManager, Role, get_auth_manager

@pytest.fixture(scope="function")
def temp_db_path(tmp_path):
    # Create a temporary SQLite DB file
    db_file = tmp_path / "test_memory.db"
    # Ensure the env variable points to this file
    os.environ["FORESIGHT_DB_PATH"] = str(db_file)
    return str(db_file)

def test_user_creation_and_authentication(temp_db_path):
    # Ensure fresh manager uses temp DB
    manager = AuthManager(db_path=temp_db_path)
    username = "testuser"
    email = "test@example.com"
    password = "SecretPass123!"
    role = Role.USER

    user = manager.create_user(username=username, email=email, password=password, role=role)
    assert user.username == username
    # Authentication should succeed with correct password
    auth_user = manager.authenticate_user(username=username, password=password)
    assert auth_user is not None
    assert auth_user.user_id == user.user_id
    # Wrong password should fail
    assert manager.authenticate_user(username=username, password="wrong") is None
