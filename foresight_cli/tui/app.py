"""Foresight Textual TUI — main application."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import var
from textual.screen import Screen
from textual.widgets import Footer, Header, TabbedContent, TabPane

from ..utils.config import CliConfig
from .screens.blocks import BlocksScreen
from .screens.dashboard import DashboardScreen
from .screens.memories import MemoriesScreen


class ForesightTUI(App):
    """Foresight interactive terminal UI."""

    DEFAULT_CSS = """
    Screen {
        background: $surface;
    }

    Header {
        background: $primary;
        color: $text;
    }

    Footer {
        background: $panel;
        color: $text-muted;
    }

    TabbedContent {
        height: 100%;
    }

    TabPane {
        padding: 1;
    }

    .stats-grid {
        layout: grid;
        grid-size: 3;
        grid-gutter: 1;
        height: auto;
        margin: 1 0;
    }

    .stat-card {
        border: solid $primary;
        padding: 1;
        height: auto;
    }

    .stat-label {
        color: $text-muted;
        text-style: bold;
    }

    .stat-value {
        color: $text;
        text-style: bold;
        content-align: center middle;
        height: 3;
    }

    .memory-list {
        height: 1fr;
    }

    .search-box {
        margin: 0 0 1 0;
    }

    .detail-panel {
        border: solid $secondary;
        height: 1fr;
        padding: 1;
    }

    Button {
        margin: 0 1;
    }

    .action-bar {
        height: auto;
        margin: 1 0;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("d", "toggle_dark", "Dark mode", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("1", "switch_tab('dashboard')", "Dashboard", show=False),
        Binding("2", "switch_tab('memories')", "Memories", show=False),
        Binding("3", "switch_tab('blocks')", "Blocks", show=False),
    ]

    TITLE = "Foresight"
    SUB_TITLE = "Memory Management Terminal"

    user_id: str | None = var(None)
    config: CliConfig | None = var(None)

    def __init__(self, user_id: str | None = None, config: CliConfig | None = None) -> None:
        super().__init__()
        self.user_id = user_id
        self.config = config

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="dashboard"):
            with TabPane("Dashboard", id="dashboard"):
                yield DashboardScreen()
            with TabPane("Memories", id="memories"):
                yield MemoriesScreen()
            with TabPane("Blocks", id="blocks"):
                yield BlocksScreen()
        yield Footer()

    def action_toggle_dark(self) -> None:
        self.dark = not self.dark

    def action_refresh(self) -> None:
        """Refresh all screens."""
        for screen in self.screen_stack:
            if hasattr(screen, "refresh_data"):
                screen.refresh_data()

    def on_mount(self) -> None:
        """Called when the app is mounted."""
        self.refresh_data()

    def refresh_data(self) -> None:
        """Refresh data on all active screens."""
        for child in self.query(Screen):
            if hasattr(child, "refresh_data"):
                child.refresh_data()
