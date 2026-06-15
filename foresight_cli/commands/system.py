"""System commands: status, init, doctor, stats, config, history."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import typer
from foresight_mcp import get_system_status
from foresight_mcp.server import init_db

from ..utils import config as cfg, output as out

app = typer.Typer(help="System management, diagnostics, and configuration.")


@app.command()
def status(
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Get Foresight system status and health."""
    init_db()
    resolved_uid = cfg.get_user_id(user_id)
    result = get_system_status(user_id=resolved_uid)

    if out.get_settings().mode in ("agent", "json"):
        if isinstance(result, str):
            try:
                out.print_json(json.loads(result))
            except json.JSONDecodeError:
                out.stdout(result)
        else:
            out.print_json(result)
        return

    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            out.stdout(result)
            return

    if isinstance(result, dict):
        pairs = [
            ("Status", result.get("status", result.get("health", "unknown"))),
            ("Database", result.get("database", result.get("db_path", "?"))),
            ("Bank ID", result.get("bank_id", "?")),
            ("User ID", result.get("user_id", resolved_uid or "?")),
            ("Memory Count", str(result.get("memory_count", result.get("count", 0)))),
            ("Crisis Signals", str(result.get("crisis_signals", 0))),
        ]
        out.kv_table(pairs, title="Foresight Status")

        by_scope = result.get("by_scope", {})
        if isinstance(by_scope, dict) and by_scope:
            out.bullet_list(
                [f"{k}: {v}" for k, v in by_scope.items()],
                title="Memories by Scope",
            )
    else:
        out.stdout(str(result))


@app.command()
def init(
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing config"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="Default user ID"),
    bank_id: str | None = typer.Option(None, "--bank-id", "-b", help="Default bank ID"),
):
    """Initialize Foresight (config, DB, directories)."""
    config_dir = cfg.CONFIG_DIR

    if config_dir.exists() and not force:
        out.info(f"Foresight config directory exists at {config_dir}")
        out.info("Use --force to reinitialize")
    else:
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir.chmod(0o700)
        out.done(f"Created config directory: {config_dir}")

    # Write config
    config = cfg.CliConfig.load()
    if user_id:
        config.user_id = user_id
    if bank_id:
        config.bank_id = bank_id
    config.save()
    out.done(f"Config saved: {cfg.CONFIG_PATH}")

    # Initialize DB
    try:
        init_db()
        out.done("Database initialized")
    except Exception as e:
        out.warn(f"Database init issue: {e}")

    # Verify
    health = get_system_status(user_id=config.user_id)
    out.info(f"Health check: {health[:100] if isinstance(health, str) else 'OK'}")

    out.panel(
        "Foresight is ready. Try:\n"
        "  foresight status          # Check health\n"
        "  foresight store 'hello'   # Store a memory\n"
        "  foresight list            # List memories\n"
        "  foresight tui             # Launch the TUI",
        title="Setup Complete",
    )


