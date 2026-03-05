"""Textual logging handler for x-ai CLI.

Bridges standard python logging to a Textual RichLog widget.
"""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from textual.widgets import RichLog


class TextualLogHandler(logging.Handler):
    """Logging handler that writes to a Textual RichLog widget."""

    def __init__(self, rich_log: "RichLog", *args: Any, **kwargs: Any) -> None:
        """Initialize the handler with a RichLog widget.

        Args:
            rich_log: The Textual RichLog widget to write to.
            *args: Passthrough.
            **kwargs: Passthrough.
        """
        super().__init__(*args, **kwargs)
        self.rich_log = rich_log

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a dictionary or formatted string."""
        try:
            msg = self.format(record)
            app = self.rich_log.app

            try:
                # Direct write works if we're in the app thread (e.g., @work workers)
                self.rich_log.write(msg)
            except RuntimeError:
                # Required if we're truly in a different thread
                app.call_from_thread(self.rich_log.write, msg)
        except Exception as exc:
            with open("/tmp/tui_handler_errors.log", "a") as f:  # noqa: PTH123
                f.write(f"emit error: {exc!r} | msg={record.getMessage()!r}\n")
