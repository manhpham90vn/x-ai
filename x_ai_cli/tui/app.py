import argparse
import asyncio
import contextlib
import logging
import re
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Static

from x_ai_cli.agent_runner import TmuxSession
from x_ai_cli.config import Config
from x_ai_cli.logger import logger, setup_logging
from x_ai_cli.orchestrator import Orchestrator
from x_ai_cli.tui.handler import TextualLogHandler
from x_ai_cli.tui.widgets import ActionLog


class ConfigScreen(ModalScreen[tuple[str, int, float, int, int]]):
    """Screen for configuring x-ai parameters."""

    CSS = """
    ConfigScreen {
        align: center middle;
    }

    #dialog {
        grid-size: 2;
        grid-gutter: 1 2;
        grid-rows: 1fr 1fr 1fr 1fr 1fr 1fr;
        padding: 1 2;
        width: 60;
        height: 28;
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

            yield Label("Planner Timeout (s):")
            yield Input(
                value=str(self.current_config.planner_timeout_sec),
                id="planner-timeout",
                type="integer",
            )

            yield Label("Executor Timeout (s):")
            yield Input(
                value=str(self.current_config.executor_timeout_sec),
                id="executor-timeout",
                type="integer",
            )

            with Horizontal(id="buttons"):
                yield Button("Save", id="save", variant="success")
                yield Button("Cancel", id="cancel", variant="error")

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        work_dir = self.query_one("#work-dir", Input).value
        max_rounds = int(self.query_one("#max-rounds", Input).value or "3")
        threshold = float(self.query_one("#threshold", Input).value or "70.0")
        planner_timeout = int(self.query_one("#planner-timeout", Input).value or "600")
        executor_timeout = int(
            self.query_one("#executor-timeout", Input).value or "600"
        )
        self.dismiss(
            (work_dir, max_rounds, threshold, planner_timeout, executor_timeout)
        )

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:

        self.dismiss()


# Regex to match ANSI background color escape sequences:
# - \x1b[4Xm (standard bg 40-47), \x1b[49m (default bg)
# - \x1b[10Xm (bright bg 100-107)
# - \x1b[48;5;Nm (256-color bg)
# - \x1b[48;2;R;G;Bm (truecolor bg)
_ANSI_BG_RE = re.compile(
    r"\x1b\["
    r"(?:"
    r"4[0-9]"  # standard bg 40-49
    r"|10[0-7]"  # bright bg 100-107
    r"|48;5;\d+"  # 256-color bg
    r"|48;2;\d+;\d+;\d+"  # truecolor bg
    r")m"
)


def _strip_ansi_bg(text: str) -> str:
    """Remove ANSI background color codes, keeping foreground colors intact."""
    return _ANSI_BG_RE.sub("", text)


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
        self._last_content: dict[str, str] = {}

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
        self._timer = self.set_interval(0.5, self._refresh_output)

    async def _refresh_output(self) -> None:
        """Capture and display tmux pane content (diff-based)."""
        log = self.query_one("#tmux-output", RichLog)

        # Capture all sessions first
        new_content: dict[str, str] = {}
        for session_name in self.session_names:
            try:
                output = await self.tmux.capture_pane_color(session_name)
                new_content[session_name] = output.rstrip()
            except Exception as e:
                new_content[session_name] = f"[red]Error capturing pane: {e}[/red]"

        # Only re-render if content actually changed
        if new_content == self._last_content:
            return

        log.clear()
        for session_name in self.session_names:
            log.write(f"[bold cyan]━━━ {session_name} ━━━[/bold cyan]")
            content = new_content.get(session_name, "")
            if content.strip():
                # Strip ANSI background colors to avoid clashing with TUI bg,
                # then convert remaining ANSI (foreground) to Rich Text
                cleaned = _strip_ansi_bg(content)
                log.write(Text.from_ansi(cleaned))
            else:
                log.write("[dim](empty)[/dim]")
            log.write("")
        self._last_content = new_content

    @on(Button.Pressed, "#close-tmux")
    def action_close(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self.dismiss(None)


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
        width: 1fr;
        height: 1fr;
        border: solid green;
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
            planner_timeout_sec=args.planner_timeout,
            executor_timeout_sec=args.executor_timeout,
            verbose=args.verbose,
        )
        self.orchestrator: Orchestrator | None = None

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header(show_clock=True)
        with Horizontal(id="main-content"):
            yield ActionLog()

        yield Input(placeholder="Ask x-ai a coding task...", id="task-input")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "x-ai Planner-Executor"

        # Setup custom logging handler to route to UI
        self._setup_tui_logging()

        log = self.query_one("#action-log", RichLog)

        # Display config inline on startup
        log.write("[bold cyan]━━━ Current Configuration ━━━[/bold cyan]")
        log.write(f"Work Dir: [green]{self.config.work_path}[/green]")
        log.write(f"Max Rounds: [green]{self.config.max_rounds}[/green]")
        log.write(f"Threshold: [green]{self.config.quality_threshold}[/green]")
        log.write(
            f"Timeouts: [green]Planner {self.config.planner_timeout_sec}s[/green], "
            f"[green]Executor {self.config.executor_timeout_sec}s[/green]\n"
        )

        log.write("[bold green]Welcome to x-ai![/bold green]")
        log.write("Type your coding task below and press Enter.")
        log.write(
            "[dim]Shortcuts:\n"
            "  [bold]Ctrl+O[/bold] Configure\n"
            "  [bold]Ctrl+T[/bold] Tmux View\n"
            "  [bold]Ctrl+Q[/bold] Quit[/dim]"
        )

        # Focus the input automatically
        self.query_one("#task-input", Input).focus()

    def action_configure(self) -> None:
        """Action handler for 'c' binding."""

        def check_response(result: tuple[str, int, float, int, int] | None) -> None:
            if result is not None:
                work_dir, max_rounds, threshold, p_timeout, e_timeout = result

                # Re-initialize config to recalculate derived paths (work_path etc)
                self.config = Config(
                    work_dir=work_dir,
                    max_rounds=max_rounds,
                    quality_threshold=threshold,
                    planner_timeout_sec=p_timeout,
                    executor_timeout_sec=e_timeout,
                    verbose=self.config.verbose,
                )

                log = self.query_one("#action-log", RichLog)
                log.write(
                    f"\n[green]Config updated: {work_dir}, {max_rounds} rounds, "
                    f"{threshold} threshold, timeouts: "
                    f"Planner {p_timeout}s, Executor {e_timeout}s[/green]"
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

        # We format for the ActionLog to show full logs now
        tui_handler.setLevel(logging.DEBUG if self.config.verbose else logging.INFO)
        tui_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
        logger.addHandler(tui_handler)

    @work(exclusive=True)
    async def run_pipeline(self, prompt: str) -> None:
        """Run the orchestrator pipeline asynchronously in background."""
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
