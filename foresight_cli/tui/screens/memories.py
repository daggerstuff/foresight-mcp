"""Memories screen — browse, search, and manage memories."""

from __future__ import annotations


from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from foresight_mcp import SearchOptions, search_memories, store_memory
from foresight_mcp.server import init_db

MEMORY_CATEGORIES = ["fact", "preference", "insight", "observation", "decision", "goal"]


class MemoryItem(ListItem):
    """A single memory in the list."""

    def __init__(self, memory_data: dict, **kwargs) -> None:
        super().__init__(**kwargs)
        self.memory_data = memory_data
        mid = memory_data.get("memory_id", memory_data.get("id", "?"))
        content = str(memory_data.get("content", ""))[:120]
        cat = memory_data.get("category", "-")
        scope = memory_data.get("scope", "-")
        label = f"[bold]{mid[:12]}[/bold] [{cat}] ({scope}) {content}"
        self._label = label

    def on_mount(self) -> None:
        self.update(self._label)


class MemoriesScreen(Screen):
    """Browse and search memories."""

    def compose(self) -> ComposeResult:
        yield Label("[bold]Memories[/bold]", classes="section-title")
        yield Horizontal(
            Input(placeholder="Search memories...", id="memory-search", classes="search-box"),
            Button("Search", variant="primary", id="btn-search"),
            Button("Refresh", id="btn-refresh"),
            classes="action-bar",
        )
        yield Horizontal(
            ListView(id="memory-list", classes="memory-list"),
            Vertical(
                Static("[bold]Details[/bold]", id="detail-title"),
                Static("Select a memory to view details", id="memory-detail", classes="detail-panel"),
                Label("\n[bold]Quick Store[/bold]"),
                Input(placeholder="Content...", id="new-memory-input"),
                Horizontal(
                    Button("Store", variant="success", id="btn-store"),
                    Button("Clear", id="btn-clear"),
                ),
                id="detail-column",
            ),
        )

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        """Load and display memories."""
        try:
            init_db()
            result = search_memories(options=SearchOptions(query_type="list", limit=50, offset=0))

            list_view = self.query_one("#memory-list", ListView)
            list_view.clear()

            if isinstance(result, list):
                for mem in result:
                    list_view.append(MemoryItem(mem))
            else:
                list_view.append(ListItem(Static("No memories found or error loading.")))

        except Exception as e:
            list_view = self.query_one("#memory-list", ListView)
            list_view.clear()
            list_view.append(ListItem(Static(f"[red]Error: {e}[/red]")))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Show memory details when selected."""
        item = event.item
        if isinstance(item, MemoryItem):
            detail = self.query_one("#memory-detail", Static)
            data = item.memory_data
            detail.update(
                f"[bold]ID:[/bold] {data.get('memory_id', data.get('id', '?'))}\n"
                f"[bold]Category:[/bold] {data.get('category', '-')}\n"
                f"[bold]Scope:[/bold] {data.get('scope', '-')}\n"
                f"[bold]Retention:[/bold] {data.get('retention', '-')}\n"
                f"[bold]Importance:[/bold] {data.get('importance', '-')}\n"
                f"\n[bold]Content:[/bold]\n{data.get('content', '-')}\n"
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "btn-search":
            search_input = self.query_one("#memory-search", Input)
            query = search_input.value.strip()
            if query:
                try:
                    init_db()
                    result = search_memories(options=SearchOptions(query_type="keyword", query=query, limit=30))
                    list_view = self.query_one("#memory-list", ListView)
                    list_view.clear()
                    if isinstance(result, list):
                        for mem in result:
                            list_view.append(MemoryItem(mem))
                    else:
                        list_view.append(ListItem(Static("No results.")))
                except Exception as e:
                    list_view = self.query_one("#memory-list", ListView)
                    list_view.clear()
                    list_view.append(ListItem(Static(f"[red]Error: {e}[/red]")))

        elif event.button.id == "btn-refresh":
            self.refresh_data()

        elif event.button.id == "btn-store":
            input_widget = self.query_one("#new-memory-input", Input)
            content = input_widget.value.strip()
            if content:
                try:
                    init_db()
                    store_memory(content=content, scope="session", retention="short_term", category="fact")
                    input_widget.value = ""
                    self.refresh_data()
                except Exception as e:
                    detail = self.query_one("#memory-detail", Static)
                    detail.update(f"[red]Error storing: {e}[/red]")

        elif event.button.id == "btn-clear":
            self.query_one("#new-memory-input", Input).value = ""
