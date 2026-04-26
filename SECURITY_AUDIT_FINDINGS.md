# Foresight MCP Detailed Security Audit Findings

## CRITICAL SEVERITY

### C1: Hardcoded Password Exposure
- **File**: `/home/vivi/pixelated/foresight-mcp/foresight_mcp/auth.py:402`
- **Issue**: Default readonly user created with hardcoded password "readonly123"
- **Impact**: Anyone with access to source code knows the password
- **Proof**: Line 402: `password="readonly123",  # In production, this would be randomly generated`

### C2: Credential Logging
- **File**: `/home/vivi/pixelated/foresight-mcp/foresight_mcp/auth.py:396`
- **Issue**: Default admin password printed in plain text during startup
- **Impact**: Passwords exposed in logs, terminal output
- **Proof**: Line 396: `print(f"[AUTH] Default admin user created: username='admin', password='{admin_password}' (SAVE THIS SECURELY)")`

## HIGH SEVERITY

### H1: Weak Password Generation
- **File**: `/home/vivi/pixelated/foresight-mcp/foresight_mcp/auth.py:385`
- **Issue**: Admin password uses only `secrets.token_urlsafe(16)` (16 chars)
- **Impact**: Reduced entropy for initial admin password
- **Proof**: Line 385: `admin_password = secrets.token_urlsafe(16)  # Generate secure password`

### H2: Insufficient Tenant ID Validation
- **File**: Multiple files using `get_current_tenant_id()`
- **Issue**: Minimal validation on tenant_id values before use in SQL
- **Impact**: Potential for SQL injection if malicious tenant_id is set
- **Proof**: Tenant ID flows through contextvar to SQL queries with limited validation

## MEDIUM SEVERITY

### M1: Information Disclosure in Error Messages
- **File**: Various files raising exceptions with internal details
- **Issue**: Some error messages may expose internal system details
- **Impact**: Information leakage that could aid attackers
- **Examples**: 
  - `/home/vivi/pixelated/foresight-mcp/foresight_mcp/projections/builder.py:207`: Path disclosure in error message
  - `/home/vivi/pixelated/foresight-mcp/foresight_mcp/connection_pool.py:54-56`: Pool exhaustion details

## LOW SEVERITY

### L1: File Operations Without Path Validation
- **File**: Multiple files using open() operations
- **Issue**: File write operations without path traversal validation
- **Impact**: Potential for writing files outside intended directories
- **Examples**:
  - `/home/vivi/pixelated/foresight-mcp/foresight_mcp/projections/builder.py`: Lines with `open(output_path, "w")`
  - `/home/vivi/pixelated/foresight-mcp/foresight_mcp/rrf_tuning.py`: Lines with `open(path, "w")` and `open(path, "r")`
  - `/home/vivi/pixelated/foresight-mcp/foresight_mcp/consumer_group.py`: Line with `open(self.state_file, "r")`

## VERIFIED SECURE COMPONENTS

### ✅ SQL Injection Prevention
- All database queries use parameterized statements with `?` placeholders
- No string concatenation or f-string SQL building found
- SQL helpers module provides additional validation layers

### ✅ Authentication System
- PBKDF2 with SHA256 and 100,000 iterations for password hashing
- Secure random salts using `secrets.token_hex(16)`
- HMAC-based comparison to prevent timing attacks
- API keys generated with `secrets.token_urlsafe(32)`

### ✅ Session Management
- Session IDs generated with `secrets.token_urlsafe(32)`
- 24-hour expiration with automatic cleanup
- Secure storage and validation of sessions

### ✅ Tenant Isolation
- ContextVars-based request-scoped tenant resolution
- Proper cleanup in middleware (reset to default after each request)
- Tenant ID included in all relevant table schemas and queries

### ✅ No Dangerous Functions
- No instances of `eval()` or `exec()` with user input
- No `pickle.load()` usage with external data
- No unsafe `yaml.load()` (only safe usage found in dependencies)
- No `os.system()` or `subprocess.*` calls with user input