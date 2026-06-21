#!/usr/bin/env bash
# Publish foresight-mcp to PyPI.
#
# Usage:
#   ./scripts/pypi-publish.sh              # Build + publish to PyPI
#   ./scripts/pypi-publish.sh --test        # Build + publish to TestPyPI
#   ./scripts/pypi-publish.sh --check       # Build + check only (no publish)
#
# Prerequisites:
#   - PyPI token in $PYPI_TOKEN or $TEST_PYPI_TOKEN
#   - uv installed
#   - twine installed (dev extra: `uv sync --extra dev`)
#
# Token auth:
#   export PYPI_TOKEN=pypi-xxxxxxxx
#   echo "$PYPI_TOKEN" | uv tool run twine upload dist/* --password stdin

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

MODE="${1:---publish}"

echo "=== foresight-mcp PyPI Publisher ==="
echo ""

# Clean previous builds
rm -rf dist/ build/ *.egg-info

# Build
echo "→ Building sdist + wheel..."
uv build
echo "  ✓ dist/ ready"
echo ""

# Check
echo "→ Running twine check..."
uv run twine check dist/* 2>&1
echo ""

if [ "$MODE" = "--check" ]; then
    echo "✓ Build verified. Not publishing (--check mode)."
    echo "  Artifacts:"
    ls -lh dist/
    exit 0
fi

if [ "$MODE" = "--test" ]; then
    echo "→ Publishing to TestPyPI..."
    if [ -z "${TEST_PYPI_TOKEN:-}" ]; then
        echo "  ERROR: TEST_PYPI_TOKEN not set." >&2
        echo "  export TEST_PYPI_TOKEN=pypi-xxxxxxxx" >&2
        exit 1
    fi
    uv run twine upload --repository testpypi --password "$TEST_PYPI_TOKEN" dist/*
    echo ""
    echo "✓ Published to TestPyPI!"
    echo "  Install: pip install --index-url https://test.pypi.org/simple/ foresight-mcp[all]"
    exit 0
fi

# Default: publish to PyPI
echo "→ Publishing to PyPI..."
if [ -z "${PYPI_TOKEN:-}" ]; then
    echo "  ERROR: PYPI_TOKEN not set." >&2
    echo "  export PYPI_TOKEN=pypi-xxxxxxxx" >&2
    exit 1
fi
uv run twine upload --password "$PYPI_TOKEN" dist/*
echo ""
echo "✓ Published to PyPI!"
echo "  Install: pip install foresight-mcp[all]"
echo "  Or:      uv pip install foresight-mcp[all]"
