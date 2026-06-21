#!/usr/bin/env python3
"""Compatibility wrapper — redirects to the new modular CLI entry point."""

from foresight_cli.cli import app

if __name__ == "__main__":
    app()
