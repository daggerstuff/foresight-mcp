#!/usr/bin/env python3
"""
Foresight CLI - Command-line interface for memory operations.

Provides rich terminal output, JSON mode, and shell completion.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.json import JSON
from rich.text import Text

# Import Foresight MCP components
from foresight_mcp import (
    store_memory,
    query_memories,
    list_memories as list_memories_api,
    get_memory as get_memory_api,
    update_memory,
    delete_memory,
    memory_status,
)
from foresight_mcp.server import (
    synthesize_memories,
    archive_memory,
    get_subconscious_block,
)
from foresight_mcp.hooks import list_hooks, register_hook, unregister_hook
from foresight_mcp.block_registry import get_registry

app = typer.Typer(
    name="foresight",
    help="Foresight Memory Management CLI",
    add_completion=True,
)
console = Console()

# Configuration
CONFIG_DIR = Path.home() / ".foresight"
CONFIG_FILE = CONFIG_DIR / "config.json"


def get_config() -> dict:
    """Load configuration from file."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def save_config(config: dict) -> None:
    """Save configuration to file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def output_json(data: dict) -> None:
    """Output data as formatted JSON."""
    console.print(JSON(json.dumps(data)))


# =============================================================================
# Memory Commands
# =============================================================================


@app.command("store")
def cmd_store(
    content: str = typer.Argument(..., help="Memory content to store"),
    scope: str = typer.Option("session", "--scope", "-s", help="Memory scope: session, arc, trait, fact"),
    retention: str = typer.Option("short_term", "--retention", "-r", help="Retention: ephemeral, short_term, long_term, permanent"),
    category: str = typer.Option("fact", "--category", "-c", help="Category label"),
    user_id: Optional[str] = typer.Option(None, "--user-id", "-u", help="User ID override"),
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Store a new memory."""
    result = store_memory(
        content=content,
        category=category,
        scope=scope,
        retention=retention,
        user_id=user_id,
    )

    if _json:
        output_json({"status": "stored", "result": result})
    else:
        console.print(Text(result, style="green"))


