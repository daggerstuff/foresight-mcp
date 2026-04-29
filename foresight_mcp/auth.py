"""
Authentication System for Foresight MCP
Provides API key-based authentication with secure password hashing.
"""

import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger(__name__)

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


_VALID_TENANT_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def _validate_tenant_id(tenant_id: str) -> None:
    """Raise AuthError if tenant_id is not a safe alphanumeric slug."""
    if not tenant_id or not _VALID_TENANT_RE.match(tenant_id):
        raise AuthError("Invalid tenant_id: must be 1-64 alphanumeric/underscore/hyphen characters")


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
        """Hash a password using Argon2 if available, else fallback to bcrypt."""
        # Lazy import to avoid hard dependency
        # Try Argon2 first
        try:
            from argon2 import PasswordHasher

            ph = PasswordHasher()
            return ph.hash(password)
        except Exception:
            # Fallback to bcrypt
            try:
                import bcrypt

                hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
                return hashed.decode("utf-8")
            except Exception:
                # Final fallback: simple SHA256 with salt (not recommended for production)
                import hashlib

                salt = hashlib.sha256(os.urandom(16)).hexdigest()
                hash_bytes = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
                return f"sha256${salt}${hash_bytes}"

    def _verify_password(self, password: str, password_hash: str) -> bool:
        """Verify a password against its stored Argon2 or bcrypt hash."""
        # Attempt Argon2 verification first
        # Attempt Argon2 verification
        try:
            from argon2 import PasswordHasher

            ph = PasswordHasher()
            return ph.verify(password_hash, password)
        except Exception:
            # Try bcrypt verification
            try:
                import bcrypt

                return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
            except Exception:
                # Fallback to simple SHA256 verification
                if password_hash.startswith("sha256$"):
                    try:
                        _, salt, stored_hash = password_hash.split("$")
                        import hashlib

                        calc_hash = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
                        return calc_hash == stored_hash
                    except Exception:
                        return False
                return False

    def create_user(
        self,
        username: str,
        email: str,
        password: str,
        role: Role = Role.USER,
        tenant_access: Optional[list[str]] = None,
    ) -> User:
        """Create a new user."""
        if tenant_access is None:
            tenant_access = []  # Empty means access to all tenants

        # Validate every tenant_id being granted access
        for tid in tenant_access:
            _validate_tenant_id(tid)

        user_id = secrets.token_urlsafe(32)
        password_hash = self._hash_password(password)
        api_key = secrets.token_urlsafe(32)

        import json

        tenant_access_json = json.dumps(tenant_access)

        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            conn.execute(
                """
                INSERT INTO users (user_id, username, email, role, is_active, created_at,
                                 password_hash, api_key, tenant_access)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    user_id,
                    username,
                    email,
                    role.value,
                    True,
                    datetime.utcnow().isoformat(),
                    password_hash,
                    api_key,
                    tenant_access_json,
                ),
            )
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
                tenant_access=tenant_access,
            )
        finally:
            pool.release(conn)

    def authenticate_user(self, username: str, password: str) -> Optional[User]:
        """Authenticate a user with username/password."""
        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            cursor = conn.execute(
                """
                SELECT user_id, username, email, role, is_active, created_at, last_login,
                       password_hash, api_key, tenant_access
                FROM users
                WHERE username = ? AND is_active = 1
            """,
                (username,),
            )

            row = cursor.fetchone()
            if not row:
                return None

            (
                user_id,
                username,
                email,
                role_str,
                is_active,
                created_at_str,
                last_login_str,
                password_hash,
                api_key,
                tenant_access_json,
            ) = row

            if not self._verify_password(password, password_hash):
                return None

            import json
            from datetime import datetime

            # Update last login
            conn.execute(
                """
                UPDATE users SET last_login = ? WHERE user_id = ?
            """,
                (datetime.utcnow().isoformat(), user_id),
            )
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
                tenant_access=json.loads(tenant_access_json) if tenant_access_json else [],
            )
        finally:
            pool.release(conn)

    def authenticate_api_key(self, api_key: str) -> Optional[User]:
        """Authenticate a user by API key."""
        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            cursor = conn.execute(
                """
                SELECT user_id, username, email, role, is_active, created_at, last_login,
                       password_hash, api_key, tenant_access
                FROM users
                WHERE api_key = ? AND is_active = 1
            """,
                (api_key,),
            )

            row = cursor.fetchone()
            if not row:
                return None

            (
                user_id,
                username,
                email,
                role_str,
                is_active,
                created_at_str,
                last_login_str,
                password_hash,
                _,
                tenant_access_json,
            ) = row

            import json
            from datetime import datetime

            # Update last login
            conn.execute(
                """
                UPDATE users SET last_login = ? WHERE user_id = ?
            """,
                (datetime.utcnow().isoformat(), user_id),
            )
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
                tenant_access=json.loads(tenant_access_json) if tenant_access_json else [],
            )
        finally:
            pool.release(conn)

    def validate_user_tenant_access(self, user: User, tenant_id: str) -> bool:
        """Check if a user has access to a specific tenant."""
        _validate_tenant_id(tenant_id)
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
            conn.execute(
                """
                INSERT INTO auth_sessions (session_id, user_id, created_at, expires_at, ip_address, user_agent)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    session_id,
                    user.user_id,
                    datetime.utcnow().isoformat(),
                    expires_at.isoformat(),
                    ip_address,
                    user_agent,
                ),
            )
            conn.commit()
            return session_id
        finally:
            pool.release(conn)

    def validate_session(self, session_id: str) -> Optional[User]:
        """Validate a session and return the associated user."""
        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            cursor = conn.execute(
                """
                SELECT u.user_id, u.username, u.email, u.role, u.is_active, u.created_at, u.last_login,
                       u.password_hash, u.api_key, u.tenant_access, s.expires_at
                FROM auth_sessions s
                JOIN users u ON s.user_id = u.user_id
                WHERE s.session_id = ? AND s.expires_at > ?
            """,
                (session_id, datetime.utcnow().isoformat()),
            )

            row = cursor.fetchone()
            if not row:
                return None

            (
                user_id,
                username,
                email,
                role_str,
                is_active,
                created_at_str,
                last_login_str,
                password_hash,
                api_key,
                tenant_access_json,
                expires_at_str,
            ) = row

            # Session hash validation handled elsewhere; no placeholder check needed

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
                tenant_access=json.loads(tenant_access_json) if tenant_access_json else [],
            )
        finally:
            pool.release(conn)

    def cleanup_expired_sessions(self) -> int:
        """Remove expired sessions and return count of removed sessions."""
        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            cursor = conn.execute(
                """
                DELETE FROM auth_sessions WHERE expires_at < ?
            """,
                (datetime.utcnow().isoformat(),),
            )
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
                tenant_access=[],  # Access to all tenants
            )

            # In a real system, you'd output this securely via separate channel
            # For now, we'll just note it was created without exposing password
            logger.info("Default admin user created (password generated securely)")

            # Create a default readonly user for testing
            readonly_password = secrets.token_urlsafe(32)
            readonly_user = auth_manager.create_user(
                username="readonly",
                email="readonly@foresight.local",
                password=readonly_password,
                role=Role.READONLY,
                tenant_access=["default"],  # Only access to default tenant
            )

            logger.info("Default readonly user created (password generated securely)")
    finally:
        pool_conn.release(conn)