@app.command()
def doctor(
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Run comprehensive diagnostics."""
    init_db()
    resolved_uid = cfg.get_user_id(user_id)
    config = cfg.ensure_config()
    passed = 0
    failed = 0
    warnings: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            if out.get_settings().mode == "agent":
                print(f"[CHECK] PASS {name}")
            else:
                out.stdout(f"  ✓ {name}", style="green")
        else:
            failed += 1
            if out.get_settings().mode == "agent":
                print(f"[CHECK] FAIL {name} | {detail}")
            else:
                out.stdout(f"  ✗ {name}: {detail}", style="red")

    if out.get_settings().mode != "agent":
        out.stderr("Foresight Diagnostics", style="bold underline")
        out.stderr("")

    # Python version
    py_ok = sys.version_info >= (3, 11)
    check("Python 3.11+", py_ok, f"Found {sys.version}")

    # Config directory
    check("Config dir exists", cfg.CONFIG_DIR.exists(), str(cfg.CONFIG_DIR))
    check("Config file exists", cfg.CONFIG_PATH.exists(), str(cfg.CONFIG_PATH))

    # DB file
    db_path = Path(config.db_path)
    if db_path.exists():
        size = db_path.stat().st_size
        check("Database file exists", True, f"{size:,} bytes")
    else:
        check("Database file exists", False, "Not yet created (will be on first use)")

    # Config values
    if config.user_id:
        check("User ID configured", True, config.user_id)
    if config.bank_id:
        check("Bank ID configured", True, config.bank_id)

    # Environment
    for env_var in ["FORESIGHT_DB_PATH", "FORESIGHT_USER_ID", "FORESIGHT_BANK_ID"]:
        if os.environ.get(env_var):
            warnings.append(f"{env_var}={os.environ[env_var]}")

    # Test DB connection
    try:
        health = get_system_status(user_id=resolved_uid)
        if isinstance(health, str):
            health = json.loads(health) if health.startswith("{") else {"raw": health}
        if isinstance(health, dict):
            mem_count = health.get("memory_count", health.get("count", "?"))
            check("Database responsive", True, f"Memories: {mem_count}")
        else:
            check("Database responsive", True)
    except Exception as e:
        check("Database responsive", False, str(e))

    # Summary
    if out.get_settings().mode != "agent":
        out.stderr("")
        if failed == 0:
            out.done(f"All {passed} checks passed" + (f" ({len(warnings)} env overrides)" if warnings else ""))
        else:
            out.warn(f"{passed} passed, {failed} failed" + (f" ({len(warnings)} env overrides)" if warnings else ""))

    if warnings and out.get_settings().mode != "agent":
        out.info("Active env overrides:")
        for w in warnings:
            out.info(f"  {w}")

    if failed > 0:
        raise typer.Exit(1)


@app.command()
def stats(
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Show memory statistics and trends."""
    init_db()
    resolved_uid = cfg.get_user_id(user_id)

    try:
        from foresight_mcp import query_memories_temporal

        temporal = query_memories_temporal(options={"window": "month", "limit": 100})
    except Exception:
        temporal = None

    # Get status for baseline stats
    health_raw = get_system_status(user_id=resolved_uid)
    health: dict[str, Any] = {}
    if isinstance(health_raw, str):
        try:
            health = json.loads(health_raw)
        except json.JSONDecodeError:
            pass
    elif isinstance(health_raw, dict):
        health = health_raw

    if out.get_settings().mode == "agent":
        stats_data = {
            "memory_count": health.get("memory_count", health.get("count", 0)),
            "crisis_signals": health.get("crisis_signals", 0),
            "by_scope": health.get("by_scope", {}),
            "temporal": temporal if isinstance(temporal, dict) else None,
        }
        out.print_json(stats_data)
        return

    mem_count = health.get("memory_count", health.get("count", 0))
    crisis = health.get("crisis_signals", 0)

    out.stderr(f"Memory Count: {mem_count}", style="bold")
    out.stderr(f"Crisis Signals: {crisis}")

    by_scope = health.get("by_scope", {})
    if isinstance(by_scope, dict) and by_scope:
        rows = [[k, str(v)] for k, v in by_scope.items()]
        out.print_table(["Scope", "Count"], rows, title="By Scope")

    if temporal:
        out.result_block(temporal if isinstance(temporal, dict) else {"raw": str(temporal)}, title="Temporal Trends")

    if mem_count == 0:
        out.info("No memories yet. Store one with: foresight store 'your first memory'")


@app.command()
def config(
    key: str | None = typer.Argument(None, help="Config key to view/set (e.g. user_id, db_path, bank_id)"),
    value: str | None = typer.Argument(None, help="Value to set (omit to view current)"),
    reset: bool = typer.Option(False, "--reset", help="Reset config to defaults"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """View or modify Foresight configuration."""
    cfg.ensure_config()

    if reset:
        c = cfg.CliConfig()
        c.save()
        out.done("Config reset to defaults")
        return

    c = cfg.CliConfig.load()

    if key and value:
        setattr(c, key, value)
        c.save()
        out.done(f"Set {key} = {value}")
        return

    if key:
        val = getattr(c, key, None)
        if val is not None:
            out.data(key, val)
        else:
            out.error(f"Unknown config key: {key}")
        return

    # Show all config
    pairs = [
        ("db_path", c.db_path),
        ("user_id", c.user_id),
        ("bank_id", c.bank_id),
        ("theme", c.theme),
        ("timeout", str(c.timeout)),
    ]
    out.kv_table(pairs, title="Foresight Configuration")
    out.info(f"\nConfig file: {cfg.CONFIG_PATH}")


@app.command()
def history(
    limit: int = typer.Option(10, "--limit", "-l", help="Number of events"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """View recent memory decay and reinforcement events."""
    init_db()
    resolved_uid = cfg.get_user_id(user_id)

    try:
        from foresight_mcp import get_decay_events

        events = get_decay_events(user_id=resolved_uid, limit=limit)
    except (json.JSONDecodeError, TypeError, OSError) as e:
        out.error(f"Failed to retrieve history: {e}")
        raise typer.Exit(1)

    if out.get_settings().mode == "agent":
        out.print_json({"events": events})
        return

    if isinstance(events, list) and events:
        rows = []
        for ev in events:
            eid = ev.get("memory_id", "?")[:12]
            etype = ev.get("event_type", ev.get("type", "?"))
            strength = ev.get("old_strength", ev.get("strength", "?"))
            ts = ev.get("timestamp", ev.get("created_at", "?"))
            rows.append([eid, etype, str(strength), str(ts)[:19]])
        out.print_table(["Memory", "Event", "Strength", "Timestamp"], rows, title=f"Recent Events (last {limit})")
    else:
        out.info("No events found.")
