"""Dashboard screen — system status overview."""

from __future__ import annotations

import json

from foresight_mcp import get_system_status
from foresight_mcp.server import init_db
from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.screen import Screen
from textual.widgets import Label, Static


class StatCard(Static):
    """A card displaying a single statistic."""

    def __init__(self, label: str, value: str = "—", *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._label = label
        self._value = value

    def on_mount(self) -> None:
        self.update(f"[bold]{self._label}[/bold]\n\n[size=24]{self._value}[/size]")

    def set_value(self, value: str) -> None:
        self._value = value
        self.update(f"[bold]{self._label}[/bold]\n\n[size=24]{value}[/size]")


class DashboardScreen(Screen):
    """Main dashboard with system stats and quick actions."""

    def compose(self) -> ComposeResult:
        yield Container(
            Label("[bold]System Status[/bold]", classes="section-title"),
            Horizontal(
                StatCard("Memories", "..."),
                StatCard("Crisis Signals", "..."),
                StatCard("Database", "..."),
                classes="stats-grid",
            ),
            Static("", id="status-detail"),
            Label("\n[bold]Quick Commands[/bold]", classes="section-title"),
            Static("[dim]Outside the TUI:[/dim]"),
            Static("  foresight store 'your memory here'    Store a new memory"),
            Static("  foresight query 'search term'         Search memories"),
            Static("  foresight --agent status              Agent-friendly status"),
            Static("  foresight doctor                       Run diagnostics"),
            Static("  foresight export memories.json         Export all memories"),
            id="dashboard-content",
        )

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        """Refresh dashboard data."""
        try:
            init_db()
            result = get_system_status()

            details: dict = {}
            if isinstance(result, str):
                try:
                    details = json.loads(result)
                except json.JSONDecodeError:
                    details = {"raw": result}
            elif isinstance(result, dict):
                details = result

            mem_count = details.get("memory_count", details.get("count", "?"))
            crisis = details.get("crisis_signals", 0)
            db_path = details.get("database", details.get("db_path", "?"))

            # Update stat cards
            for child in self.query(StatCard):
                label = child._label
                if "Memories" in label:
                    child.set_value(str(mem_count))
                elif "Crisis" in label:
                    child.set_value(str(crisis))
                elif "Database" in label:
                    child.set_value("OK" if db_path else "Not configured")

            detail_widget = self.query_one("#status-detail", Static)
            detail_widget.update(
                f"\n[dim]Database:[/dim] {db_path}\n"
                f"[dim]User:[/dim] {details.get('user_id', 'default')}\n"
                f"[dim]Bank:[/dim] {details.get('bank_id', 'default')}\n"
                f"[dim]Scopes:[/dim] {json.dumps(details.get('by_scope', {}))}\n"
            )

        except Exception as e:
            detail_widget = self.query_one("#status-detail", Static)
            detail_widget.update(f"[red]Error loading status: {e}[/red]")
