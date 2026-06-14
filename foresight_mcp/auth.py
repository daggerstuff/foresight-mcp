"""
Authentication System for Foresight MCP
Provides API key-based authentication with secure password hashing.
"""

import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from fastmcp.server.middleware import Middleware as _Middleware
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent

from .config import DB_PATH
from .connection_pool import get_pool
from .tenant_middleware import resolve_tenant_id_from_message

logger = logging.getLogger(__name__)

# Optional dependencies for password hashing
try:
    from argon2 import PasswordHasher

    HAS_ARGON2 = True
except Exception:
    PasswordHasher = None
    HAS_ARGON2 = False

try:
    import bcrypt

    HAS_BCRYPT = True
except Exception:
    bcrypt = None
    HAS_BCRYPT = False


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
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_login: datetime | None = None
    # Hashed password (Argon2, bcrypt, or PBKDF2-SHA256)
    password_hash: str = ""
    # API key for programmatic access
    api_key: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    # Associated tenant(s) - empty list means access to all tenants
    tenant_access: list[str] = field(default_factory=list)


class AuthError(Exception):
    """Authentication-related errors."""


_VALID_TENANT_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def _utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _env_truthy(value: str | None) -> bool:
    """Interpret common truthy environment-variable values."""
    if value is None:
        return False
    return value.lower() in {"1", "true", "yes", "on"}


def _should_require_api_key() -> bool:
    """Require API keys by default unless an explicit local override disables it."""
    explicit = os.environ.get("FORESIGHT_REQUIRE_API_KEY")
    if explicit is not None:
        return _env_truthy(explicit)
    return not _env_truthy(os.environ.get("FORESIGHT_ALLOW_UNAUTHENTICATED"))


def _parse_db_timestamp(value: str | None) -> datetime | None:
    """Parse stored timestamps and normalize legacy naive values to UTC."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


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
        """Hash a password using Argon2, bcrypt, or PBKDF2-SHA256."""
        # Use optional dependencies if available
        if HAS_ARGON2 and PasswordHasher:
            ph = PasswordHasher()
            return ph.hash(password)
        # Fallback to bcrypt
        if HAS_BCRYPT and bcrypt:
            hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
            return hashed.decode("utf-8")
        # Final fallback: PBKDF2-SHA256 from the Python standard library
        iterations = 600_000
        salt = secrets.token_hex(16)
        hash_bytes = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            iterations,
        ).hex()
        return f"pbkdf2_sha256${iterations}${salt}${hash_bytes}"

    def _verify_password(self, password: str, password_hash: str) -> bool:  # noqa: PLR0911
        """Verify a password against its stored Argon2, bcrypt, PBKDF2, or legacy hash."""
        # Use optional dependencies if available
        if HAS_ARGON2 and PasswordHasher:
            try:
                ph = PasswordHasher()
                return ph.verify(password_hash, password)
            except Exception:
                # Continue to next fallback
                pass

        if HAS_BCRYPT and bcrypt:
            try:
                return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
            except Exception:
                # Continue to next fallback
                pass

        # Fallback to PBKDF2 verification
        if password_hash.startswith("pbkdf2_sha256$"):
            try:
                _, iteration_str, salt, stored_hash = password_hash.split("$", 3)
                calc_hash = hashlib.pbkdf2_hmac(
                    "sha256",
                    password.encode("utf-8"),
                    salt.encode("utf-8"),
                    int(iteration_str),
                ).hex()
                return secrets.compare_digest(calc_hash, stored_hash)
            except Exception:
                return False

        # Legacy fallback for pre-hardening hashes
        if password_hash.startswith("sha256$"):
            try:
                _, salt, stored_hash = password_hash.split("$")
                calc_hash = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
                return secrets.compare_digest(calc_hash, stored_hash)
            except Exception:
                return False
        return False

    def create_user(
        self,
        username: str,
        email: str,
        password: str,
        role: Role = Role.USER,
        tenant_access: list[str] | None = None,
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
                    _utcnow().isoformat(),
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
                created_at=_utcnow(),
                password_hash=password_hash,
                api_key=api_key,
                tenant_access=tenant_access,
            )
        finally:
            pool.release(conn)

    def authenticate_user(self, username: str, password: str) -> User | None:
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

            # Update last login
            conn.execute(
                """
                UPDATE users SET last_login = ? WHERE user_id = ?
            """,
                (_utcnow().isoformat(), user_id),
            )
            conn.commit()

            return User(
                user_id=user_id,
                username=username,
                email=email,
                role=Role(role_str),
                is_active=bool(is_active),
                created_at=_parse_db_timestamp(created_at_str) or _utcnow(),
                last_login=_parse_db_timestamp(last_login_str),
                password_hash=password_hash,
                api_key=api_key,
                tenant_access=json.loads(tenant_access_json) if tenant_access_json else [],
            )
        finally:
            pool.release(conn)

    def authenticate_api_key(self, api_key: str) -> User | None:
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

            # Update last login
            conn.execute(
                """
                UPDATE users SET last_login = ? WHERE user_id = ?
            """,
                (_utcnow().isoformat(), user_id),
            )
            conn.commit()

            return User(
                user_id=user_id,
                username=username,
                email=email,
                role=Role(role_str),
                is_active=bool(is_active),
                created_at=_parse_db_timestamp(created_at_str) or _utcnow(),
                last_login=_parse_db_timestamp(last_login_str),
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
        expires_at = _utcnow() + timedelta(hours=24)

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
                    _utcnow().isoformat(),
                    expires_at.isoformat(),
                    ip_address,
                    user_agent,
                ),
            )
            conn.commit()
            return session_id
        finally:
            pool.release(conn)

    def validate_session(self, session_id: str) -> User | None:
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
                WHERE s.session_id = ? AND s.expires_at > ? AND u.is_active = 1
            """,
                (session_id, _utcnow().isoformat()),
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
                _expires_at_str,
            ) = row

            # Session hash validation handled elsewhere; no placeholder check needed

            return User(
                user_id=user_id,
                username=username,
                email=email,
                role=Role(role_str),
                is_active=bool(is_active),
                created_at=_parse_db_timestamp(created_at_str) or _utcnow(),
                last_login=_parse_db_timestamp(last_login_str),
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
                (_utcnow().isoformat(),),
            )
            deleted_count = cursor.rowcount
            conn.commit()
            return deleted_count
        finally:
            pool.release(conn)


