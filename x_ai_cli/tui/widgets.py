"""Widgets for the Textual TUI."""

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog


class ActionLog(Vertical):
    """A container for the RichLog widget."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.rich_log = RichLog(
            id="action-log", highlight=True, auto_scroll=True, markup=True
        )

    def compose(self) -> ComposeResult:
        yield self.rich_log
