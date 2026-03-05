"""Textual logging handler for x-ai CLI.

Bridges standard python logging to a Textual RichLog widget.
"""

import logging
import threading
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
            # If we're in the same thread (async worker), write directly
            app_thread = getattr(app, "_thread", None)
            if threading.current_thread() is app_thread:
                self.rich_log.write(msg)
            else:
                app.call_from_thread(self.rich_log.write, msg)
        except Exception:
            pass  # Silently ignore errors during shutdown
