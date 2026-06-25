"""Output formatting for human, JSON, and agent modes.

Three output modes:
- human (default): rich-formatted, colorful, for terminal humans
- json: machine-readable JSON, pipe-safe
- agent: compact, no ANSI, parseable prefix lines, designed for AI agents
"""

from __future__ import annotations

import json as _json
import sys
from dataclasses import dataclass
from typing import Any, Literal

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

OutputMode = Literal["human", "json", "agent"]

_stdout_console = Console(file=sys.stdout)
_stderr_console = Console(file=sys.stderr)


@dataclass
class OutputSettings:
    """Global output settings shared across all commands."""

    mode: OutputMode = "human"
    user_id: str | None = None
    color: bool = True
    verbose: bool = False

    # When True, use stderr for human messages, stdout for JSON/data only
    pipe_safe: bool = False


# Global settings instance
_settings = OutputSettings()


def get_settings() -> OutputSettings:
    return _settings


def configure(
    *,
    mode: OutputMode | None = None,
    color: bool | None = None,
    pipe_safe: bool | None = None,
    verbose: bool | None = None,
) -> None:
    """Update global output settings."""
    if mode is not None:
        _settings.mode = mode
    if color is not None:
        _settings.color = color
    if pipe_safe is not None:
        _settings.pipe_safe = pipe_safe
    if verbose is not None:
        _settings.verbose = verbose


# ── Agent-mode helpers ──────────────────────────────────────────────

_AGENT_PREFIXES = {
    "ok": "[OK]",
    "info": "[INFO]",
    "warn": "[WARN]",
    "error": "[ERROR]",
    "data": "[DATA]",
    "result": "[RESULT]",
    "done": "[DONE]",
}


def _agent_line(prefix_key: str, *parts: str) -> str:
    """Build an agent-parseable output line."""
    prefix = _AGENT_PREFIXES.get(prefix_key, f"[{prefix_key.upper()}]")
    return f"{prefix} {' | '.join(str(p) for p in parts)}"


def _agent_json(label: str, data: Any) -> str:
    """Emit a tagged JSON line for agent consumption."""
    payload = _json.dumps(data, default=str)
    return f"[{label.upper()}] {payload}"


# ── Public API ──────────────────────────────────────────────────────


def stdout(*args: Any, style: str | None = None, sep: str = " ") -> None:
    """Write to stdout respecting output mode."""
    text = sep.join(str(a) for a in args)

    if _settings.mode == "agent":
        print(_agent_line("ok", text))
        return

    if _settings.pipe_safe and _settings.mode == "human":
        _stderr_console.print(text, style=style)
        return

    _stdout_console.print(text, style=style)


def stderr(*args: Any, style: str | None = None, sep: str = " ") -> None:
    """Write to stderr (always visible, never piped)."""
    text = sep.join(str(a) for a in args)
    if _settings.mode == "agent":
        print(_agent_line("info", text), file=sys.stderr)
        return
    _stderr_console.print(text, style=style)


def info(*args: Any, sep: str = " ") -> None:
    """Info-level message."""
    text = sep.join(str(a) for a in args)
    if _settings.mode == "agent":
        print(_agent_line("info", text), file=sys.stderr)
        return
    _stderr_console.print(Text(text, style="cyan"))


def warn(*args: Any, sep: str = " ") -> None:
    """Warning-level message."""
    text = sep.join(str(a) for a in args)
    if _settings.mode == "agent":
        print(_agent_line("warn", text), file=sys.stderr)
        return
    _stderr_console.print(Text(text, style="yellow"))


def error(*args: Any, sep: str = " ") -> None:
    """Error-level message."""
    text = sep.join(str(a) for a in args)
    if _settings.mode == "agent":
        print(_agent_line("error", text), file=sys.stderr)
        return
    _stderr_console.print(Text(text, style="bold red"))


def done(*args: Any, sep: str = " ") -> None:
    """Success/done message."""
    text = sep.join(str(a) for a in args)
    if _settings.mode == "agent":
        print(_agent_line("done", text))
        return
    _stderr_console.print(Text(text, style="bold green"))


def data(label: str, value: Any) -> None:
    """Emit a labeled data point (machine-friendly in agent mode)."""
    if _settings.mode == "agent":
        print(_agent_json("data", {label: value}))
        return
    _stderr_console.print(f"[bold]{label}:[/bold] {value}")


def result_block(data: Any, title: str = "Result") -> None:
    """Display a result block (JSON data)."""
    if _settings.mode == "agent":
        print(_agent_json("result", data))
        return
    if _settings.pipe_safe:
        # On stderr so it doesn't pollute stdout
        _stderr_console.print(Panel(_json.dumps(data, indent=2, default=str), title=title))
    else:
        _stdout_console.print(Panel(_json.dumps(data, indent=2, default=str), title=title))


def print_table(columns: list[str], rows: list[list[Any]], *, title: str | None = None) -> None:
    """Print a table respecting output mode."""
    if _settings.mode == "agent":
        # Tab-separated, first line header
        header = "\t".join(columns)
        print(f"[TABLE] {header}")
        for row in rows:
            print(f"[ROW]   {chr(9).join(str(c) for c in row)}")
        return

    table = Table(*columns, title=title, title_style="bold")
    for row in rows:
        table.add_row(*[str(c) for c in row])
    if _settings.pipe_safe:
        _stderr_console.print(table)
    else:
        _stdout_console.print(table)


def print_json(data: Any) -> None:
    """Print JSON to stdout (works in all modes)."""
    dumped = _json.dumps(data, indent=2, default=str)
    if _settings.mode == "agent":
        print(_agent_json("json", data))
        return
    print(dumped)


def confirm(message: str, default: bool = True) -> bool:
    """Ask user for confirmation. Falls back to default in agent mode."""
    if _settings.mode == "agent":
        return default
    result = _stdout_console.input(f"[bold]{message}[/bold] (y/n) ")
    return result.strip().lower() in ("y", "yes")


def panel(text: str, title: str = "", style: str = "blue") -> None:
    """Show a rich panel."""
    if _settings.mode == "agent":
        print(_agent_line("info", f"{title}: {text}"))
        return
    (_stderr_console if _settings.pipe_safe else _stdout_console).print(Panel(text, title=title, border_style=style))


def markdown(text: str) -> None:
    """Render markdown content."""
    if _settings.mode == "agent":
        print(_agent_line("info", text))
        return
    (_stderr_console if _settings.pipe_safe else _stdout_console).print(Markdown(text))


def bullet_list(items: list[str], title: str | None = None) -> None:
    """Print a bullet list."""
    if _settings.mode == "agent":
        print(_agent_json("list", {"title": title, "items": items}))
        return
    if title:
        stderr(f"\n{title}:", style="bold underline")
    for item in items:
        stderr(f"  • {item}")


def kv_table(pairs: list[tuple[str, Any]], title: str | None = None) -> None:
    """Print a key-value table."""
    if _settings.mode == "agent":
        print(_agent_json("kv", {"title": title, "pairs": {k: v for k, v in pairs}}))
        return
    title_str = f"\n{title}:" if title else ""
    if title_str:
        stderr(title_str, style="bold underline")
    for k, v in pairs:
        stderr(f"  {k}: {v}", style="green" if ":" in k else "")


def die(code: int = 1, *args: Any) -> None:
    """Print error and exit."""
    error(*args)
    raise SystemExit(code)
