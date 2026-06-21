#!/usr/bin/env python3
"""Entry point for running foresight-mcp as a module."""

from __future__ import annotations

import contextlib
import sys
from importlib.metadata import PackageNotFoundError, version as pkg_version

from .server import main as run_server


def main() -> None:
    """Support lightweight CLI flags before starting the MCP server."""
    if "-h" in sys.argv or "--help" in sys.argv:
        return

    if "--health" in sys.argv:
        return

    if "--version" in sys.argv:
        with contextlib.suppress(PackageNotFoundError):
            print(f"foresight-mcp {pkg_version('foresight-mcp')}")
        return

    run_server()


if __name__ == "__main__":
    main()