@app.command("get")
def cmd_get(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    user_id: Optional[str] = typer.Option(None, "--user-id", "-u", help="User ID override"),
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Retrieve a specific memory by ID."""
    result = get_memory_api(memory_id, user_id=user_id)

    if _json:
        output_json({"id": memory_id, "result": result})
    else:
        console.print(result)


@app.command("list")
def cmd_list(
    limit: int = typer.Option(10, "--limit", "-l", help="Number of memories"),
    offset: int = typer.Option(0, "--offset", "-o", help="Offset"),
    user_id: Optional[str] = typer.Option(None, "--user-id", "-u", help="User ID override"),
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List all memories."""
    result = list_memories_api(user_id=user_id, limit=limit, offset=offset)

    if _json:
        output_json({"memories": result})
    else:
        console.print(result)


@app.command("query")
def cmd_query(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(5, "--limit", "-l", help="Number of results"),
    user_id: Optional[str] = typer.Option(None, "--user-id", "-u", help="User ID override"),
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Search memories by content."""
    result = query_memories(query, user_id=user_id, limit=limit)

    if _json:
        output_json({"query": query, "result": result})
    else:
        console.print(result)


@app.command("update")
def cmd_update(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    content: Optional[str] = typer.Option(None, "--content", "-c", help="New content"),
    category: Optional[str] = typer.Option(None, "--category", help="New category"),
    scope: Optional[str] = typer.Option(None, "--scope", help="New scope"),
    retention: Optional[str] = typer.Option(None, "--retention", help="New retention"),
    user_id: Optional[str] = typer.Option(None, "--user-id", "-u", help="User ID override"),
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Update an existing memory."""
    result = update_memory(
        memory_id=memory_id,
        content=content,
        category=category,
        scope=scope,
        retention=retention,
        user_id=user_id,
    )

    if _json:
        output_json({"id": memory_id, "result": result})
    else:
        console.print(Text(result, style="yellow"))


@app.command("delete")
def cmd_delete(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    user_id: Optional[str] = typer.Option(None, "--user-id", "-u", help="User ID override"),
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Delete a memory by ID."""
    result = delete_memory(memory_id, user_id=user_id)

    if _json:
        output_json({"id": memory_id, "result": result})
    else:
        console.print(Text(result, style="red"))


@app.command("synthesize")
def cmd_synthesize(
    user_id: Optional[str] = typer.Option(None, "--user-id", "-u", help="User ID override"),
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Run synthesis on all memories to detect stance shifts and merge candidates."""
    result = synthesize_memories(user_id=user_id)

    if _json:
        output_json({"result": result})
    else:
        console.print(result)


@app.command("archive")
def cmd_archive(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    user_id: Optional[str] = typer.Option(None, "--user-id", "-u", help="User ID override"),
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Archive a memory to a ghost node."""
    result = archive_memory(memory_id, user_id=user_id)

    if _json:
        output_json({"id": memory_id, "result": result})
    else:
        console.print(Text(result, style="dim"))


# =============================================================================
# Block Commands
# =============================================================================


block_app = typer.Typer(help="Memory block management.")
app.add_typer(block_app, name="block")


@block_app.command("list")
def cmd_block_list(
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List all registered block schemas."""
    registry = get_registry()
    schemas = registry.list_schemas()

    if _json:
        output_json({"schemas": [s.to_dict() for s in schemas]})
    else:
        table = Table(title="Memory Block Schemas")
        table.add_column("Label", style="cyan")
        table.add_column("Description")
        table.add_column("Retention", style="magenta")
        table.add_column("Merge", style="green")
        table.add_column("Injection", style="blue")

        for schema in schemas:
            table.add_row(
                schema.label,
                schema.description,
                schema.retention_policy.value,
                schema.merge_strategy.value,
                schema.injection_point.value,
            )

        console.print(table)


@block_app.command("create")
def cmd_block_create(
    label: str = typer.Argument(..., help="Block label"),
    content: str = typer.Option("", "--content", "-c", help="Block content"),
    user_id: Optional[str] = typer.Option(None, "--user-id", "-u", help="User ID override"),
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Create a new memory block."""
    registry = get_registry()
    block = registry.create_block(label, content)

    if _json:
        output_json({"block": block.to_dict()})
    else:
        console.print(Text(f"Created block '{label}'", style="green"))


@block_app.command("get")
def cmd_block_get(
    label: str = typer.Argument(..., help="Block label"),
    user_id: Optional[str] = typer.Option(None, "--user-id", "-u", help="User ID override"),
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Get a specific memory block."""
    result = get_subconscious_block(label, user_id=user_id)

    if _json:
        output_json({"label": label, "result": result})
    else:
        console.print(result)


# =============================================================================
# Hook Commands
# =============================================================================


hook_app = typer.Typer(help="Event hook management.")
app.add_typer(hook_app, name="hook")


@hook_app.command("list")
def cmd_hook_list(
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List all registered hooks."""
    result = list_hooks()

    if _json:
        output_json({"result": result})
    else:
        console.print(result)


@hook_app.command("register")
def cmd_hook_register(
    name: str = typer.Argument(..., help="Hook name"),
    event_type: str = typer.Argument(..., help="Event type (e.g., memory.stored)"),
    url: str = typer.Option(..., "--url", "-u", help="Webhook URL"),
    retry_count: int = typer.Option(3, "--retry", "-r", help="Retry count"),
    timeout: int = typer.Option(30, "--timeout", "-t", help="Timeout in seconds"),
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Register a new HTTP webhook hook."""
    result = register_hook(
        name=name,
        event_type=event_type,
        hook_type="http",
        url=url,
        retry_count=retry_count,
        timeout=timeout,
    )

    if _json:
        output_json({"result": result})
    else:
        console.print(Text(result, style="green"))


@hook_app.command("unregister")
def cmd_hook_unregister(
    hook_id: str = typer.Argument(..., help="Hook ID"),
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Unregister a hook by ID."""
    result = unregister_hook(hook_id)

    if _json:
        output_json({"id": hook_id, "result": result})
    else:
        console.print(Text(result, style="yellow"))


# =============================================================================
# Event Commands
# =============================================================================


event_app = typer.Typer(help="Event log and audit trail.")
app.add_typer(event_app, name="event")


@event_app.command("log")
def cmd_event_log(
    since: str = typer.Option("1h", "--since", "-s", help="Time range (e.g., 1h, 30m, 2d)"),
    entity: Optional[str] = typer.Option(None, "--entity", "-e", help="Entity filter (e.g., memory:*)"),
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """View event log."""
    # TODO: Implement event log retrieval from EventStore
    result = "Event log retrieval not yet implemented"

    if _json:
        output_json({"result": result})
    else:
        console.print(result)


# =============================================================================
# Status Command
# =============================================================================


@app.command("status")
def cmd_status(
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Get system status."""
    result = memory_status()

    if _json:
        output_json(json.loads(result))
    else:
        console.print(result)


# =============================================================================
# Main entry point
# =============================================================================

def main():
    """CLI entry point."""
    app()


if __name__ == "__main__":
    app()
