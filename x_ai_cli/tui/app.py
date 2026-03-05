import argparse
import asyncio
import contextlib
import logging
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Static

from x_ai_cli.agent_runner import TmuxSession
from x_ai_cli.config import Config
from x_ai_cli.logger import logger, setup_logging
from x_ai_cli.orchestrator import Orchestrator
from x_ai_cli.tui.handler import TextualLogHandler
from x_ai_cli.tui.widgets import ActionLog


class ConfigScreen(ModalScreen[tuple[str, int, float]]):
    """Screen for configuring x-ai parameters."""

    CSS = """
    ConfigScreen {
        align: center middle;
    }

    #dialog {
        grid-size: 2;
        grid-gutter: 1 2;
        grid-rows: 1fr 1fr 1fr 1fr;
        padding: 1 2;
        width: 60;
        height: 20;
        border: thick cyan;
        background: $surface;
    }

    Label {
        padding-top: 1;
        content-align: center middle;
    }

    #buttons {
        column-span: 2;
        align: center middle;
    }

    Button {
        margin: 0 1;
    }
    """

    def __init__(self, current_config: Config) -> None:
        super().__init__()
        self.current_config = current_config

    def compose(self) -> ComposeResult:
        with Grid(id="dialog"):
            yield Label("Work Dir:")
            yield Input(value=str(self.current_config.work_path), id="work-dir")

            yield Label("Max Rounds:")
            yield Input(
                value=str(self.current_config.max_rounds),
                id="max-rounds",
                type="integer",
            )

            yield Label("Threshold:")
            yield Input(
                value=str(self.current_config.quality_threshold),
                id="threshold",
                type="number",
            )

            with Horizontal(id="buttons"):
                yield Button("Save", id="save", variant="success")
                yield Button("Cancel", id="cancel", variant="error")

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        work_dir = self.query_one("#work-dir", Input).value
        max_rounds = int(self.query_one("#max-rounds", Input).value or "3")
        threshold = float(self.query_one("#threshold", Input).value or "70.0")
        self.dismiss((work_dir, max_rounds, threshold))

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:

        self.dismiss()