# Global auth manager instance
class _AuthManagerSingleton:
    """Thread-safe singleton holder for AuthManager."""

    def __init__(self):
        self._instance: AuthManager | None = None
        self._lock = __import__("threading").RLock()

    def get(self) -> AuthManager:
        with self._lock:
            if self._instance is None:
                self._instance = AuthManager()
            return self._instance

    def reset(self) -> None:
        with self._lock:
            self._instance = None


_auth_manager = _AuthManagerSingleton()


def get_auth_manager() -> AuthManager:
    """Get the global auth manager instance."""
    return _auth_manager.get()


def initialize_default_users() -> None:
    """Create default users if none exist."""
    auth_manager = get_auth_manager()

    # Check if any users exist
    pool_conn = get_pool(DB_PATH)
    conn = pool_conn.acquire()

    try:
        cursor = conn.execute("SELECT COUNT(*) FROM users")
        count = cursor.fetchone()[0]

        if count == 0:
            # Create default admin user
            admin_password = secrets.token_urlsafe(32)  # Increase to 32 chars for stronger password
            auth_manager.create_user(
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
            auth_manager.create_user(
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


class AuthMiddleware(_Middleware):
    """FastMCP middleware that authenticates API calls via API key."""

    @staticmethod
    def _error_result(message: str) -> ToolResult:
        return ToolResult(
            content=[TextContent(type="text", text=message)],
            meta={"isError": True},
        )

    async def on_call_tool(self, context, call_next):
        if not _should_require_api_key():
            return await call_next(context)

        # Extract API key from request metadata
        api_key = None
        message = getattr(context, "message", None)
        if message:
            meta = getattr(message, "meta", None)
            if meta and hasattr(meta, "model_extra") and meta.model_extra:
                api_key = meta.model_extra.get("api_key")
        if not api_key:
            return self._error_result("Authentication required: missing api_key")
        user = get_auth_manager().authenticate_api_key(api_key)
        if not user:
            return self._error_result("Invalid API key")
        tenant_id = resolve_tenant_id_from_message(message)
        if not get_auth_manager().validate_user_tenant_access(user, tenant_id):
            return self._error_result(f"Tenant access denied for tenant '{tenant_id}'")
        # Proceed to next middleware
        return await call_next(context)
