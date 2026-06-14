"""Analysis commands: synthesize, reflect, profile, diff, rollback."""

from __future__ import annotations

import json

import typer
from foresight_mcp import (
    AnalysisAction,
    ProfileConfig,
    VersionAction,
    analyze_memories,
    manage_memory_versions,
    profile_to_prompt,
    synthesize_profile,
)
from foresight_mcp.server import init_db

from ..utils import config as cfg, output as out

app = typer.Typer(help="Analyze, synthesize, reflect, and version memories.")


def _init_and_user(user_id_override: str | None = None):
    init_db()
    return cfg.get_user_id(user_id_override)


@app.command()
def synthesize(
    limit: int = typer.Option(50, "--limit", "-l", help="Memory limit for synthesis"),
    enhanced: bool = typer.Option(False, "--enhanced", help="Use enhanced synthesis with contradiction detection"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Run synthesis to find patterns, contradictions, and insights."""
    _init_and_user(user_id)
    result = analyze_memories(options=AnalysisAction(action="synthesize", limit=limit, enhanced=enhanced))

    if out.get_settings().mode == "agent":
        out.print_json({"synthesis": result})
    else:
        out.result_block(result, title="Memory Synthesis")


@app.command()
def reflect(
    period: str = typer.Option("weekly", "--period", "-p", help="Reflection period (daily/weekly/monthly)"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Run reflection analysis over a time period."""
    _init_and_user(user_id)
    result = analyze_memories(options=AnalysisAction(action="reflect", period=period))

    if out.get_settings().mode == "agent":
        out.print_json({"reflection": result})
    else:
        out.result_block(result, title=f"Reflection ({period})")


@app.command()
def profile(
    max_static: int = typer.Option(20, "--max-static", help="Max stable-fact memories"),
    max_dynamic: int = typer.Option(10, "--max-dynamic", help="Max recent-context memories"),
    no_synthesis: bool = typer.Option(False, "--no-synthesis", help="Disable contradiction detection"),
    prompt_format: bool = typer.Option(False, "--prompt", "-p", help="Output as formatted prompt block"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Build a user profile (static facts + dynamic context)."""
    uid = _init_and_user(user_id)
    result = synthesize_profile(
        uid,
        config=ProfileConfig(
            max_static_memories=max_static,
            max_dynamic_memories=max_dynamic,
            include_synthesis=not no_synthesis,
        ),
    )

    if prompt_format:
        result = profile_to_prompt(result)

    if out.get_settings().mode == "agent" or prompt_format:
        out.console_print(result)
        return

    try:
        parsed = result if isinstance(result, dict) else json.loads(result)
        static_items = parsed.get("static", [])
        dynamic_items = parsed.get("dynamic", [])
        out.stderr(
            f"User Profile ({len(static_items)} static, {len(dynamic_items)} dynamic items)", style="bold underline"
        )
        if static_items:
            out.stderr("\nStatic (stable facts):", style="underline")
            for s in static_items:
                out.stderr(f"  • {s}")
        if dynamic_items:
            out.stderr("\nDynamic (recent context):", style="underline")
            for d in dynamic_items:
                out.stderr(f"  • {d}")
    except (json.JSONDecodeError, TypeError):
        out.console_print(result)


@app.command()
def diff(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    v1: int = typer.Argument(..., help="Version 1"),
    v2: int = typer.Argument(..., help="Version 2"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Compare two versions of a memory."""
    _init_and_user(user_id)
    result = manage_memory_versions(
        options=VersionAction(action="diff", memory_id=memory_id, version1=v1, version2=v2),
    )
    out.result_block(result, title=f"Diff {memory_id} (v{v1} → v{v2})")


@app.command()
def rollback(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    version: int = typer.Argument(..., help="Version to rollback to"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Rollback a memory to a specific version."""
    _init_and_user(user_id)
    result = manage_memory_versions(
        options=VersionAction(action="rollback", memory_id=memory_id, to_version=version),
    )
    out.warn(f"Rolled back {memory_id} to version {version}: {result}")
