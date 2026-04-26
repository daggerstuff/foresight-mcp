"""
Authentication System for Foresight MCP
Provides API key-based authentication with secure password hashing.
"""

import os
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import hmac

from .tenant_context import get_current_tenant_id, set_current_tenant_id, reset_tenant_context
from .connection_pool import get_pool
from .config import DB_PATH


class Role(Enum):
    """User roles with associated permissions."""
    ADMIN = "admin"
    USER = "user"
    READONLY = "readonly"


@dataclass
class User:
    """User information for authentication."""
    user_id: str
    username: str
    email: str
    role: Role
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_login: Optional[datetime] = None
    # Hashed password (bcrypt-style, simplified for this implementation)
    password_hash: str = ""
    # API key for programmatic access
    api_key: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    # Associated tenant(s) - empty list means access to all tenants
    tenant_access: list[str] = field(default_factory=list)


class AuthError(Exception):
    """Authentication-related errors."""
    pass


class AuthManager:
    """Manages user authentication and authorization."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Initialize authentication tables."""
        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            # Users table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    role TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_login TEXT,
                    password_hash TEXT NOT NULL,
                    api_key TEXT UNIQUE NOT NULL,
                    tenant_access TEXT  -- JSON array of tenant IDs, empty = all
                )
            """)

            # Sessions table for tracking active sessions
            conn.execute("""
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    ip_address TEXT,
                    user_agent TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
            """)

            # Create indexes
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON auth_sessions(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON auth_sessions(expires_at)")

            conn.commit()
        finally:
            pool.release(conn)

    def _hash_password(self, password: str) -> str:
        """Hash a password using a secure method (simplified bcrypt-like)."""
        # In production, use bcrypt or argon2
        salt = secrets.token_hex(16)
        # Use PBKDF2 with SHA256 for key derivation
        key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
        return f"pbkdf2_sha256$100000${salt}${key.hex()}"

    def _verify_password(self, password: str, password_hash: str) -> bool:
        """Verify a password against its hash."""
        try:
            if not password_hash.startswith("pbkdf2_sha256$"):
                return False

            parts = password_hash.split("$")
            if len(parts) != 4:
                return False

            _, iterations_str, salt, hash_value = parts
            iterations = int(iterations_str)

            computed_hash = hashlib.pbkdf2_hmac(
                'sha256',
                password.encode('utf-8'),
                salt.encode('utf-8'),
                iterations
            )

            return hmac.compare_digest(computed_hash.hex(), hash_value)
        except Exception:
            return False

    def create_user(self, username: str, email: str, password: str,
                   role: Role = Role.USER, tenant_access: Optional[list[str]] = None) -> User:
        """Create a new user."""
        if tenant_access is None:
            tenant_access = []  # Empty means access to all tenants

        user_id = secrets.token_urlsafe(16)
        password_hash = self._hash_password(password)
        api_key = secrets.token_urlsafe(32)

        import json
        tenant_access_json = json.dumps(tenant_access)

        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            conn.execute("""
                INSERT INTO users (user_id, username, email, role, is_active, created_at,
                                 password_hash, api_key, tenant_access)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id, username, email, role.value, True,
                datetime.utcnow().isoformat(), password_hash, api_key, tenant_access_json
            ))
            conn.commit()

            return User(
                user_id=user_id,
                username=username,
                email=email,
                role=role,
                is_active=True,
                created_at=datetime.utcnow(),
                password_hash=password_hash,
                api_key=api_key,
                tenant_access=tenant_access
            )
        finally:
            pool.release(conn)

    def authenticate_user(self, username: str, password: str) -> Optional[User]:
        """Authenticate a user with username/password."""
        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            cursor = conn.execute("""
                SELECT user_id, username, email, role, is_active, created_at, last_login,
                       password_hash, api_key, tenant_access
                FROM users
                WHERE username = ? AND is_active = 1
            """, (username,))

            row = cursor.fetchone()
            if not row:
                return None

            (user_id, username, email, role_str, is_active, created_at_str,
             last_login_str, password_hash, api_key, tenant_access_json) = row

            if not self._verify_password(password, password_hash):
                return None

            import json
            from datetime import datetime

            # Update last login
            conn.execute("""
                UPDATE users SET last_login = ? WHERE user_id = ?
            """, (datetime.utcnow().isoformat(), user_id))
            conn.commit()

            return User(
                user_id=user_id,
                username=username,
                email=email,
                role=Role(role_str),
                is_active=bool(is_active),
                created_at=datetime.fromisoformat(created_at_str) if created_at_str else datetime.utcnow(),
                last_login=datetime.fromisoformat(last_login_str) if last_login_str else None,
                password_hash=password_hash,
                api_key=api_key,
                tenant_access=json.loads(tenant_access_json) if tenant_access_json else []
            )
        finally:
            pool.release(conn)

    def authenticate_api_key(self, api_key: str) -> Optional[User]:
        """Authenticate a user by API key."""
        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            cursor = conn.execute("""
                SELECT user_id, username, email, role, is_active, created_at, last_login,
                       password_hash, api_key, tenant_access
                FROM users
                WHERE api_key = ? AND is_active = 1
            """, (api_key,))

            row = cursor.fetchone()
            if not row:
                return None

            (user_id, username, email, role_str, is_active, created_at_str,
             last_login_str, password_hash, _, tenant_access_json) = row

            import json
            from datetime import datetime

            # Update last login
            conn.execute("""
                UPDATE users SET last_login = ? WHERE user_id = ?
            """, (datetime.utcnow().isoformat(), user_id))
            conn.commit()

            return User(
                user_id=user_id,
                username=username,
                email=email,
                role=Role(role_str),
                is_active=bool(is_active),
                created_at=datetime.fromisoformat(created_at_str) if created_at_str else datetime.utcnow(),
                last_login=datetime.fromisoformat(last_login_str) if last_login_str else None,
                password_hash=password_hash,
                api_key=api_key,
                tenant_access=json.loads(tenant_access_json) if tenant_access_json else []
            )
        finally:
            pool.release(conn)

    def validate_user_tenant_access(self, user: User, tenant_id: str) -> bool:
        """Check if a user has access to a specific tenant."""
        # Empty tenant_access means access to all tenants
        if not user.tenant_access:
            return True

        return tenant_id in user.tenant_access

    def create_session(self, user: User, ip_address: str = "", user_agent: str = "") -> str:
        """Create a new authentication session."""
        session_id = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(hours=24)

        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            conn.execute("""
                INSERT INTO auth_sessions (session_id, user_id, created_at, expires_at, ip_address, user_agent)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                session_id, user.user_id,
                datetime.utcnow().isoformat(),
                expires_at.isoformat(),
                ip_address,
                user_agent
            ))
            conn.commit()
            return session_id
        finally:
            pool.release(conn)

    def validate_session(self, session_id: str) -> Optional[User]:
        """Validate a session and return the associated user."""
        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            cursor = conn.execute("""
                SELECT u.user_id, u.username, u.email, u.role, u.is_active, u.created_at, u.last_login,
                       u.password_hash, u.api_key, u.tenant_access, s.expires_at
                FROM auth_sessions s
                JOIN users u ON s.user_id = u.user_id
                WHERE s.session_id = ? AND s.expires_at > ?
            """, (session_id, datetime.utcnow().isoformat()))

            row = cursor.fetchone()
            if not row:
                return None

            (user_id, username, email, role_str, is_active, created_at_str,
             last_login_str, password_hash, api_key, tenant_access_json, expires_at_str) = row

            if not self._verify_password("", password_hash):  # Just check if hash is valid format
                return None

            import json
            from datetime import datetime

            return User(
                user_id=user_id,
                username=username,
                email=email,
                role=Role(role_str),
                is_active=bool(is_active),
                created_at=datetime.fromisoformat(created_at_str) if created_at_str else datetime.utcnow(),
                last_login=datetime.fromisoformat(last_login_str) if last_login_str else None,
                password_hash=password_hash,
                api_key=api_key,
                tenant_access=json.loads(tenant_access_json) if tenant_access_json else []
            )
        finally:
            pool.release(conn)

    def cleanup_expired_sessions(self) -> int:
        """Remove expired sessions and return count of removed sessions."""
        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            cursor = conn.execute("""
                DELETE FROM auth_sessions WHERE expires_at < ?
            """, (datetime.utcnow().isoformat(),))
            deleted_count = cursor.rowcount
            conn.commit()
            return deleted_count
        finally:
            pool.release(conn)


# Global auth manager instance
_auth_manager: Optional[AuthManager] = None
_auth_lock = __import__("threading").RLock()


def get_auth_manager() -> AuthManager:
    """Get the global auth manager instance."""
    global _auth_manager
    with _auth_lock:
        if _auth_manager is None:
            _auth_manager = AuthManager()
        return _auth_manager


def initialize_default_users() -> None:
    """Create default users if none exist."""
    auth_manager = get_auth_manager()
    pool = get_auth_manager().db_path

    # Check if any users exist
    pool_conn = get_pool(DB_PATH)
    conn = pool_conn.acquire()

    try:
        cursor = conn.execute("SELECT COUNT(*) FROM users")
        count = cursor.fetchone()[0]

        if count == 0:
            # Create default admin user
            admin_password = secrets.token_urlsafe(32)  # Increase to 32 chars for stronger password
            admin_user = auth_manager.create_user(
                username="admin",
                email="admin@foresight.local",
                password=admin_password,
                role=Role.ADMIN,
                tenant_access=[]  # Access to all tenants
            )

            # In a real system, you'd output this securely via separate channel
            # For now, we'll just note it was created without exposing password
            print("[AUTH] Default admin user created: username='admin' (password generated securely)")

            # Create a default readonly user for testing
            readonly_password = secrets.token_urlsafe(16)
            readonly_user = auth_manager.create_user(
                username="readonly",
                email="readonly@foresight.local",
                password=readonly_password,
                role=Role.READONLY,
                tenant_access=["default"]  # Only access to default tenant
            )

            print(f"[AUTH] Default readonly user created: username='readonly', password='{readonly_password}' (SAVE THIS SECURELY)")
    finally:
        pool_conn.release(conn)