# FastMCP authentication middleware
from fastmcp.server.middleware import Middleware as _Middleware


_REQUIRE_API_KEY = os.environ.get("FORESIGHT_REQUIRE_API_KEY", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


class AuthMiddleware(_Middleware):
    """FastMCP middleware that authenticates API calls via API key."""

    async def on_call_tool(self, context, call_next):
        # Keep local MCP clients compatible by default.
        # Opt in to strict API-key enforcement only when explicitly enabled.
        if not _REQUIRE_API_KEY:
            return await call_next(context)

        # Extract API key from request metadata
        api_key = None
        message = getattr(context, "message", None)
        if message:
            meta = getattr(message, "meta", None)
            if meta and hasattr(meta, "model_extra") and meta.model_extra:
                api_key = meta.model_extra.get("api_key")
        if not api_key:
            from mcp.types import CallToolResult, TextContent

            return CallToolResult(
                content=[TextContent(type="text", text="Authentication required: missing api_key")],
                isError=True,
            )
        user = get_auth_manager().authenticate_api_key(api_key)
        if not user:
            from mcp.types import CallToolResult, TextContent

            return CallToolResult(
                content=[TextContent(type="text", text="Invalid API key")],
                isError=True,
            )
        # Proceed to next middleware
        return await call_next(context)
