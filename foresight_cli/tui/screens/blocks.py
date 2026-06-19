"""Context blocks screen — view and manage context blocks."""

from __future__ import annotations

import json

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from foresight_mcp import ContextBlockAction, manage_context_blocks
from foresight_mcp.server import init_db

BLOCK_LABELS = [
    "guidance",
    "pending_items",
    "project_context",
    "user_preferences",
    "session_patterns",
    "core_directives",
    "tool_guidelines",
    "self_improvement",
]


class BlockItem(ListItem):
    """A context block in the list."""

    def __init__(self, label: str, content: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.block_label = label
        preview = (content or "(empty)")[:80]
        self._display = f"[bold]{label}[/bold]  [dim]{preview}[/dim]"

    def on_mount(self) -> None:
        self.update(self._display)


class BlocksScreen(Screen):
    """View and manage context blocks."""

    def compose(self) -> ComposeResult:
        yield Label("[bold]Context Blocks[/bold]", classes="section-title")
        yield Horizontal(
            ListView(id="block-list", classes="memory-list"),
            Vertical(
                Static("[bold]Block Details[/bold]", id="block-detail-title"),
                Static("Select a block to view", id="block-detail", classes="detail-panel"),
                Label("\n[bold]Edit Block[/bold]"),
                Input(placeholder="Enter new content...", id="block-content-input"),
                Horizontal(
                    Button("Update", variant="primary", id="btn-update"),
                    Button("Reset", id="btn-reset"),
                    Button("Clear", id="btn-clear-block"),
                    Button("Refresh", id="btn-refresh-blocks"),
                ),
                id="block-detail-column",
            ),
        )

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        """Load blocks from Foresight."""
        try:
            init_db()
            list_view = self.query_one("#block-list", ListView)
            list_view.clear()

            # Try to get actual blocks
            try:
                result = manage_context_blocks(options=ContextBlockAction(action="list"))
                if isinstance(result, str):
                    payload = json.loads(result)
                    blocks = payload.get("blocks", []) if isinstance(payload, dict) else []
                elif isinstance(result, dict):
                    blocks = result.get("blocks", [])
                else:
                    blocks = []

                if blocks:
                    for b in blocks:
                        label = b.get("label", "?")
                        content = b.get("content", "")
                        list_view.append(BlockItem(label, content))
                else:
                    # Show default block labels
                    for label in BLOCK_LABELS:
                        list_view.append(BlockItem(label))
            except Exception:
                # Show default labels
                for label in BLOCK_LABELS:
                    list_view.append(BlockItem(label))

        except Exception as e:
            list_view = self.query_one("#block-list", ListView)
            list_view.clear()
            list_view.append(ListItem(Static(f"[red]Error: {e}[/red]")))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Show block details on selection."""
        item = event.item
        if isinstance(item, BlockItem):
            detail = self.query_one("#block-detail", Static)
            try:
                result = manage_context_blocks(
                    options=ContextBlockAction(action="get", label=item.block_label)
                )
                if isinstance(result, str):
                    payload = json.loads(result)
                    content = (
                        payload.get("content", "(empty)") if isinstance(payload, dict) else result
                    )
                elif isinstance(result, dict):
                    content = result.get("content", "(empty)")
                else:
                    content = str(result)
                detail.update(f"[bold]{item.block_label}[/bold]\n\n{content}")
            except Exception as e:
                detail.update(f"[bold]{item.block_label}[/bold]\n\n[red]Error: {e}[/red]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button actions."""
        list_view = self.query_one("#block-list", ListView)
        selected = list_view.highlighted_child
        if not isinstance(selected, BlockItem):
            return

        label = selected.block_label
        detail = self.query_one("#block-detail", Static)

        if event.button.id == "btn-update":
            input_widget = self.query_one("#block-content-input", Input)
            content = input_widget.value.strip()
            if content:
                try:
                    init_db()
                    manage_context_blocks(
                        options=ContextBlockAction(action="update", label=label, content=content)
                    )
                    detail.update(f"[bold]{label}[/bold]\n\n{content}")
                    input_widget.value = ""
                    self.refresh_data()
                except Exception as e:
                    detail.update(f"[red]Error updating block: {e}[/red]")

        elif event.button.id == "btn-reset":
            try:
                init_db()
                manage_context_blocks(options=ContextBlockAction(action="reset", label=label))
                detail.update(f"[bold]{label}[/bold]\n\n(Reset to default)")
                self.refresh_data()
            except Exception as e:
                detail.update(f"[red]Error resetting block: {e}[/red]")

        elif event.button.id == "btn-clear-block":
            try:
                init_db()
                manage_context_blocks(options=ContextBlockAction(action="clear", label=label))
                detail.update(f"[bold]{label}[/bold]\n\n(Cleared)")
                self.refresh_data()
            except Exception as e:
                detail.update(f"[red]Error clearing block: {e}[/red]")

        elif event.button.id == "btn-refresh-blocks":
            self.refresh_data()
