"""Context block management commands."""

from __future__ import annotations

import json

import typer
from foresight_mcp import ContextBlockAction, manage_context_blocks
from foresight_mcp.server import init_db

from ..utils import config as cfg, output as out

app = typer.Typer(help="Manage Foresight context blocks (guidance, preferences, state).")


def _init_and_user(user_id_override: str | None = None):
    init_db()
    return cfg.get_user_id(user_id_override)


def _emit(result: str) -> None:
    """Render a tool result envelope."""
    try:
        payload = json.loads(result)
    except Exception:
        out.stderr(result)
        return

    if not payload.get("ok", False):
        message = payload.get("error", {}).get("message", result)
        out.error(message)
        raise typer.Exit(1)

    if out.get_settings().mode == "agent":
        out.print_json(payload)
        return

    if "label" in payload and "content" in payload:
        out.stderr(f"\n[{payload['label']}]", style="bold underline")
        out.stdout(payload["content"])
        return
    if "blocks" in payload:
        blocks = payload["blocks"]
        if isinstance(blocks, list):
            out.bullet_list(
                [f"{b.get('label', '?')}: {str(b.get('content', ''))[:60]}" for b in blocks], title="Context Blocks"
            )
        else:
            out.print_json(blocks)
        return
    if "run" in payload:
        out.print_json(payload["run"])
        return

    out.print_json(payload)


@app.command("list")
def list_blocks(
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """List non-empty context blocks."""
    _init_and_user(user_id)
    result = manage_context_blocks(options=ContextBlockAction(action="list"))
    _emit(result)


@app.command("get")
def get_block(
    label: str = typer.Argument(..., help="Block label (e.g. guidance, preferences, pending_items)"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Get a specific context block."""
    _init_and_user(user_id)
    result = manage_context_blocks(options=ContextBlockAction(action="get", label=label))
    _emit(result)


@app.command("update")
def update_block(
    label: str = typer.Argument(..., help="Block label"),
    content: str = typer.Argument(..., help="New block content"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Update a context block with new content."""
    _init_and_user(user_id)
    result = manage_context_blocks(options=ContextBlockAction(action="update", label=label, content=content))
    _emit(result)


@app.command("reset")
def reset_block(
    label: str = typer.Argument(..., help="Block label to reset"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Reset a context block to its default value."""
    _init_and_user(user_id)
    result = manage_context_blocks(options=ContextBlockAction(action="reset", label=label))
    _emit(result)


@app.command("clear")
def clear_block(
    label: str = typer.Argument(..., help="Block label to clear"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Clear a context block's content."""
    _init_and_user(user_id)
    result = manage_context_blocks(options=ContextBlockAction(action="clear", label=label))
    _emit(result)
