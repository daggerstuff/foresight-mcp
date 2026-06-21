---
sidebar_label: Installation
title: Installation Guide
---

# Installation

Install Foresight Memory Architecture for your environment.

## Python Installation

### Using uv (Recommended)

```bash
# Clone the repository
git clone https://github.com/daggerstuff/foresight-mcp.git
cd foresight-mcp

# Install with uv
uv pip install -e .

# Verify installation
python -c "from foresight_mcp import memory_status; print(memory_status())"
```

### Using pip

```bash
pip install foresight-mcp
```

## TypeScript Installation

```bash
# Install the SDK
pnpm add @foresight/core
# or
npm install @foresight/core
# or
yarn add @foresight/core
```

### Package readiness check

The SDK package is verified locally with pnpm before release preparation:

```bash
cd packages/foresight-core
pnpm build
pnpm pack --pack-destination /tmp/foresight-core-pack
```

### TypeScript Configuration

Add to your `tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "strict": true,
    "esModuleInterop": true
  }
}
```

## CLI Installation

The CLI is included with the Python package:

```bash
# Verify CLI installation
foresight --help

# Enable shell completion (bash)
foresight --install-completion
```

## Configuration

### Environment Variables

| Variable            | Description             | Default                  |
| ------------------- | ----------------------- | ------------------------ |
| `FORESIGHT_DB_PATH` | Path to SQLite database | `~/.foresight/memory.db` |
| `FORESIGHT_USER_ID` | Default user ID         | System user              |
| `FORESIGHT_BANK_ID` | Memory bank identifier  | `default`                |

### Config File

Create `~/.foresight/config.json`:

```json
{
  "userId": "my-user-id",
  "bankId": "production",
  "timeout": 30000
}
```

## Docker (Coming Soon)

```bash
docker pull foresight/foresight-mcp:latest
```

## Verification

Run the built-in health check:

```bash
foresight status
```

Expected output:

```json
{
  "status": "healthy",
  "database": "/home/user/.foresight/memory.db",
  "bank_id": "default",
  "user_id": "user",
  "memory_count": 0,
  "crisis_signals": 0,
  "by_scope": {}
}
```

## Troubleshooting

### Import Errors

Ensure you're using Python 3.11+:

```bash
python --version
```

### Permission Issues

If you encounter permission errors:

```bash
# Ensure ~/.foresight directory exists and is writable
mkdir -p ~/.foresight
chmod 700 ~/.foresight
```

### SQLite Errors

The database is created automatically. If you need to reset:

```bash
rm ~/.foresight/memory.db
# Database will be recreated on next operation
```