class TmuxViewerScreen(ModalScreen[None]):
    """Modal screen to view live tmux output from active Claude sessions."""

    CSS = """
    TmuxViewerScreen {
        align: center middle;
    }

    #tmux-dialog {
        width: 100%;
        height: 95%;
        border: thick cyan;
        background: $surface;
    }

    #tmux-header {
        height: 3;
        content-align: center middle;
        text-style: bold;
        color: cyan;
        margin-bottom: 1;
    }

    #tmux-output {
        height: 1fr;
        border: solid green;
    }

    #tmux-footer {
        height: 3;
        align: center middle;
    }
    """

    BINDINGS = [
        ("escape", "close", "Close"),
    ]

    def __init__(
        self,
        tmux: TmuxSession,
        session_names: list[str],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.tmux = tmux
        self.session_names = session_names
        self._timer: Any = None

    def compose(self) -> ComposeResult:
        with Vertical(id="tmux-dialog"):
            sessions_label = ", ".join(self.session_names)
            yield Static(
                f"Tmux Viewer — [{sessions_label}]",
                id="tmux-header",
            )
            yield RichLog(
                id="tmux-output",
                highlight=False,
                auto_scroll=True,
                markup=True,
                wrap=True,
            )
            with Horizontal(id="tmux-footer"):
                yield Button("Close", id="close-tmux", variant="error")

    async def on_mount(self) -> None:
        """Start polling tmux output."""
        await self._refresh_output()
        self._timer = self.set_interval(1.0, self._refresh_output)

    async def _refresh_output(self) -> None:
        """Capture and display tmux pane content."""
        log = self.query_one("#tmux-output", RichLog)
        log.clear()
        for session_name in self.session_names:
            log.write(f"[bold cyan]━━━ {session_name} ━━━[/bold cyan]")
            try:
                output = await self.tmux.capture_pane(session_name)
                if output.strip():
                    log.write(output.rstrip())
                else:
                    log.write("[dim](empty)[/dim]")
            except Exception as e:
                log.write(f"[red]Error capturing pane: {e}[/red]")
            log.write("")

    @on(Button.Pressed, "#close-tmux")
    def action_close(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self.dismiss(None)


class Sidebar(Vertical):
    """Sidebar for config and status."""

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("x-ai Status", id="title", classes="sidebar-title")
            yield RichLog(
                id="status-panel",
                highlight=False,
                auto_scroll=True,
                markup=True,
                wrap=True,
            )
            yield Static("\nConfig", classes="sidebar-title")
            yield Static(id="config-panel")


class XAITui(App[None]):
    """Textual TUI for x-ai orchestrator."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #task-input {
        height: 3;
        margin: 0 1 1 1;
    }

    #main-content {
        height: 1fr;
    }

    ActionLog {
        width: 3fr;
        height: 1fr;
        border: solid green;
    }

    Sidebar {
        width: 1fr;
        height: 1fr;
        border: solid cyan;
        padding: 1;
    }

    .sidebar-title {
        text-style: bold;
        color: cyan;
    }


    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+o", "configure", "Configure"),
        ("ctrl+t", "view_tmux", "Tmux View"),
    ]

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()
        self.args = args
        self.config = Config(
            work_dir=args.work_dir,
            max_rounds=args.max_rounds,
            quality_threshold=args.threshold,
            verbose=args.verbose,
        )
        self.orchestrator: Orchestrator | None = None

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header(show_clock=True)
        with Horizontal(id="main-content"):
            yield ActionLog()
            yield Sidebar()

        yield Input(placeholder="Ask x-ai a coding task...", id="task-input")
        yield Footer()

    def _update_config_panel(self) -> None:
        config_panel = self.query_one("#config-panel", Static)
        config_panel.update(
            f"Work Dir: {self.config.work_path}\n"
            f"Max Rounds: {self.config.max_rounds}\n"
            f"Threshold: {self.config.quality_threshold}"
        )

    def on_mount(self) -> None:
        self.title = "x-ai Planner-Executor"

        # Setup config panel info
        self._update_config_panel()

        # Setup custom logging handler to route to UI
        self._setup_tui_logging()

        log = self.query_one("#action-log", RichLog)
        log.write("[bold green]Welcome to x-ai![/bold green]")
        log.write("Type your coding task below and press Enter.")
        log.write(
            "[dim]Shortcuts:\n"
            "  [bold]Ctrl+O[/bold] Configure\n"
            "  [bold]Ctrl+T[/bold] Tmux View\n"
            "  [bold]Ctrl+Q[/bold] Quit[/dim]"
        )

    def action_configure(self) -> None:
        """Action handler for 'c' binding."""

        def check_response(result: tuple[str, int, float] | None) -> None:
            if result is not None:
                work_dir, max_rounds, threshold = result

                # Re-initialize config to recalculate derived paths (work_path etc)
                self.config = Config(
                    work_dir=work_dir,
                    max_rounds=max_rounds,
                    quality_threshold=threshold,
                    verbose=self.config.verbose,
                )
                self._update_config_panel()

                log = self.query_one("#action-log", RichLog)
                log.write(
                    f"\n[green]Config updated: {work_dir}, {max_rounds} rounds, "
                    f"{threshold} threshold[/green]"
                )

        self.push_screen(ConfigScreen(self.config), check_response)

    def action_view_tmux(self) -> None:
        """Action handler for 't' binding — open tmux viewer."""
        if self.orchestrator is None:
            log = self.query_one("#action-log", RichLog)
            log.write("[yellow]No orchestrator running yet.[/yellow]")
            return

        sessions = list(self.orchestrator.runner._active_sessions)
        if not sessions:
            log = self.query_one("#action-log", RichLog)
            log.write("[yellow]No active tmux sessions.[/yellow]")
            return

        self.push_screen(
            TmuxViewerScreen(
                tmux=self.orchestrator.runner.tmux,
                session_names=sessions,
            )
        )

    def _setup_tui_logging(self) -> None:
        """Redirect orchestrator logs to Textual RichLog widget."""
        setup_logging(
            verbose=self.config.verbose,
            log_to_file=self.config.log_to_file,
            logs_dir=self.config.logs_path,
        )

        # Remove standard rich handler from our main logger to avoid stdout
        logger.handlers.clear()

        # Add the Textual log component
        rich_log = self.query_one("#action-log", RichLog)
        tui_handler = TextualLogHandler(rich_log)

        # Recreate a console formatting specifically targeting the RichLog via
        # a string format wrapper.
        # TextualLogHandler handles passing string containing Markup tags
        tui_handler.setLevel(logging.DEBUG if self.config.verbose else logging.INFO)
        tui_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(tui_handler)

        # Also route logs to the status panel in the sidebar
        status_log = self.query_one("#status-panel", RichLog)
        status_handler = TextualLogHandler(status_log)
        status_handler.setLevel(logging.INFO)
        status_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
        logger.addHandler(status_handler)

    @work(exclusive=True)
    async def run_pipeline(self, prompt: str) -> None:
        """Run the orchestrator pipeline asynchronously in background."""
        # Clear status panel for a fresh run
        with contextlib.suppress(Exception):
            self.query_one("#status-panel", RichLog).clear()

        self.orchestrator = Orchestrator(self.config)
        orchestrator = self.orchestrator

        logger.info("[phase]▶ PIPELINE STARTED[/phase]")

        try:
            result = await self.orchestrator.run_pipeline(prompt)
            if result.status == "success":
                logger.info("[success]✓ Pipeline completed successfully[/success]")
            else:
                logger.warning("[warning]⚠ Pipeline finished (best effort)[/warning]")
        except asyncio.CancelledError:
            logger.warning("Pipeline cancelled.")
            await orchestrator.runner.cleanup_all()
        except Exception as e:
            logger.error(f"Pipeline error: {e}")
        finally:
            with contextlib.suppress(Exception):
                self.query_one("#task-input", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        if not prompt:
            return

        # Disable input while running
        # event.input.value = ""
        log = self.query_one("#action-log", RichLog)
        log.write(f"\n[bold cyan]Task >[/bold cyan] {prompt}")

        self.run_pipeline(prompt)
        event.input.value = ""
